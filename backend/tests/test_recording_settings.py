"""G7 recording per practice (notice in the greeting, recording off) and the O5 check that each
number is on a LiveKit inbound trunk."""

import asyncio
from types import SimpleNamespace

import pytest

from app import onboarding, receptionist
from app.models import Call, CallStatus, Practice
from app.onboarding import _trunk_item, checklist, trunk_numbers
from app.prompts import default_greeting, with_recording_notice
from app.routers import practices as practices_router
from app.schemas import PracticeIn, PracticeOut, RecordingSettings

HOURS = {"mon": [["09:00", "14:00"]]}
SERVICES = [{"id": "check", "name": "Έλεγχος", "duration_minutes": 30}]
NUMBER = "+302100000009"


def _practice(**kw):
    base = dict(name="Οδοντιατρείο", greeting="", hours=HOURS, services=SERVICES, knowledge_base={},
                calendar_id=None, phone_numbers=[NUMBER], notifications={}, onboarding={},
                recording_enabled=True, recording_notice=False)
    return SimpleNamespace(**(base | kw))


def _items(p, **kw):
    return {i["id"]: i for i in checklist(p, [], set(), 0, settings=SimpleNamespace(
        google_service_account_json="", google_oauth_client_id="", smtp_host="", email_from="", telnyx_api_key="",
        sms_from="", founder_email="", founder_sms="", data_encryption_key=""), **kw)}


# --- greeting ---


def test_greeting_with_notice():
    p = _practice()
    el = default_greeting(p, language="el", recording_notice=True)
    assert el == ("Οδοντιατρείο. Είμαι ο ψηφιακός βοηθός. Η κλήση ηχογραφείται· αν θέλετε να σβηστεί, πείτε μου. "
                  "Πώς μπορώ να σας βοηθήσω; For English, say English.")
    en = default_greeting(p, language="en", recording_notice=True)
    assert en == ("I'm a digital assistant. This call is recorded; tell me if you'd like the recording deleted. "
                  "How can I help you?")
    # Off: unchanged.
    assert "ηχογραφ" not in default_greeting(p, language="el")
    # Custom greeting: before the question, or at the end; never twice.
    custom = _practice(greeting="Γεια σας, είμαι η ψηφιακή βοηθός. Τι θέλετε;")
    assert default_greeting(custom, language="el", recording_notice=True) == (
        "Γεια σας, είμαι η ψηφιακή βοηθός. Η κλήση ηχογραφείται· αν θέλετε να σβηστεί, πείτε μου. Τι θέλετε;")
    assert with_recording_notice("Γεια σας!", "el").endswith("πείτε μου.")
    already = "Είμαι ο ψηφιακός βοηθός και η κλήση καταγράφεται. Πώς βοηθώ;"
    assert with_recording_notice(already, "el") == already


# --- checklist ---


def test_g7_needs_the_notice_or_no_recording():
    assert _items(_practice())["recording_notice"]["status"] == "todo"
    assert _items(_practice(recording_notice=True))["recording_notice"]["status"] == "ok"
    assert _items(_practice(recording_enabled=False))["recording_notice"]["status"] == "ok"
    # A custom greeting that already says it still counts.
    assert _items(_practice(greeting="Ψηφιακός βοηθός, η κλήση ηχογραφείται."))["recording_notice"]["status"] == "ok"


def test_trunk_item():
    item = _items(_practice())["numbers_on_trunk"]
    assert item["status"] == "not_configured" and not item["required"]
    assert _trunk_item([NUMBER], {"status": "unknown", "error": "x"})["status"] == "unknown"
    assert _trunk_item([NUMBER], {"status": "ok", "numbers": ["302100000009"], "any_number": False})["status"] == "ok"
    missing = _trunk_item([NUMBER, "+302100000010"], {"status": "ok", "numbers": ["302100000009"],
                                                       "any_number": False})
    assert missing["status"] == "warning" and "+302100000010" in missing["detail"]
    assert "302100000009" not in missing["detail"].replace("+302100000010", "")
    # A trunk without numbers takes calls to any number.
    assert _trunk_item([NUMBER], {"status": "ok", "numbers": [], "any_number": True})["status"] == "ok"
    assert _trunk_item([], None)["status"] == "warning"


LIVEKIT = SimpleNamespace(livekit_url="wss://x", livekit_api_key="k", livekit_api_secret="s")


