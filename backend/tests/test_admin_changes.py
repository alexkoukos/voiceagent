"""OP2 by phone: only registered staff, readback then the caller's yes, PIN with lockout."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app import admin_changes, receptionist
from app.models import AdminRequest, Call, CallStatus, ConfigVersion, Practice, Staff, TranscriptEntry, TranscriptRole
from app.routers import ops
from app.schemas import AdminChangeArgs, AdminConfirmArgs, AdminLoginArgs, AdminPinIn
from sqlalchemy import select

OWNER, NURSE = "+306911111111", "+306922222222"


async def _setup(db):
    practice = Practice(name="Test", timezone="Europe/Athens", hours={"mon": [["09:00", "14:00"]]},
                        services=[{"id": "clean", "name": "Καθαρισμός", "duration_minutes": 30, "price": "50€"}],
                        phone_numbers=["+302100000000"])
    db.add(practice)
    await db.flush()
    owner = Staff(practice_id=practice.id, name="Μαρία", role="owner", phone=OWNER)
    nurse = Staff(practice_id=practice.id, name="Γιώργος", role="staff", phone=NURSE)
    db.add_all([owner, nurse])
    await db.commit()
    return practice, owner, nurse


@pytest.mark.asyncio
async def test_change_rules_staff_only_own_leave_and_prices_to_approval(sessions, monkeypatch):
    later = (datetime.now(timezone.utc) + timedelta(days=10)).date()
    parse = AsyncMock()
    monkeypatch.setattr(admin_changes, "parse", parse)
    now = datetime.now(timezone.utc)

    async def ask(practice, who, text):
        try:
            req = await admin_changes.request(db, practice, who, text, channel="phone", now=now)
        except admin_changes.Rejected as e:
            return e.reply, None
        return admin_changes.confirm_prompt(practice, req.readback), req

    async with sessions() as db:
        practice, owner, nurse = await _setup(db)
        parse.return_value = {"action": "closure", "date_from": later.isoformat(), "date_to": later.isoformat()}
        reply, req = await ask(practice, owner, "Κλειστά ...")
        assert "Σωστά;" in reply and "Όλη η επιχείρηση" in reply
        assert not (await db.get(Practice, practice.id)).rules.get("closures")
        assert "Έγινε" in await admin_changes.decide(db, practice, req, True)
        assert len((await db.get(Practice, practice.id)).rules["closures"]) == 1

        # Staff: "I'm off" means them; other people's leave and prices are refused.
        _, req = await ask(practice, nurse, "λείπω")
        assert req.parsed["staff_id"] == nurse.id
        parse.return_value = {"action": "closure", "date_from": later.isoformat(), "staff_name": "Μαρία"}
        assert "μόνο τη δική σας" in (await ask(practice, nurse, "η Μαρία λείπει"))[0]
        parse.return_value = {"action": "price", "service_name": "καθαρισμός", "price": "55€"}
        assert "μόνο τη δική σας" in (await ask(practice, nurse, "55€"))[0]

        # Owner: price goes to approval, not live.
        _, req = await ask(practice, owner, "ο καθαρισμός 55€")
        assert "έλεγχο" in await admin_changes.decide(db, practice, req, True)
        assert (await db.get(Practice, practice.id)).services[0]["price"] == "50€"
        pending = (await db.execute(select(ConfigVersion).where(ConfigVersion.status == "pending"))).scalars().all()
        assert pending[0].changes["services"][0]["price"] == "55€" and pending[0].source == "phone"

        parse.return_value = {"action": "closure", "date_from": "2020-01-01", "date_to": "2020-01-02"}
        assert "Δεν κατάλαβα" in (await ask(practice, owner, "παλιό"))[0]


@pytest.mark.asyncio
async def test_phone_pin_lockout_and_change(sessions, monkeypatch):
    later = (datetime.now(timezone.utc) + timedelta(days=5)).date()
    monkeypatch.setattr(admin_changes, "parse", AsyncMock(return_value={
        "action": "hours", "days": {"tue": [["10:00", "13:00"]]}}))
    async with sessions() as db:
        practice, owner, _ = await _setup(db)

        def new_call(number):
            c = Call(practice_id=practice.id, direction="inbound", caller_number=number, status=CallStatus.active)
            db.add(c)
            return c

        stranger = new_call("+306955555555")
        await db.commit()
        assert (await receptionist.tool_admin_login(db, stranger, AdminLoginArgs(pin="1234")))["error"] == "not_available"

        await ops.set_admin_pin(practice.id, AdminPinIn(pin="4821"), db)
        call = new_call(OWNER)
        await db.commit()
        db.add(TranscriptEntry(call_id=call.id, role=TranscriptRole.friend, text="ο κωδικός είναι 48 21"))
        await db.commit()
        assert (await receptionist.tool_admin_change(db, call, AdminChangeArgs(request="Τρίτη 10-1")))["error"] == "login_first"
        for _ in range(3):
            out = await receptionist.tool_admin_login(db, call, AdminLoginArgs(pin="0000"))
        assert out["tries_left"] == 0
        assert (await receptionist.tool_admin_login(db, call, AdminLoginArgs(pin="4821")))["error"] == "locked"
        text = (await db.execute(select(TranscriptEntry.text).where(TranscriptEntry.call_id == call.id))).scalar_one()
        assert "48" not in text and "••••" in text

        call2 = new_call(OWNER)
        await db.commit()
        assert (await receptionist.tool_admin_login(db, call2, AdminLoginArgs(pin="4 8 2 1")))["ok"]
        out = await receptionist.tool_admin_change(db, call2, AdminChangeArgs(request="Τρίτη 10 με 1"))
        assert "Τρίτη 10:00-13:00" in out["say_and_ask"]
        # The model's word is not enough: the caller's own reply after the readback decides.
        assert (await receptionist.tool_admin_confirm(db, call2, AdminConfirmArgs(confirmation_text="ναι")))["error"] == "confirmation_required"
        db.add(TranscriptEntry(call_id=call2.id, role=TranscriptRole.friend, text="Ναι, σωστά, αλλά και τη Δευτέρα"))
        await db.commit()
        unclear = AdminConfirmArgs(confirmation_text="Ναι, σωστά, αλλά και τη Δευτέρα")
        assert (await receptionist.tool_admin_confirm(db, call2, unclear))["error"] == "confirmation_required"
        db.add(TranscriptEntry(call_id=call2.id, role=TranscriptRole.friend, text="Ναι, σωστά.",
                               created_at=datetime.utcnow() + timedelta(seconds=1)))
        await db.commit()
        yes = AdminConfirmArgs(confirmation_text="Ναι, σωστά.")
        assert (await receptionist.tool_admin_confirm(db, call2, yes))["say"].startswith("Έγινε")
        practice = await db.get(Practice, practice.id)
        assert practice.hours["tue"] == [["10:00", "13:00"]] and practice.hours["mon"] == [["09:00", "14:00"]]
        assert (await receptionist.tool_admin_confirm(db, call2, yes))["error"] == "nothing_pending"

        meta = await receptionist.build_metadata(db, practice, call2)
        assert "admin_login" in meta["prompt"]
        assert "admin_login" not in (await receptionist.build_metadata(db, practice, stranger))["prompt"]


@pytest.mark.asyncio
async def test_wrong_pins_lock_the_practice_across_calls(sessions):
    async with sessions() as db:
        practice, _, _ = await _setup(db)
        await ops.set_admin_pin(practice.id, AdminPinIn(pin="4821"), db)
        calls = []
        for _ in range(admin_changes.PIN_PRACTICE_FAILS // admin_changes.PIN_TRIES):
            call = Call(practice_id=practice.id, direction="inbound", caller_number=OWNER, status=CallStatus.active)
            db.add(call)
            await db.commit()
            assert (await receptionist.tool_admin_start(db, call))["ok"]
            for _ in range(admin_changes.PIN_TRIES):
                await receptionist.tool_admin_login(db, call, AdminLoginArgs(pin="0000"))
            calls.append(call)
        redial = Call(practice_id=practice.id, direction="inbound", caller_number=OWNER, status=CallStatus.active)
        db.add(redial)
        await db.commit()
        assert (await receptionist.tool_admin_start(db, redial))["error"] == "locked"
        assert (await receptionist.tool_admin_login(db, redial, AdminLoginArgs(pin="4821")))["error"] == "locked"
