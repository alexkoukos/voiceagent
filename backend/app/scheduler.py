"""Background jobs, run once a minute in the (single) backend process:
daily digest (20:00), monthly value report, reminder calls, retention, stale calls."""

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select

from app import booking, finalize, notifications, receptionist, texts
from app.database import async_session
from app.models import (
    Appointment, AppointmentStatus, Call, CallStatus, Handoff, Practice, TranscriptEntry,
)
from app.storage import delete_recording_later

logger = logging.getLogger("scheduler")


def _at(local: datetime, hhmm: str) -> bool:
    """True during the minute window after hh:mm local (the job itself is deduplicated)."""
    h, m = map(int, hhmm.split(":"))
    target = local.replace(hour=h, minute=m, second=0, microsecond=0)
    return target <= local < target + timedelta(minutes=10)


async def digest(db, practice: Practice, local: datetime) -> None:
    if not notifications.business_emails(practice):
        return
    tz = ZoneInfo(practice.timezone)
    start = datetime.combine(local.date(), time(0), tz)
    rows = await db.execute(
        select(Call.outcome, func.count()).where(
            Call.practice_id == practice.id, Call.direction != "web",
            Call.created_at >= start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None),
        ).group_by(Call.outcome)
    )
    outcomes = {o or "in_progress": n for o, n in rows.all()}
    tomorrow = start + timedelta(days=1)
    appts = (await db.execute(
        select(Appointment).where(
            Appointment.practice_id == practice.id, Appointment.status == AppointmentStatus.booked,
            Appointment.starts_at >= tomorrow, Appointment.starts_at < tomorrow + timedelta(days=1),
        ).order_by(Appointment.starts_at)
    )).scalars()
    staff = await booking.staff_of(db, practice.id)
    described = [booking.describe(practice, a, staff, practice.language) for a in appts]
    subject, body = texts.digest_email(practice, booking.say_date(local.date(), practice.language), outcomes, described)
    await notifications.queue_business(db, practice, kind="digest", subject=subject, body=body,
                                       dedupe_key=f"digest:{practice.id}:{local.date()}")


async def month_stats(db, practice: Practice, first: date, last: date) -> tuple[int, int]:
    tz = ZoneInfo(practice.timezone)
    lo = datetime.combine(first, time(0), tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    hi = datetime.combine(last + timedelta(days=1), time(0), tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    calls = (await db.execute(select(func.count()).select_from(Call).where(
        Call.practice_id == practice.id, Call.direction == "inbound", Call.created_at >= lo, Call.created_at < hi,
    ))).scalar_one()
    bookings = (await db.execute(select(func.count()).select_from(Appointment).where(
        Appointment.practice_id == practice.id, Appointment.source.in_(["agent", "waitlist"]),
        Appointment.created_at >= lo, Appointment.created_at < hi,
    ))).scalar_one()
    return calls, bookings


async def monthly_report(db, practice: Practice, local: datetime) -> None:
    if not (practice.notifications or {}).get("monthly_report", True) or not notifications.business_emails(practice):
        return
    last = local.date().replace(day=1) - timedelta(days=1)
    first = last.replace(day=1)
    calls, bookings = await month_stats(db, practice, first, last)
    subject, body = texts.monthly_email(practice, first.strftime("%m/%Y"), calls, bookings,
                                        bookings * (practice.avg_booking_value or 0), practice.guarantee_threshold)
    await notifications.queue_business(db, practice, kind="monthly_report", subject=subject, body=body,
                                       dedupe_key=f"monthly:{practice.id}:{first:%Y-%m}")


async def queue_reminders(db, practice: Practice, local: datetime) -> None:
    tz = ZoneInfo(practice.timezone)
    tomorrow = datetime.combine(local.date() + timedelta(days=1), time(0), tz)
    appts = (await db.execute(
        select(Appointment).where(
            Appointment.practice_id == practice.id, Appointment.status == AppointmentStatus.booked,
            Appointment.reminder_status == "none",
            Appointment.starts_at >= tomorrow, Appointment.starts_at < tomorrow + timedelta(days=1),
        )
    )).scalars()
    for appt in appts:
        await receptionist.queue_reminder(db, practice, appt)


async def retention(db, practice: Practice) -> None:
    now = datetime.utcnow()
    old_rec = now - timedelta(days=practice.retention_recordings_days)
    for call in (await db.execute(select(Call).where(
        Call.practice_id == practice.id, Call.recording_url.is_not(None), Call.created_at < old_rec,
    ))).scalars():
        delete_recording_later(call.recording_url)
        call.recording_url = None
    old_tr = now - timedelta(days=practice.retention_transcripts_days)
    ids = select(Call.id).where(Call.practice_id == practice.id, Call.created_at < old_tr)
    await db.execute(delete(TranscriptEntry).where(TranscriptEntry.call_id.in_(ids)))


async def stale(db) -> None:
    """Agents that died never report back: close their calls and handoffs."""
    now = datetime.utcnow()
    for h in (await db.execute(select(Handoff).where(
        Handoff.status == "ringing", Handoff.created_at < now - timedelta(seconds=90)
    ))).scalars():
        h.status, h.resolved_at = "unanswered", now
    cutoff = now - timedelta(seconds=receptionist.get_settings().max_call_duration_seconds + receptionist.STALE_SECONDS)
    for call in (await db.execute(select(Call).where(
        Call.practice_id.is_not(None), Call.status.in_([CallStatus.dialing, CallStatus.active]), Call.created_at < cutoff,
    ))).scalars():
        call.status = CallStatus.completed if call.started_at else CallStatus.failed
        call.ended_at = now
        if call.started_at:
            call.duration_seconds = int((now - call.started_at).total_seconds())
        finalize.schedule(call.id)


_last_retention: date | None = None


async def tick(now: datetime | None = None) -> None:
    global _last_retention
    now = now or datetime.now(ZoneInfo("UTC"))
    async with async_session() as db:
        practices = list((await db.execute(select(Practice))).scalars())
        for practice in practices:
            local = now.astimezone(ZoneInfo(practice.timezone))
            if _at(local, (practice.notifications or {}).get("digest_time", "20:00")):
                await digest(db, practice, local)
            if local.day == 1 and _at(local, "09:00"):
                await monthly_report(db, practice, local)
            rem = practice.reminders or {}
            if rem.get("enabled") and _at(local, rem.get("time", "18:00")):
                await queue_reminders(db, practice, local)
        if _last_retention != now.date():
            for practice in practices:
                await retention(db, practice)
            _last_retention = now.date()
        await stale(db)
        await notifications.commit_ignoring_duplicates(db)
        from app.dispatcher import start_next_queued
        await start_next_queued(db)
    notifications.kick()


async def run() -> None:
    while True:
        try:
            await tick()
        except Exception:
            logger.exception("scheduler tick")
        await asyncio.sleep(60)
