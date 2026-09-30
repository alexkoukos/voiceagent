"""Measured per-call cost from reported usage and the versioned price list.

Unknown prices stay unknown: a line without a rate has cost_usd None and makes the
call's breakdown incomplete, so totals are then a lower bound ("known_usd").
"""

import json
from datetime import datetime

from sqlalchemy import select

from app.config import CONFIG_DIR
from app.models import Call, CallTelemetryEvent

PRICING = json.loads((CONFIG_DIR / "pricing.json").read_text(encoding="utf-8"))
TOKEN_FIELDS = {  # usage field -> (specific rate, generic fallback rate)
    "input_text_tokens": ("input_text", "input"), "input_audio_tokens": ("input_audio", "input"),
    "output_text_tokens": ("output_text", "output"), "output_audio_tokens": ("output_audio", "output"),
}


def version_at(when: datetime) -> dict:
    versions = sorted(PRICING["versions"], key=lambda v: v["effective_from"])
    chosen = versions[0]
    for version in versions:
        if version["effective_from"] <= when.strftime("%Y-%m-%d"):
            chosen = version
    return chosen


def _rate_entry(version: dict, usage: dict) -> dict | None:
    for entry in version["models"]:
        if entry["type"] == usage["type"] and entry["model_contains"].lower() in (
                f"{usage['provider']}/{usage['model']}".lower()):
            return entry
    return None


def price_usage(version: dict, usage: dict) -> dict:
    line = {"component": usage["type"].removesuffix("_usage"), "provider": usage["provider"],
            "model": usage["model"], "quantity": {k: v for k, v in usage.items()
                                                  if k not in ("type", "provider", "model")},
            "cost_usd": None}
    entry = _rate_entry(version, usage)
    if entry is None:
        line["unpriced"] = "no rate for this model"
        return line
    if usage["type"] == "llm_usage":
        rates = entry["usd_per_million"]
        detailed = [f for f in TOKEN_FIELDS if usage.get(f)]
        parts = ([(usage[f], rates.get(TOKEN_FIELDS[f][0], rates.get(TOKEN_FIELDS[f][1]))) for f in detailed]
                 if detailed else
                 [(usage.get("input_tokens", 0), rates.get("input")), (usage.get("output_tokens", 0), rates.get("output"))])
        if any(rate is None and count for count, rate in parts):
            line["unpriced"] = "missing token rate"
            return line
        line["cost_usd"] = sum(count * (rate or 0) for count, rate in parts) / 1_000_000
    elif usage["type"] == "stt_usage":
        if entry.get("usd_per_minute") is None:
            line["unpriced"] = "unknown rate"
            return line
        line["cost_usd"] = usage.get("audio_duration", 0) / 60 * entry["usd_per_minute"]
    elif usage["type"] == "tts_usage":
        if entry.get("usd_per_1k_characters") is None:
            line["unpriced"] = "unknown rate"
            return line
        line["cost_usd"] = usage.get("characters_count", 0) / 1000 * entry["usd_per_1k_characters"]
    else:
        if entry.get("usd_per_request") is None:
            line["unpriced"] = "unknown rate"
            return line
        line["cost_usd"] = usage.get("total_requests", 0) * entry["usd_per_request"]
    return line


def breakdown(call: Call, usage: list[dict]) -> dict:
    version = version_at(call.created_at)
    lines = [price_usage(version, item) for item in usage]
    minutes = call.duration_seconds / 60 if call.duration_seconds is not None else None
    telephony_rate = version["telephony_usd_per_minute"].get(call.direction)
    for component, rate in (("telephony", telephony_rate), ("livekit", version.get("livekit_usd_per_minute"))):
        if component == "telephony" and call.direction == "web":
            continue
        line = {"component": component, "provider": "telnyx" if component == "telephony" else "livekit",
                "model": call.direction, "quantity": {"minutes": minutes}, "cost_usd": None}
        if minutes is None:
            line["unpriced"] = "call not ended"
        elif rate is None:
            line["unpriced"] = "unknown rate"
        else:
            line["cost_usd"] = minutes * rate
        lines.append(line)
    known = sum(line["cost_usd"] or 0 for line in lines)
    for line in lines:
        if line["cost_usd"] is not None:
            line["cost_usd"] = round(line["cost_usd"], 6)
    return {"pricing_version": version["id"], "currency": "USD", "lines": lines,
            "known_usd": round(known, 6), "complete": all(line["cost_usd"] is not None for line in lines),
            "usage_reported": bool(usage)}


async def recompute(db, call: Call) -> None:
    """Idempotent: rebuilds from every session's usage report for this call."""
    payloads = (await db.execute(select(CallTelemetryEvent.payload).where(
        CallTelemetryEvent.call_id == call.id,
        CallTelemetryEvent.payload["event"].as_string() == "usage_reported",
    ))).scalars().all()
    usage = [line for payload in payloads for line in payload.get("usage", [])]
    call.cost_breakdown = breakdown(call, usage)
