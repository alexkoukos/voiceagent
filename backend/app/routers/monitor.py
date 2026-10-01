"""Founder-only live call monitor: response times, stage timings and the call's log.

The page itself carries no data; the browser sends the founder key on each request.
"""

from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_master_token
from app.database import get_db
from app.models import Call, CallTelemetryEvent, Practice, RoutingEvent, TranscriptEntry

router = APIRouter(tags=["monitor"])
PAGE = (Path(__file__).resolve().parents[1] / "templates" / "monitor.html").read_text(encoding="utf-8")
NO_STORE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer"}


def _time(value):
    return value.isoformat() + "Z" if value else None


def percentile(values, share):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(share * (len(ordered) - 1)))]


@router.get("/monitor", response_class=HTMLResponse, include_in_schema=False)
async def monitor_page():
    return HTMLResponse(PAGE, headers=NO_STORE)


@router.get("/monitor/api/calls", dependencies=[Depends(require_master_token)])
async def recent_calls(limit: int = Query(default=25, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(Call, Practice.name).outerjoin(Practice, Practice.id == Call.practice_id)
        .order_by(Call.created_at.desc()).limit(limit))).all()
    ids = [call.id for call, _ in rows]
    latencies: dict[str, list[float]] = {}
    if ids:
        events = (await db.execute(select(CallTelemetryEvent.call_id, CallTelemetryEvent.payload).where(
            CallTelemetryEvent.call_id.in_(ids),
            CallTelemetryEvent.payload["event"].as_string() == "response_latency_estimate",
        ))).all()
        for call_id, payload in events:
            latencies.setdefault(call_id, []).append(payload["latency_ms"])
    result = []
    for call, practice in rows:
        values = latencies.get(call.id, [])
        result.append({
            "id": call.id, "practice": practice, "direction": call.direction,
            "status": call.status.value, "outcome": call.outcome, "end_reason": call.end_reason,
            "created_at": _time(call.created_at), "started_at": _time(call.started_at),
            "ended_at": _time(call.ended_at), "duration_seconds": call.duration_seconds,
            "turns": len(values), "p50_ms": percentile(values, 0.5), "p95_ms": percentile(values, 0.95),
            "cost_usd": (call.cost_breakdown or {}).get("known_usd"),
            "cost_complete": (call.cost_breakdown or {}).get("complete"),
        })
    return {"calls": result}


@router.get("/monitor/api/calls/{call_id}", dependencies=[Depends(require_master_token)])
async def call_live(call_id: str, after: int = Query(default=0, ge=0), db: AsyncSession = Depends(get_db)):
    """Call state, full transcript and routing (small), plus telemetry after the cursor."""
    call = await db.get(Call, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    practice = await db.get(Practice, call.practice_id) if call.practice_id else None
    transcript = (await db.execute(select(TranscriptEntry).where(TranscriptEntry.call_id == call_id)
                                   .order_by(TranscriptEntry.created_at))).scalars().all()
    routing = (await db.execute(select(RoutingEvent).where(RoutingEvent.call_id == call_id)
                                .order_by(RoutingEvent.created_at))).scalars().all()
    rows = (await db.execute(select(CallTelemetryEvent).where(
        CallTelemetryEvent.call_id == call_id, CallTelemetryEvent.id > after,
    ).order_by(CallTelemetryEvent.id).limit(1000))).scalars().all()
    return {
        "call": {
            "id": call.id, "practice": practice.name if practice else None, "direction": call.direction,
            "status": call.status.value, "outcome": call.outcome, "end_reason": call.end_reason,
            "flags": call.flags or [], "language": call.language,
            "created_at": _time(call.created_at), "started_at": _time(call.started_at),
            "ended_at": _time(call.ended_at), "duration_seconds": call.duration_seconds,
            "cost_breakdown": call.cost_breakdown,
        },
        "transcript": [{"role": entry.role.value, "text": entry.text, "at": _time(entry.created_at)}
                       for entry in transcript],
        "routing": [{"kind": item.kind, "value": item.value, "rule": item.rule, "at": _time(item.created_at)}
                    for item in routing],
        "events": [{"cursor": row.id, "received_at": _time(row.received_at), **row.payload} for row in rows],
        "next_after": rows[-1].id if rows else after,
    }


SUCCESS = {"booked", "rescheduled", "cancelled", "confirmed", "info_given", "message_taken", "transferred"}
STAGES = ("transcription_delay_ms", "end_of_turn_delay_ms", "llm_node_ttft_ms", "tts_node_ttfb_ms", "e2e_latency_ms")


def spread(values):
    return {"n": len(values), "p50": percentile(values, 0.5), "p95": percentile(values, 0.95),
            "p99": percentile(values, 0.99)}


@router.get("/monitor/api/summary", dependencies=[Depends(require_master_token)])
async def summary(days: int = Query(default=7, ge=1, le=90), db: AsyncSession = Depends(get_db)):
    """The small ASTRA dashboard: volume, outcomes, latency spread and cost for a window."""
    since = datetime.utcnow() - timedelta(days=days)
    calls = (await db.execute(select(Call).where(Call.created_at >= since))).scalars().all()
    ids = [call.id for call in calls]
    replies, tools, stages = [], [], {name: [] for name in STAGES}
    if ids:
        events = (await db.execute(select(CallTelemetryEvent.payload).where(
            CallTelemetryEvent.call_id.in_(ids),
            CallTelemetryEvent.payload["event"].as_string().in_(
                ["response_latency_estimate", "tool_request_ended", "message_metrics"]),
        ))).scalars().all()
        for event in events:
            if event["event"] == "response_latency_estimate":
                replies.append(event["latency_ms"])
            elif event["event"] == "tool_request_ended":
                tools.append(event["duration_ms"])
            else:
                for name in STAGES:
                    if name in (event.get("latencies") or {}):
                        stages[name].append(event["latencies"][name])
    outcomes = Counter(call.outcome or call.status.value for call in calls)
    minutes = sum(call.duration_seconds or 0 for call in calls) / 60
    successes = sum(outcomes[name] for name in SUCCESS)
    priced = [call.cost_breakdown for call in calls if call.cost_breakdown]
    known = sum(item["known_usd"] for item in priced)
    by_component = Counter()
    for item in priced:
        for line in item["lines"]:
            by_component[line["component"]] += line["cost_usd"] or 0
    unpriced = sorted({f"{line['component']} {line['provider']}/{line['model']}: {line['unpriced']}"
                       for item in priced for line in item["lines"] if line.get("unpriced")})
    return {
        "days": days, "calls": len(calls), "minutes": round(minutes, 1),
        "outcomes": dict(outcomes), "successful": successes,
        "failed": sum(1 for call in calls if call.status.value == "failed" or call.outcome == "failed"),
        "transfers": outcomes["transferred"], "appointments": outcomes["booked"],
        "tool_errors": sum(1 for call in calls if "tool_error" in (call.flags or [])),
        "reply_ms": spread(replies), "tool_ms": spread(tools),
        "stages_ms": {name.removesuffix("_ms"): spread(values) for name, values in stages.items()},
        "cost": {
            "currency": "USD", "known_total": round(known, 4), "calls_priced": len(priced),
            "calls_complete": sum(1 for item in priced if item["complete"]),
            "per_call": round(known / len(priced), 4) if priced else None,
            "per_minute": round(known / minutes, 4) if minutes and priced else None,
            "per_successful_outcome": round(known / successes, 4) if successes and priced else None,
            "by_component": {k: round(v, 4) for k, v in by_component.items()},
            "unpriced": unpriced,
        },
    }
