import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app import events, finalize
from app.config import get_settings
from app.database import get_db
from app.dispatcher import start_next_queued
from app import receptionist
from app.models import Call, CallStatus, TranscriptEntry
from app.schemas import (
    AdminChangeArgs, AdminConfirmArgs, AdminLoginArgs, AppointmentRef, BookAppointment, CallEvent, CheckAvailability, FindArgs, FlagArgs, HandoffResult, InboundStart,
    MessageArgs, PrepareAction, RescheduleArgs, RouteArgs, TransferArgs, WaitlistArgs, normalize_phone,
)
from app.storage import queue_recording_deletion

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

    for flag in event.flags:
        if flag not in (call.flags or []):
            call.flags = [*(call.flags or []), flag]
    if event.delete_recording:
        call.delete_requested = True
        await db.execute(delete(TranscriptEntry).where(TranscriptEntry.call_id == call.id))
        if call.recording_url:
            await queue_recording_deletion(db, call.recording_url, call.id)
            call.recording_url = None
    if event.transcript_role and event.transcript_text and not call.delete_requested:
        db.add(
            TranscriptEntry(
                call_id=call.id, role=event.transcript_role, text=event.transcript_text
            )
        )
    # A finished call stays finished (a late "failed" from a dying agent must not overwrite it).
    if event.status and was_in_progress:
        call.status = event.status
        if event.status == CallStatus.active and call.started_at is None:
            call.started_at = datetime.utcnow()
        if event.status in (CallStatus.completed, CallStatus.failed):
            call.ended_at = datetime.utcnow()
            if call.started_at:
                call.duration_seconds = int((call.ended_at - call.started_at).total_seconds())
    if event.end_reason:
        call.end_reason = event.end_reason
    if event.latency_ms_median is not None:
        call.latency_ms_median = event.latency_ms_median
    if "recording_refused" in event.flags and call.recording_url:
        await queue_recording_deletion(db, call.recording_url, call.id)
        call.recording_url = None
    if event.recording_url:
        if call.delete_requested or "recording_refused" in (call.flags or []):
            await queue_recording_deletion(db, event.recording_url, call.id)
        else:
            call.recording_url = event.recording_url
    await db.commit()
    events.publish(call.id)
    if call.practice_id:
        events.publish(f"practice:{call.practice_id}")
    if call.status in (CallStatus.completed, CallStatus.failed) and call.practice_id and not call.finalized:
        finalize.schedule(call.id)
    if was_in_progress and call.status in (CallStatus.completed, CallStatus.failed):
        await start_next_queued(db)


@router.post("/inbound", dependencies=[Depends(require_agent_token)])
async def inbound_call(payload: InboundStart, db: AsyncSession = Depends(get_db)):
    """A phone call came in on a trunk: find the practice by the number dialed, open the
    call record and hand the agent its prompt. 429 with a line to say when lines are full."""
    practice = await receptionist.practice_for_number(db, normalize_phone(payload.dialed_number))
    if practice is None:
        raise HTTPException(status_code=404, detail="No practice for this number")
    caller = normalize_phone(payload.caller_number) if payload.caller_number else None
    try:
        call, metadata = await receptionist.start_call(db, practice, direction="inbound", caller_number=caller)
    except receptionist.Blocked:
        # The agent hangs up on any error, without a word.
        raise HTTPException(status_code=403, detail="blocked")
    except (receptionist.Busy, receptionist.OverCap) as e:
        language = receptionist.call_language(practice, caller)
        line = (receptionist.cap_line if isinstance(e, receptionist.OverCap) else receptionist.busy_line)(practice, language)
        return JSONResponse(status_code=429, content={"busy_line": line, "language": language, "voice": practice.voice})
    return metadata


async def _receptionist_call(db: AsyncSession, call_id: str) -> Call:
    call = await db.get(Call, call_id)
    if call is None or call.practice_id is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return call


tools = APIRouter(prefix="/calls/{call_id}/tools", dependencies=[Depends(require_agent_token)])


