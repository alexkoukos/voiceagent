"""take_message is called repeatedly as details arrive: one message per call and recipient."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app import notifications, receptionist
from app.models import Call, CallStatus, Message, Notification, Practice, Staff
from app.schemas import MessageArgs


@pytest.mark.asyncio
async def test_repeat_calls_update_one_message_and_alert_once(sessions, monkeypatch):
    monkeypatch.setattr(notifications, "kick", lambda: None)
    monkeypatch.setattr(receptionist.events, "publish", lambda _key: None)
    async with sessions() as db:
        practice = Practice(name="Test", phone_numbers=["+302100000009"])
        db.add(practice)
        await db.flush()
        doctor = Staff(practice_id=practice.id, name="Δρ. Νίκος", role="doctor", aliases=["γιατρό"])
        call = Call(persona="", scenario="", status=CallStatus.active, direction="inbound",
                    practice_id=practice.id, caller_number="+306900000001")
        db.add_all([doctor, call])
        await db.commit()
        # EVAL-009: name first, then time, then reason; later turns omit earlier fields.
        for args in (MessageArgs(caller_name="Δημήτρης", for_whom="γιατρό"),
                     MessageArgs(best_time="το απόγευμα", for_whom="γιατρό", urgent=True),
                     MessageArgs(reason="Αποτελέσματα", for_whom="γιατρό", urgent=True,
                                 callback_number="690 000 0002")):
            assert (await receptionist.tool_take_message(db, call, args))["saved"]
        await receptionist.tool_take_message(db, call, MessageArgs(caller_name="Δημήτρης", reason="Άλλο"))
        messages = (await db.execute(select(Message).where(Message.call_id == call.id)
                                     .order_by(Message.created_at))).scalars().all()
        assert len(messages) == 2  # one for the doctor, one for the business
        first = messages[0]
        assert (first.staff_id, first.caller_name, first.best_time, first.reason, first.urgent) == (
            doctor.id, "Δημήτρης", "το απόγευμα", "Αποτελέσματα", True)
        assert first.callback_number == "+306900000002"  # spoken national number, stored as E.164
        alerts = (await db.execute(select(Notification).where(Notification.kind == "message"))).scalars().all()
        assert len(alerts) == 1
