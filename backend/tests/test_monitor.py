"""Founder-only live monitor endpoints against real Postgres."""

import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.config import get_settings
from app.database import get_db
from app.models import Call, CallStatus, CallTelemetryEvent, TranscriptEntry, TranscriptRole
from app.routers.monitor import percentile, router

FOUNDER = {"x-api-key": "test-founder"}


def payload(call_id, sequence, event, **fields):
    return {"schema_version": 1, "event": event, "call_id": call_id, "session_id": str(uuid.uuid4()),
            "agent_version": "test", "engine": "pipeline", "sequence": sequence,
            "observed_at_unix_ns": 1_790_000_000_000_000_000 + sequence, "elapsed_ns": sequence, **fields}


@pytest_asyncio.fixture
async def monitor_http(sessions, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_api_token", "test-founder")
    monkeypatch.setattr(settings, "app_api_token", "test-dialer")
    app = FastAPI()
    app.include_router(router)

    async def db():
        async with sessions() as session:
            yield session
    app.dependency_overrides[get_db] = db
    async with sessions() as db:
        call = Call(persona="", scenario="", status=CallStatus.active, direction="web")
        db.add(call)
        await db.flush()
        db.add(TranscriptEntry(call_id=call.id, role=TranscriptRole.friend, text="Γεια σας"))
        for sequence, latency in enumerate([800, 1200, 3000], start=1):
            db.add(CallTelemetryEvent(call_id=call.id, session_id=str(uuid.uuid4()), sequence=sequence,
                                      payload=payload(call.id, sequence, "response_latency_estimate",
                                                      latency_ms=latency)))
        db.add(CallTelemetryEvent(call_id=call.id, session_id=str(uuid.uuid4()), sequence=4,
                                  payload=payload(call.id, 4, "caller_speech_started")))
        await db.commit()
        call_id = call.id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, call_id


def test_percentile_is_nearest_rank():
    assert percentile([], 0.5) is None
    assert percentile([3, 1, 2], 0.5) == 2
    assert percentile([1, 2, 3, 4], 0.95) == 4


@pytest.mark.asyncio
async def test_monitor_requires_founder_key(monitor_http):
    http, call_id = monitor_http
    for headers in ({}, {"x-api-key": "test-dialer"}):
        assert (await http.get("/monitor/api/calls", headers=headers)).status_code == 403
        assert (await http.get(f"/monitor/api/calls/{call_id}", headers=headers)).status_code == 403
    page = await http.get("/monitor")
    assert page.status_code == 200 and "Live call monitor" in page.text
    assert page.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_monitor_lists_latency_and_streams_by_cursor(monitor_http):
    http, call_id = monitor_http
    calls = (await http.get("/monitor/api/calls", headers=FOUNDER)).json()["calls"]
    assert calls[0]["id"] == call_id
    assert (calls[0]["turns"], calls[0]["p50_ms"], calls[0]["p95_ms"]) == (3, 1200, 3000)
    live = (await http.get(f"/monitor/api/calls/{call_id}", headers=FOUNDER)).json()
    assert live["call"]["status"] == "active"
    assert live["transcript"] == [{"role": "friend", "text": "Γεια σας", "at": live["transcript"][0]["at"]}]
    assert len(live["events"]) == 4
    later = (await http.get(f"/monitor/api/calls/{call_id}", headers=FOUNDER,
                            params={"after": live["next_after"]})).json()
    assert later["events"] == [] and later["next_after"] == live["next_after"]
    assert (await http.get("/monitor/api/calls/missing", headers=FOUNDER)).status_code == 404


@pytest.mark.asyncio
async def test_summary_reports_spread_outcomes_and_unknown_costs(monitor_http, sessions):
    http, call_id = monitor_http
    async with sessions() as db:
        call = await db.get(Call, call_id)
        call.outcome, call.duration_seconds = "booked", 120
        call.cost_breakdown = {"known_usd": 0.5, "complete": False, "lines": [
            {"component": "llm", "provider": "g", "model": "m", "cost_usd": 0.5},
            {"component": "livekit", "provider": "livekit", "model": "web", "cost_usd": None, "unpriced": "unknown rate"}]}
        await db.commit()
    assert (await http.get("/monitor/api/summary")).status_code == 403
    data = (await http.get("/monitor/api/summary", headers=FOUNDER, params={"days": 1})).json()
    assert data["calls"] == 1 and data["successful"] == 1 and data["appointments"] == 1
    assert data["reply_ms"] == {"n": 3, "p50": 1200, "p95": 3000, "p99": 3000}
    assert data["cost"]["per_call"] == 0.5 and data["cost"]["per_minute"] == 0.25
    assert data["cost"]["calls_complete"] == 0
    assert data["cost"]["unpriced"] == ["livekit livekit/web: unknown rate"]
