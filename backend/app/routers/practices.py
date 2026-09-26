"""Founder-side setup and the receptionist call log (manual onboarding, PRD scope)."""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import booking, config_changes, events, finalize, metrics, notifications, receptionist, texts
from app.config import CONFIG_DIR, get_settings
from app.database import get_db
from app.models import (
    AdminLink, Appointment, Call, ConfigVersion, Device, Handoff, Message, Notification, Practice, RoutingEvent, Staff, TranscriptEntry,
    TranscriptRole, WaitlistEntry,
)
from app.schemas import (
    AppointmentCreate, AppointmentMove, AppointmentOut, AdminLinkIn, AdminLinkOut, CallReview, ClosureIn, ClosureOut, ConfigVersionOut, DeviceIn, HandoffJoin, HandoffOut,
    MessageOut, MessageUpdate, PracticeIn, PracticeOut, ReceptionistCallDetail, ReceptionistCallOut, RecordingSettings, StaffIn,
    StaffOut, WaitlistOut,
)

router = APIRouter(prefix="/practices", tags=["practices"])
misc = APIRouter(tags=["practices"])
VERTICALS_DIR = CONFIG_DIR / "verticals"


async def _get(db: AsyncSession, practice_id: str) -> Practice:
    practice = await db.get(Practice, practice_id)
    if practice is None:
        raise HTTPException(status_code=404, detail="Practice not found")
    return practice


async def _save(db: AsyncSession, obj):
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Demo slug already taken")
    await db.refresh(obj)
    return obj


# --- verticals (templates) ---


@misc.get("/verticals")
async def list_verticals():
    out = []
    for f in sorted(VERTICALS_DIR.glob("*.json")):
        v = json.loads(f.read_text(encoding="utf-8"))
        out.append({"id": f.stem, "label": v["label"], "wave": v["wave"]})
    return out


@misc.get("/verticals/{vertical}")
async def get_vertical(vertical: str):
    """A starting PracticeIn for this vertical: fill in name, numbers and the knowledge base, then POST it."""
    path = VERTICALS_DIR / f"{vertical}.json"
    if not path.is_file() or not vertical.isalnum():
        raise HTTPException(status_code=404, detail="Unknown vertical")
    v = json.loads(path.read_text(encoding="utf-8"))
    return {k: v[k] for k in ("hours", "services", "routing_rules", "knowledge_base") if k in v} | {
        "vertical": vertical, "rules": v.get("rules", {}),
    }


# --- practices ---


async def _check_numbers(db: AsyncSession, numbers: list[str], practice_id: str | None = None) -> None:
    """An inbound call is matched to its practice by the dialed number, so a number belongs to
    one active practice only."""
    taken = []
    for other in (await db.execute(select(Practice).where(Practice.offboarded_at.is_(None)))).scalars():
        if other.id != practice_id:
            taken += [n for n in numbers if n in (other.phone_numbers or [])]
    if taken:
        raise HTTPException(status_code=409, detail={"error": "number_in_use", "numbers": sorted(set(taken))})


def _check_vertical(vertical: str) -> None:
    if vertical and not (vertical.isalnum() and (VERTICALS_DIR / f"{vertical}.json").is_file()):
        raise HTTPException(status_code=422, detail={"error": "unknown_vertical", "vertical": vertical})


def _check_staff_services(practice: Practice, payload: StaffIn, current: list[str] | None = None) -> None:
    """New service ids must exist. Ids the person already has are let through: a service
    removed from the practice later (price list import, edit) must not block every edit of
    this person."""
    known = {s["id"] for s in practice.services or []} | set(current or [])
    unknown = [i for i in payload.service_ids if i not in known]
    if unknown:
        raise HTTPException(status_code=422, detail={"error": "unknown_service", "service_ids": unknown})


@router.post("", response_model=PracticeOut)
async def create_practice(payload: PracticeIn, db: AsyncSession = Depends(get_db)):
    _check_vertical(payload.vertical)
    await _check_numbers(db, payload.phone_numbers)
    # G7: new practices record and say so; existing rows kept the old behaviour (migration 0020).
    practice = Practice(**({"recording_enabled": True, "recording_notice": True} | payload.to_columns()))
    db.add(practice)
    return await _save(db, practice)


