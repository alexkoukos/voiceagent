"""Edge cases of the onboarding checklist, go-live, number/staff checks and the G7 recording
notice that the first tests didn't reach."""

from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import onboarding, receptionist
from app.models import Call, CallStatus, Practice
from app.onboarding import _trunk_item, checklist, ready, trunk_numbers
from app.prompts import default_greeting, mentions_recording, with_recording_notice
from app.routers import ops
from app.routers import practices as practices_router
from app.schemas import DpaIn, OnboardingIn, PracticeIn, StaffIn

HOURS = {"mon": [["09:00", "14:00"]]}
SERVICES = [{"id": "check", "name": "Έλεγχος", "duration_minutes": 30},
            {"id": "clean", "name": "Καθαρισμός", "duration_minutes": 45}]
NONE = SimpleNamespace(google_service_account_json="", google_oauth_client_id="", smtp_host="", email_from="",
                       telnyx_api_key="", sms_from="", founder_email="", founder_sms="", data_encryption_key="")
ALL = SimpleNamespace(google_service_account_json="{}", google_oauth_client_id="x", smtp_host="smtp",
                      email_from="a@b", telnyx_api_key="k", sms_from="+300", founder_email="", founder_sms="+30690",
                      data_encryption_key="k")


def _practice(**kw):
    base = dict(name="Ιατρείο", greeting="", hours=HOURS, services=SERVICES, knowledge_base={"Διεύθυνση": "Οδός 1"},
                calendar_id=None, phone_numbers=["+302100000001"], notifications={"emails": ["a@b.gr"]},
                onboarding={}, recording_enabled=True, recording_notice=True)
    return SimpleNamespace(**(base | kw))


def _items(p, staff=(), calls=0, settings=ALL, trunk=None):
    return {i["id"]: i for i in checklist(p, list(staff), set(), calls, settings=settings, trunk=trunk)}


# --- checklist: every item in each state ---


def test_every_item_reaches_ok():
    p = _practice(onboarding={"dpa": {"signed_on": "2026-09-26", "signed_by": "O"},
                              "forwarding_confirmed_at": "x"},
                  notifications={"emails": ["a@b.gr"], "fallback_number": "+306900000000"})
    trunk = {"status": "ok", "numbers": ["302100000001"], "any_number": False}
    items = _items(p, [SimpleNamespace(active=True, calendar_id=None)], calls=1, trunk=trunk)
    assert {k: v["status"] for k, v in items.items() if v["status"] != "ok"} == {}
    assert ready(list(items.values()))
    assert all(v["detail"] == "" for v in items.values())


def test_hours_and_services_edge_states():
    # Days present but all closed count as no hours.
    assert _items(_practice(hours={"mon": [], "tue": []}))["hours"]["status"] == "todo"
    assert _items(_practice(hours=None))["hours"]["status"] == "todo"
    assert _items(_practice(services=[]))["services"]["status"] == "todo"
    no_duration = [{"id": "x", "name": "X"}, *SERVICES]
    assert _items(_practice(services=no_duration))["services"]["status"] == "todo"
    # Inactive staff don't count.
    assert _items(_practice(), [SimpleNamespace(active=False, calendar_id=None)])["staff"]["status"] == "warning"


def test_greeting_disclosure_and_notice_states():
    # A custom greeting that hides the AI blocks go-live (G1).
    items = _items(_practice(greeting="Καλησπέρα, Μαρία εδώ."))
    assert items["ai_disclosure"]["status"] == "todo" and not ready(list(items.values()))
    # Recording on, notice off: todo; "medical records" in the greeting is not a notice.
    p = _practice(recording_notice=False, greeting="Hi, I'm the AI assistant. Ask about your medical records.")
    assert _items(p)["recording_notice"]["status"] == "todo"
    p = _practice(recording_notice=False, greeting="Hi, I'm the AI assistant. This call is being recorded.")
    assert _items(p)["recording_notice"]["status"] == "ok"
    # Older rows passed without the attribute behave as "recording on, no notice".
    legacy = SimpleNamespace(**{k: v for k, v in vars(_practice()).items()
                                if k not in ("recording_enabled", "recording_notice")})
    assert _items(legacy)["recording_notice"]["status"] == "todo"


