import asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import booking, finalize, gcal, metrics, notifications, receptionist, scheduler, storage
from app.models import Appointment, Call, CallStatus, Notification, Practice, RecordingDeletion, WaitlistEntry

ATH = ZoneInfo("Europe/Athens")
DAY = date(2026, 9, 28)
NOW = datetime(2026, 9, 25, 9, tzinfo=ATH)
SERVICE = {"id": "check", "name": "Check-up", "duration_minutes": 30}


async def seed(sessions, **values):
    async with sessions() as db:
        p = Practice(name="Test practice", timezone="Europe/Athens",
                     hours={"mon": [["09:00", "12:00"]]}, services=[SERVICE],
                     rules={"min_notice_minutes": 0}, **values)
        db.add(p)
        await db.commit()
        return p


async def book(db, practice, **values):
    args = dict(day=DAY, start_time="09:00", service_id="check", customer_name="Test Caller",
                customer_phone=None, call_id=None, now=NOW)
    return await booking.book(db, practice, **(args | values))


@pytest.mark.asyncio
async def test_concurrent_booking_never_double_books(sessions):
    p = await seed(sessions)

    async def attempt():
        async with sessions() as db:
            return await book(db, p)

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    assert sum(isinstance(r, Appointment) for r in results) == 1
    failures = [r for r in results if isinstance(r, booking.BookingError)]
    assert len(failures) == 1 and failures[0].code == "slot_taken"
    assert failures[0].details["alternatives"][0]["free_times"] == ["09:30", "09:45"]


@pytest.mark.asyncio
async def test_booking_retry_and_cancel_retry(sessions):
    p = await seed(sessions)
    async with sessions() as db:
        call = Call(practice_id=p.id, persona="", scenario="")
        db.add(call)
        await db.commit()
        first = await book(db, p, call_id=call.id)
        retry = await book(db, p, call_id=call.id)
        assert retry.id == first.id
        await booking.cancel(db, p, appointment_id=first.id)
        retry = await booking.cancel(db, p, appointment_id=first.id)
        assert retry.status == "cancelled"
        assert (await db.execute(select(func.count()).select_from(Appointment))).scalar_one() == 1


@pytest.mark.asyncio
async def test_reschedule_excludes_only_own_google_event(sessions, monkeypatch):
    p = await seed(sessions)
    async with sessions() as db:
        appt = await book(db, p)
        p = await db.get(Practice, p.id)
        p.calendar_id = "test@example.invalid"
        appt.gcal_event_id = "own-event"
        await db.commit()
        monkeypatch.setattr(gcal, "configured", lambda: True)
        external_busy = AsyncMock(return_value=[
            (datetime(2026, 9, 28, 10, tzinfo=ATH), datetime(2026, 9, 28, 11, tzinfo=ATH)),
        ])
        monkeypatch.setattr(gcal, "busy_except", external_busy)
        move = AsyncMock()
        monkeypatch.setattr(gcal, "move_event", move)
        moved, old = await booking.reschedule(db, p, appointment_id=appt.id, day=DAY, start_time="09:15", now=NOW)
        assert old.astimezone(ATH).strftime("%H:%M") == "09:00"
        assert moved.starts_at.astimezone(ATH).strftime("%H:%M") == "09:15"
        assert external_busy.call_args.args[-1] == "own-event"
        await booking.reschedule(db, p, appointment_id=appt.id, day=DAY, start_time="09:15", now=NOW)
        assert move.await_count == 1
        with pytest.raises(booking.BookingError, match="slot_taken"):
            await booking.reschedule(db, p, appointment_id=appt.id, day=DAY, start_time="10:00", now=NOW)


@pytest.mark.asyncio
async def test_assigned_calendar_requires_credentials(sessions):
    p = await seed(sessions, calendar_id="test@example.invalid")
    async with sessions() as db:
        with pytest.raises(booking.BookingError, match="calendar_error"):
            await book(db, p)
        assert (await db.execute(select(func.count()).select_from(Appointment))).scalar_one() == 0


@pytest.mark.asyncio
async def test_cancel_does_not_silently_leave_remote_event(sessions):
    p = await seed(sessions)
    async with sessions() as db:
        appt = await book(db, p)
        appt.gcal_event_id = "remote-event"
        appt_id = appt.id
        p = await db.get(Practice, p.id)
        p.calendar_id = "test@example.invalid"
        await db.commit()
        with pytest.raises(booking.BookingError, match="calendar_error"):
            await booking.cancel(db, p, appointment_id=appt_id)
        assert (await db.get(Appointment, appt_id)).status == "booked"


