"""HTTP -> authorization -> Postgres workflows, with provider boundaries isolated."""
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from fastapi.routing import APIRoute

from app import config_changes, finalize, onboarding
from app.config import get_settings
from app.database import get_db
from app.main import app
from app.models import Alert, Call, CallStatus, ImportRecord, Practice, Staff

MASTER = {"x-api-key": "test-founder"}
DIALER = {"x-api-key": "test-dialer"}
AGENT = {"x-agent-token": "test-agent"}


@pytest_asyncio.fixture
async def http(sessions, monkeypatch):
    settings = get_settings()
    for key, value in {"admin_api_token": "test-founder", "app_api_token": "test-dialer",
                       "internal_api_token": "test-agent", "google_maps_api_key": ""}.items():
        monkeypatch.setattr(settings, key, value)
    async def database():
        async with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = database
    monkeypatch.setattr("app.routers.internal.start_next_queued", AsyncMock())
    monkeypatch.setattr("app.dispatcher.start_next_queued", AsyncMock())
    monkeypatch.setattr(finalize, "schedule", lambda _: None)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def create(http, name):
    response = await http.post("/practices", headers=MASTER, json={
        "name": name, "slug": f"test-{name.lower()}",
        "hours": {d: [["09:00", "17:00"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
        "rules": {"min_notice_minutes": 0},
        "services": [{"id": "check", "name": "Check-up", "duration_minutes": 30}],
        "notifications": {"customer_sms": False},
    })
    assert response.status_code == 200, response.text
    return response.json()


async def key_for(http, pid):
    response = await http.post(f"/practices/{pid}/api-keys", headers=MASTER)
    assert response.status_code == 200, response.text
    return {"x-api-key": response.json()["token"]}, response.json()["id"]


@pytest.mark.asyncio
async def test_every_practice_route_rejects_foreign_tenant(http):
    a, b = await create(http, "Alpha"), await create(http, "Bravo")
    key, _ = await key_for(http, a["id"])
    checked = 0
    for template, operations in app.openapi()["paths"].items():
        if "{practice_id}" not in template:
            continue
        path = template.replace("{practice_id}", b["id"])
        path = re.sub(r"\{[^}]+\}", "foreign-resource", path)
        for method in operations:
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            response = await http.request(method, path, headers=key, json={})
            assert response.status_code in (403, 404), (method, path, response.status_code, response.text)
            checked += 1
    assert checked >= 40
    assert (await http.get("/practices", headers=key)).json()[0]["id"] == a["id"]
    assert len((await http.get("/practices", headers=key)).json()) == 1
    assert (await http.get("/practices", headers=DIALER)).status_code == 403
    assert (await http.post("/practices", headers=key, json=a)).status_code == 403
    assert (await http.post(f"/practices/{a['id']}/api-keys", headers=key)).status_code == 403


@pytest.mark.asyncio
async def test_calls_recordings_alerts_devices_and_revocation_are_scoped(http, sessions):
    a, b = await create(http, "Alpha"), await create(http, "Bravo")
    key, key_id = await key_for(http, a["id"])
    async with sessions() as db:
        ca = Call(practice_id=a["id"], persona="", scenario="", status=CallStatus.completed)
        cb = Call(practice_id=b["id"], persona="", scenario="", status=CallStatus.completed)
        aa, ab = Alert(practice_id=a["id"], kind="test", subject="A", body=""), Alert(practice_id=b["id"], kind="test", subject="B", body="")
        person = Staff(practice_id=b["id"], name="B staff")
        db.add_all([ca, cb, aa, ab, person]); await db.commit()
        ids = ca.id, cb.id, aa.id, ab.id, person.id
    assert (await http.get(f"/calls/{ids[0]}", headers=key)).status_code == 200
    for path in (f"/calls/{ids[1]}", f"/calls/{ids[1]}/recording"):
        assert (await http.get(path, headers=key)).status_code == 404
        assert (await http.get(path, headers=DIALER)).status_code == 403
    assert (await http.post(f"/calls/{ids[1]}/hangup", headers=key)).status_code == 404
    assert (await http.delete(f"/calls/{ids[1]}/recording", headers=key)).status_code == 404
    assert [x["id"] for x in (await http.get("/alerts", headers=key)).json()] == [ids[2]]
    assert (await http.post(f"/alerts/{ids[3]}/ack", headers=key)).status_code == 404
    assert (await http.post("/devices", headers=key, json={"token": "a" * 64, "practice_id": b["id"]})).status_code == 403
    assert (await http.post("/devices", headers=key, json={"token": "a" * 64, "practice_id": a["id"], "staff_id": ids[4]})).status_code == 404
    assert (await http.delete(f"/practices/{a['id']}/api-keys/{key_id}", headers=MASTER)).status_code == 204
    assert (await http.get(f"/practices/{a['id']}", headers=key)).status_code == 403


@pytest.mark.asyncio
async def test_import_edit_freeze_approve_and_rollback_http(http, sessions):
    a = await create(http, "Alpha")
    key, _ = await key_for(http, a["id"])
    async with sessions() as db:
        practice = await db.get(Practice, a["id"])
        draft = await onboarding._propose_google(db, practice, {
            "id": "google-test", "displayName": {"text": "Imported name"}, "formattedAddress": "Test address",
            "regularOpeningHours": {"periods": [{"open": {"day": 1, "hour": 10}, "close": {"day": 1, "hour": 16}}]},
        }, source_ref="test")
        await db.commit(); vid = draft.id
    base = f"/practices/{a['id']}"
    assert (await http.get(base, headers=key)).json()["name"] == "Alpha"
    # Male/female toggle: female is the default.
    assert (await http.get(base, headers=key)).json()["voice"] == "Kore"
    assert (await http.put(base + "/voice", headers=key, json={"gender": "male"})).json()["voice"] == "Zubenelgenubi"
    assert (await http.get(base, headers=key)).json()["voice"] == "Zubenelgenubi"
    assert (await http.put(base + "/voice", headers=key, json={"gender": "robot"})).status_code == 422
    await http.put(base + "/voice", headers=key, json={"gender": "female"})
    evidence = (await http.get(base + "/imports", headers=key)).json()
    assert evidence[0]["version_id"] == vid and evidence[0]["confidence"]["review_required"]
    edit = await http.patch(base + f"/versions/{vid}", headers=key, json={"changes": {"name": "Reviewed name"}})
    assert edit.status_code == 200, edit.text
    assert (await http.put(base + "/publish-freeze", headers=key, json={"frozen": True})).status_code == 200
    assert (await http.post(base + f"/versions/{vid}/approve", headers=key)).status_code == 409
    await http.put(base + "/publish-freeze", headers=key, json={"frozen": False})
    assert (await http.post(base + f"/versions/{vid}/approve", headers=key)).status_code == 200
    assert (await http.get(base, headers=key)).json()["name"] == "Reviewed name"
    versions = (await http.get(base + "/versions", headers=key)).json()
    baseline = next(v for v in versions if v["source"] == "baseline")
    assert (await http.post(base + f"/versions/{baseline['id']}/rollback", headers=key)).status_code == 200
    assert (await http.get(base, headers=key)).json()["name"] == "Alpha"


@pytest.mark.asyncio
async def test_number_calendar_and_staff_assignment_cannot_cross_tenants(http):
    a, b = await create(http, "Alpha"), await create(http, "Bravo")
    a.update(phone_numbers=["+302100000001"], calendar_id="calendar-A")
    assert (await http.put(f"/practices/{a['id']}", headers=MASTER, json=a)).status_code == 200
    key, _ = await key_for(http, b["id"])
    for field, value in (("phone_numbers", a["phone_numbers"]), ("calendar_id", "calendar-A")):
        response = await http.put(f"/practices/{b['id']}", headers=key, json={**b, field: value})
        assert response.status_code == 403, response.text
    response = await http.post(f"/practices/{b['id']}/staff", headers=key,
                               json={"name": "Other", "calendar_id": "calendar-A"})
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_inbound_booking_confirmation_finalization_and_customer_isolation(http, sessions):
    a, b = await create(http, "Alpha"), await create(http, "Bravo")
    a["phone_numbers"] = ["+302100000001"]
    await http.put(f"/practices/{a['id']}", headers=MASTER, json=a)
    start = await http.post("/internal/inbound", headers=AGENT,
                            json={"dialed_number": a["phone_numbers"][0], "caller_number": "+306900000001"})
    assert start.status_code == 200, start.text
    cid = start.json()["call_id"]
    day = (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat()
    tool = f"/internal/calls/{cid}/tools"
    async def post(name, payload):
        response = await http.post(tool + "/" + name, headers=AGENT, json=payload)
        assert response.status_code == 200, response.text
        return response.json()
    args = {"date": day, "time": "10:00", "service_id": "check", "customer_name": "Test Caller"}
    assert (await post("book_appointment", args))["error"] == "confirmation_required"
    offer = await post("check_availability", {"when": day, "service_id": "check"})
    assert "10:00" in offer["free_times"]
    readback = await post("prepare_action", {"action": "book", **args})
    result = await post("book_appointment", {**args, "confirmation_id": readback["confirmation_id"], "confirmation_text": "ναι"})
    assert result["booked"]
    response = await http.post(f"/internal/calls/{cid}/events", headers=AGENT,
                               json={"status": "completed", "transcript_role": "friend", "transcript_text": "Ναι, σωστά."})
    assert response.status_code == 204, response.text
    await finalize.finalize(cid)
    detail = (await http.get(f"/practices/{a['id']}/calls/{cid}", headers=MASTER)).json()
    assert detail["status"] == "completed" and detail["outcome"] == "booked"
    assert detail["appointment"]["customer_name"] == "Test Caller"
    assert detail["summary"] == "Test summary."
    assert (await http.get(f"/practices/{b['id']}/appointments", headers=MASTER)).json() == []
    assert (await http.get(f"/practices/{b['id']}/calls/{cid}", headers=MASTER)).status_code == 404


@pytest.mark.asyncio
async def test_database_rejects_cross_tenant_references(sessions):
    from sqlalchemy.exc import IntegrityError
    from app.models import Customer, Message, Device, AdminLink, CalendarConnection
    async with sessions() as db:
        a,b = Practice(name="A"),Practice(name="B")
        db.add_all([a,b]); await db.flush()
        staff = Staff(practice_id=b.id,name="B")
        customer = Customer(practice_id=b.id,phone="+306900000001",name="B")
        call = Call(practice_id=b.id,persona="",scenario="")
        db.add_all([staff,customer,call]);await db.commit()
        aid,sid,cuid,cid=a.id,staff.id,customer.id,call.id
    constructors = [
        lambda: Call(practice_id=aid,customer_id=cuid,persona="",scenario=""),
        lambda: Message(practice_id=aid,call_id=cid,reason="Test"),
        lambda: Message(practice_id=aid,staff_id=sid,reason="Test"),
        lambda: Device(token="a"*64,practice_id=aid,staff_id=sid),
        lambda: CalendarConnection(practice_id=aid,staff_id=sid,google_email="a@example.com",calendar_id="a",refresh_token="test"),
    ]
    for constructor in constructors:
        async with sessions() as db:
            db.add(constructor())
            with pytest.raises(IntegrityError,match="_tenant"):
                await db.commit()


@pytest.mark.asyncio
async def test_sms_recipient_must_uniquely_identify_practice(sessions, monkeypatch):
    from app import admin_changes
    parse=AsyncMock()
    monkeypatch.setattr(admin_changes,"parse",parse)
    async with sessions() as db:
        a,b=Practice(name="A",phone_numbers=["+302100000001"]),Practice(name="B",phone_numbers=["+302100000002"])
        db.add_all([a,b]);await db.flush()
        db.add_all([Staff(practice_id=p.id,name="Owner",role="owner",phone="+306900000001") for p in (a,b)])
        await db.commit()
        for recipient in ("", "+302100000099"):
            assert await admin_changes.handle_sms(db,"+306900000001",recipient,"ναι",datetime.now(timezone.utc)) is None
        parse.assert_not_called()


@pytest.mark.asyncio
async def test_websocket_scope_is_checked_before_accept(http, sessions):
    import asyncio
    from contextlib import suppress
    a,b=await create(http,"Alpha"),await create(http,"Bravo")
    key,_=await key_for(http,a["id"])
    async with sessions() as db:
        call=Call(practice_id=b["id"],persona="",scenario="")
        db.add(call);await db.commit();cid=call.id
    async def connect(path, headers):
        outputs=asyncio.Queue()
        inputs=asyncio.Queue()
        await inputs.put({"type":"websocket.connect"})
        scope={"type":"websocket","asgi":{"version":"3.0"},"scheme":"ws","path":path,
               "raw_path":path.encode(),"query_string":b"","root_path":"",
               "headers":[(k.encode(),v.encode()) for k,v in headers.items()],
               "server":("test",80),"client":("127.0.0.1",1234),"subprotocols":[],
               "extensions":{"websocket.http.response":{}}}
        task=asyncio.create_task(app(scope,inputs.get,outputs.put))
        try:
            return await asyncio.wait_for(outputs.get(),3)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError): await task
    assert (await connect(f"/practices/{a['id']}/ws",key))["type"] == "websocket.accept"
    for path in (f"/practices/{b['id']}/ws",f"/calls/{cid}/ws"):
        result=await connect(path,key)
        assert result["type"] == "websocket.http.response.start" and result["status"] == 404
    assert (await connect(f"/practices/{a['id']}/ws",DIALER))["status"] == 403


@pytest.mark.asyncio
async def test_billing_configuration_and_status(http):
    practice = await create(http, "Billing")
    path = f"/practices/{practice['id']}/billing"
    unconfigured = await http.get(path, headers=MASTER)
    assert unconfigured.status_code == 200
    assert unconfigured.json()["paid_period_starts_on"] is None

    configured = await http.put(path, headers=MASTER, json={
        "pilot_started_on": "2030-01-25", "monthly_fee": "199.00",
    })
    assert configured.status_code == 200
    assert configured.json()["paid_period_starts_on"] == "2030-02-08"
    assert configured.json()["monthly_fee"] == "199.00"

    key, _ = await key_for(http, practice["id"])
    status = await http.get(path, headers=key)
    assert status.status_code == 200
    assert status.json() == configured.json()


@pytest.mark.asyncio
async def test_billing_guarantee_excludes_demo_other_tenant_and_duplicates(sessions):
    from decimal import Decimal
    from app import billing
    from app.models import Appointment, BillingAccount, BillingDraft
    from datetime import date
    async with sessions() as db:
        a,b=Practice(name="A",guarantee_threshold=2),Practice(name="B")
        db.add_all([a,b]);await db.flush()
        db.add(BillingAccount(practice_id=a.id,pilot_started_on=date(2030,1,1),monthly_fee=Decimal("199.00")))
        for i,(owner,direction) in enumerate(((a,"inbound"),(a,"web"),(b,"inbound"))):
            appt=Appointment(practice_id=owner.id,service_id="check",service_name="Check",
                             customer_name="Test",starts_at=datetime(2030,2,5,10+i,tzinfo=timezone.utc),
                             ends_at=datetime(2030,2,5,11+i,tzinfo=timezone.utc))
            db.add(appt);await db.flush()
            db.add(Call(practice_id=owner.id,persona="",scenario="",direction=direction,appointment_id=appt.id,
                        outcome="booked",status=CallStatus.completed,created_at=datetime(2030,2,1)))
        await db.commit()
        assert await billing.prepare_due(db,a,datetime(2030,2,1,tzinfo=timezone.utc)) == []
        drafts=await billing.prepare_due(db,a,datetime(2030,3,1,tzinfo=timezone.utc))
        assert len(drafts)==1 and drafts[0].bookings==1 and drafts[0].amount==0 and drafts[0].status=="waived"
        assert drafts[0].period_start == date(2030,1,15)
        assert drafts[0].period_end == date(2030,2,15)
        await db.commit()
        assert await billing.prepare_due(db,a,datetime(2030,3,1,tzinfo=timezone.utc)) == []
        assert (await db.execute(select(BillingDraft).where(BillingDraft.practice_id==b.id))).scalars().all()==[]
    assert billing.month_after(date(2030,1,31),1)==date(2030,2,28)
    assert billing.month_after(date(2030,1,31),2)==date(2030,3,31)


@pytest.mark.asyncio
async def test_public_demo_global_admission_is_atomic(http, monkeypatch):
    import asyncio
    from app import receptionist
    a=await create(http,"Alpha")
    monkeypatch.setattr(get_settings(),"max_concurrent_calls",1)
    monkeypatch.setattr(receptionist,"dispatch",AsyncMock())
    monkeypatch.setattr(receptionist,"room_token",lambda *args,**kwargs: "test-token")
    results=await asyncio.gather(*(http.post(f"/demo/{a['slug']}/session") for _ in range(2)))
    assert sorted(r.status_code for r in results)==[200,429]


@pytest.mark.asyncio
async def test_demo_voice_toggle_picks_the_call_voice(http, monkeypatch):
    from app import receptionist
    a=await create(http,"Alpha")
    dispatch=AsyncMock()
    monkeypatch.setattr(receptionist,"dispatch",dispatch)
    monkeypatch.setattr(receptionist,"room_token",lambda *args,**kwargs: "test-token")
    page=await http.get(f"/demo/{a['slug']}")
    assert 'data-default="female"' in page.text and "Ανδρική" in page.text
    assert (await http.post(f"/demo/{a['slug']}/session",json={"gender":"male"})).status_code==200
    assert dispatch.await_args.args[1]["voice"]=="Zubenelgenubi"


@pytest.mark.asyncio
async def test_speed_dial_calls_a_number_without_saving_it(http, monkeypatch):
    start = AsyncMock()
    monkeypatch.setattr("app.routers.calls.start_call", start)
    body = {"scenario": "Ρώτα αν είναι σπίτι.", "phone_number": "690 762 6384"}
    first = await http.post("/calls", headers=DIALER, json=body)
    assert first.status_code == 200, first.text
    friend = start.await_args.args[2]
    assert friend.phone_number == "+306907626384" and friend.name == "+306907626384"
    # Not added to the friends list, and the same unsaved entry is reused.
    assert (await http.get("/friends", headers=DIALER)).json() == []
    await http.post("/calls", headers=DIALER, json=body)
    assert start.await_args.args[2].id == friend.id
    # A saved friend with that number is used instead.
    saved = (await http.post("/friends", headers=DIALER, json={"name": "Νίκος", "phone_number": "+306907626384"})).json()
    await http.post("/calls", headers=DIALER, json=body)
    assert start.await_args.args[2].id == saved["id"]
    for bad in ({"scenario": "x"}, {"scenario": "x", "phone_number": "12"},
                {"scenario": "x", "phone_number": "+306907626384", "friend_id": saved["id"]}):
        assert (await http.post("/calls", headers=DIALER, json=bad)).status_code == 422


@pytest.mark.asyncio
async def test_sparring_page_is_secret_web_only(http, monkeypatch):
    import json
    from app.routers import sparring
    settings = get_settings()
    monkeypatch.setattr(settings, "sparring_token", "")
    assert (await http.get("/sparring/anything")).status_code == 404
    monkeypatch.setattr(settings, "sparring_token", "s3cret-token")
    assert (await http.get("/sparring/wrong")).status_code == 404
    page = await http.get("/sparring/s3cret-token")
    assert page.status_code == 200 and "Μπάμπη" in page.text and 'data-default="male"' in page.text

    dispatched = []

    class FakeLK:
        def __init__(self, *a): self.agent_dispatch = self
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def create_dispatch(self, request): dispatched.append(request)

    monkeypatch.setattr(sparring.api, "LiveKitAPI", FakeLK)
    monkeypatch.setattr("app.receptionist.room_token", lambda *a, **k: "tok")
    r = await http.post("/sparring/s3cret-token/session", json={"gender": "female"})
    assert r.status_code == 200, r.text
    meta = json.loads(dispatched[0].metadata)
    # Never dials or records, and never shows up in the app's call history.
    assert meta["test_no_dial"] and meta["sparring"] and "phone" not in json.dumps(meta).lower()
    assert meta["voice"] == "Kore" and "νταής" in meta["prompt"]
    assert dispatched[0].room == f"spar-{meta['call_id']}"
    assert (await http.get("/calls", headers=DIALER)).json() == []
