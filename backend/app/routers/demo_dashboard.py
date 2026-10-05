"""Shareable, revocable telemetry for calls started through one demo link."""
import hashlib
import math
import re
import secrets
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from string import Template
from statistics import median

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_master_token
from app.database import get_db
from app.models import Call, CallTelemetryEvent, DemoDashboard, Practice
from app.routers import demo
from app.routers.monitor import NO_STORE, STAGES, percentile as ranked_percentile

router = APIRouter(tags=["demo-dashboard"])
PAGE = Template((Path(__file__).resolve().parents[1] / "templates" / "demo-dashboard.html").read_text())
COMPONENTS = {"llm", "stt", "tts", "livekit", "eot", "interruption"}
OUTCOMES = {"booked", "rescheduled", "cancelled", "confirmed", "info_given", "message_taken",
            "transferred", "abandoned", "failed"}
RESOLVED = OUTCOMES - {"transferred", "abandoned", "failed"}
EVENTS = ["answer_latency_estimate", "response_latency_estimate", "message_metrics", "tool_request_ended"]


def percentile(values, share):
    if share == .5:
        return median(values) if values else None
    return ranked_percentile(values, share)


class LinkIn(BaseModel):
    expires_in_days: int | None = Field(default=7, ge=1, le=30)


def iso(value):
    return value.isoformat() + "Z" if value else None


def demo_only(practice):
    return (practice is not None and not practice.offboarded_at
            and (practice.routing_rules or {}).get("demo_only") is True)


async def link_context(token: str, db: AsyncSession):
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise HTTPException(404, "Demo link unavailable", headers=NO_STORE)
    link = (await db.execute(select(DemoDashboard).where(
        DemoDashboard.token_hash == hashlib.sha256(token.encode()).hexdigest(),
        DemoDashboard.revoked_at.is_(None),
        or_(DemoDashboard.expires_at.is_(None), DemoDashboard.expires_at > datetime.utcnow()),
    ))).scalar_one_or_none()
    practice = await db.get(Practice, link.practice_id) if link else None
    if not demo_only(practice):
        raise HTTPException(404, "Demo link unavailable", headers=NO_STORE)
    return link, practice


@router.post("/practices/{practice_id}/demo-dashboard", dependencies=[Depends(require_master_token)])
async def create_link(practice_id: str, payload: LinkIn, db: AsyncSession = Depends(get_db)):
    practice = await db.get(Practice, practice_id)
    if not demo_only(practice):
        raise HTTPException(409, "A dedicated practice marked routing_rules.demo_only is required")
    token = secrets.token_urlsafe(32)
    link = DemoDashboard(practice_id=practice.id, token_hash=hashlib.sha256(token.encode()).hexdigest(),
                         expires_at=(datetime.utcnow() + timedelta(days=payload.expires_in_days)
                                     if payload.expires_in_days is not None else None))
    db.add(link)
    await db.commit()
    return JSONResponse({"id": link.id, "path": f"/demo-dashboard/{token}",
                         "expires_at": iso(link.expires_at)}, headers=NO_STORE)


@router.patch("/practices/{practice_id}/demo-dashboard/{link_id}", dependencies=[Depends(require_master_token)])
async def update_link(practice_id: str, link_id: str, payload: LinkIn, db: AsyncSession = Depends(get_db)):
    link = await db.get(DemoDashboard, link_id)
    practice = await db.get(Practice, practice_id)
    if link is None or link.practice_id != practice_id or link.revoked_at or not demo_only(practice):
        raise HTTPException(404, "Not found")
    link.expires_at = (datetime.utcnow() + timedelta(days=payload.expires_in_days)
                       if payload.expires_in_days is not None else None)
    await db.commit()
    return JSONResponse({"id": link.id, "expires_at": iso(link.expires_at)}, headers=NO_STORE)


@router.delete("/practices/{practice_id}/demo-dashboard/{link_id}", dependencies=[Depends(require_master_token)])
async def revoke_link(practice_id: str, link_id: str, db: AsyncSession = Depends(get_db)):
    link = await db.get(DemoDashboard, link_id)
    if link is None or link.practice_id != practice_id:
        raise HTTPException(404, "Not found")
    link.revoked_at = datetime.utcnow()
    await db.commit()
    return Response(status_code=204, headers=NO_STORE)


@router.get("/demo-dashboard/{token}", response_class=HTMLResponse, include_in_schema=False)
async def page(token: str, db: AsyncSession = Depends(get_db)):
    link, practice = await link_context(token, db)
    return HTMLResponse(PAGE.substitute(name=escape(practice.name), expires=escape("Expires " + iso(link.expires_at) if link.expires_at else "No automatic expiry"),
                                        call_path=f"/demo-dashboard/{token}/call"), headers=NO_STORE)


@router.get("/demo-dashboard/{token}/call", response_class=HTMLResponse, include_in_schema=False)
async def call_page(token: str, db: AsyncSession = Depends(get_db)):
    _, practice = await link_context(token, db)
    return HTMLResponse(await demo.demo_page(practice.slug, db, embed=True), headers=NO_STORE)