def test_notification_items():
    # SMS not wanted: ok even without credentials.
    p = _practice(notifications={"emails": ["a@b.gr"], "customer_sms": False})
    assert _items(p, settings=NONE)["sms"]["status"] == "ok"
    # Wanted (default) without credentials: the server's job, never blocking.
    item = _items(_practice(), settings=NONE)["sms"]
    assert item["status"] == "not_configured" and not item["required"]
    # Urgent SMS alone also wants SMS.
    p = _practice(notifications={"emails": ["a@b.gr"], "customer_sms": False, "urgent_sms": "+30690"})
    assert _items(p, settings=NONE)["sms"]["status"] == "not_configured"
    # FOUNDER_SMS alone is enough for alerts.
    assert _items(_practice())["alerts"]["status"] == "ok"
    assert _items(_practice(notifications={}), settings=NONE)["email"]["status"] == "todo"


def test_trunk_states_never_block_go_live():
    p = _practice(onboarding={"dpa": {"signed_on": "2026-09-26", "signed_by": "O"}})
    for trunk in (None, {"status": "not_configured"}, {"status": "unknown", "error": "x"},
                  {"status": "ok", "numbers": [], "any_number": False}):
        items = _items(p, calls=1, trunk=trunk)
        assert not items["numbers_on_trunk"]["required"]
        assert ready(list(items.values())), trunk
    # Formatting differences don't matter: digits are compared.
    assert _trunk_item(["+302100000001"], {"status": "ok", "numbers": ["302100000001"],
                                           "any_number": False})["status"] == "ok"


@pytest.mark.asyncio
async def test_trunk_with_no_numbers_takes_any(monkeypatch):
    monkeypatch.setattr(onboarding, "_trunk_cache", None)

    async def listed(s):
        return [SimpleNamespace(numbers=[]), SimpleNamespace(numbers=["+302100000001"])]

    monkeypatch.setattr(onboarding, "_list_inbound_trunks", listed)
    s = SimpleNamespace(livekit_url="wss://x", livekit_api_key="k", livekit_api_secret="s")
    assert (await trunk_numbers(s))["any_number"] is True
    monkeypatch.setattr(onboarding, "_trunk_cache", None)


# --- greeting text ---


def test_recording_words():
    assert not mentions_recording("Ask about your medical records.")
    assert not mentions_recording("A new record for our clinic!")
    assert mentions_recording("Calls may be recorded.")
    assert mentions_recording("This call is being RECORDED.")
    assert mentions_recording("We're recording this call.")
    assert mentions_recording("Η ΚΛΗΣΗ ΗΧΟΓΡΑΦΕΙΤΑΙ.")
    # An English greeting about records still gets the notice.
    out = with_recording_notice("I'm the AI assistant. Need your records?", "en")
    assert out == "I'm the AI assistant. This call is recorded; tell me if you'd like the recording deleted. Need your records?"


def test_notice_before_a_greek_question_mark():
    # U+037E, what some keyboards type for the Greek question mark.
    out = with_recording_notice("Γεια σας. Πώς μπορώ να βοηθήσω;", "el")
    assert out == "Γεια σας. Η κλήση ηχογραφείται· αν θέλετε να σβηστεί, πείτε μου. Πώς μπορώ να βοηθήσω;"


def test_default_greeting_notice_in_english_practice():
    p = SimpleNamespace(name="Clinic", greeting="  ")
    assert default_greeting(p, language="en", recording_notice=True) == (
        "I'm a digital assistant. This call is recorded; tell me if you'd like the recording deleted. "
        "How can I help you?")
    # Custom greeting with notice off: exactly as typed (trimmed).
    p = SimpleNamespace(name="Clinic", greeting=" Hello, AI assistant here. How can I help? ")
    assert default_greeting(p, language="en") == "Hello, AI assistant here. How can I help?"


# --- Postgres ---


