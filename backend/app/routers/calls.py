from datetime import datetime

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
from fastapi.concurrency import run_in_threadpool

from app.database import get_db
from app.livekit_dispatch import dispatch_call, end_call
from app.models import Call, CallStatus, Friend
from app.prompts import build_call_prompt
from app.schemas import CallCreate, CallDetailOut, CallOut
from app.storage import delete_recording, presigned_recording_url

router = APIRouter(prefix="/calls", tags=["calls"])


@router.post("", response_model=CallOut)
async def create_call(payload: CallCreate, db: AsyncSession = Depends(get_db)):
    friend = await db.get(Friend, payload.friend_id)
    if friend is None:
        raise HTTPException(status_code=404, detail="Friend not found")

    call = Call(
        friend_id=friend.id,
        persona=payload.persona,
        scenario=payload.scenario,
        context=payload.context,
        reveal=payload.reveal,
        voice=payload.voice,
        max_duration_seconds=payload.max_duration_seconds,
        status=CallStatus.pending,
    )
    db.add(call)
    await db.commit()
    await db.refresh(call)

    merged_prompt = build_call_prompt(
        friend_name=friend.name,
        persona=call.persona,
        scenario=call.scenario,
        context=call.context,
        reveal=call.reveal,
        max_duration_seconds=call.max_duration_seconds,
    )

    try:
        await dispatch_call(
            call_id=call.id,
            friend_phone_number=friend.phone_number,
            merged_prompt=merged_prompt,
            voice=call.voice,
            max_duration_seconds=call.max_duration_seconds,
        )
        call.status = CallStatus.dialing
        call.started_at = datetime.utcnow()
    except Exception:
        call.status = CallStatus.failed
        await db.commit()
        raise

    await db.commit()
    await db.refresh(call)
    return call


@router.get("/{call_id}", response_model=CallDetailOut)
async def get_call(call_id: str, db: AsyncSession = Depends(get_db)):
    call = await db.get(Call, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    await db.refresh(call, attribute_names=["transcript_entries"])
    return call


@router.get("", response_model=list[CallOut])
async def list_calls(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Call).order_by(Call.created_at.desc()))
    return result.scalars().all()


@router.get("/{call_id}/recording")
async def get_recording(call_id: str, db: AsyncSession = Depends(get_db)):
    call = await db.get(Call, call_id)
    if call is None or not call.recording_url:
        raise HTTPException(status_code=404, detail="No recording")
    url = await run_in_threadpool(presigned_recording_url, call.recording_url)
    return {"url": url, "expires_in": 600}


@router.delete("/{call_id}/recording", status_code=204)
async def delete_call_recording(call_id: str, db: AsyncSession = Depends(get_db)):
    call = await db.get(Call, call_id)
    if call is None or not call.recording_url:
        raise HTTPException(status_code=404, detail="No recording")
    await run_in_threadpool(delete_recording, call.recording_url)
    call.recording_url = None
    await db.commit()


@router.post("/{call_id}/hangup", response_model=CallOut)
async def hangup_call(call_id: str, db: AsyncSession = Depends(get_db)):
    call = await db.get(Call, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    if call.status not in (CallStatus.dialing, CallStatus.active):
        raise HTTPException(status_code=400, detail="Call is not in progress")

    try:
        await end_call(call.id)
    except Exception:
        raise HTTPException(status_code=502, detail="Could not end the call")
    call.status = CallStatus.completed
    call.ended_at = datetime.utcnow()
    await db.commit()
    await db.refresh(call)
    return call
