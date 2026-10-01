"""O1, O2, O5: Google hours, price list merge, forwarding codes."""

from unittest.mock import AsyncMock

from app.onboarding import _slug, forwarding_codes, hours_from_google, merge_services, routing_instructions
from app.models import Practice
from app.schemas import CallRouting, PracticeIn
from pydantic import ValidationError
import pytest
from fastapi import HTTPException


def test_google_hours_split_days_and_24h():
    opening = {"periods": [
        {"open": {"day": 1, "hour": 9, "minute": 0}, "close": {"day": 1, "hour": 14, "minute": 0}},
        {"open": {"day": 1, "hour": 17, "minute": 30}, "close": {"day": 1, "hour": 21, "minute": 0}},
        {"open": {"day": 5, "hour": 20, "minute": 0}, "close": {"day": 6, "hour": 2, "minute": 0}},
    ]}
    h = hours_from_google(opening)
    assert h["mon"] == [["09:00", "14:00"], ["17:30", "21:00"]]
    assert h["fri"] == [["20:00", "23:59"]]
    assert h["sat"] == [["00:00", "02:00"]]
    assert h["sun"] == []
    assert all(v == [["00:00", "23:59"]] for v in hours_from_google(
        {"periods": [{"open": {"day": 0, "hour": 0}}]}).values())
    assert h["tue"] == []


def test_price_list_merge_keeps_ids_and_flags_gaps():
    current = [{"id": "check", "name": "Καθαρισμός δοντιών", "duration_minutes": 30, "price": "45€"}]
    extracted = [
        {"name": "καθαρισμός  δοντιών", "price": "50€", "duration_minutes": None, "confidence": 0.95},
        {"name": "Λεύκανση", "price": "250€", "duration_minutes": 60, "confidence": 0.9},
        {"name": "Σφράγισμα", "price": None, "duration_minutes": 40, "confidence": 0.9},
        {"name": "Θολό", "price": "20€", "duration_minutes": 20, "confidence": 0.4},
        {"name": "Λεύκανση", "price": "250€", "duration_minutes": 60, "confidence": 0.9},
    ]
    services, check = merge_services(current, extracted)
    assert services[0] == {"id": "check", "name": "Καθαρισμός δοντιών", "duration_minutes": 30, "price": "50€"}
    assert [s["name"] for s in services] == ["Καθαρισμός δοντιών", "Λεύκανση", "Σφράγισμα", "Θολό"]
    assert services[1]["id"] == "leukansi" and services[1]["duration_minutes"] == 60
    assert check == ["Σφράγισμα", "Θολό"]


def test_slugs_are_ascii_and_unique():
    used = {"check"}
    assert _slug("Ψυχρή Θεραπεία", used) == "psychri-therapeia"
    assert _slug("Ψυχρή Θεραπεία", used) == "psychri-therapeia-2"
    assert _slug("!!!", used) == "service"


def test_forwarding_codes():
    backup = forwarding_codes("+302100000000", "backup")
    assert [c["code"] for c in backup] == ["**61*+302100000000#", "**67*+302100000000#"]
    assert forwarding_codes("+302100000000", "backup", 25)[0]["code"] == "**61*+302100000000*11*25#"
    with pytest.raises(ValueError):
        forwarding_codes("+302100000000", "full")


def test_routing_requires_confirmed_capability_and_ai_did():
    with pytest.raises(ValidationError):
        CallRouting(mode="human_first", public_number="+302100000001",
                    ai_destination_number="+302100000002")
    with pytest.raises(ValidationError):
        CallRouting(mode="human_first", public_number="+302100000001",
                    ai_destination_number="+302100000002", no_answer_seconds=20,
                    capabilities={"no_answer": True})
    payload = dict(name="Clinic", services=[{"id": "check", "name": "Check", "duration_minutes": 30}],
                   phone_numbers=["+302100000002"],
                   call_routing={"mode": "human_first", "public_number": "+302100000001",
                                 "ai_destination_number": "+302100000002", "provider": "Example PBX",
                                 "capabilities": {"no_answer": True}})
    setup = PracticeIn.model_validate(payload)
    assert setup.call_routing.mode == "human_first"
    with pytest.raises(ValidationError):
        PracticeIn.model_validate({**payload, "phone_numbers": ["+302100000003"]})


