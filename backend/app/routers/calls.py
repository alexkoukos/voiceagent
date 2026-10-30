from fastapi import APIRouter, HTTPException
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
from fastapi.concurrency import run_in_threadpool

from starlette.requests import HTTPConnection

from app.auth import require_admin_token, require_app_token
from app import events
from app.routers import recordings
from app.database import get_db
from app.models import Call, CallTelemetryEvent, TranscriptEntry
from app.storage import SEALED, presigned_recording_url, queue_recording_deletion, recording_exists

async def protect_practice_call(connection: HTTPConnection, db: AsyncSession = Depends(get_db)):
    value = connection.headers.get("x-api-key", "")
    call_id = connection.path_params.get("call_id")
    if not call_id:
        require_app_token(value)
        return
    call = await db.get(Call, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    if call.practice_id:
        await require_admin_token(connection, db)
        if db.info.get("tenant_id") and db.info["tenant_id"] != call.practice_id:
            raise HTTPException(status_code=404, detail="Call not found")
    else:
        require_app_token(value)


router = APIRouter(prefix="/calls", tags=["calls"], dependencies=[Depends(protect_practice_call)])


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
    call = await db.get(Call, call_id, with_for_update=True)
    if call is None or not call.recording_url:
        raise HTTPException(status_code=404, detail="No recording")
    await queue_recording_deletion(db, call.recording_url, call.id)
    call.recording_url = None
    call.delete_requested = True
    await db.execute(delete(TranscriptEntry).where(TranscriptEntry.call_id == call.id))
    await db.execute(delete(CallTelemetryEvent).where(CallTelemetryEvent.call_id == call.id))
    await db.commit()
    events.publish(call.id)
