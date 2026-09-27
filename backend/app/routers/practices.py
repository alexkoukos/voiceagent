"""Founder-side setup and the receptionist call log (manual onboarding, PRD scope)."""

import json
import hashlib
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field, ValidationError

from app.tenant_resources import validate_assignments
from app.auth import require_master_token
from app import booking, config_changes, events, finalize, metrics, notifications, receptionist, texts
from app.config import CONFIG_DIR, get_settings
from app.database import get_db
from app.models import (
    AdminLink, TenantApiKey, CalendarFeed, Appointment, Call, ConfigVersion, Device, Handoff, ImportRecord, Message, Notification, Practice, RoutingEvent, Staff, TranscriptEntry,
    TranscriptRole, WaitlistEntry,
)
from app.schemas import (
    AppointmentCreate, AppointmentMove, AppointmentOut, AdminLinkIn, AdminLinkOut, CallReview, ClosureIn, ClosureOut, ConfigVersionOut, DeviceIn, HandoffJoin, HandoffOut,
    MessageOut, MessageUpdate, PracticeIn, PracticeOut, ReceptionistCallDetail, ReceptionistCallOut, StaffIn,
    StaffOut, WaitlistOut, ConfigVersionEdit, PublishFreezeIn,
)

router = APIRouter(prefix="/practices", tags=["practices"])
misc = APIRouter(tags=["practices"])
VERTICALS_DIR = CONFIG_DIR / "verticals"


async def _get(db: AsyncSession, practice_id: str) -> Practice:
    if db.info.get("tenant_id") and db.info["tenant_id"] != practice_id:
        raise HTTPException(status_code=404, detail="Practice not found")
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
    path = next((f for f in VERTICALS_DIR.glob("*.json") if f.stem == vertical), None)
    if path is None:
        raise HTTPException(status_code=404, detail="Unknown vertical")
    v = json.loads(path.read_text(encoding="utf-8"))
    return {k: v[k] for k in ("hours", "services", "routing_rules", "knowledge_base") if k in v} | {
        "vertical": vertical, "rules": v.get("rules", {}),
    }


# --- practices ---


@router.post("", response_model=PracticeOut)
async def create_practice(payload: PracticeIn, db: AsyncSession = Depends(get_db)):
    await validate_assignments(db, "", numbers=payload.phone_numbers, calendar_id=payload.calendar_id)
    practice = Practice(**payload.to_columns())
    db.add(practice)
    return await _save(db, practice)


@router.get("", response_model=list[PracticeOut])
async def list_practices(db: AsyncSession = Depends(get_db)):
    query = select(Practice).order_by(Practice.created_at)
    if db.info.get("tenant_id"):
        query = query.where(Practice.id == db.info["tenant_id"])
    return (await db.execute(query)).scalars().all()


@router.get("/{practice_id}", response_model=PracticeOut)
async def get_practice(practice_id: str, db: AsyncSession = Depends(get_db)):
    return await _get(db, practice_id)


