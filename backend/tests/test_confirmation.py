"""A noisy call cannot write an appointment without an offered slot and a fresh yes."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import receptionist
from app.models import Appointment, Call, Practice, TranscriptEntry, TranscriptRole
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
        call = Call(practice_id=practice.id, language="en")
        db.add(call)
        await db.commit()

        today = datetime.now(ZoneInfo("Europe/Athens")).date()
        day = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        raw = BookAppointment(date=day, time="09:00", service_id="check", customer_name="Ada",
                              customer_phone="+306912345678")
        assert (await receptionist.tool_book(db, call, raw))["error"] == "confirmation_required"
        missing = await receptionist.tool_prepare_action(db, call, PrepareAction(
            action="book", date=day, time="09:00", service_id="check", customer_name="Ada"))
        assert missing["error"] == "check_availability_first"

        offer = await receptionist.tool_check_availability(db, call, CheckAvailability(
            when=day.isoformat(), service_id="check"))
        assert "09:00" in offer["free_times"]
        assert (await receptionist.tool_prepare_action(db, call, PrepareAction(
            action="book", date=day, time="12:00", service_id="check", customer_name="Ada")))["error"] == "check_availability_first"
        # No calling number (web demo): the readback needs a contact phone first.
        assert (await receptionist.tool_prepare_action(db, call, PrepareAction(
            action="book", date=day, time="09:00", service_id="check", customer_name="Ada")))["error"] == "phone_required"
        readback = await receptionist.tool_prepare_action(db, call, PrepareAction(
            action="book", date=day, time="09:00", service_id="check", customer_name="Ada",
            customer_phone="+30 691 234 5678"))
        assert "Ada" in readback["say"] and "at 9 a.m." in readback["say"]
        assert "phone 6 9 1 2 3 4 5 6 7 8" in readback["say"]
        other_phone = raw.model_copy(update={"customer_phone": "+306900000000",
                                             "confirmation_id": readback["confirmation_id"],
                                             "confirmation_text": "yes"})
        assert (await receptionist.tool_book(db, call, other_phone))["error"] == "confirmation_required"
        for answer in ("", "yes, but wait", "no", "maybe"):
            args = raw.model_copy(update={"confirmation_id": readback["confirmation_id"],
                                          "confirmation_text": answer})
            assert (await receptionist.tool_book(db, call, args))["error"] == "confirmation_required"
        wrong_slot = raw.model_copy(update={"time": "09:30", "confirmation_id": readback["confirmation_id"],
                                            "confirmation_text": "yes"})
        assert (await receptionist.tool_book(db, call, wrong_slot))["error"] == "confirmation_required"
        confirmed = raw.model_copy(update={"confirmation_id": readback["confirmation_id"],
                                           "confirmation_text": "yes, correct"})
        # A yes the caller never said (nothing stored after the readback) is refused.
        assert (await receptionist.tool_book(db, call, confirmed))["error"] == "confirmation_required"
        db.add(TranscriptEntry(call_id=call.id, role=TranscriptRole.friend, text="yes, correct"))
        await db.commit()
        assert (await receptionist.tool_book(db, call, confirmed))["booked"] is True
        assert await db.get(Appointment, call.appointment_id)


@pytest.mark.asyncio
async def test_yes_must_be_the_callers_latest_line(sessions):
    """The worker's yes is refused when the stored transcript says something else after it."""
    async with sessions() as db:
        practice = Practice(name="Test", timezone="Europe/Athens",
                            hours={d: [["09:00", "12:00"]] for d in ("mon", "tue", "wed", "thu", "fri")},
                            services=[{"id": "check", "name": "Check-up", "duration_minutes": 30}],
                            rules={"min_notice_minutes": 0})
        db.add(practice)
        await db.flush()
        call = Call(practice_id=practice.id, language="en", caller_number="+306900000001")
        db.add(call)
        await db.commit()
        day = datetime.now(ZoneInfo("Europe/Athens")).date() + timedelta(days=1)
        while day.weekday() > 4:
            day += timedelta(days=1)
        offer = await receptionist.tool_check_availability(db, call, CheckAvailability(
            when=day.isoformat(), service_id="check"))
        slot = offer["free_times"][0]
        db.add(TranscriptEntry(call_id=call.id, role=TranscriptRole.friend, text="yes"))  # before the readback
        await db.commit()
        readback = await receptionist.tool_prepare_action(db, call, PrepareAction(
            action="book", date=day, time=slot, service_id="check", customer_name="Ada"))
        args = BookAppointment(date=day, time=slot, service_id="check", customer_name="Ada",
                               confirmation_id=readback["confirmation_id"], confirmation_text="yes")
        assert (await receptionist.tool_book(db, call, args))["error"] == "confirmation_required"
        db.add(TranscriptEntry(call_id=call.id, role=TranscriptRole.friend, text="yes"))
        db.add(TranscriptEntry(call_id=call.id, role=TranscriptRole.friend, text="hmm, no, wait",
                               created_at=datetime.utcnow() + timedelta(seconds=1)))
        await db.commit()
        assert (await receptionist.tool_book(db, call, args))["error"] == "confirmation_required"
        db.add(TranscriptEntry(call_id=call.id, role=TranscriptRole.friend, text="yes",
                               created_at=datetime.utcnow() + timedelta(seconds=2)))
        await db.commit()
        assert (await receptionist.tool_book(db, call, args))["booked"] is True
