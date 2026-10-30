"""Appointments under a number the caller only says (audit P0-2): name first, no details,
SMS code before any change."""

import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import receptionist
from app.config import get_settings
from app.models import Appointment, Call, Notification, Practice
from app.schemas import FindArgs, PrepareAction, VerifyCodeArgs

OTHER = "+306900000077"


async def _setup(db):
    practice = Practice(name="Ιατρείο Δοκιμής", timezone="Europe/Athens",
                        services=[{"id": "visit", "name": "Επίσκεψη", "duration_minutes": 30}])
    db.add(practice)
    await db.flush()
    start = datetime.now(timezone.utc) + timedelta(days=3)
    appt = Appointment(practice_id=practice.id, customer_name="Μαρία Παπαδάκη", customer_phone=OTHER,
                       service_id="visit", service_name="Επίσκεψη", starts_at=start, ends_at=start + timedelta(minutes=30))
    call = Call(practice_id=practice.id, persona="", scenario="", language="el", caller_number="+306900000001")
    db.add_all([appt, call])
    await db.commit()
    return practice, appt, call


@pytest.mark.asyncio
async def test_spoken_number_needs_name_then_sms_code(sessions, monkeypatch):
    settings = get_settings()
    async with sessions() as db:
        practice, appt, call = await _setup(db)

        assert (await receptionist.tool_find_appointments(db, call, FindArgs(phone=OTHER)))["error"] == "name_required"
        wrong = await receptionist.tool_find_appointments(db, call, FindArgs(phone=OTHER, customer_name="Ελένη Παπαδάκη"))
        nobody = await receptionist.tool_find_appointments(db, call, FindArgs(phone="+306900000078", customer_name="Μαρία Παπαδάκη"))
        assert wrong == nobody == receptionist.NOT_FOUND  # a wrong name looks exactly like no appointment

        monkeypatch.setattr(settings, "telnyx_api_key", "")
        unavailable = await receptionist.tool_find_appointments(db, call, FindArgs(phone=OTHER, customer_name="Μαρία Παπαδάκη"))
        assert unavailable["error"] == "verification_unavailable"

        monkeypatch.setattr(settings, "telnyx_api_key", "key")
        monkeypatch.setattr(settings, "sms_from", "+302100000000")
        sent = await receptionist.tool_find_appointments(db, call, FindArgs(phone=OTHER, customer_name="μαρια παπαδακη"))
        assert sent["verification"] == "sms_sent" and "appointments" not in sent
        assert appt.id not in str(sent) and "Επίσκεψη" not in str(sent)
        sms = (await db.execute(select(Notification).where(Notification.kind == "verify_code"))).scalar_one()
        assert sms.recipient == OTHER
        code = re.search(r"\d{6}", sms.body).group()

        # Not changeable before the code.
        early = await receptionist.tool_prepare_action(db, call, PrepareAction(action="cancel", appointment_id=appt.id))
        assert early["error"] == "call find_appointments first"
        bad = "000000" if code != "000000" else "111111"
        assert (await receptionist.tool_verify_code(db, call, VerifyCodeArgs(code=bad)))["tries_left"] == 2
        verified = await receptionist.tool_verify_code(db, call, VerifyCodeArgs(code=code))
        assert [a["appointment_id"] for a in verified["appointments"]] == [appt.id]
        assert (await receptionist.tool_verify_code(db, call, VerifyCodeArgs(code=code)))["error"] == "nothing_to_verify"
        readback = await receptionist.tool_prepare_action(db, call, PrepareAction(action="cancel", appointment_id=appt.id))
        assert readback.get("confirmation_id")


@pytest.mark.asyncio
async def test_sms_code_locks_after_three_wrong_tries(sessions, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "telnyx_api_key", "key")
    monkeypatch.setattr(settings, "sms_from", "+302100000000")
    async with sessions() as db:
        _, appt, call = await _setup(db)
        await receptionist.tool_find_appointments(db, call, FindArgs(phone=OTHER, customer_name="Μαρία Παπαδάκη"))
        sms = (await db.execute(select(Notification).where(Notification.kind == "verify_code"))).scalar_one()
        code = re.search(r"\d{6}", sms.body).group()
        bad = "000000" if code != "000000" else "111111"
        results = [await receptionist.tool_verify_code(db, call, VerifyCodeArgs(code=bad)) for _ in range(3)]
        assert results[-1]["error"] == "locked"
        assert (await receptionist.tool_verify_code(db, call, VerifyCodeArgs(code=code)))["error"] == "locked"
