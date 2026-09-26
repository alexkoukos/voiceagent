from datetime import datetime

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
from fastapi.concurrency import run_in_threadpool

from app import events
from app.routers import recordings
from app.database import get_db
from app.config import get_settings
from app.languages import language_for_phone
from app.dispatcher import active_count, start_call, start_next_queued
from app.livekit_dispatch import end_call
from app.models import Call, CallStatus, Friend, TranscriptEntry
from app.schemas import CallCreate, CallDetailOut, CallOut
from app.storage import SEALED, presigned_recording_url, queue_recording_deletion, recording_exists

router = APIRouter(prefix="/calls", tags=["calls"])


@router.post("", response_model=CallOut)
async def create_call(payload: CallCreate, db: AsyncSession = Depends(get_db)):
    friend = await db.get(Friend, payload.friend_id)
    if friend is None or friend.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Friend not found")

    settings = get_settings()
    if payload.from_own_number and not settings.own_caller_number:
        raise HTTPException(status_code=400, detail="No own caller number is configured")
    call = Call(
        friend_id=friend.id,
        persona=payload.persona,
        scenario=payload.scenario,
        context=payload.context,
        reveal=payload.reveal,
        voice=payload.voice,
        max_duration_seconds=min(payload.max_duration_seconds, settings.max_call_duration_seconds),
        from_own_number=payload.from_own_number,
        language=payload.language or language_for_phone(friend.phone_number),
        status=CallStatus.queued,
    )
    db.add(call)
    await db.commit()
    await db.refresh(call)

    if await active_count(db) < settings.max_concurrent_calls:
        await start_call(db, call, friend)
        await db.refresh(call)
    return call


@router.websocket("/{call_id}/ws")
async def call_updates(websocket: WebSocket, call_id: str):
    await websocket.accept()
    q = events.subscribe(call_id)
    try:
        await websocket.send_text("changed")
        while True:
            await q.get()
            await websocket.send_text("changed")
    except WebSocketDisconnect:
        pass
    finally:
        events.unsubscribe(call_id, q)


@router.get("/{call_id}", response_model=CallDetailOut)
async def get_call(call_id: str, db: AsyncSession = Depends(get_db)):
    call = await db.get(Call, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    await db.refresh(call, attribute_names=["transcript_entries"])
    return call


@router.get("", response_model=list[CallOut])
async def list_calls(db: AsyncSession = Depends(get_db)):
    # The app's history is outbound calls; receptionist calls are listed per practice.
    result = await db.execute(
        select(Call).where(Call.friend_id.is_not(None)).order_by(Call.created_at.desc())
    )
    return result.scalars().all()


@router.get("/{call_id}/recording")
async def get_recording(call_id: str, db: AsyncSession = Depends(get_db)):
    call = await db.get(Call, call_id)
    if call is None or not call.recording_url:
        raise HTTPException(status_code=404, detail="No recording")
    if not await run_in_threadpool(recording_exists, call.recording_url):
        raise HTTPException(status_code=409, detail="Recording is still uploading")
    if call.recording_url.endswith(SEALED):
        # Encrypted at rest: the backend decrypts it for this link only (routers/recordings.py).
        return {"url": recordings.playback_url(call.id), "expires_in": recordings.LINK_SECONDS}
    url = await run_in_threadpool(presigned_recording_url, call.recording_url)
    return {"url": url, "expires_in": 600}


@router.delete("/{call_id}/recording", status_code=204)
async def delete_call_recording(call_id: str, db: AsyncSession = Depends(get_db)):
    call = await db.get(Call, call_id)
    if call is None or not call.recording_url:
        raise HTTPException(status_code=404, detail="No recording")
    await queue_recording_deletion(db, call.recording_url, call.id)
    call.recording_url = None
    call.delete_requested = True
    await db.execute(delete(TranscriptEntry).where(TranscriptEntry.call_id == call.id))
    await db.commit()
    events.publish(call.id)


@router.post("/{call_id}/hangup", response_model=CallOut)
async def hangup_call(call_id: str, db: AsyncSession = Depends(get_db)):
    call = await db.get(Call, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")

    if call.status == CallStatus.queued:
        call.status = CallStatus.cancelled
        call.ended_at = datetime.utcnow()
    elif call.status in (CallStatus.dialing, CallStatus.active):
        try:
            await end_call(call.id)
        except Exception:
            raise HTTPException(status_code=502, detail="Could not end the call")
        call.status = CallStatus.completed
        call.ended_at = datetime.utcnow()
        if call.started_at:
            call.duration_seconds = int((call.ended_at - call.started_at).total_seconds())
    else:
        raise HTTPException(status_code=400, detail="Call is not in progress")

    await db.commit()
    await db.refresh(call)
    events.publish(call.id)
    await start_next_queued(db)
    return call
