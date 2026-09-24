from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import Call, CallStatus, TranscriptEntry
from app.schemas import CallEvent

router = APIRouter(prefix="/internal", tags=["internal"])


def require_agent_token(x_agent_token: str = Header(default="")) -> None:
    expected = get_settings().internal_api_token
    if not expected or x_agent_token != expected:
        raise HTTPException(status_code=401, detail="Invalid agent token")


@router.post("/calls/{call_id}/events", status_code=204, dependencies=[Depends(require_agent_token)])
async def call_event(call_id: str, event: CallEvent, db: AsyncSession = Depends(get_db)):
    call = await db.get(Call, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")

    if event.transcript_role and event.transcript_text:
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
    if event.recording_url:
        call.recording_url = event.recording_url
    await db.commit()
