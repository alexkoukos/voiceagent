"""Onboarding: the go-live checklist, confirmations, and checks when a practice is created."""

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models import Call, CallStatus, Practice
from app.onboarding import checklist, discloses_ai, mentions_recording, ready
from app.routers import ops
from app.routers import practices as practices_router
from app.schemas import DpaIn, OnboardingIn, PracticeIn, StaffIn

HOURS = {"mon": [["09:00", "14:00"]]}
SERVICES = [{"id": "check", "name": "Έλεγχος", "duration_minutes": 30}]
GREETING = "Οδοντιατρείο. Είμαι ο ψηφιακός βοηθός και η κλήση ηχογραφείται. Πώς μπορώ να βοηθήσω;"

NO_CREDENTIALS = SimpleNamespace(
    google_service_account_json="", google_oauth_client_id="", smtp_host="", email_from="", telnyx_api_key="",
    sms_from="", founder_email="", founder_sms="", data_encryption_key="",
)
ALL_CREDENTIALS = SimpleNamespace(
    google_service_account_json="{}", google_oauth_client_id="x", smtp_host="smtp", email_from="a@b", telnyx_api_key="k",
    sms_from="+300", founder_email="f@b", founder_sms="", data_encryption_key="k",
)


def _practice(**kw):
    base = dict(hours=HOURS, services=SERVICES, knowledge_base={"Διεύθυνση": "Οδός 1"}, calendar_id=None,
                phone_numbers=["+302100000000"], greeting=GREETING, notifications={"emails": ["a@b.gr"]},
                onboarding={"dpa": {"signed_on": "2026-09-26", "signed_by": "Owner"}})
    return SimpleNamespace(**(base | kw))


def _staff(**kw):
    return SimpleNamespace(**({"active": True, "calendar_id": None} | kw))


def _by_id(items):
    return {i["id"]: i for i in items}


def test_complete_practice_is_ready():
    items = checklist(_practice(), [_staff()], set(), answered_calls=1, settings=ALL_CREDENTIALS)
    assert ready(items)
    assert all(i["status"] == "ok" for i in items if i["required"])
    assert _by_id(items)["forwarding"]["status"] == "warning"  # optional, not confirmed


def test_new_practice_lists_what_is_missing():
    p = _practice(phone_numbers=[], greeting="", notifications={}, onboarding={},
                  knowledge_base={"Διεύθυνση": "", "Πάρκινγκ": ""})
    items = _by_id(checklist(p, [], set(), answered_calls=0, settings=NO_CREDENTIALS))
    assert not ready(list(items.values()))
    # Missing for the business
    for key in ("numbers", "recording_notice", "dpa", "email", "test_call"):
        assert items[key]["status"] == "todo", key
    # The default greeting already says it's a digital assistant (G1).
    assert items["ai_disclosure"]["status"] == "ok"
    assert items["knowledge_base"]["status"] == "warning" and "Πάρκινγκ" in items["knowledge_base"]["detail"]
    # Server credentials are reported as not configured, not as the business's job.
    for key in ("sms", "alerts", "encryption"):
        assert items[key]["status"] == "not_configured", key
    assert items["numbers"]["prd"] == "O5" and items["dpa"]["prd"] == "G2"


def test_email_without_smtp_is_not_configured():
    items = _by_id(checklist(_practice(), [], set(), answered_calls=1, settings=NO_CREDENTIALS))
    assert items["email"]["status"] == "not_configured"


def test_calendars_need_a_connection_or_the_service_account():
    p = _practice(calendar_id="shared@group")
    staff = [_staff(calendar_id="doc@group"), _staff(calendar_id="gone@group", active=False)]
    oauth_only = SimpleNamespace(**(vars(ALL_CREDENTIALS) | {"google_service_account_json": ""}))
    items = _by_id(checklist(p, staff, {"doc@group"}, answered_calls=1, settings=oauth_only))
    assert items["calendars"]["status"] == "todo" and items["calendars"]["detail"].startswith("1 calendar")
    items = _by_id(checklist(p, staff, {"doc@group", "shared@group"}, answered_calls=1, settings=oauth_only))
    assert items["calendars"]["status"] == "ok"
    items = _by_id(checklist(p, staff, set(), answered_calls=1, settings=NO_CREDENTIALS))
    assert items["calendars"]["status"] == "not_configured"


def test_greeting_checks():
    assert discloses_ai("Γεια σας, είμαι ο ψηφιακός βοηθός")
    assert discloses_ai("Hi, I'm an AI assistant.")
    assert not discloses_ai("Καλησπέρα, Μαρία από το ιατρείο")
    assert not discloses_ai("Said by a maid")  # no "ai" inside words
    assert mentions_recording("Η κλήση καταγράφεται για λόγους ποιότητας")
    assert mentions_recording("This call is recorded.")
    assert not mentions_recording("Πώς μπορώ να βοηθήσω;")
    items = _by_id(checklist(_practice(greeting="Καλησπέρα, Μαρία εδώ. Η κλήση ηχογραφείται."), [], set(), 1,
                             settings=ALL_CREDENTIALS))
    assert items["ai_disclosure"]["status"] == "todo"