@router.put("/{practice_id}", response_model=PracticeOut)
async def update_practice(practice_id: str, payload: PracticeIn, db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    if db.info.get("tenant_id") and any(getattr(payload, key) != getattr(practice, key)
                                        for key in ("phone_numbers", "calendar_id", "outbound_number")):
        raise HTTPException(status_code=403, detail="Number and calendar provisioning requires founder access or Google connect")
    await validate_assignments(db, practice_id, numbers=payload.phone_numbers, calendar_id=payload.calendar_id)
    await config_changes.lock_config(db, practice)
    columns = payload.to_columns()
    # Closures have their own calls; a full update never drops them.
    columns["rules"]["closures"] = (practice.rules or {}).get("closures") or []
    try:
        await config_changes.publish(db, practice, {k: columns.pop(k) for k in config_changes.FIELDS},
                                     source="app", author="founder")
    except config_changes.ChangeError as exc:
        raise _change_error(exc)
    for k, v in columns.items():
        setattr(practice, k, v)
    return await _save(db, practice)


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
    await _get(db, practice_id)
    if db.info.get("tenant_id") and payload.calendar_id:
        raise HTTPException(status_code=403, detail="Use Google connect to assign a calendar")
    await validate_assignments(db, practice_id, calendar_id=payload.calendar_id)
    person = Staff(practice_id=practice_id, **payload.to_columns())
    db.add(person)
    return await _save(db, person)


@router.put("/{practice_id}/staff/{staff_id}", response_model=StaffOut)
async def update_staff(practice_id: str, staff_id: str, payload: StaffIn, db: AsyncSession = Depends(get_db)):
    person = await db.get(Staff, staff_id)
    if person is None or person.practice_id != practice_id:
        raise HTTPException(status_code=404, detail="Staff not found")
    if db.info.get("tenant_id") and payload.calendar_id != person.calendar_id:
        raise HTTPException(status_code=403, detail="Use Google connect to assign a calendar")
    await validate_assignments(db, practice_id, calendar_id=payload.calendar_id)
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
    appointment = await db.get(Appointment, call.appointment_id) if call.appointment_id else None
    detail["appointment"] = appointment if appointment and appointment.practice_id == practice_id else None
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


@router.put("/{practice_id}/publish-freeze")
async def publish_freeze(practice_id: str, payload: PublishFreezeIn, db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    await config_changes.lock_config(db, practice)
    practice.publish_frozen = payload.frozen
    await db.commit()
    events.publish(f"practice:{practice.id}")
    return {"frozen": practice.publish_frozen}


@router.get("/{practice_id}/imports")
async def list_imports(practice_id: str, db: AsyncSession = Depends(get_db)):
    await _get(db, practice_id)
    rows = (await db.execute(select(ImportRecord).where(ImportRecord.practice_id == practice_id)
                             .order_by(ImportRecord.created_at.desc()).limit(100))).scalars()
    return [{"id": r.id, "version_id": r.version_id, "source": r.source, "extracted": r.extracted,
             "confidence": r.confidence, "created_at": r.created_at} for r in rows]


@router.patch("/{practice_id}/versions/{version_id}", response_model=ConfigVersionOut)
async def edit_version(practice_id: str, version_id: str, payload: ConfigVersionEdit,
                       db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    try:
        version = await config_changes.edit_pending(db, practice, version_id, payload.changes)
    except config_changes.ChangeError as exc:
        raise _change_error(exc)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await db.commit()
    events.publish(f"practice:{practice.id}")
    return version


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
    tenant = db.info.get("tenant_id")
    if tenant and payload.practice_id != tenant:
        raise HTTPException(status_code=403, detail="Device must belong to this practice")
    if payload.practice_id:
        await _get(db, payload.practice_id)
    if payload.staff_id:
        person = await db.get(Staff, payload.staff_id)
        if not person or person.practice_id != payload.practice_id:
            raise HTTPException(status_code=404, detail="Staff not found")
    device = await db.get(Device, payload.token)
    if device and tenant and device.practice_id != tenant:
        raise HTTPException(status_code=409, detail="Device already registered")
    if device is None:
        db.add(Device(**payload.model_dump()))
    else:
        device.practice_id, device.staff_id, device.environment = payload.practice_id, payload.staff_id, payload.environment
    await db.commit()


@router.post("/{practice_id}/api-keys", dependencies=[Depends(require_master_token)])
async def create_api_key(practice_id: str, db: AsyncSession = Depends(get_db)):
    await _get(db, practice_id)
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    db.add(TenantApiKey(token_hash=digest, practice_id=practice_id))
    await db.commit()
    return {"id": digest, "token": token, "practice_id": practice_id}


@router.delete("/{practice_id}/api-keys/{key_id}", status_code=204,
               dependencies=[Depends(require_master_token)])
async def revoke_api_key(practice_id: str, key_id: str, db: AsyncSession = Depends(get_db)):
    key = await db.get(TenantApiKey, key_id)
    if not key or key.practice_id != practice_id:
        raise HTTPException(status_code=404, detail="Not found")
    key.revoked_at = datetime.utcnow()
    await db.commit()


class FeedIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(max_length=4000)
    staff_id: str | None = None


@router.get("/{practice_id}/calendar/feeds")
async def list_feeds(practice_id: str, db: AsyncSession = Depends(get_db)):
    await _get(db, practice_id)
    feeds = (await db.execute(select(CalendarFeed).where(CalendarFeed.practice_id == practice_id))).scalars()
    return [{"id": f.id, "name": f.name, "staff_id": f.staff_id} for f in feeds]


@router.post("/{practice_id}/calendar/feeds")
async def add_feed(practice_id: str, payload: FeedIn, db: AsyncSession = Depends(get_db)):
    from app import ical_feed, public_fetch
    from datetime import timedelta
    practice = await _get(db, practice_id)
    if payload.staff_id:
        person = await db.get(Staff, payload.staff_id)
        if not person or person.practice_id != practice_id:
            raise HTTPException(status_code=404, detail="Staff not found")
    try:
        data, _ = await public_fetch.get(payload.url)
        now = datetime.now(timezone.utc)
        ical_feed.intervals(data, now, now + timedelta(days=7), practice.timezone)
    except Exception:
        raise HTTPException(status_code=422, detail="Calendar feed unavailable or invalid")
    feed = CalendarFeed(practice_id=practice_id, **payload.model_dump())
    db.add(feed)
    await db.commit()
    return {"id": feed.id, "name": feed.name, "staff_id": feed.staff_id}


@router.delete("/{practice_id}/calendar/feeds/{feed_id}", status_code=204)
async def remove_feed(practice_id: str, feed_id: str, db: AsyncSession = Depends(get_db)):
    feed = await db.get(CalendarFeed, feed_id)
    if not feed or feed.practice_id != practice_id:
        raise HTTPException(status_code=404, detail="Not found")
    await db.delete(feed)
    await db.commit()


@router.post("/{practice_id}/test-session", dependencies=[Depends(require_master_token)])
async def test_session(practice_id: str, db: AsyncSession = Depends(get_db)):
    """Opt-in isolated voice harness. Never enabled on a production backend."""
    if not get_settings().test_sessions_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    practice = await _get(db, practice_id)
    staff = await booking.staff_of(db, practice_id)
    if (not (practice.slug or "").startswith("test-") or practice.phone_numbers or practice.calendar_id
            or any(p.calendar_id or p.phone for p in staff)
            or (practice.notifications or {}).get("customer_sms", True)
            or (practice.notifications or {}).get("emails") or (practice.reminders or {}).get("enabled")):
        raise HTTPException(status_code=409, detail="Use an isolated test practice without external recipients")
    try:
        call, metadata = await receptionist.start_call(db, practice, direction="web", caller_number="+306900000001")
    except (receptionist.Busy, receptionist.OverCap):
        raise HTTPException(status_code=429, detail="busy")
    room = receptionist.room_of(call)
    try:
        await receptionist.dispatch(room, metadata)
    except Exception:
        from app.models import CallStatus
        call.status = CallStatus.failed
        call.end_reason = "error"
        await db.commit()
        raise HTTPException(status_code=502, detail="Could not start test session")
    return {"url": get_settings().livekit_url,
            "token": receptionist.room_token(room, f"caller-{call.id}", "Synthetic caller"), "call_id": call.id}
