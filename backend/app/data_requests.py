"""Patient data requests (PRD OP8): everything one caller left with one practice, found by
phone number, exported or erased, and logged for the controller (the practice).

Erasure keeps the call rows (outcome, duration, cost) so metrics and invoices still add up,
but removes everything that identifies the caller: number, name, transcript, summary,
recording, messages, waitlist entries, and the text of notifications about them.
"""

import hashlib
from datetime import datetime, timezone

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Appointment, AppointmentStatus, Call, Customer, DataRequest, Message, Notification, Practice, TranscriptEntry,
    WaitlistEntry,
)
from app.schemas import normalize_phone
from app.storage import queue_recording_deletion

ERASED = "erased"


class HasUpcoming(Exception):
    def __init__(self, appointments: list[Appointment]) -> None:
        super().__init__("has_upcoming")
        self.appointments = appointments


def phone_hash(practice: Practice, phone: str) -> str:
    return hashlib.sha256(f"{practice.id}:{phone}".encode()).hexdigest()


async def _calls(db: AsyncSession, practice: Practice, phone: str) -> list[Call]:
    return list((await db.execute(
        select(Call).where(Call.practice_id == practice.id, Call.caller_number == phone).order_by(Call.created_at)
    )).scalars())


async def _messages(db: AsyncSession, practice: Practice, phone: str, call_ids: list[str]) -> list[Message]:
    cond = Message.callback_number == phone
    if call_ids:
        cond = or_(cond, Message.call_id.in_(call_ids))
    return list((await db.execute(select(Message).where(Message.practice_id == practice.id, cond))).scalars())


async def export(db: AsyncSession, practice: Practice, phone: str) -> dict:
    phone = normalize_phone(phone)
    customer = (await db.execute(
        select(Customer).where(Customer.practice_id == practice.id, Customer.phone == phone)
    )).scalar_one_or_none()
    calls = await _calls(db, practice, phone)
    call_ids = [c.id for c in calls]
    transcripts: dict[str, list] = {}
    if call_ids:
        for t in (await db.execute(select(TranscriptEntry).where(TranscriptEntry.call_id.in_(call_ids))
                                   .order_by(TranscriptEntry.created_at))).scalars():
            transcripts.setdefault(t.call_id, []).append(
                {"speaker": "caller" if t.role.value == "friend" else "agent", "text": t.text,
                 "at": t.created_at.isoformat()})
    appointments = list((await db.execute(select(Appointment).where(
        Appointment.practice_id == practice.id, Appointment.customer_phone == phone
    ).order_by(Appointment.starts_at))).scalars())
    messages = await _messages(db, practice, phone, call_ids)
    waitlist = list((await db.execute(select(WaitlistEntry).where(
        WaitlistEntry.practice_id == practice.id, WaitlistEntry.phone == phone))).scalars())
    out = {
        "practice": practice.name,
        "phone": phone,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "customer": {"name": customer.name, "since": customer.created_at.isoformat()} if customer else None,
        "calls": [{
            "id": c.id, "at": c.created_at.isoformat(), "duration_seconds": c.duration_seconds,
            "outcome": c.outcome, "summary": c.summary, "recording": bool(c.recording_url),
            "transcript": transcripts.get(c.id, []),
        } for c in calls],
        "appointments": [{
            "service": a.service_name, "starts_at": a.starts_at.isoformat(), "status": a.status.value
            if hasattr(a.status, "value") else a.status, "name": a.customer_name,
        } for a in appointments],
        "messages": [{"at": m.created_at.isoformat(), "name": m.caller_name, "reason": m.reason,
                      "best_time": m.best_time} for m in messages],
        "waitlist": [{"service": w.service_id, "from": w.date_from.isoformat(), "to": w.date_to.isoformat(),
                      "status": w.status} for w in waitlist],
    }
    await _log(db, practice, phone, "export", out)
    return out


async def erase(db: AsyncSession, practice: Practice, phone: str, now: datetime) -> dict:
    """Raises HasUpcoming while the caller still has a booked future appointment: cancel it
    first so the calendar and the customer's SMS stay correct."""
    phone = normalize_phone(phone)
    upcoming = list((await db.execute(select(Appointment).where(
        Appointment.practice_id == practice.id, Appointment.customer_phone == phone,
        Appointment.status == AppointmentStatus.booked, Appointment.starts_at >= now,
    ))).scalars())
    if upcoming:
        raise HasUpcoming(upcoming)
    calls = await _calls(db, practice, phone)
    call_ids = [c.id for c in calls]
    counts = {"calls": len(calls), "recordings": 0}
    for c in calls:
        if c.recording_url:
            await queue_recording_deletion(db, c.recording_url, c.id)
            counts["recordings"] += 1
        c.recording_url = None
        c.caller_number = None
        c.customer_id = None
        c.summary = None
        c.review = None
        c.delete_requested = True
    if call_ids:
        await db.execute(delete(TranscriptEntry).where(TranscriptEntry.call_id.in_(call_ids)))
    messages = await _messages(db, practice, phone, call_ids)
    counts["messages"] = len(messages)
    for m in messages:
        await db.delete(m)
    counts["waitlist"] = (await db.execute(delete(WaitlistEntry).where(
        WaitlistEntry.practice_id == practice.id, WaitlistEntry.phone == phone))).rowcount
    appointments = list((await db.execute(select(Appointment).where(
        Appointment.practice_id == practice.id, Appointment.customer_phone == phone))).scalars())
    counts["appointments"] = len(appointments)
    for a in appointments:
        a.customer_name, a.customer_phone, a.customer_id = "—", None, None
    note_cond = Notification.recipient == phone
    if call_ids:
        note_cond = or_(note_cond, Notification.call_id.in_(call_ids))
    notes = list((await db.execute(select(Notification).where(Notification.practice_id == practice.id, note_cond))).scalars())
    counts["notifications"] = len(notes)
    for n in notes:
        n.subject, n.body, n.data = "", "", {}
        if n.recipient == phone:
            n.recipient = ERASED
        if n.status == "pending":
            n.status = "failed"
            n.last_error = "erased on request"
    customer = (await db.execute(
        select(Customer).where(Customer.practice_id == practice.id, Customer.phone == phone)
    )).scalar_one_or_none()
    counts["customer"] = int(customer is not None)
    if customer:
        await db.flush()
        await db.delete(customer)
    await _log(db, practice, phone, "erase", counts)
    return counts


async def _log(db: AsyncSession, practice: Practice, phone: str, kind: str, data: dict) -> None:
    counts = data if kind == "erase" else {k: len(data[k]) for k in ("calls", "appointments", "messages", "waitlist")}
    db.add(DataRequest(practice_id=practice.id, phone_hash=phone_hash(practice, phone), kind=kind, counts=counts))
