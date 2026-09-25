import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.config import get_settings
from app.database import get_db
from app.dispatcher import start_next_queued
from app.models import Call, CallStatus, TranscriptEntry
from app.schemas import CallEvent
from app.storage import delete_recording_later

router = APIRouter(prefix="/internal", tags=["internal"])


def require_agent_token(x_agent_token: str = Header(default="")) -> None:
    expected = get_settings().internal_api_token
    if not expected or not secrets.compare_digest(x_agent_token, expected):
        raise HTTPException(status_code=401, detail="Invalid agent token")


@router.post("/calls/{call_id}/events", status_code=204, dependencies=[Depends(require_agent_token)])
async def call_event(call_id: str, event: CallEvent, db: AsyncSession = Depends(get_db)):
    call = await db.get(Call, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")

    was_in_progress = call.status in (CallStatus.dialing, CallStatus.active)

    if event.delete_recording:
        call.delete_requested = True
        await db.execute(delete(TranscriptEntry).where(TranscriptEntry.call_id == call.id))
        if call.recording_url:
            delete_recording_later(call.recording_url)
            call.recording_url = None
    if event.transcript_role and event.transcript_text and not call.delete_requested:
        db.add(
            TranscriptEntry(
                call_id=call.id, role=event.transcript_role, text=event.transcript_text
            )
        )
    if event.status:
        call.status = event.status
        if event.status == CallStatus.active and call.started_at is None:
            call.started_at = datetime.utcnow()
        if event.status in (CallStatus.completed, CallStatus.failed):
            call.ended_at = datetime.utcnow()
            if call.started_at:
                call.duration_seconds = int((call.ended_at - call.started_at).total_seconds())
    if event.end_reason:
        call.end_reason = event.end_reason
    if event.recording_url:
        if call.delete_requested:
            delete_recording_later(event.recording_url)
        else:
            call.recording_url = event.recording_url
    await db.commit()
    events.publish(call.id)
    if was_in_progress and call.status in (CallStatus.completed, CallStatus.failed):
        await start_next_queued(db)
