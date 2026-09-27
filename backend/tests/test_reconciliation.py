import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app import gcal, receptionist, reconciliation, scheduler
from app.models import Alert, Appointment, Notification, Practice


@pytest.mark.asyncio
async def test_calendar_drift_overlap_and_alert_deduplication(sessions, monkeypatch):
    now = datetime(2026, 9, 26, tzinfo=timezone.utc)
    start = now + timedelta(days=1, hours=8)
    async with sessions() as db:
        practice = Practice(name="Test", calendar_id="test-calendar", services=[],
                            notifications={"emails": ["owner@example.invalid"]})
        db.add(practice)
        await db.flush()
        appointment = Appointment(practice_id=practice.id, service_id="check", service_name="Check",
                                  customer_name="Test", starts_at=start, ends_at=start + timedelta(minutes=30),
                                  gcal_event_id="ours")
        db.add(appointment)
        await db.commit()
        remote = [{"id": "ours", "start": {"dateTime": (start + timedelta(hours=1)).isoformat()},
                   "end": {"dateTime": (start + timedelta(hours=1, minutes=30)).isoformat()}},
                  {"id": "external", "start": {"date": "2026-09-27"}, "end": {"date": "2026-09-28"},
                   "calendar_timezone": "Europe/Athens"}]
        monkeypatch.setattr(gcal, "calendar_events", AsyncMock(return_value=remote))
        assert await reconciliation.check(db, practice, now) == 1
        await db.commit()
        assert await reconciliation.check(db, practice, now) == 0
        await db.commit()
        alert = (await db.execute(select(Alert))).scalar_one()
        assert "changed" in alert.body and "Overlaps" in alert.body
        messages = (await db.execute(select(Notification).where(Notification.channel == "email"))).scalars().all()
        assert len(messages) == 1
        assert appointment.starts_at == start  # Human review, no silent move.
        monkeypatch.setattr(gcal, "calendar_events", AsyncMock(return_value=[]))
        monkeypatch.setattr(gcal, "get_event", AsyncMock(return_value=None))
        assert await reconciliation.check(db, practice, now) == 1


@pytest.mark.asyncio
async def test_reconciliation_every_five_minutes_and_failure_isolation(sessions, monkeypatch):
    now = datetime.now(timezone.utc)
    async with sessions() as db:
        practices = [Practice(name=name, services=[]) for name in ["Broken", "Healthy"]]
        db.add_all(practices)
        await db.commit()
    monkeypatch.setattr(scheduler, "async_session", sessions)
    monkeypatch.setattr(gcal, "configured", lambda: True)
    monkeypatch.setattr(scheduler, "_last_calendar_reconciliation", {})
    cleanup = AsyncMock()
    monkeypatch.setattr(scheduler, "reconcile_calendar_events", cleanup)

    async def check(_db, practice, _now):
        if practice.name == "Broken":
            raise RuntimeError("Provider unavailable")
    check_mock = AsyncMock(side_effect=check)
    monkeypatch.setattr(reconciliation, "check", check_mock)
    ids = [p.id for p in practices]
    await scheduler.reconcile_due(ids, now)
    assert ids[1] in scheduler._last_calendar_reconciliation
    assert ids[0] not in scheduler._last_calendar_reconciliation
    await scheduler.reconcile_due([ids[1]], now + timedelta(minutes=4))
    assert check_mock.await_count == 2
    await scheduler.reconcile_due([ids[1]], now + timedelta(minutes=5))
    assert check_mock.await_count == 3


@pytest.mark.asyncio
async def test_simultaneous_inbound_calls_respect_practice_limit(sessions):
    async with sessions() as db:
        practice = Practice(name="Test", services=[], max_concurrent_calls=1)
        db.add(practice)
        await db.commit()

    async def enter():
        async with sessions() as db:
            p = await db.get(Practice, practice.id)
            return await receptionist.start_call(db, p, direction="inbound", caller_number=None)
    results = await asyncio.gather(enter(), enter(), return_exceptions=True)
    assert sum(isinstance(result, receptionist.Busy) for result in results) == 1
    assert sum(isinstance(result, tuple) for result in results) == 1
