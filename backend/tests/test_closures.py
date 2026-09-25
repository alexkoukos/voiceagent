"""OP3: dated closures and staff leave stop offers, and appointments inside them are listed for rebooking."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app import booking
from app.models import Appointment, Practice, Staff
from app.routers import practices as practices_router
from app.schemas import ClosureIn

ATH = ZoneInfo("Europe/Athens")
WEEK = {k: [["09:00", "12:00"]] for k in ("mon", "tue", "wed", "thu", "fri")}


def _practice(closures):
    return SimpleNamespace(timezone="Europe/Athens", hours=WEEK,
                           rules={"min_notice_minutes": 0, "closures": closures})


def test_closure_blocks_business_or_one_person():
    now = datetime(2026, 8, 1, 8, tzinfo=ATH)
    day = date(2026, 8, 12)  # Wednesday
    whole = _practice([{"id": "c", "from": "2026-08-10", "to": "2026-08-25"}])
    assert booking.free_slots(whole, day, 30, [], now) == []
    assert booking.free_slots(whole, day, 30, [], now, staff_id="g") == []
    assert booking.free_slots(whole, date(2026, 8, 26), 30, [], now)

    leave = _practice([{"id": "c", "from": "2026-08-12", "to": "2026-08-12", "staff_id": "g"}])
    assert booking.free_slots(leave, day, 30, [], now, staff_id="g") == []
    assert booking.free_slots(leave, day, 30, [], now, staff_id="m")
    assert booking.free_slots(leave, day, 30, [], now)


def test_hours_state_skips_a_long_closure():
    p = _practice([{"id": "c", "from": "2026-08-10", "to": "2026-08-25"}])
    p.rules["max_days_ahead"] = 60
    state, next_open = booking.hours_state(p, datetime(2026, 8, 11, 10, tzinfo=ATH))
    assert state == "closed"
    assert next_open == datetime(2026, 8, 26, 9, tzinfo=ATH)


@pytest.mark.asyncio
async def test_agent_hears_why_and_closure_lists_appointments(sessions):
    today = datetime.now(ATH).date()
    monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    async with sessions() as db:
        practice = Practice(name="Test", timezone="Europe/Athens", hours=WEEK,
                            services=[{"id": "check", "name": "Check-up", "duration_minutes": 30}],
                            rules={"min_notice_minutes": 0})
        db.add(practice)
        await db.flush()
        giorgos = Staff(practice_id=practice.id, name="Γιώργος")
        maria = Staff(practice_id=practice.id, name="Μαρία")
        db.add_all([giorgos, maria])
        await db.flush()
        start = datetime.combine(monday, datetime.min.time(), ATH).replace(hour=10)
        db.add_all([
            Appointment(practice_id=practice.id, customer_name="Ada", customer_phone="+306900000001",
                        service_id="check", service_name="Check-up", starts_at=start,
                        ends_at=start + timedelta(minutes=30), staff_id=giorgos.id),
            Appointment(practice_id=practice.id, customer_name="Bo", service_id="check", service_name="Check-up",
                        starts_at=start, ends_at=start + timedelta(minutes=30), staff_id=maria.id),
        ])
        await db.commit()

        out = await practices_router.add_closure(practice.id, ClosureIn(
            date_from=monday, date_to=monday + timedelta(days=1), staff_id=giorgos.id, reason="άδεια"), db)
        assert [a.customer_name for a in out.to_rebook] == ["Ada"]

        practice = await db.get(Practice, practice.id)
        now = datetime.now(ATH)
        away = await booking.check_availability(db, practice, monday.isoformat(), "check", now,
                                                staff_name="Γιώργο", language="el")
        assert away["free_times"] == []
        assert away["staff_away"]["reason"] == "άδεια"
        assert away["next_days_with_free_times"][0]["date"] == (monday + timedelta(days=2)).isoformat()
        anyone = await booking.check_availability(db, practice, monday.isoformat(), "check", now, language="el")
        assert anyone["free_times"] and "staff_away" not in anyone

        await practices_router.add_closure(practice.id, ClosureIn(date_from=monday, date_to=monday), db)
        practice = await db.get(Practice, practice.id)
        closed = await booking.check_availability(db, practice, monday.isoformat(), "check", now, language="el")
        assert closed["free_times"] == [] and "business_closed" in closed

        listed = await practices_router.list_closures(practice.id, db)
        assert len(listed) == 2
        await practices_router.delete_closure(practice.id, listed[0].id, db)
        assert len(await practices_router.list_closures(practice.id, db)) == 1
