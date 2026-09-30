"""Real Postgres ingestion, authorization, retry and lifecycle tests."""

import uuid
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import select

from app.config import get_settings
from app.database import get_db
from app.models import Call, CallStatus, CallTelemetryEvent, Practice
from app.scheduler import retention
from app.routers.internal import router
from app.telemetry import TelemetryBatch

AGENT = {"x-agent-token": "test-agent"}
FOUNDER = {"x-api-key": "test-founder"}


def event(call_id, sequence=1, session_id=None, **overrides):
    return {"schema_version": 1, "event": "session_observation_started",
            "call_id": call_id, "session_id": session_id or str(uuid.uuid4()),
            "agent_version": "test", "engine": "pipeline", "sequence": sequence,
            "observed_at_unix_ns": 1_790_000_000_123_456_789,
            "elapsed_ns": 12345, "turn_id": None, **overrides}


@pytest_asyncio.fixture
async def telemetry_http(sessions, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "internal_api_token", "test-agent")
    monkeypatch.setattr(settings, "admin_api_token", "test-founder")
    monkeypatch.setattr(settings, "app_api_token", "test-dialer")
    app = FastAPI()
    app.include_router(router)
    async def db():
        async with sessions() as session:
            yield session
    app.dependency_overrides[get_db] = db
    async with sessions() as db:
        call = Call(persona="", scenario="", status=CallStatus.completed)
        db.add(call)
        await db.commit()
        call_id = call.id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, call_id


@pytest.mark.asyncio
async def test_telemetry_deduplication_pagination_and_precision(telemetry_http):
    http, cid = telemetry_http
    path = f"/internal/calls/{cid}/telemetry"
    first = event(cid, 2)
    second = event(cid, 1, first["session_id"])
    batch = {"events": [first, second, first]}
    response = await http.post(path, headers=AGENT, json=batch)
    assert response.status_code == 200, response.text
    assert response.json()["inserted"] == 2
    assert (await http.post(path, headers=AGENT, json=batch)).json()["inserted"] == 0
    page = (await http.get(path, headers=FOUNDER, params={"limit": 1})).json()
    assert page["has_more"]
    assert page["events"][0]["sequence"] == 2  # retrieval is ingestion order
    assert page["events"][0]["observed_at_unix_ns"] == first["observed_at_unix_ns"]
    next_page = (await http.get(path, headers=FOUNDER, params={"after": page["next_after"]})).json()
    assert not next_page["has_more"]
    assert [e["sequence"] for e in next_page["events"]] == [1]
    empty = (await http.get(path, headers=FOUNDER, params={"after": next_page["next_after"]})).json()
    assert empty["events"] == []
    assert empty["next_after"] == next_page["next_after"]


@pytest.mark.asyncio
async def test_telemetry_auth_and_call_boundary(telemetry_http):
    http, cid = telemetry_http
    path = f"/internal/calls/{cid}/telemetry"
    for headers in ({}, FOUNDER, {"x-api-key": "test-dialer"}, {"x-api-key": "tenant-key"}):
        assert (await http.post(path, headers=headers, json={"events": [event(cid)]})).status_code == 401
    for headers in ({}, AGENT, {"x-api-key": "test-dialer"}, {"x-api-key": "tenant-key"}):
        assert (await http.get(path, headers=headers)).status_code == 403
    assert (await http.post(path, headers=AGENT, json={"events": [event("other-call")]})).status_code == 422
    assert (await http.post("/internal/calls/missing/telemetry", headers=AGENT,
                            json={"events": [event("missing")]})).status_code == 404
    assert (await http.get("/internal/calls/missing/telemetry", headers=FOUNDER)).status_code == 404
    assert (await http.get(path, headers=FOUNDER, params={"limit": 501})).status_code == 422
    assert (await http.get(path, headers=FOUNDER)).json()["events"] == []


@pytest.mark.asyncio
async def test_erasure_removes_telemetry_and_discards_late_uploads(telemetry_http):
    http, cid = telemetry_http
    path = f"/internal/calls/{cid}/telemetry"
    payload = {"events": [event(cid)]}
    assert (await http.post(path, headers=AGENT, json=payload)).json()["inserted"] == 1
    response = await http.post(f"/internal/calls/{cid}/events", headers=AGENT, json={"delete_recording": True})
    assert response.status_code == 204
    assert (await http.get(path, headers=FOUNDER)).json()["events"] == []
    assert (await http.post(path, headers=AGENT, json=payload)).json() == {"inserted": 0, "discarded": 1}