@pytest.mark.asyncio
async def test_trunk_numbers_is_timeboxed_and_cached(monkeypatch):
    monkeypatch.setattr(onboarding, "_trunk_cache", None)
    assert (await trunk_numbers(SimpleNamespace(livekit_url="", livekit_api_key="", livekit_api_secret="")))[
        "status"] == "not_configured"

    calls = []

    async def slow(s):
        calls.append(1)
        await asyncio.sleep(5)

    monkeypatch.setattr(onboarding, "TRUNK_TIMEOUT", 0.05)
    monkeypatch.setattr(onboarding, "_list_inbound_trunks", slow)
    assert (await trunk_numbers(LIVEKIT))["status"] == "unknown"
    # The failure is cached briefly: no second slow request right away.
    assert (await trunk_numbers(LIVEKIT))["status"] == "unknown" and len(calls) == 1

    async def listed(s):
        calls.append(1)
        return [SimpleNamespace(numbers=["+30 210 000 0009"]), SimpleNamespace(numbers=["+302100000011"])]

    monkeypatch.setattr(onboarding, "_trunk_cache", None)
    monkeypatch.setattr(onboarding, "_list_inbound_trunks", listed)
    result = await trunk_numbers(LIVEKIT)
    assert result == {"status": "ok", "numbers": ["302100000009", "302100000011"], "any_number": False}
    await trunk_numbers(LIVEKIT)
    assert len(calls) == 2  # cached

    async def broken(s):
        raise RuntimeError("401")

    monkeypatch.setattr(onboarding, "_trunk_cache", None)
    monkeypatch.setattr(onboarding, "_list_inbound_trunks", broken)
    assert (await trunk_numbers(LIVEKIT))["status"] == "unknown"


# --- metadata and endpoints (Postgres) ---


async def _call(db, practice, direction="inbound", purpose=None):
    call = Call(practice_id=practice.id, direction=direction, caller_number="+306911111111", persona="",
                scenario="", status=CallStatus.active, language="el", purpose=purpose)
    db.add(call)
    await db.commit()
    return call


@pytest.mark.asyncio
async def test_metadata_follows_the_practice_setting(sessions):
    async with sessions() as db:
        practice = Practice(name="Οδοντιατρείο", timezone="Europe/Athens", hours=HOURS, services=SERVICES,
                            phone_numbers=[NUMBER])
        db.add(practice)
        await db.commit()
        # Existing practices (and the model default): recording on, no notice.
        assert practice.recording_enabled is True and practice.recording_notice is False
        meta = await receptionist.build_metadata(db, practice, await _call(db, practice))
        assert meta["record"] and not meta["recording_notice"] and "ηχογραφ" not in meta["greeting"]

        practice.recording_notice = True
        await db.commit()
        meta = await receptionist.build_metadata(db, practice, await _call(db, practice))
        assert meta["record"] and meta["recording_notice"] and "ηχογραφείται" in meta["greeting"]
        # The web demo is never recorded, so it never says it is.
        web = await receptionist.build_metadata(db, practice, await _call(db, practice, "web"))
        assert not web["record"] and "ηχογραφ" not in web["greeting"] and "ΔΕΝ ηχογραφείται" in web["prompt"]

        practice.recording_enabled = False
        await db.commit()
        meta = await receptionist.build_metadata(db, practice, await _call(db, practice))
        assert not meta["record"] and not meta["recording_enabled"] and not meta["recording_notice"]
        assert "ηχογραφ" not in meta["greeting"]
        assert "ΔΕΝ ηχογραφείται" in meta["prompts"]["el"] and "NOT recorded" in meta["prompts"]["en"]


@pytest.mark.asyncio
async def test_new_practices_get_the_notice_updates_keep_it(sessions):
    async with sessions() as db:
        created = await practices_router.create_practice(PracticeIn.model_validate(
            {"name": "New", "services": SERVICES, "hours": HOURS, "phone_numbers": [NUMBER]}), db)
        assert created.recording_enabled and created.recording_notice
        out = PracticeOut.model_validate(created)
        assert out.recording_enabled and out.recording_notice

        # A full update from an older app build (no recording fields) changes nothing.
        r = await practices_router.set_recording(created.id, RecordingSettings(recording_notice=False), db)
        assert r.recording_enabled and not r.recording_notice
        body = out.model_dump(exclude={"recording_enabled", "recording_notice"})
        await practices_router.update_practice(created.id, PracticeIn.model_validate(body), db)
        assert (await practices_router.get_recording(created.id, db)).recording_notice is False
        # ...and one that sends them applies them.
        body |= {"recording_enabled": False}
        await practices_router.update_practice(created.id, PracticeIn.model_validate(body), db)
        r = await practices_router.get_recording(created.id, db)
        assert r.recording_enabled is False and r.recording_notice is False

        # An explicit choice at creation wins.
        other = await practices_router.create_practice(PracticeIn.model_validate(
            {"name": "Other", "services": SERVICES, "hours": HOURS, "recording_notice": False}), db)
        assert other.recording_enabled and not other.recording_notice
