"""Pilot dates and the booking guarantee. Produces drafts, never legal invoices or charges."""
import calendar
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from app.models import Appointment, BillingAccount, BillingDraft, Call, CallStatus


def month_after(anchor: date, offset: int) -> date:
    year, month = divmod(anchor.year * 12 + anchor.month - 1 + offset, 12)
    month += 1
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


async def prepare_due(db, practice, now: datetime) -> list[BillingDraft]:
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"billing:{practice.id}"})
    account = await db.get(BillingAccount, practice.id)
    if not account or practice.offboarded_at:
        return []
    tz = ZoneInfo(practice.timezone)
    today = now.astimezone(tz).date()
    anchor = account.pilot_started_on + timedelta(days=30)
    offset, drafts = 0, []
    while (end := month_after(anchor, offset + 1)) <= today:
        start = month_after(anchor, offset)
        offset += 1
        exists = (await db.execute(select(BillingDraft.id).where(
            BillingDraft.practice_id == practice.id, BillingDraft.period_start == start))).first()
        if exists:
            continue
        rows = (await db.execute(select(Call.id, Call.appointment_id).join(
            Appointment, (Appointment.id == Call.appointment_id) & (Appointment.practice_id == Call.practice_id)
        ).where(Call.practice_id == practice.id, Call.direction == "inbound", Call.status == CallStatus.completed,
                Call.outcome == "booked", Call.created_at >= datetime.combine(start,time.min,tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None),
                Call.created_at < datetime.combine(end,time.min,tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None),
                Appointment.status == "booked").order_by(Call.created_at))).all()
        # A retried call or duplicate report never counts the same appointment twice.
        evidence = list({appt: cid for cid, appt in rows}.values())
        waived = len(evidence) < practice.guarantee_threshold
        draft = BillingDraft(practice_id=practice.id, period_start=start, period_end=end,
                             bookings=len(evidence), threshold=practice.guarantee_threshold,
                             amount=0 if waived else account.monthly_fee,
                             status="waived" if waived else "pending_provider", call_ids=evidence)
        db.add(draft); drafts.append(draft)
    await db.flush()
    return drafts