@pytest.mark.asyncio
async def test_availability_skips_closed_days_and_horizon(sessions, monkeypatch):
    p = await seed(sessions)
    busy = AsyncMock(return_value=[])
    monkeypatch.setattr(booking, "busy_intervals", busy)
    async with sessions() as db:
        assert await booking.availability(db, p, date(2026, 9, 29), SERVICE, NOW) == {}
        assert await booking.availability(db, p, date(2027, 9, 27), SERVICE, NOW) == {}
        busy.assert_not_awaited()
        assert await booking.availability(db, p, DAY, SERVICE, NOW)
        busy.assert_awaited_once()


@pytest.mark.asyncio
async def test_buffer_includes_previous_day_booking(sessions):
    p = await seed(sessions)
    async with sessions() as db:
        p = await db.get(Practice, p.id)
        p.hours = {"mon": [["00:00", "02:00"]]}
        p.rules = {"buffer_minutes": 30, "min_notice_minutes": 0}
        db.add(Appointment(practice_id=p.id, customer_name="Existing", service_id="check", service_name="Check-up",
                           starts_at=datetime(2026, 9, 27, 23, tzinfo=ATH),
                           ends_at=datetime(2026, 9, 27, 23, 50, tzinfo=ATH)))
        await db.commit()
        slots = await booking.availability(db, p, DAY, SERVICE, NOW)
        assert next(iter(slots)).strftime("%H:%M") == "00:30"


@pytest.mark.asyncio
async def test_notification_duplicate_preserves_other_changes(sessions):
    p = await seed(sessions)
    async with sessions() as db:
        args = dict(practice_id=p.id, kind="test", channel="sms", recipient="test", dedupe_key="same")
        assert await notifications.queue(db, **args)
        await db.commit()
        p = await db.get(Practice, p.id)
        p.name = "Updated"
        assert await notifications.queue(db, **args) is None
        await notifications.queue(db, **(args | {"dedupe_key": "second"}))
        await db.commit()
    async with sessions() as db:
        assert (await db.get(Practice, p.id)).name == "Updated"
        assert (await db.execute(select(func.count()).select_from(Notification))).scalar_one() == 2
        with pytest.raises(IntegrityError):
            await notifications.queue(db, **(args | {"practice_id": "missing", "dedupe_key": "invalid"}))


@pytest.mark.asyncio
async def test_concurrent_business_queue_is_deduplicated(sessions):
    p = await seed(sessions, notifications={"emails": ["test@example.invalid"], "urgent_sms": ["test"]})

    async def enqueue():
        async with sessions() as db:
            await notifications.queue_business(db, p, kind="urgent", subject="Test", body="Test",
                                               urgent=True, dedupe_key="same-business-event")
            await db.commit()

    await asyncio.gather(enqueue(), enqueue())
    async with sessions() as db:
        rows = list((await db.execute(select(Notification))).scalars())
        assert sorted(n.channel for n in rows) == ["email", "none", "sms"]


@pytest.mark.asyncio
async def test_unconfigured_notification_can_be_sent_later(sessions, monkeypatch):
    p = await seed(sessions)
    async with sessions() as db:
        n = await notifications.queue(db, practice_id=p.id, kind="test", channel="email", recipient="test")
        await db.commit()
    monkeypatch.setattr(notifications, "send", AsyncMock(side_effect=notifications.NotConfigured("No SMTP")))
    assert await notifications.process_due() == 1
    async with sessions() as db:
        row = await db.get(Notification, n.id)
        assert row.status == "pending" and row.attempts == 0 and row.last_error == "No SMTP"
        row.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
        await db.commit()
    sender = AsyncMock()
    monkeypatch.setattr(notifications, "send", sender)
    assert await notifications.process_due() == 1
    async with sessions() as db:
        row = await db.get(Notification, n.id)
        assert row.status == "sent" and row.attempts == 1 and row.sent_at
    sender.assert_awaited_once()


@pytest.mark.asyncio
async def test_notification_workers_send_each_row_once_with_bounded_concurrency(sessions, monkeypatch):
    p = await seed(sessions)
    async with sessions() as db:
        for i in range(12):
            await notifications.queue(db, practice_id=p.id, kind="test", channel="sms", recipient=str(i))
        await db.commit()
    sent, active, peak = [], 0, 0

    async def send(n):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        sent.append(n.id)
        active -= 1

    monkeypatch.setattr(notifications, "send", send)
    assert await notifications.process_due() == 12
    assert len(set(sent)) == 12 and 1 < peak <= 5