@tools.post("/route_call")
async def t_route(call_id: str, args: RouteArgs, db: AsyncSession = Depends(get_db)):
    return await receptionist.tool_route(db, await _receptionist_call(db, call_id), args)


@tools.post("/emergency")
async def t_emergency(call_id: str, db: AsyncSession = Depends(get_db)):
    return await receptionist.tool_emergency(db, await _receptionist_call(db, call_id))


@tools.post("/check_availability")
async def t_check(call_id: str, args: CheckAvailability, db: AsyncSession = Depends(get_db)):
    return await receptionist.tool_check_availability(db, await _receptionist_call(db, call_id), args)


@tools.post("/prepare_action")
async def t_prepare_action(call_id: str, args: PrepareAction, db: AsyncSession = Depends(get_db)):
    return await receptionist.tool_prepare_action(db, await _receptionist_call(db, call_id), args)


@tools.post("/book_appointment")
async def t_book(call_id: str, args: BookAppointment, db: AsyncSession = Depends(get_db)):
    return await receptionist.tool_book(db, await _receptionist_call(db, call_id), args)


@tools.post("/admin_login")
async def t_admin_login(call_id: str, args: AdminLoginArgs, db: AsyncSession = Depends(get_db)):
    return await receptionist.tool_admin_login(db, await _receptionist_call(db, call_id), args)


@tools.post("/admin_change")
async def t_admin_change(call_id: str, args: AdminChangeArgs, db: AsyncSession = Depends(get_db)):
    return await receptionist.tool_admin_change(db, await _receptionist_call(db, call_id), args)


@tools.post("/admin_confirm")
async def t_admin_confirm(call_id: str, args: AdminConfirmArgs, db: AsyncSession = Depends(get_db)):
    return await receptionist.tool_admin_confirm(db, await _receptionist_call(db, call_id), args)


@tools.post("/find_appointments")
async def t_find(call_id: str, args: FindArgs, db: AsyncSession = Depends(get_db)):
    return await receptionist.tool_find_appointments(db, await _receptionist_call(db, call_id), args)


@tools.post("/reschedule_appointment")
async def t_reschedule(call_id: str, args: RescheduleArgs, db: AsyncSession = Depends(get_db)):
    return await receptionist.tool_reschedule(db, await _receptionist_call(db, call_id), args)


@tools.post("/cancel_appointment")
async def t_cancel(call_id: str, args: AppointmentRef, db: AsyncSession = Depends(get_db)):
    return await receptionist.tool_cancel(db, await _receptionist_call(db, call_id), args)


@tools.post("/confirm_appointment")
async def t_confirm(call_id: str, args: AppointmentRef, db: AsyncSession = Depends(get_db)):
    return await receptionist.tool_confirm(db, await _receptionist_call(db, call_id), args)


@tools.post("/take_message")
async def t_message(call_id: str, args: MessageArgs, db: AsyncSession = Depends(get_db)):
    return await receptionist.tool_take_message(db, await _receptionist_call(db, call_id), args)


@tools.post("/transfer_to_human")
async def t_transfer(call_id: str, args: TransferArgs, db: AsyncSession = Depends(get_db)):
    return await receptionist.tool_transfer(db, await _receptionist_call(db, call_id), args)


@tools.post("/handoff_result")
async def t_handoff_result(call_id: str, args: HandoffResult, db: AsyncSession = Depends(get_db)):
    return await receptionist.tool_handoff_result(db, await _receptionist_call(db, call_id), args)


@tools.post("/add_to_waitlist")
async def t_waitlist(call_id: str, args: WaitlistArgs, db: AsyncSession = Depends(get_db)):
    return await receptionist.tool_add_to_waitlist(db, await _receptionist_call(db, call_id), args)


@tools.post("/flag")
async def t_flag(call_id: str, args: FlagArgs, db: AsyncSession = Depends(get_db)):
    return await receptionist.tool_flag(db, await _receptionist_call(db, call_id), args.flag)


router.include_router(tools)