@router.get("", response_model=list[PracticeOut])
async def list_practices(db: AsyncSession = Depends(get_db)):
    return (await db.execute(select(Practice).order_by(Practice.created_at))).scalars().all()


@router.get("/{practice_id}", response_model=PracticeOut)
async def get_practice(practice_id: str, db: AsyncSession = Depends(get_db)):
    return await _get(db, practice_id)


@router.put("/{practice_id}", response_model=PracticeOut)
async def update_practice(practice_id: str, payload: PracticeIn, db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    if payload.vertical != practice.vertical:
        _check_vertical(payload.vertical)
    await _check_numbers(db, payload.phone_numbers, practice.id)
    columns = payload.to_columns()
    # Closures have their own calls; a full update never drops them.
    columns["rules"]["closures"] = (practice.rules or {}).get("closures") or []
    await config_changes.publish(db, practice, {k: columns.pop(k) for k in config_changes.FIELDS},
                                 source="app", author="founder")
    for k, v in columns.items():
        setattr(practice, k, v)
    return await _save(db, practice)


@router.get("/{practice_id}/recording", response_model=RecordingSettings)
async def get_recording(practice_id: str, db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    return RecordingSettings(recording_enabled=practice.recording_enabled, recording_notice=practice.recording_notice)


@router.put("/{practice_id}/recording", response_model=RecordingSettings)
async def set_recording(practice_id: str, payload: RecordingSettings, db: AsyncSession = Depends(get_db)):
    """G7: recording on/off and the "this call is recorded" line in the greeting. Applies from the next call."""
    practice = await _get(db, practice_id)
    for key in ("recording_enabled", "recording_notice"):
        if getattr(payload, key) is not None:
            setattr(practice, key, getattr(payload, key))
    await db.commit()
    return await get_recording(practice_id, db)


@router.websocket("/{practice_id}/ws")
async def practice_updates(websocket: WebSocket, practice_id: str):
    """Sends "changed" whenever a call, message or handoff of this practice changes."""
    await websocket.accept()
    q = events.subscribe(f"practice:{practice_id}")
    try:
        await websocket.send_text("changed")
        while True:
            await q.get()
            await websocket.send_text("changed")
    except WebSocketDisconnect:
        pass
    finally:
        events.unsubscribe(f"practice:{practice_id}", q)


# --- staff ---


@router.get("/{practice_id}/staff", response_model=list[StaffOut])
async def list_staff(practice_id: str, db: AsyncSession = Depends(get_db)):
    await _get(db, practice_id)
    return (await db.execute(select(Staff).where(Staff.practice_id == practice_id).order_by(Staff.created_at))).scalars().all()


@router.post("/{practice_id}/staff", response_model=StaffOut)
async def create_staff(practice_id: str, payload: StaffIn, db: AsyncSession = Depends(get_db)):
    _check_staff_services(await _get(db, practice_id), payload)
    person = Staff(practice_id=practice_id, **payload.to_columns())
    db.add(person)
    return await _save(db, person)


@router.put("/{practice_id}/staff/{staff_id}", response_model=StaffOut)
async def update_staff(practice_id: str, staff_id: str, payload: StaffIn, db: AsyncSession = Depends(get_db)):
    person = await db.get(Staff, staff_id)
    if person is None or person.practice_id != practice_id:
        raise HTTPException(status_code=404, detail="Staff not found")
    _check_staff_services(await _get(db, practice_id), payload, person.service_ids)
    for k, v in payload.to_columns().items():
        setattr(person, k, v)
    return await _save(db, person)


# --- call log ---


@router.get("/{practice_id}/calls", response_model=list[ReceptionistCallOut])
async def list_practice_calls(
    practice_id: str, outcome: str | None = None, include_web: bool = True, limit: int = 200,
    db: AsyncSession = Depends(get_db),
):
    await _get(db, practice_id)
    q = select(Call).where(Call.practice_id == practice_id)
    if outcome:
        q = q.where(Call.outcome == outcome)
    if not include_web:
        q = q.where(Call.direction != "web")
    return (await db.execute(q.order_by(Call.created_at.desc()).limit(min(limit, 500)))).scalars().all()


async def _call(db: AsyncSession, practice_id: str, call_id: str) -> Call:
    call = await db.get(Call, call_id)
    if call is None or call.practice_id != practice_id:
        raise HTTPException(status_code=404, detail="Call not found")
    return call


@router.get("/{practice_id}/calls/{call_id}", response_model=ReceptionistCallDetail)
async def get_practice_call(practice_id: str, call_id: str, db: AsyncSession = Depends(get_db)):
    call = await _call(db, practice_id, call_id)

    async def rows(model, order):
        return list((await db.execute(select(model).where(model.call_id == call.id).order_by(order))).scalars())

    detail = ReceptionistCallOut.model_validate(call).model_dump()
    detail["transcript_entries"] = await rows(TranscriptEntry, TranscriptEntry.created_at)
    detail["routing"] = [r for r in await rows(RoutingEvent, RoutingEvent.created_at) if r.kind not in ("found", "action")]
    detail["messages"] = await rows(Message, Message.created_at)
    detail["handoffs"] = await rows(Handoff, Handoff.created_at)
    detail["appointment"] = await db.get(Appointment, call.appointment_id) if call.appointment_id else None
    return ReceptionistCallDetail.model_validate(detail)


@router.put("/{practice_id}/calls/{call_id}/review", response_model=ReceptionistCallOut)
async def review_call(practice_id: str, call_id: str, payload: CallReview, db: AsyncSession = Depends(get_db)):
    """Human review for the accuracy metrics (routing and booking correct?)."""
    call = await _call(db, practice_id, call_id)
    call.review = payload.model_dump()
    await db.commit()
    await db.refresh(call)
    return call


@router.post("/{practice_id}/calls/{call_id}/retry-summary")
async def retry_summary(practice_id: str, call_id: str, db: AsyncSession = Depends(get_db)):
    """Operator recovery after the summary model failed during call finalization."""
    call = await _call(db, practice_id, call_id)
    if not call.finalized:
        raise HTTPException(status_code=409, detail="Call has not been finalized")
    entries = (await db.execute(select(TranscriptEntry).where(TranscriptEntry.call_id == call.id)
                                .order_by(TranscriptEntry.created_at))).scalars()
    transcript = "\n".join(f"{'Caller' if e.role == TranscriptRole.friend else 'Assistant'}: {e.text}"
                           for e in entries)
    summary = await finalize.summarize(transcript, call.language or "el")
    if not summary:
        raise HTTPException(status_code=503, detail="Summary model unavailable or transcript empty")
    call.summary = summary
    call.flags = [f for f in (call.flags or []) if f != "summary_fallback"]
    await db.commit()
    events.publish(call.id)
    return {"summary": summary}


@router.get("/{practice_id}/notifications")
async def list_notifications(practice_id: str, status: str = "failed", db: AsyncSession = Depends(get_db)):
    await _get(db, practice_id)
    if status not in {"failed", "pending", "sent"}:
        raise HTTPException(status_code=400, detail="Unsupported notification status")
    rows = (await db.execute(select(Notification).where(
        Notification.practice_id == practice_id, Notification.status == status,
    ).order_by(Notification.created_at.desc()).limit(200))).scalars()
    return [{"id": n.id, "kind": n.kind, "channel": n.channel, "recipient": n.recipient,
             "attempts": n.attempts, "last_error": n.last_error, "call_id": n.call_id} for n in rows]


@router.post("/{practice_id}/notifications/{notification_id}/retry")
async def retry_notification(practice_id: str, notification_id: str, db: AsyncSession = Depends(get_db)):
    await _get(db, practice_id)
    item = await db.get(Notification, notification_id)
    if item is None or item.practice_id != practice_id:
        raise HTTPException(status_code=404, detail="Notification not found")
    if item.status != "failed":
        raise HTTPException(status_code=409, detail="Only failed notifications can be retried")
    item.status = "pending"
    item.attempts = 0
    item.last_error = None
    item.next_attempt_at = datetime.utcnow()
    await db.commit()
    notifications.kick()
    return {"status": "pending"}


@router.get("/{practice_id}/metrics")
async def practice_metrics(practice_id: str, days: int = 30, include_web: bool = False, db: AsyncSession = Depends(get_db)):
    return await metrics.compute(db, await _get(db, practice_id), days, include_web)


@router.get("/{practice_id}/routing")
async def routing_analytics(practice_id: str, days: int = 30, db: AsyncSession = Depends(get_db)):
    return await metrics.routing_report(db, await _get(db, practice_id), days)


# --- messages ---


@router.get("/{practice_id}/messages", response_model=list[MessageOut])
async def list_messages(practice_id: str, status: str | None = None, db: AsyncSession = Depends(get_db)):
    await _get(db, practice_id)
    q = select(Message).where(Message.practice_id == practice_id)
    if status:
        q = q.where(Message.status == status)
    return (await db.execute(q.order_by(Message.created_at.desc()).limit(300))).scalars().all()


@router.patch("/{practice_id}/messages/{message_id}", response_model=MessageOut)
async def update_message(practice_id: str, message_id: str, payload: MessageUpdate, db: AsyncSession = Depends(get_db)):
    m = await db.get(Message, message_id)
    if m is None or m.practice_id != practice_id:
        raise HTTPException(status_code=404, detail="Message not found")
    m.status = payload.status
    await db.commit()
    await db.refresh(m)
    events.publish(f"practice:{practice_id}")
    return m


# --- handoffs (W1) ---


@router.get("/{practice_id}/handoffs", response_model=list[HandoffOut])
async def list_handoffs(practice_id: str, active: bool = True, db: AsyncSession = Depends(get_db)):
    await _get(db, practice_id)
    q = select(Handoff).where(Handoff.practice_id == practice_id)
    if active:
        q = q.where(Handoff.status == "ringing")
    return (await db.execute(q.order_by(Handoff.created_at.desc()).limit(50))).scalars().all()


@router.post("/{practice_id}/handoffs/{handoff_id}/join")
async def join_handoff(practice_id: str, handoff_id: str, payload: HandoffJoin, db: AsyncSession = Depends(get_db)):
    """A LiveKit token to join the live call from the app. Talking makes the agent step back;
    listen-only just listens (and sees the live transcript)."""
    h = await db.get(Handoff, handoff_id)
    if h is None or h.practice_id != practice_id:
        raise HTTPException(status_code=404, detail="Handoff not found")
    if h.status not in ("ringing", "joined"):
        raise HTTPException(status_code=409, detail="The caller is no longer waiting")
    from app.config import get_settings
    import uuid
    kind = "listen" if payload.listen_only else "join"
    identity = f"staff-{kind}-{uuid.uuid4().hex[:8]}"
    return {
        "url": get_settings().livekit_url,
        "token": receptionist.room_token(h.room_name, identity, "Staff", can_publish=not payload.listen_only),
        "call_id": h.call_id,
    }


# --- appointments ---


@router.get("/{practice_id}/appointments", response_model=list[AppointmentOut])
async def list_appointments(practice_id: str, upcoming: bool = False, db: AsyncSession = Depends(get_db)):
    await _get(db, practice_id)
    q = select(Appointment).where(Appointment.practice_id == practice_id)
    if upcoming:
        q = q.where(Appointment.starts_at >= datetime.now(timezone.utc), Appointment.status == "booked")
    return (await db.execute(q.order_by(Appointment.starts_at))).scalars().all()


async def _sms(db, practice, kind, appt) -> None:
    if not appt.customer_phone or (practice.notifications or {}).get("customer_sms") is False:
        return
    staff = await booking.staff_of(db, practice.id)
    await notifications.queue(
        db, practice_id=practice.id, kind=f"{kind}_customer", channel="sms", recipient=appt.customer_phone,
        body=texts.customer_sms(practice, kind, booking.describe(practice, appt, staff, practice.language), practice.language),
    )


def _booking_http_error(e: booking.BookingError) -> HTTPException:
    return HTTPException(status_code=409 if e.code == "slot_taken" else 400, detail={"error": e.code, **e.details})


@router.post("/{practice_id}/appointments", response_model=AppointmentOut)
async def create_appointment(practice_id: str, payload: AppointmentCreate, db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    try:
        appt = await booking.book(
            db, practice, day=payload.date, start_time=payload.time, service_id=payload.service_id,
            customer_name=payload.customer_name, customer_phone=payload.customer_phone, call_id=None,
            now=datetime.now(timezone.utc), staff_name=payload.staff, source="app",
        )
    except booking.BookingError as e:
        raise _booking_http_error(e)
    practice = await _get(db, practice_id)
    await _sms(db, practice, "booked", appt)
    await db.commit()
    notifications.kick()
    return appt


@router.put("/{practice_id}/appointments/{appointment_id}", response_model=AppointmentOut)
async def move_appointment(practice_id: str, appointment_id: str, payload: AppointmentMove, db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    try:
        appt, _ = await booking.reschedule(db, practice, appointment_id=appointment_id, day=payload.date,
                                           start_time=payload.time, now=datetime.now(timezone.utc))
    except booking.BookingError as e:
        raise _booking_http_error(e)
    practice = await _get(db, practice_id)
    await _sms(db, practice, "rescheduled", appt)
    await db.commit()
    notifications.kick()
    return appt


@router.delete("/{practice_id}/appointments/{appointment_id}", response_model=AppointmentOut)
async def cancel_appointment(practice_id: str, appointment_id: str, db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    try:
        appt = await booking.cancel(db, practice, appointment_id=appointment_id)
    except booking.BookingError as e:
        raise _booking_http_error(e)
    practice = await _get(db, practice_id)
    await _sms(db, practice, "cancelled", appt)
    await receptionist.offer_freed_slot(db, practice, appt)
    await db.commit()
    notifications.kick()
    from app.dispatcher import start_next_queued
    await start_next_queued(db)
    return appt


# --- closures and leave (OP3) ---


def _change_error(e: config_changes.ChangeError) -> HTTPException:
    return HTTPException(status_code=404 if e.code in ("not_found", "unknown_staff") else 409, detail=e.code)


async def _closure_out(db: AsyncSession, practice: Practice, c: dict) -> ClosureOut:
    return ClosureOut(
        id=c["id"], date_from=c["from"], date_to=c["to"], staff_id=c.get("staff_id"), reason=c.get("reason"),
        to_rebook=[AppointmentOut.model_validate(a) for a in await config_changes.to_rebook(db, practice, c)],
    )


@router.get("/{practice_id}/closures", response_model=list[ClosureOut])
async def list_closures(practice_id: str, db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    closures = sorted(booking.rules_for(practice)["closures"] or [], key=lambda c: c["from"])
    return [await _closure_out(db, practice, c) for c in closures]


@router.post("/{practice_id}/closures", response_model=ClosureOut)
async def add_closure(practice_id: str, payload: ClosureIn, db: AsyncSession = Depends(get_db)):
    """The agent stops offering these days at once; appointments already in the range are
    returned in `to_rebook` and emailed to the business."""
    practice = await _get(db, practice_id)
    try:
        closure = await config_changes.add_closure(
            db, practice, date_from=payload.date_from, date_to=payload.date_to, staff_id=payload.staff_id,
            reason=payload.reason, source="app", author="founder")
    except config_changes.ChangeError as e:
        raise _change_error(e)
    out = await _closure_out(db, practice, closure)
    await db.commit()
    notifications.kick()
    events.publish(f"practice:{practice.id}")
    return out


@router.delete("/{practice_id}/closures/{closure_id}", status_code=204)
async def delete_closure(practice_id: str, closure_id: str, db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    try:
        await config_changes.remove_closure(db, practice, closure_id, source="app", author="founder")
    except config_changes.ChangeError as e:
        raise _change_error(e)
    await db.commit()
    events.publish(f"practice:{practice.id}")


# --- config versions, approval queue and doctor links (OP2) ---


@router.get("/{practice_id}/versions", response_model=list[ConfigVersionOut])
async def list_versions(practice_id: str, status: str | None = None, db: AsyncSession = Depends(get_db)):
    """Newest first. `status=pending` is the approval queue."""
    await _get(db, practice_id)
    q = select(ConfigVersion).where(ConfigVersion.practice_id == practice_id)
    if status:
        q = q.where(ConfigVersion.status == status)
    return (await db.execute(q.order_by(ConfigVersion.created_at.desc()).limit(100))).scalars().all()


async def _decide(db: AsyncSession, practice_id: str, version_id: str, action) -> ConfigVersion:
    practice = await _get(db, practice_id)
    try:
        version = await action(db, practice, version_id)
    except config_changes.ChangeError as e:
        raise _change_error(e)
    await db.commit()
    events.publish(f"practice:{practice.id}")
    return version


@router.post("/{practice_id}/versions/{version_id}/approve", response_model=ConfigVersionOut)
async def approve_version(practice_id: str, version_id: str, db: AsyncSession = Depends(get_db)):
    return await _decide(db, practice_id, version_id, config_changes.approve)


@router.post("/{practice_id}/versions/{version_id}/reject", response_model=ConfigVersionOut)
async def reject_version(practice_id: str, version_id: str, db: AsyncSession = Depends(get_db)):
    return await _decide(db, practice_id, version_id, config_changes.reject)


@router.post("/{practice_id}/versions/{version_id}/rollback", response_model=ConfigVersionOut | None)
async def rollback_version(practice_id: str, version_id: str, db: AsyncSession = Depends(get_db)):
    """Back to how things were right after this version. None when that is already the case."""
    async def action(db, practice, version_id):
        return await config_changes.rollback(db, practice, version_id, author="founder")
    return await _decide(db, practice_id, version_id, action)


@router.post("/{practice_id}/links", response_model=AdminLinkOut)
async def create_link(practice_id: str, payload: AdminLinkIn, db: AsyncSession = Depends(get_db)):
    """A magic link for the business: hours, closures and leave, prices and FAQ, no app needed."""
    practice = await _get(db, practice_id)
    try:
        token, link = await config_changes.create_link(db, practice, staff_id=payload.staff_id, hours=payload.hours)
    except config_changes.ChangeError as e:
        raise _change_error(e)
    await db.commit()
    url = f"{get_settings().backend_public_url.rstrip('/')}/manage/{token}"
    return AdminLinkOut(url=url, expires_at=link.expires_at)


@router.delete("/{practice_id}/links", status_code=204)
async def revoke_links(practice_id: str, db: AsyncSession = Depends(get_db)):
    """Turns off every link of this practice."""
    await _get(db, practice_id)
    for link in (await db.execute(select(AdminLink).where(AdminLink.practice_id == practice_id))).scalars():
        link.revoked = True
    await db.commit()


@router.get("/{practice_id}/waitlist", response_model=list[WaitlistOut])
async def list_waitlist(practice_id: str, db: AsyncSession = Depends(get_db)):
    await _get(db, practice_id)
    return (await db.execute(select(WaitlistEntry).where(WaitlistEntry.practice_id == practice_id)
                             .order_by(WaitlistEntry.created_at.desc()))).scalars().all()


# --- devices (push) ---


@misc.post("/devices", status_code=204)
async def register_device(payload: DeviceIn, db: AsyncSession = Depends(get_db)):
    device = await db.get(Device, payload.token)
    if device is None:
        db.add(Device(**payload.model_dump()))
    else:
        device.practice_id, device.staff_id, device.environment = payload.practice_id, payload.staff_id, payload.environment
    await db.commit()
