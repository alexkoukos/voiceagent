"""OP1 daily real test call: place it once a day and alert when it does not connect."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app import scheduler
from app.models import Alert, Call, CallStatus, Practice

ATH = ZoneInfo("Europe/Athens")
TEST_NUMBER = "+306900000009"
SERVICE = {"id": "check", "name": "Check-up", "duration_minutes": 30}


async def _seed(sessions, **test_call) -> Practice:
    async with sessions() as db:
        p = Practice(name="Test practice", timezone="Europe/Athens",
                     hours={"mon": [["09:00", "12:00"]]}, services=[SERVICE],
                     notifications={"test_call": {"enabled": True, "number": TEST_NUMBER,
                                                  "time": "09:00", **test_call}})
        db.add(p)
        await db.commit()
        return p


def _test_call(practice: Practice, **kw) -> Call:
    return Call(practice_id=practice.id, direction="outbound", purpose="test", use_case="outbound",
                persona="", scenario="", caller_number=TEST_NUMBER, created_at=datetime.utcnow(), **kw)


async def _count(db, model, **where) -> int:
    return (await db.execute(select(func.count()).select_from(model).filter_by(**where))).scalar_one()


@pytest.mark.asyncio
async def test_test_call_queued_once_per_day(sessions):
    p = await _seed(sessions)
    # "Today" at the configured time, so the queued call's created_at falls in the same local day.
    local = datetime.now(ATH).replace(hour=9, minute=0, second=0, microsecond=0)
    async with sessions() as db:
        practice = await db.get(Practice, p.id)
        await scheduler.test_call(db, practice, local)
        await db.commit()
        assert await _count(db, Call, purpose="test") == 1
        call = (await db.execute(select(Call).where(Call.purpose == "test"))).scalar_one()
        assert call.direction == "outbound" and call.status == CallStatus.queued
        assert call.caller_number == TEST_NUMBER
        # A later tick inside the same window does not place a second call.
        await scheduler.test_call(db, practice, local.replace(minute=6))
        await db.commit()
        assert await _count(db, Call, purpose="test") == 1


@pytest.mark.asyncio
async def test_disabled_or_off_window_places_nothing(sessions):
    p = await _seed(sessions, enabled=False)
    async with sessions() as db:
        practice = await db.get(Practice, p.id)
        await scheduler.test_call(db, practice, datetime(2026, 9, 28, 9, 0, tzinfo=ATH))
        await db.commit()
        assert await _count(db, Call, purpose="test") == 0
    p2 = await _seed(sessions)  # enabled, but well outside the 09:00 window
    async with sessions() as db:
        practice = await db.get(Practice, p2.id)
        await scheduler.test_call(db, practice, datetime(2026, 9, 28, 15, 0, tzinfo=ATH))
        await db.commit()
        assert await _count(db, Call, purpose="test") == 0


@pytest.mark.asyncio
async def test_unconnected_test_call_alerts_once(sessions):
    p = await _seed(sessions)
    now = datetime.now(timezone.utc)
    async with sessions() as db:
        practice = await db.get(Practice, p.id)
        db.add(_test_call(practice, status=CallStatus.failed, end_reason="no_answer"))
        await db.commit()
        await scheduler.check_test_call(db, practice, now)
        await db.commit()
        assert await _count(db, Alert, kind="test_call_failed") == 1
        # A second check the same day does not raise it again.
        await scheduler.check_test_call(db, practice, now)
        await db.commit()
        assert await _count(db, Alert, kind="test_call_failed") == 1


@pytest.mark.asyncio
async def test_connected_test_call_does_not_alert(sessions):
    p = await _seed(sessions)
    now = datetime.now(timezone.utc)
    async with sessions() as db:
        practice = await db.get(Practice, p.id)
        db.add(_test_call(practice, status=CallStatus.completed, started_at=datetime.utcnow()))
        await db.commit()
        await scheduler.check_test_call(db, practice, now)
        await db.commit()
        assert await _count(db, Alert, kind="test_call_failed") == 0


@pytest.mark.asyncio
async def test_still_ringing_test_call_waits_before_alerting(sessions):
    p = await _seed(sessions)
    now = datetime.now(timezone.utc)
    async with sessions() as db:
        practice = await db.get(Practice, p.id)
        db.add(_test_call(practice, status=CallStatus.dialing))  # just dispatched, no started_at yet
        await db.commit()
        await scheduler.check_test_call(db, practice, now)
        await db.commit()
        assert await _count(db, Alert, kind="test_call_failed") == 0