def test_instructions_do_not_claim_precise_rings_without_capability():
    practice = Practice(name="Clinic", language="en", call_routing={"mode": "human_first",
                        "public_number": "+302100000001", "ai_destination_number": "+302100000002",
                        "provider": "Example PBX", "capabilities": {"no_answer": True}})
    instructions = routing_instructions(practice)
    assert instructions["ready"]
    assert any("do not promise" in step for step in instructions["steps"])
    assert instructions["public_number"] == "+302100000001"


def test_mobile_dial_codes_need_verified_provider():
    from app.routers.ops import _mobile_forwarding_verified
    practice = Practice(name="Clinic", call_routing={"phone_system": "mobile", "provider": "Nova GR"})
    assert not _mobile_forwarding_verified(practice)
    practice.call_routing = {**practice.call_routing, "carrier_configuration_confirmed": True}
    assert _mobile_forwarding_verified(practice)
    practice.call_routing = {**practice.call_routing, "phone_system": "sip_pbx"}
    assert not _mobile_forwarding_verified(practice)


@pytest.mark.asyncio
async def test_forwarding_endpoint_respects_configured_mode_and_timeout():
    from app.routers.ops import forwarding

    practice = Practice(id="clinic", name="Clinic", call_routing={
        "mode": "human_first", "phone_system": "mobile", "provider": "Nova GR",
        "carrier_configuration_confirmed": True,
        "ai_destination_number": "+302100000002", "no_answer_seconds": 25,
        "busy_behavior": "normal"})

    class DB:
        info = {}

        async def get(self, _model, _id):
            return practice

    db = DB()
    out = await forwarding(practice.id, mode="backup", db=db)
    assert out["codes"] == [{"what": "no_answer", "code": "**61*+302100000002*11*25#"}]
    with pytest.raises(HTTPException) as error:
        await forwarding(practice.id, mode="full", db=db)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_routing_setup_update_requires_provisioned_ai_number():
    from app.routers.ops import update_routing_setup

    practice = Practice(id="clinic", name="Clinic", language="en", phone_numbers=["+302100000002"])

    class DB:
        info = {}
        committed = False

        async def get(self, _model, _id):
            return practice

        async def commit(self):
            self.committed = True

    db = DB()
    setup = CallRouting(mode="ai_first", public_number="+302100000001",
                        ai_destination_number="+302100000003", capabilities={"unconditional": True})
    with pytest.raises(HTTPException) as error:
        await update_routing_setup(practice.id, setup, db)
    assert error.value.status_code == 422 and not db.committed
    setup.ai_destination_number = "+302100000002"
    result = await update_routing_setup(practice.id, setup, db)
    assert db.committed and result["ready"]
    assert practice.call_routing["public_number"] == "+302100000001"


@pytest.mark.asyncio
async def test_older_app_cannot_remove_the_active_ai_destination(monkeypatch):
    from app.routers import practices as routes

    practice = Practice(id="clinic", name="Clinic", phone_numbers=["+302100000002"],
                        call_routing={"mode": "human_first", "ai_destination_number": "+302100000002"})

    class DB:
        info = {}

        async def get(self, _model, _id):
            return practice

    monkeypatch.setattr(routes, "validate_assignments", AsyncMock())
    monkeypatch.setattr(routes.config_changes, "lock_config", AsyncMock())
    publish = AsyncMock()
    monkeypatch.setattr(routes.config_changes, "publish", publish)
    payload = PracticeIn.model_validate({"name": "Clinic", "phone_numbers": ["+302100000003"],
                                         "services": [{"id": "check", "name": "Check", "duration_minutes": 30}]})

    with pytest.raises(HTTPException) as error:
        await routes.update_practice(practice.id, payload, DB())

    assert error.value.status_code == 422
    publish.assert_not_awaited()


def test_google_special_hours_override_weekly_hours_and_staff():
    from datetime import date
    from app.booking import hours_on
    from app.models import Practice
    from app.onboarding import special_hours_from_google
    special = special_hours_from_google({
        "specialDays": [{"date": {"year":2030,"month":1,"day":7}}, {"date":{"year":2030,"month":1,"day":8}}],
        "periods": [{"open":{"date":{"year":2030,"month":1,"day":7},"hour":10},
                     "close":{"date":{"year":2030,"month":1,"day":7},"hour":12}}]})
    p = Practice(name="Test",hours={"mon":[["09:00","17:00"]],"tue":[["09:00","17:00"]]},rules={"date_hours":special})
    assert hours_on(p,date(2030,1,7)) == [["10:00","12:00"]]
    assert hours_on(p,date(2030,1,8)) == []
    assert hours_on(p,date(2030,1,7),{"mon":[["11:00","17:00"]]}) == [("11:00","12:00")]