@pytest.mark.asyncio
async def test_call_deletion_cascades_telemetry(telemetry_http, sessions):
    http, cid = telemetry_http
    await http.post(f"/internal/calls/{cid}/telemetry", headers=AGENT, json={"events": [event(cid)]})
    async with sessions() as db:
        await db.delete(await db.get(Call, cid))
        await db.commit()
        assert (await db.execute(select(CallTelemetryEvent))).scalars().all() == []


@pytest.mark.parametrize("overrides", [
    {"transcript": "private caller text"}, {"sequence": 0}, {"schema_version": 2},
    {"latencies": {"secret": 1}}, {"latency_ms": float("inf")},
    {"event": "tool_request_ended"}, {"agent_version": "x" * 129},
])
def test_schema_rejects_content_and_invalid_measurements(overrides):
    with pytest.raises(ValidationError):
        TelemetryBatch.model_validate({"events": [event("call", **overrides)]})


def test_batch_size_is_bounded():
    for records in ([], [event("call")] * 51):
        with pytest.raises(ValidationError):
            TelemetryBatch.model_validate({"events": records})


@pytest.mark.asyncio
async def test_retention_removes_events_and_rejects_expired_uploads(telemetry_http, sessions):
    http, cid = telemetry_http
    path = f"/internal/calls/{cid}/telemetry"
    payload = {"events": [event(cid)]}
    await http.post(path, headers=AGENT, json=payload)
    async with sessions() as db:
        practice = Practice(name="Test", retention_transcripts_days=1)
        db.add(practice)
        await db.flush()
        call = await db.get(Call, cid)
        call.practice_id = practice.id
        call.created_at = datetime.utcnow() - timedelta(days=2)
        await db.commit()
        await retention(db, practice)
        await db.commit()
    assert (await http.get(path, headers=FOUNDER)).json()["events"] == []
    assert (await http.post(path, headers=AGENT, json=payload)).json()["discarded"] == 1


@pytest.mark.asyncio
async def test_legacy_call_telemetry_expires_after_30_days(telemetry_http, sessions):
    http, cid = telemetry_http
    async with sessions() as db:
        call = await db.get(Call, cid)
        call.created_at = datetime.utcnow() - timedelta(days=31)
        await db.commit()
    response = await http.post(f"/internal/calls/{cid}/telemetry", headers=AGENT,
                               json={"events": [event(cid)]})
    assert response.json() == {"inserted": 0, "discarded": 1}


@pytest.mark.asyncio
async def test_real_worker_payloads_upload_and_can_be_retrieved(telemetry_http):
    http, cid = telemetry_http
    source = Path(__file__).resolve().parents[2] / "agent" / "telemetry.py"
    spec = importlib.util.spec_from_file_location("worker_telemetry", source)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    uploader = worker.TelemetryUploader(cid, "http://test", "test-agent", client=http)
    uploader.start()
    tracker = worker.CallTelemetry(cid, "pipeline", sink=uploader.enqueue)
    tracker.attach(SimpleNamespace(on=lambda *_args: None))
    tracker.user_state(SimpleNamespace(old_state="listening", new_state="speaking"))
    tracker.user_state(SimpleNamespace(old_state="speaking", new_state="listening"))
    tracker.agent_state(SimpleNamespace(old_state="thinking", new_state="speaking"))
    tracker.message(SimpleNamespace(item=SimpleNamespace(
        type="message", id="message-1", role="assistant", content="PRIVATE", metrics={"llm_node_ttft": .1})))
    tracker.tools_completed(SimpleNamespace(zipped=lambda: [(
        SimpleNamespace(call_id="sdk-tool-1", name="book_appointment", arguments="PRIVATE"),
        SimpleNamespace(is_error=False, output="PRIVATE"),
    )]))
    with worker.observe_span(tracker, "tool_request", tool_name="book_appointment", source="worker_http"):
        pass
    with worker.observe_span(tracker, "transfer", handoff_id="handoff-1", mode="sip", source="worker_transfer") as span:
        span.finish("joined")
    tracker.finish()
    await uploader.close()
    assert uploader.dropped == 0
    response = await http.get(f"/internal/calls/{cid}/telemetry", headers=FOUNDER)
    events = response.json()["events"]
    assert len(events) == 12
    assert events[-1]["event"] == "session_observation_ended"
    assert [e["sequence"] for e in events] == list(range(1, 13))
    assert "PRIVATE" not in response.text
