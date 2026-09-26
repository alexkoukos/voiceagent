"""Background jobs, run once a minute in the (single) backend process:
daily digest (20:00), monthly value report, reminder calls, retention, stale calls."""

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select

from app import alerts, booking, health, finalize, gcal, notifications, receptionist, texts
from app.database import async_session
from app.models import (
    Appointment, AppointmentStatus, Call, CallStatus, Handoff, Notification, Practice, TranscriptEntry,
)
from app.storage import process_recording_deletions, queue_recording_deletion, seal_recordings

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


# OP1: a daily real test call must connect within this window, or the telephony path is broken.
TEST_CALL_CONNECT_WITHIN = timedelta(minutes=3)


def _midnight_utc(local: datetime) -> datetime:
    tz = local.tzinfo or ZoneInfo("UTC")
    return datetime.combine(local.date(), time(0), tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


async def _todays_test_call(db, practice: Practice, local: datetime) -> Call | None:
    return (await db.execute(
        select(Call).where(
            Call.practice_id == practice.id, Call.purpose == "test", Call.created_at >= _midnight_utc(local),
        ).order_by(Call.created_at.desc()).limit(1)
    )).scalars().first()


async def test_call(db, practice: Practice, local: datetime) -> None:
    """OP1: once a day at the configured time, place a real call that exercises the whole path."""
    cfg = (practice.notifications or {}).get("test_call") or {}
    if not cfg.get("enabled") or not cfg.get("number"):
        return
    if not _at(local, cfg.get("time", "09:00")):
        return
    if await _todays_test_call(db, practice, local):
        return  # already placed today (the _at window spans several ticks)
    await receptionist.queue_test_call(db, practice, cfg["number"])


async def check_test_call(db, practice: Practice, now: datetime) -> None:
    """OP1: alert (OP9) when today's test call did not connect. A connected call sets started_at."""
    cfg = (practice.notifications or {}).get("test_call") or {}
    if not cfg.get("enabled"):
        return
    local = now.astimezone(ZoneInfo(practice.timezone))
    call = await _todays_test_call(db, practice, local)
    if call is None or call.started_at is not None:
        return  # no test today yet, or it connected
    terminal = call.status in (CallStatus.completed, CallStatus.failed, CallStatus.cancelled)
    if not terminal and datetime.utcnow() - call.created_at <= TEST_CALL_CONNECT_WITHIN:
        return  # still ringing; give it time before alerting
    await alerts.raise_alert(
        db, practice, "test_call_failed", f"{practice.name}: the daily test call did not connect",
        f"An automated test call did not reach the number ({call.end_reason or call.status.value}). "
        "Inbound calls may be failing; check the agent, LiveKit and the Telnyx trunk.",
        call_id=call.id, dedupe_key=f"test_call:{practice.id}:{local.date()}")


# OP7: after offboarding, all recordings and transcripts go within this many days (DPA).
OFFBOARD_PURGE_DAYS = 30


async def retention(db, practice: Practice) -> None:
    now = datetime.utcnow()
    rec_days, tr_days = practice.retention_recordings_days, practice.retention_transcripts_days
    if practice.offboarded_at and practice.offboarded_at <= now - timedelta(days=OFFBOARD_PURGE_DAYS):
        rec_days = tr_days = 0
    old_rec = now - timedelta(days=rec_days)
    for call in (await db.execute(select(Call).where(
        Call.practice_id == practice.id, Call.recording_url.is_not(None), Call.created_at < old_rec,
    ))).scalars():
        await queue_recording_deletion(db, call.recording_url, call.id)
        call.recording_url = None
    old_tr = now - timedelta(days=tr_days)
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


async def failed_notifications(db) -> None:
    """A notification that gave up is an alert (OP9): someone did not hear about a call."""
    since = datetime.utcnow() - timedelta(days=2)
    for n in (await db.execute(select(Notification).where(
        Notification.status == "failed", Notification.created_at >= since, Notification.channel != "none",
        ~Notification.kind.startswith("alert_"), Notification.last_error != "erased on request",
    ))).scalars():
        practice = await db.get(Practice, n.practice_id) if n.practice_id else None
        await alerts.raise_alert(db, practice, "notification_failed",
                                 f"{practice.name if practice else '—'}: {n.channel} {n.kind} failed",
                                 n.last_error or "", call_id=n.call_id, dedupe_key=f"notification:{n.id}")


async def recover_finalizations(db) -> None:
    """Retry completed calls left unfinished by a crash or failed transaction."""
    ids = (await db.execute(select(Call.id).where(
        Call.practice_id.is_not(None), Call.finalized.is_(False),
        Call.status.in_([CallStatus.completed, CallStatus.failed]),
    ).order_by(Call.ended_at).limit(100))).scalars()
    for call_id in ids:
        finalize.schedule(call_id)


async def recover_summary_notifications(db, practice: Practice) -> None:
    """Queue business emails for calls finalized before an email recipient was configured."""
    emails = notifications.business_emails(practice)
    if not emails:
        return
    delivered_or_queued = select(Notification.id).where(
        Notification.call_id == Call.id, Notification.kind == "call_summary", Notification.channel == "email",
    ).exists()
    calls = (await db.execute(select(Call).where(
        Call.practice_id == practice.id, Call.finalized.is_(True),
        Call.status.in_([CallStatus.completed, CallStatus.failed]), ~delivered_or_queued,
    ).order_by(Call.ended_at).limit(50))).scalars()
    for call in calls:
        if not finalize.wants_business_summary(practice, call):
            continue
        appt = await db.get(Appointment, call.appointment_id) if call.appointment_id else None
        staff = await booking.staff_of(db, practice.id)
        described = booking.describe(practice, appt, staff, practice.language) if appt else None
        subject, body = texts.call_email(practice, call, described, None, None)
        for i, email in enumerate(emails):
            await notifications.queue(db, practice_id=practice.id, kind="call_summary", channel="email",
                                      recipient=email, subject=subject, body=body, call_id=call.id,
                                      dedupe_key=f"summary:{call.id}:email:{i}")


async def reconcile_calendar_events(db, practice: Practice, now: datetime) -> None:
    """Delete orphaned events after a crash, or reattach a committed appointment."""
    staff = await booking.staff_of(db, practice.id)
    calendar_ids = {c for c in [practice.calendar_id, *(p.calendar_id for p in staff)] if c}
    horizon = int((practice.rules or {}).get("max_days_ahead", 60))
    for calendar_id in calendar_ids:
        events = await gcal.owned_events(calendar_id, now - timedelta(days=90), now + timedelta(days=horizon + 1))
        for event in events:
            key = event.get("extendedProperties", {}).get("private", {}).get("voiceagent_key", "")
            if not key.startswith(f"{practice.id}:") or not event.get("id") or not event.get("created"):
                continue
            created = datetime.fromisoformat(event["created"].replace("Z", "+00:00"))
            if now - created < timedelta(minutes=10):
                continue  # a live booking transaction may still be finishing
            stored = (await db.execute(select(Appointment).where(
                Appointment.practice_id == practice.id, Appointment.gcal_event_id == event["id"],
            ))).scalar_one_or_none()
            if stored and stored.status == AppointmentStatus.booked:
                continue
            if stored:
                await gcal.delete_event(calendar_id, event["id"])
                continue
            local_key = key[len(practice.id) + 1:]
            if ":" in local_key:
                candidate = (await db.execute(select(Appointment).where(
                    Appointment.practice_id == practice.id, Appointment.idempotency_key == local_key,
                ))).scalar_one_or_none()
            else:
                candidate = await db.get(Appointment, local_key)
            if (candidate and candidate.practice_id == practice.id and candidate.status == AppointmentStatus.booked
                    and not candidate.gcal_event_id):
                try:
                    remote_start = datetime.fromisoformat(event["start"]["dateTime"].replace("Z", "+00:00"))
                    remote_end = datetime.fromisoformat(event["end"]["dateTime"].replace("Z", "+00:00"))
                    if candidate.starts_at == remote_start and candidate.ends_at == remote_end:
                        candidate.gcal_event_id = event["id"]
                        continue
                except (KeyError, ValueError):
                    pass
            await gcal.delete_event(calendar_id, event["id"])


_last_retention: date | None = None
_last_calendar_reconciliation: date | None = None


async def tick(now: datetime | None = None) -> None:
    global _last_retention, _last_calendar_reconciliation
    now = now or datetime.now(ZoneInfo("UTC"))
    async with async_session() as db:
        practices = list((await db.execute(select(Practice))).scalars())
        for practice in practices:
            if practice.offboarded_at:
                continue
            local = now.astimezone(ZoneInfo(practice.timezone))
            await recover_summary_notifications(db, practice)
            if _at(local, (practice.notifications or {}).get("digest_time", "20:00")):
                await digest(db, practice, local)
            if local.day == 1 and _at(local, "09:00"):
                await monthly_report(db, practice, local)
            rem = practice.reminders or {}
            if rem.get("enabled") and _at(local, rem.get("time", "18:00")):
                await queue_reminders(db, practice, local)
            await test_call(db, practice, local)
            await check_test_call(db, practice, now)
        retained = _last_retention != now.date()
        if retained:
            for practice in practices:
                await retention(db, practice)
        await stale(db)
        await failed_notifications(db)
        await health.check(db)
        await alerts.escalate(db)
        await db.commit()
        if retained:
            _last_retention = now.date()
        if gcal.configured() and _last_calendar_reconciliation != now.date():
            try:
                for practice in practices:
                    await reconcile_calendar_events(db, practice, now)
                await db.commit()
                _last_calendar_reconciliation = now.date()
            except Exception:
                await db.rollback()
                logger.exception("Calendar reconciliation failed; will retry")
        # Finalizers use their own sessions and must see committed terminal state.
        await recover_finalizations(db)
        from app.dispatcher import start_next_queued
        await start_next_queued(db)
    notifications.kick()
    await process_recording_deletions()
    async with async_session() as db:
        await seal_recordings(db)


async def run() -> None:
    while True:
        try:
            await tick()
        except Exception:
            logger.exception("scheduler tick")
        await asyncio.sleep(60)
