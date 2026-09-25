"""A noisy call cannot write an appointment without an offered slot and a fresh yes."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import receptionist
from app.models import Appointment, Call, Practice
from app.schemas import BookAppointment, CheckAvailability, PrepareAction


@pytest.mark.asyncio
async def test_booking_requires_trusted_offer_and_explicit_yes(sessions):
    async with sessions() as db:
        practice = Practice(name="Test", timezone="Europe/Athens",
                            hours={"mon": [["09:00", "12:00"]]},
                            services=[{"id": "check", "name": "Check-up", "duration_minutes": 30}],
                            rules={"min_notice_minutes": 0})
        db.add(practice)
        await db.flush()
        call = Call(practice_id=practice.id, persona="", scenario="", language="en")
        db.add(call)
        await db.commit()

        today = datetime.now(ZoneInfo("Europe/Athens")).date()
        day = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        raw = BookAppointment(date=day, time="09:00", service_id="check", customer_name="Ada")
        assert (await receptionist.tool_book(db, call, raw))["error"] == "confirmation_required"
        missing = await receptionist.tool_prepare_action(db, call, PrepareAction(
            action="book", date=day, time="09:00", service_id="check", customer_name="Ada"))
        assert missing["error"] == "check_availability_first"

        offer = await receptionist.tool_check_availability(db, call, CheckAvailability(
            when=day.isoformat(), service_id="check"))
        assert "09:00" in offer["free_times"]
        assert (await receptionist.tool_prepare_action(db, call, PrepareAction(
            action="book", date=day, time="12:00", service_id="check", customer_name="Ada")))["error"] == "check_availability_first"
        readback = await receptionist.tool_prepare_action(db, call, PrepareAction(
            action="book", date=day, time="09:00", service_id="check", customer_name="Ada"))
        assert "Ada" in readback["say"] and "09:00" in readback["say"]
        for answer in ("", "yes, but wait", "no", "maybe"):
            args = raw.model_copy(update={"confirmation_id": readback["confirmation_id"],
                                          "confirmation_text": answer})
            assert (await receptionist.tool_book(db, call, args))["error"] == "confirmation_required"
        wrong_slot = raw.model_copy(update={"time": "09:30", "confirmation_id": readback["confirmation_id"],
                                            "confirmation_text": "yes"})
        assert (await receptionist.tool_book(db, call, wrong_slot))["error"] == "confirmation_required"
        confirmed = raw.model_copy(update={"confirmation_id": readback["confirmation_id"],
                                           "confirmation_text": "yes, correct"})
        assert (await receptionist.tool_book(db, call, confirmed))["booked"] is True
        assert await db.get(Appointment, call.appointment_id)