@router.post("/demo-dashboard/{token}/call/session")
async def call_session(token: str, payload: demo.DemoSessionRequest | None = None, db: AsyncSession = Depends(get_db)):
    link, practice = await link_context(token, db)
    result = await demo.start_demo_session(practice, db, dashboard_id=link.id, voice=payload.voice if payload else None)
    return JSONResponse(result, headers=NO_STORE)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def summarize(calls, events):
    """Numbers and fixed labels only. No raw payloads, transcripts or patient details."""
    timings = defaultdict(lambda: {"answer": [], "sound": [], "tool": [], "stages": defaultdict(list)})
    allowed = {call.id for call in calls}
    for call_id, payload in events:
        if call_id not in allowed:
            continue
        timing = timings[call_id]
        kind = payload.get("event")
        if kind in {"answer_latency_estimate", "response_latency_estimate"} and number(payload.get("latency_ms")):
            timing["answer" if kind == "answer_latency_estimate" else "sound"].append(payload["latency_ms"])
        elif kind == "tool_request_ended" and number(payload.get("duration_ms")):
            timing["tool"].append(payload["duration_ms"])
        elif kind == "message_metrics":
            for key, value in (payload.get("latencies") or {}).items():
                if key in STAGES and number(value):
                    timing["stages"][key].append(value)
    answers, sounds, tools, stages = [], [], [], defaultdict(list)
    components = Counter()
    unknown = set()
    versions = set()
    cost_total = 0
    priced_minutes = 0
    priced = complete = 0
    public_calls = []
    outcomes = Counter()
    finished = 0
    for index, call in enumerate(calls):
        timing = timings[call.id]
        answers += timing["answer"]
        sounds += timing["sound"]
        tools += timing["tool"]
        for name, values in timing["stages"].items():
            stages[name] += values
        ended = call.status.value in {"completed", "failed", "cancelled"}
        finished += ended
        outcome = call.outcome if call.outcome in OUTCOMES else "pending" if not ended else "unclassified"
        outcomes[outcome] += 1
        cost = call.cost_breakdown or {}
        has_cost = bool(cost.get("usage_reported")) and number(cost.get("known_usd"))
        full_cost = has_cost and cost.get("complete") is True and ended
        if has_cost:
            priced += 1
            complete += full_cost
            cost_total += cost["known_usd"]
            if number(call.duration_seconds):
                priced_minutes += call.duration_seconds / 60
            if cost.get("pricing_version"):
                versions.add(str(cost["pricing_version"]))
            for line in cost.get("lines", []):
                component = line.get("component")
                if component not in COMPONENTS:
                    continue
                if number(line.get("cost_usd")):
                    components[component] += line["cost_usd"]
                else:
                    unknown.add(component)
        public_calls.append({
            "label": f"Demo call {len(calls) - index}", "status": call.status.value,
            "outcome": outcome, "created_at": iso(call.created_at),
            "duration_seconds": call.duration_seconds, "answer_ms": percentile(timing["answer"], .5),
            "answer_samples": len(timing["answer"]),
            "known_usd": cost.get("known_usd") if has_cost else None,
            "cost_complete": bool(full_cost),
        })
    resolved = sum(outcomes[k] for k in RESOLVED)
    return {
        "calls": public_calls, "count": len(calls), "finished": finished,
        "active": sum(call.status.value == "active" for call in calls),
        "bookings": outcomes["booked"], "outcomes": dict(outcomes),
        "resolved_pct": round(100 * resolved / finished, 1) if finished else None,
        "answer": {"p50_ms": percentile(answers, .5), "p95_ms": percentile(answers, .95), "samples": len(answers)},
        "first_sound_ms": percentile(sounds, .5), "tool_ms": percentile(tools, .5),
        "stages": {key: percentile(stages[key], .5) for key in STAGES},
        "cost": {"known_usd": round(cost_total, 6) if priced else None,
                 "per_call_usd": round(cost_total / priced, 6) if priced else None,
                 "per_minute_usd": round(cost_total / priced_minutes, 6) if priced_minutes and complete == priced else None,
                 "reported_calls": priced, "complete_calls": complete,
                 "components": {key: round(value, 6) for key, value in components.items()},
                 "unpriced_components": sorted(unknown), "versions": sorted(versions)},
    }


@router.get("/demo-dashboard/{token}/data")
async def data(token: str, db: AsyncSession = Depends(get_db)):
    link, practice = await link_context(token, db)
    # A matching practice or slug alone never grants access to earlier/phone calls.
    calls = list((await db.execute(select(Call).where(
        Call.demo_dashboard_id == link.id, Call.practice_id == practice.id,
        Call.direction == "web", Call.delete_requested.is_(False),
    ).order_by(Call.created_at.desc()).limit(101))).scalars())
    more = len(calls) > 100
    calls = calls[:100]
    events = []
    if calls:
        events = list((await db.execute(select(CallTelemetryEvent.call_id, CallTelemetryEvent.payload).where(
            CallTelemetryEvent.call_id.in_([call.id for call in calls]),
            CallTelemetryEvent.payload["event"].as_string().in_(EVENTS),
        ).order_by(CallTelemetryEvent.id.desc()).limit(10001))).all())
    result = summarize(calls, list(reversed(events[:10000])))
    return JSONResponse({**result, "updated_at": iso(datetime.utcnow()), "expires_at": iso(link.expires_at),
                         "older_calls_omitted": more, "timings_limited": len(events) > 10000}, headers=NO_STORE)