@pytest.mark.asyncio
async def test_finalization_serialized_and_scheduler_recovers(sessions, monkeypatch):
    p = await seed(sessions, notifications={"emails": ["test@example.invalid"]})
    async with sessions() as db:
        done = Call(practice_id=p.id, persona="", scenario="", direction="inbound",
                    status=CallStatus.completed, outcome="booked", ended_at=datetime.utcnow())
        active = Call(practice_id=p.id, persona="", scenario="", status=CallStatus.active)
        db.add_all([done, active])
        await db.commit()
    summarize = AsyncMock(return_value="Summary")
    monkeypatch.setattr(finalize, "summarize", summarize)
    await asyncio.gather(finalize.finalize(done.id), finalize.finalize(done.id), finalize.finalize(active.id))
    summarize.assert_awaited_once()
    async with sessions() as db:
        assert (await db.get(Call, done.id)).finalized
        assert not (await db.get(Call, active.id)).finalized
        assert (await db.execute(select(func.count()).select_from(Notification))).scalar_one() == 2
        failed = Call(practice_id=p.id, persona="", scenario="", status=CallStatus.failed)
        db.add(failed)
        await db.commit()
        scheduled = []
        monkeypatch.setattr(finalize, "schedule", scheduled.append)
        await scheduler.recover_finalizations(db)
        assert scheduled == [failed.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["book", "reschedule", "cancel"])
async def test_calendar_rollback_returns_tool_error(sessions, monkeypatch, operation):
    p = await seed(sessions)
    async with sessions() as db:
        appt = await book(db, p)
        call = Call(practice_id=p.id, persona="", scenario="", appointment_id=appt.id)
        db.add(call)
        await db.commit()
        call_id = call.id

        async def failed_write(db, *args, **kwargs):
            await db.rollback()
            raise booking.BookingError("calendar_error")

        monkeypatch.setattr(booking, operation, failed_write)
        monkeypatch.setattr(receptionist, "_confirmed", AsyncMock(return_value=True))
        args = SimpleNamespace(appointment_id=appt.id, date=DAY, time="10:00", service_id="check",
                               customer_name="Caller", customer_phone=None, staff=None)
        tool = getattr(receptionist, "tool_" + operation)
        result = await tool(db, call, args)
        assert result["error"] == "calendar_error"
        assert "tool_error" in (await db.get(Call, call_id)).flags


@pytest.mark.asyncio
async def test_availability_requires_appointment_lookup(sessions, monkeypatch):
    p = await seed(sessions)
    async with sessions() as db:
        call = Call(practice_id=p.id, persona="", scenario="")
        db.add(call)
        await db.commit()
        check = AsyncMock()
        monkeypatch.setattr(booking, "check_availability", check)
        result = await receptionist.tool_check_availability(db, call, SimpleNamespace(appointment_id="unseen"))
        assert result == {"error": "call find_appointments first"}
        check.assert_not_awaited()


@pytest.mark.asyncio
async def test_notification_provider_failure_is_retried(sessions, monkeypatch):
    p = await seed(sessions)
    async with sessions() as db:
        n = await notifications.queue(db, practice_id=p.id, kind="test", channel="sms", recipient="test")
        await db.commit()
    monkeypatch.setattr(notifications, "send", AsyncMock(side_effect=RuntimeError("provider unavailable")))
    assert await notifications.process_due() == 1
    async with sessions() as db:
        row = await db.get(Notification, n.id)
        assert row.status == "pending" and row.attempts == 1
        assert row.next_attempt_at > datetime.utcnow()
        row.attempts = notifications.MAX_ATTEMPTS - 1
        row.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
        await db.commit()
    assert await notifications.process_due() == 1
    async with sessions() as db:
        row = await db.get(Notification, n.id)
        assert row.status == "failed" and row.attempts == notifications.MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_notification_metric_counts_pending_failed_and_missing(sessions):
    p = await seed(sessions, notifications={"emails": ["test@example.invalid"]})
    ended = datetime.utcnow() - timedelta(minutes=2)
    async with sessions() as db:
        calls = [Call(practice_id=p.id, persona="", scenario="", direction="inbound",
                      status=CallStatus.completed, outcome="booked", ended_at=ended) for _ in range(4)]
        db.add_all(calls)
        await db.flush()
        for call, status in zip(calls, ["sent", "pending", "failed"]):
            n = await notifications.queue(db, practice_id=p.id, kind="call_summary", channel="email",
                                          recipient="test", call_id=call.id, status=status)
            if status == "sent":
                n.sent_at = ended + timedelta(seconds=30)
        duplicate_recipient = await notifications.queue(db, practice_id=p.id, kind="call_summary", channel="email",
                                                       recipient="other", call_id=calls[0].id, status="sent")
        duplicate_recipient.sent_at = ended + timedelta(seconds=40)
        await db.commit()
        result = await metrics.compute(db, p)
        assert result["notified_within_60s_pct"] == 25.0


