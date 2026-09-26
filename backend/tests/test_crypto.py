"""Patient data is unreadable in the database but normal to the app; lookups by phone work."""

import base64
import os

import pytest
from sqlalchemy import select, text

from app import crypto
from app.config import get_settings
from app.models import Call, CallStatus, Customer, Practice, TranscriptEntry, TranscriptRole


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(get_settings(), "data_encryption_key", base64.b64encode(os.urandom(32)).decode())
    crypto._keys.cache_clear()
    yield
    crypto._keys.cache_clear()


def test_round_trip_and_deterministic_lookup(key):
    a, b = crypto.encrypt("Άντα"), crypto.encrypt("Άντα")
    assert a != b and a.startswith("enc1:") and crypto.decrypt(a) == "Άντα"
    p1, p2 = crypto.encrypt("+306900000001", lookup=True), crypto.encrypt("+306900000001", lookup=True)
    assert p1 == p2 and crypto.decrypt(p1) == "+306900000001"
    assert crypto.decrypt("plain old row") == "plain old row"


@pytest.mark.asyncio
async def test_rows_encrypted_at_rest(sessions, key):
    async with sessions() as db:
        practice = Practice(name="Test", timezone="Europe/Athens", hours={},
                            services=[{"id": "c", "name": "C", "duration_minutes": 30}])
        db.add(practice)
        await db.flush()
        db.add(Customer(practice_id=practice.id, phone="+306900000001", name="Άντα"))
        call = Call(practice_id=practice.id, direction="inbound", caller_number="+306900000001", persona="",
                    scenario="", status=CallStatus.completed, summary="Πονάει το δόντι της")
        db.add(call)
        await db.flush()
        db.add(TranscriptEntry(call_id=call.id, role=TranscriptRole.friend, text="με πονάει το δόντι"))
        await db.commit()

        raw = (await db.execute(text("select caller_number, summary from calls"))).one()
        assert raw.caller_number.startswith("enc1d:") and "δόντι" not in raw.summary
        raw_t = (await db.execute(text("select text from transcript_entries"))).scalar_one()
        assert raw_t.startswith("enc1:")

        found = (await db.execute(select(Customer).where(Customer.phone == "+306900000001"))).scalar_one()
        assert found.name == "Άντα"
        again = (await db.execute(select(Call).where(Call.caller_number == "+306900000001"))).scalar_one()
        assert again.summary == "Πονάει το δόντι της"