async def _db_practice(db, **kw) -> Practice:
    practice = Practice(**({"name": "Ιατρείο", "timezone": "Europe/Athens", "hours": HOURS, "services": SERVICES,
                            "phone_numbers": ["+302100000001"]} | kw))
    db.add(practice)
    await db.commit()
    return practice


async def _call(db, practice, direction="inbound", purpose=None, language="el"):
    call = Call(practice_id=practice.id, direction=direction, caller_number="+306911111111", persona="",
                scenario="", status=CallStatus.active, language=language, purpose=purpose)
    db.add(call)
    await db.commit()
    return call


@pytest.mark.asyncio
async def test_reactivate_refuses_a_number_now_used_elsewhere(sessions):
    async with sessions() as db:
        old = await _db_practice(db)
        await ops.offboard(old.id, db)
        new = await practices_router.create_practice(PracticeIn.model_validate(
            {"name": "New", "services": SERVICES, "hours": HOURS, "phone_numbers": ["+302100000001"]}), db)
        with pytest.raises(HTTPException) as e:
            await ops.reactivate(old.id, db)
        assert e.value.status_code == 409 and e.value.detail["numbers"] == ["+302100000001"]
        assert (await db.get(Practice, old.id)).offboarded_at is not None
        assert (await receptionist.practice_for_number(db, "+302100000001")).id == new.id
        # Once the number is free again, it comes back.
        new.offboarded_at = datetime.utcnow()
        await db.commit()
        assert (await ops.reactivate(old.id, db))["offboarded_at"] is None
        # Reactivating an active practice is a no-op, not a conflict with itself.
        assert (await ops.reactivate(old.id, db))["offboarded_at"] is None


@pytest.mark.asyncio
async def test_staff_keeps_a_service_the_practice_dropped(sessions):
    async with sessions() as db:
        practice = await _db_practice(db)
        person = await practices_router.create_staff(practice.id, StaffIn(name="Dr", service_ids=["check", "clean"]), db)
        # The practice drops "clean" (edit or price list import).
        practice.services = [SERVICES[0]]
        await db.commit()
        # Renaming the person with the ids they already have still works...
        out = await practices_router.update_staff(practice.id, person.id,
                                                  StaffIn(name="Dr B", service_ids=["check", "clean"]), db)
        assert out.name == "Dr B"
        # ...but adding an unknown one doesn't.
        with pytest.raises(HTTPException) as e:
            await practices_router.update_staff(practice.id, person.id,
                                                StaffIn(name="Dr B", service_ids=["check", "whiten"]), db)
        assert e.value.status_code == 422 and e.value.detail["service_ids"] == ["whiten"]
        with pytest.raises(HTTPException):
            await practices_router.create_staff(practice.id, StaffIn(name="New", service_ids=["clean"]), db)


@pytest.mark.asyncio
async def test_update_can_keep_or_change_numbers(sessions):
    async with sessions() as db:
        a = await _db_practice(db)
        b = await _db_practice(db, phone_numbers=["+302100000002"])
        body = {"name": "B", "services": SERVICES, "hours": HOURS}
        with pytest.raises(HTTPException) as e:
            await practices_router.update_practice(b.id, PracticeIn.model_validate(
                body | {"phone_numbers": ["+302100000002", "+302100000001"]}), db)
        assert e.value.status_code == 409 and e.value.detail["numbers"] == ["+302100000001"]
        # A legacy vertical that no longer has a template doesn't block an unrelated edit.
        a.vertical = "retired"
        await db.commit()
        await practices_router.update_practice(a.id, PracticeIn.model_validate(
            {"name": "A2", "services": SERVICES, "hours": HOURS, "vertical": "retired",
             "phone_numbers": ["+302100000001"]}), db)
        with pytest.raises(HTTPException):
            await practices_router.update_practice(a.id, PracticeIn.model_validate(
                {"name": "A2", "services": SERVICES, "hours": HOURS, "vertical": "gone"}), db)


