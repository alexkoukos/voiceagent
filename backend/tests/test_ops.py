"""OP7 offboarding, OP8 patient data requests, OP9 alert escalation, OP10 cost cap and spam."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app import alerts, receptionist, scheduler
from app.config import get_settings
from app.models import (
    Alert, Appointment, Call, CallStatus, Customer, DataRequest, Message, Notification, Practice, TranscriptEntry,
    TranscriptRole, WaitlistEntry,
)
from app.routers import ops
from app.schemas import CostCapIn, PhoneIn

PHONE = "+306900000001"


async def _practice(db, **kw) -> Practice:
    practice = Practice(name="Test", timezone="Europe/Athens", hours={"mon": [["09:00", "14:00"]]},
                        services=[{"id": "check", "name": "Check-up", "duration_minutes": 30}],
                        phone_numbers=["+302100000000"], **kw)
    db.add(practice)
    await db.commit()
    return practice


def _call(practice, **kw) -> Call:
    return Call(practice_id=practice.id, direction="inbound", caller_number=PHONE, persona="", scenario="",
                status=CallStatus.completed, **kw)


@pytest.mark.asyncio
async def test_export_then_erase_one_caller(sessions, monkeypatch):
    deleted = []

    async def fake_queue(db, key, call_id=None):
        deleted.append(key)
    monkeypatch.setattr("app.data_requests.queue_recording_deletion", fake_queue)
    async with sessions() as db:
        practice = await _practice(db)
        customer = Customer(practice_id=practice.id, phone=PHONE, name="Ada")
        db.add(customer)
        await db.flush()
        call = _call(practice, customer_id=customer.id, summary="Ada booked", recording_url="rec/1.ogg")
        other = Call(practice_id=practice.id, direction="inbound", caller_number="+306900000002", persona="",
                     scenario="", status=CallStatus.completed, summary="someone else")
        db.add_all([call, other])
        await db.flush()
        start = datetime.now(timezone.utc) + timedelta(days=2)
        future = Appointment(practice_id=practice.id, customer_name="Ada", customer_phone=PHONE, service_id="check",
                             service_name="Check-up", starts_at=start, ends_at=start + timedelta(minutes=30),
                             customer_id=customer.id)
        db.add_all([
            TranscriptEntry(call_id=call.id, role=TranscriptRole.friend, text="Είμαι η Άντα"),
            Message(practice_id=practice.id, call_id=call.id, caller_name="Ada", callback_number=PHONE, reason="x"),
            WaitlistEntry(practice_id=practice.id, customer_name="Ada", phone=PHONE, service_id="check",
                          date_from=start.date(), date_to=start.date()),
            Notification(practice_id=practice.id, kind="booked_customer", channel="sms", recipient=PHONE, body="Ada"),
            future,
        ])
        await db.commit()

        out = await ops.export_caller(practice.id, PhoneIn(phone="+30 690 000 0001"), db)
        assert out["customer"]["name"] == "Ada"
        assert out["calls"][0]["transcript"][0]["text"] == "Είμαι η Άντα"
        assert len(out["messages"]) == 1 and len(out["waitlist"]) == 1

        with pytest.raises(HTTPException) as busy:
            await ops.erase_caller(practice.id, PhoneIn(phone=PHONE), db)
        assert busy.value.status_code == 409
        future = await db.get(Appointment, future.id)
        future.status = "cancelled"
        await db.commit()

        counts = await ops.erase_caller(practice.id, PhoneIn(phone=PHONE), db)
        assert counts["calls"] == 1 and counts["recordings"] == 1 and counts["customer"] == 1
        assert deleted == ["rec/1.ogg"]
        call, other = await db.get(Call, call.id), await db.get(Call, other.id)
        assert call.caller_number is None and call.summary is None and call.recording_url is None
        assert other.summary == "someone else"
        again = await ops.export_caller(practice.id, PhoneIn(phone=PHONE), db)
        assert again["calls"] == [] and again["customer"] is None and again["messages"] == []
        assert (await db.get(Appointment, future.id)).customer_name == "—"
        log = await ops.list_data_requests(practice.id, db)
        assert [r.kind for r in log].count("erase") == 1
        assert all(PHONE not in r.phone_hash for r in log)


@pytest.mark.asyncio
async def test_cost_cap_blocked_and_offboarded(sessions):
    async with sessions() as db:
        practice = await _practice(db, monthly_cost_cap_eur=1.0)
        db.add(_call(practice, cost_estimate=0.85))
        await db.commit()
        # 85%: alert, still answers.
        call, _ = await receptionist.start_call(db, practice, direction="inbound", caller_number="+306911111111")
        call.status = CallStatus.completed
        call.cost_estimate = 0.2
        await db.commit()
        with pytest.raises(receptionist.OverCap):
            await receptionist.start_call(db, practice, direction="inbound", caller_number="+306911111111")
        kinds = [a.subject for a in await ops.list_alerts(db=db)]
        assert any("80%" in k for k in kinds) and any("reached" in k for k in kinds)

        await ops.set_cost_cap(practice.id, CostCapIn(monthly_cost_cap_eur=None), db)
        await ops.block(practice.id, PhoneIn(phone="+306922222222"), db)
        with pytest.raises(receptionist.Blocked):
            await receptionist.start_call(db, practice, direction="inbound", caller_number="+306922222222")
        await ops.unblock(practice.id, PhoneIn(phone="+306922222222"), db)

        assert await receptionist.practice_for_number(db, "+302100000000") is not None
        out = await ops.offboard(practice.id, db)
        assert out["forwarding_off_code"] == "##002#"
        assert await receptionist.practice_for_number(db, "+302100000000") is None
        await ops.reactivate(practice.id, db)
        assert await receptionist.practice_for_number(db, "+302100000000") is not None
        csv = await ops.export_csv(practice.id, db)
        assert b"call" in csv.body


@pytest.mark.asyncio
async def test_silent_calls_get_blocked_and_alerts_escalate(sessions, monkeypatch):
    async with sessions() as db:
        practice = await _practice(db)
        calls = [_call(practice, duration_seconds=3) for _ in range(3)]
        db.add_all(calls)
        await db.commit()
        assert await receptionist.block_if_spam(db, calls[-1])
        await db.commit()
        assert PHONE in (await db.get(Practice, practice.id)).blocked_numbers

        monkeypatch.setattr(get_settings(), "backup_email", "backup@example.com")
        monkeypatch.setattr(get_settings(), "founder_email", "me@example.com")
        alert = await alerts.raise_alert(db, practice, "test", "Something", dedupe_key="t1")
        assert await alerts.raise_alert(db, practice, "test", "Something", dedupe_key="t1") is None
        assert await alerts.escalate(db) == 0
        alert.created_at = datetime.utcnow() - timedelta(minutes=31)
        await db.commit()
        acked = await alerts.raise_alert(db, practice, "test", "Seen", dedupe_key="t2")
        acked.created_at = datetime.utcnow() - timedelta(minutes=31)
        await ops.ack_alert(acked.id, db)
        assert await alerts.escalate(db) >= 1
        await db.commit()
        sent = [n.recipient for n in (await db.execute(
            Notification.__table__.select().where(Notification.kind == "alert_test"))).all()]
        assert "backup@example.com" in [r for r in sent]
        backup = (await db.execute(Notification.__table__.select().where(
            Notification.recipient == "backup@example.com"))).all()
        assert all("Seen" not in row.subject for row in backup)