# --- endpoints (Postgres) ---


async def _db_practice(db, **kw) -> Practice:
    practice = Practice(**({"name": "Test", "timezone": "Europe/Athens", "hours": HOURS, "services": SERVICES,
                            "phone_numbers": ["+302100000001"], "greeting": GREETING,
                            "notifications": {"emails": ["a@b.gr"]}} | kw))
    db.add(practice)
    await db.commit()
    return practice


@pytest.mark.asyncio
async def test_go_live_needs_every_required_item(sessions, monkeypatch):
    from app import onboarding
    monkeypatch.setattr(onboarding, "get_settings", lambda: ALL_CREDENTIALS)
    async with sessions() as db:
        practice = await _db_practice(db)
        with pytest.raises(HTTPException) as e:
            await ops.go_live(practice.id, db)
        assert e.value.status_code == 409
        assert set(e.value.detail["missing"]) == {"dpa", "test_call"}

        report = await ops.update_onboarding(practice.id, OnboardingIn(
            dpa=DpaIn(signed_on=date(2026, 9, 26), signed_by="Owner")), db)
        assert report["dpa"] == {"signed_on": "2026-09-26", "signed_by": "Owner"}
        assert not report["ready"]

        # A completed web demo call counts as the test call.
        db.add(Call(practice_id=practice.id, direction="web", status=CallStatus.completed, persona="", scenario=""))
        await db.commit()
        report = await ops.go_live(practice.id, db)
        assert report["ready"] and report["live_at"]
        first = report["live_at"]
        assert (await ops.go_live(practice.id, db))["live_at"] == first

        # Confirmations: set, kept on repeat, cleared by false; DPA untouched when omitted.
        r = await ops.update_onboarding(practice.id, OnboardingIn(forwarding_confirmed=True), db)
        assert {i["id"]: i["status"] for i in r["items"]}["forwarding"] == "ok"
        r = await ops.update_onboarding(practice.id, OnboardingIn(forwarding_confirmed=False), db)
        assert {i["id"]: i["status"] for i in r["items"]}["forwarding"] == "warning"
        assert r["dpa"]["signed_by"] == "Owner"
        r = await ops.update_onboarding(practice.id, OnboardingIn.model_validate({"dpa": None}), db)
        assert r["dpa"] is None

        # A full practice update keeps the onboarding state.
        from app.schemas import PracticeOut
        body = PracticeOut.model_validate(await db.get(Practice, practice.id)).model_dump()
        await practices_router.update_practice(practice.id, PracticeIn.model_validate(body), db)
        assert (await db.get(Practice, practice.id)).onboarding["live_at"] == first


@pytest.mark.asyncio
async def test_create_checks_numbers_vertical_and_staff_services(sessions):
    async with sessions() as db:
        existing = await _db_practice(db)
        base = {"name": "New", "services": SERVICES, "hours": HOURS}
        with pytest.raises(HTTPException) as e:
            await practices_router.create_practice(PracticeIn.model_validate(
                base | {"phone_numbers": ["+30 210 000 0001"]}), db)
        assert e.value.status_code == 409 and e.value.detail["error"] == "number_in_use"
        with pytest.raises(HTTPException) as e:
            await practices_router.create_practice(PracticeIn.model_validate(base | {"vertical": "nope"}), db)
        assert e.value.detail["error"] == "unknown_vertical"

        created = await practices_router.create_practice(PracticeIn.model_validate(
            base | {"vertical": "dentist", "phone_numbers": ["+302100000002"]}), db)
        # Updating a practice with its own numbers is fine.
        from app.schemas import PracticeOut
        body = PracticeOut.model_validate(existing).model_dump()
        await practices_router.update_practice(existing.id, PracticeIn.model_validate(body), db)

        with pytest.raises(HTTPException) as e:
            await practices_router.create_staff(created.id, StaffIn(name="Dr", service_ids=["whitening"]), db)
        assert e.value.status_code == 422 and e.value.detail["service_ids"] == ["whitening"]
        person = await practices_router.create_staff(created.id, StaffIn(name="Dr", service_ids=["check"]), db)
        assert person.service_ids == ["check"]

        # An offboarded practice's number can be reused.
        from datetime import datetime
        existing.offboarded_at = datetime.utcnow()
        await db.commit()
        await practices_router.create_practice(PracticeIn.model_validate(
            base | {"phone_numbers": ["+302100000001"]}), db)