@pytest.mark.asyncio
async def test_repeated_cancellation_offers_waitlist_slot_once(sessions):
    p = await seed(sessions, reminders={"waitlist": True})
    async with sessions() as db:
        appt = await book(db, p)
        await book(db, p, start_time="09:30", customer_phone="test-0")
        for i in range(2):
            db.add(WaitlistEntry(practice_id=p.id, customer_name="Test", phone=f"test-{i}",
                                 service_id="check", date_from=DAY, date_to=DAY))
        await db.commit()
        assert await receptionist.offer_freed_slot(db, p, appt)
        await db.commit()
        assert await receptionist.offer_freed_slot(db, p, appt) is None
        await db.commit()
        assert (await db.execute(select(func.count()).select_from(Call))).scalar_one() == 1


@pytest.mark.asyncio
async def test_waitlist_does_not_call_number_without_existing_appointment(sessions):
    p = await seed(sessions, reminders={"waitlist": True})
    async with sessions() as db:
        appt = await book(db, p)
        db.add(WaitlistEntry(practice_id=p.id, customer_name="New prospect", phone="+306900000001",
                             service_id="check", date_from=DAY, date_to=DAY))
        await db.commit()
        assert await receptionist.offer_freed_slot(db, p, appt) is None


@pytest.mark.asyncio
async def test_recording_deletion_survives_provider_failure_and_restart(sessions, monkeypatch):
    p = await seed(sessions)
    monkeypatch.setattr(storage, "async_session", sessions)
    deleted = []

    def failing_delete(key):
        raise OSError("bucket unavailable")

    monkeypatch.setattr(storage, "delete_recording", failing_delete)
    async with sessions() as db:
        call = Call(practice_id=p.id, persona="", scenario="", status=CallStatus.completed,
                    ended_at=datetime.utcnow() - timedelta(minutes=20))
        db.add(call)
        await db.flush()
        await storage.queue_recording_deletion(db, "recordings/test.mp4", call.id)
        await db.commit()
    assert await storage.process_recording_deletions() == 0
    async with sessions() as db:
        item = await db.get(RecordingDeletion, "recordings/test.mp4")
        assert item.attempts == 1 and item.last_error
        item.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
        await db.commit()

    monkeypatch.setattr(storage, "delete_recording", deleted.append)
    monkeypatch.setattr(storage, "recording_exists", lambda key: False)
    assert await storage.process_recording_deletions() == 1
    assert deleted == ["recordings/test.mp4"]
    async with sessions() as db:
        assert await db.get(RecordingDeletion, "recordings/test.mp4") is None


@pytest.mark.asyncio
async def test_summary_email_recovered_after_recipient_is_configured(sessions):
    p = await seed(sessions, notifications={"emails": []})
    async with sessions() as db:
        call = Call(practice_id=p.id, persona="", scenario="", direction="inbound",
                    status=CallStatus.completed, outcome="abandoned", finalized=True,
                    summary=finalize.fallback_summary(SimpleNamespace(outcome="abandoned"), "el"),
                    ended_at=datetime.utcnow())
        db.add(call)
        await db.commit()
        p.notifications = {"emails": ["owner@example.invalid"]}
        await scheduler.recover_summary_notifications(db, p)
        await db.commit()
        rows = (await db.execute(select(Notification).where(Notification.call_id == call.id,
                                                         Notification.channel == "email"))).scalars().all()
        assert len(rows) == 1 and rows[0].status == "pending"
        await scheduler.recover_summary_notifications(db, p)
        await db.commit()
        assert (await db.execute(select(func.count()).select_from(Notification).where(
            Notification.call_id == call.id, Notification.channel == "email"))).scalar_one() == 1