@pytest.mark.asyncio
async def test_onboarding_put_is_partial_and_keeps_timestamps(sessions, monkeypatch):
    monkeypatch.setattr(onboarding, "get_settings", lambda: ALL)
    async with sessions() as db:
        practice = await _db_practice(db, notifications={"emails": ["a@b.gr"]}, recording_notice=True)
        r = await ops.update_onboarding(practice.id, OnboardingIn(test_call_confirmed=True), db)
        first = (await db.get(Practice, practice.id)).onboarding["test_call_confirmed_at"]
        assert {i["id"]: i["status"] for i in r["items"]}["test_call"] == "ok"
        # Confirming again keeps the first time; other fields untouched by an empty PUT.
        await ops.update_onboarding(practice.id, OnboardingIn(test_call_confirmed=True), db)
        await ops.update_onboarding(practice.id, OnboardingIn(), db)
        state = (await db.get(Practice, practice.id)).onboarding
        assert state["test_call_confirmed_at"] == first and "dpa" not in state
        with pytest.raises(HTTPException) as e:
            await ops.go_live(practice.id, db)
        assert e.value.detail["missing"] == ["dpa"]
        await ops.update_onboarding(practice.id, OnboardingIn(dpa=DpaIn(signed_on=date(2026, 9, 1), signed_by="O")), db)
        live = await ops.go_live(practice.id, db)
        assert live["ready"] and live["live_at"]
        # Un-confirming after go-live leaves the go-live record alone.
        r = await ops.update_onboarding(practice.id, OnboardingIn(test_call_confirmed=False), db)
        assert not r["ready"] and r["live_at"] == live["live_at"]


@pytest.mark.asyncio
async def test_outbound_calls_say_the_notice(sessions, monkeypatch):
    purpose = {"el": "υπενθύμιση", "en": "reminder", "greeting_el": "Χαιρέτα.", "greeting_en": "Say hello."}

    async def fake_purpose(db, practice, call, staff, language):
        return purpose

    monkeypatch.setattr(receptionist, "_purpose", fake_purpose)
    async with sessions() as db:
        practice = await _db_practice(db, recording_notice=True)
        meta = await receptionist.build_metadata(db, practice, await _call(db, practice, "outbound", "reminder"))
        assert meta["record"] and meta["recording_notice"] and "ηχογραφείται" in meta["greeting_instruction"]
        meta = await receptionist.build_metadata(
            db, practice, await _call(db, practice, "outbound", "reminder", language="en"))
        assert "recorded" in meta["greeting_instruction"]
        # A custom greeting that already says it counts as the notice for outbound calls too.
        practice.recording_notice = False
        practice.greeting = "Ψηφιακός βοηθός. Η κλήση ηχογραφείται. Πώς βοηθώ;"
        await db.commit()
        meta = await receptionist.build_metadata(db, practice, await _call(db, practice, "outbound", "reminder"))
        assert meta["recording_notice"] and "ηχογραφείται" in meta["greeting_instruction"]
        inbound = await receptionist.build_metadata(db, practice, await _call(db, practice))
        assert inbound["greeting"] == practice.greeting  # never twice
        # No notice without recording, nor on the daily test call.
        practice.recording_enabled = False
        await db.commit()
        meta = await receptionist.build_metadata(db, practice, await _call(db, practice, "outbound", "reminder"))
        assert not meta["record"] and not meta["recording_notice"]
        assert "ηχογραφείται" not in meta["greeting_instruction"]
        practice.recording_enabled, practice.greeting, practice.recording_notice = True, "", True
        await db.commit()
        monkeypatch.undo()
        test = await receptionist.build_metadata(db, practice, await _call(db, practice, "outbound", "test"))
        assert test["record"] and not test["recording_notice"] and "ηχογραφ" not in test["greeting_instruction"]


@pytest.mark.asyncio
async def test_web_demo_never_records_even_with_notice(sessions):
    async with sessions() as db:
        practice = await _db_practice(db, recording_notice=True, greeting="")
        meta = await receptionist.build_metadata(db, practice, await _call(db, practice, "web"))
        assert not meta["record"] and not meta["recording_notice"] and meta["recording_enabled"]
        assert "ηχογραφ" not in meta["greeting"] and "NOT recorded" in meta["prompts"]["en"]
