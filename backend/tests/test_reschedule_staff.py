"""A move keeps the appointment's staff member, so availability must be theirs only."""

from datetime import date, datetime, timedelta, timezone

import pytest

from app import booking, receptionist
from app.models import Call, CallStatus, Practice, Staff
from app.schemas import CheckAvailability, FindArgs, PrepareAction

HOURS = {d: [["09:00", "14:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}


@pytest.mark.asyncio
async def test_reschedule_offers_only_the_appointments_own_staff_times(sessions):
    # EVAL-004: "anyone free" offered a time only the other dentist had; the move then
    # failed with slot_taken after the caller had already said yes.
    async with sessions() as db:
        practice = Practice(name="Test", timezone="Europe/Athens", hours=HOURS,
                            services=[{"id": "checkup", "name": "Έλεγχος", "duration_minutes": 30}],
                            rules={"min_notice_minutes": 0})
        db.add(practice)
        await db.flush()
        monday_only = Staff(practice_id=practice.id, name="Δρ. Άλφα", role="doctor", hours={"mon": [["09:00", "14:00"]]})
        everyday = Staff(practice_id=practice.id, name="Δρ. Βήτα", role="doctor")
        db.add_all([monday_only, everyday])
        await db.commit()
        monday = date.today() + timedelta(days=7 - date.today().weekday())
        appt = await booking.book(db, practice, day=monday, start_time="10:00", service_id="checkup",
                                  customer_name="Πελάτης", customer_phone="+306900000001", call_id=None,
                                  now=datetime.now(timezone.utc), staff_name="Άλφα", source="app")
        assert appt.staff_id == monday_only.id
        call = Call(persona="", scenario="", status=CallStatus.active, direction="inbound",
                    practice_id=practice.id, caller_number="+306900000001")
        db.add(call)
        await db.commit()
        await receptionist.tool_find_appointments(db, call, FindArgs())
        result = await receptionist.tool_check_availability(db, call, CheckAvailability(
            when="Τρίτη", service_id="checkup", appointment_id=appt.id))
        offered = [result, *(result.get("next_days_with_free_times") or [])]
        for day in offered:
            if day.get("free_times"):
                assert date.fromisoformat(day["date"]).weekday() == 0, result  # Δρ. Άλφα works Mondays only

        monday_offer = next(d for d in offered if d.get("free_times"))
        # The model may or may not name the appointment's own dentist in prepare_action.
        for staff in (None, "Δρ. Άλφα", "Άλφα"):
            prepared = await receptionist.tool_prepare_action(db, call, PrepareAction(
                action="reschedule", date=monday_offer["date"], time=monday_offer["free_times"][0],
                service_id="checkup", appointment_id=appt.id, staff=staff))
            assert prepared.get("confirmation_id"), (staff, prepared)