@pytest.mark.asyncio
async def test_calendar_reconciliation_reattaches_or_deletes_old_events(sessions, monkeypatch):
    p = await seed(sessions)
    async with sessions() as db:
        appt = await book(db, p)
        p.calendar_id = "calendar@example.invalid"
        await db.commit()
        created = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()

        def remote(event_id, key):
            return {"id": event_id, "created": created,
                    "start": {"dateTime": appt.starts_at.isoformat()},
                    "end": {"dateTime": appt.ends_at.isoformat()},
                    "extendedProperties": {"private": {"voiceagent_key": key}}}

        events = [remote("managed", f"{p.id}:{appt.id}"), remote("orphan", f"{p.id}:lost")]
        monkeypatch.setattr(gcal, "owned_events", AsyncMock(return_value=events))
        deleted = AsyncMock()
        monkeypatch.setattr(gcal, "delete_event", deleted)
        await scheduler.reconcile_calendar_events(db, p, datetime.now(timezone.utc))
        await db.commit()
        assert appt.gcal_event_id == "managed"
        deleted.assert_awaited_once_with("calendar@example.invalid", "orphan")


@pytest.mark.asyncio
async def test_earlier_later_and_next_day_follow_the_last_offer(sessions, monkeypatch):
    p = await seed(sessions)
    async with sessions() as db:
        p = await db.get(Practice, p.id)
        p.hours = {"mon": [["09:00", "12:00"]], "tue": [["09:00", "12:00"]]}
        await db.commit()
    monkeypatch.setattr(receptionist, "utcnow", lambda: NOW)
    monkeypatch.setattr(booking, "busy_intervals", AsyncMock(return_value=[]))

    def ask(when, **extra):
        return SimpleNamespace(when=when, service_id="check", staff=None, appointment_id=None,
                               after=extra.get("after"), before=extra.get("before"))

    async with sessions() as db:
        call = Call(practice_id=p.id, persona="", scenario="")
        db.add(call)
        await db.commit()
        first = await receptionist.tool_check_availability(db, call, ask("τη Δευτέρα"))
        assert first["date"] == "2026-09-28" and first["free_times"][:3] == ["09:00", "09:15", "09:30"]
        # "Later" without a time: after the third time offered, same day.
        later = await receptionist.tool_check_availability(db, call, ask("αργότερα"))
        assert later["date"] == "2026-09-28" and later["free_times"][0] == "09:45"
        # The model passes the latest time it offered.
        later = await receptionist.tool_check_availability(db, call, ask("πιο αργά", after="10:30"))
        assert later["free_times"][0] == "10:45"
        earlier = await receptionist.tool_check_availability(db, call, ask("νωρίτερα", before="10:00"))
        assert earlier["free_times"][-1] == "09:45"
        nxt = await receptionist.tool_check_availability(db, call, ask("την επόμενη μέρα"))
        assert nxt["date"] == "2026-09-29" and nxt["free_times"][0] == "09:00"


@pytest.mark.asyncio
async def test_stuck_dialing_call_stops_holding_a_line(sessions):
    p = await seed(sessions)
    async with sessions() as db:
        old = datetime.utcnow() - timedelta(seconds=receptionist.DIALING_STALE_SECONDS + 5)
        db.add(Call(practice_id=p.id, persona="", scenario="", status=CallStatus.dialing, created_at=old))
        db.add(Call(practice_id=p.id, persona="", scenario="", status=CallStatus.active, created_at=old))
        await db.commit()
        assert await receptionist.active_calls(db, p.id) == 1
        from app.dispatcher import active_count
        assert await active_count(db) == 1


@pytest.mark.asyncio
async def test_off_topic_three_strikes(sessions):
    from app import routing
    p = await seed(sessions, language="el")
    async with sessions() as db:
        call = Call(practice_id=p.id, persona="", scenario="")
        db.add(call)
        await db.commit()
        strikes = []
        for _ in range(3):
            strikes.append(await routing.route(db, p, call, intent="off_topic", now=NOW))
            await db.commit()
        assert [s["path"] for s in strikes] == ["refuse", "refuse", "end_call"]
        assert strikes[0]["say"] == routing.STRIKE_LINES["el"][0]
        assert "θα κλείσω την κλήση" in strikes[1]["say"]
        assert strikes[2]["say"] == routing.END_LINE["el"]
