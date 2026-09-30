"""Cost breakdown from reported usage and the versioned price list."""

import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import costs
from app.models import Call, CallStatus, CallTelemetryEvent

GEMINI_LIVE = {"type": "llm_usage", "provider": "Google", "model": "gemini-3.8-live",
               "input_text_tokens": 1_000_000, "input_audio_tokens": 1_000_000,
               "output_text_tokens": 0, "output_audio_tokens": 1_000_000}


def call(direction="inbound", seconds=120):
    return SimpleNamespace(direction=direction, duration_seconds=seconds, created_at=datetime(2026, 9, 30))


def test_llm_audio_and_text_tokens_are_priced_separately():
    line = costs.price_usage(costs.version_at(datetime(2026, 9, 30)), GEMINI_LIVE)
    assert line["cost_usd"] == pytest.approx(0.75 + 3.0 + 12.0)


def test_generic_rates_and_stt_tts_units():
    version = costs.version_at(datetime(2026, 9, 30))
    lite = {"type": "llm_usage", "provider": "google", "model": "gemini-3.5-flash-lite",
            "input_tokens": 2_000_000, "output_tokens": 100_000}
    assert costs.price_usage(version, lite)["cost_usd"] == pytest.approx(0.6 + 0.25)
    stt = {"type": "stt_usage", "provider": "livekit", "model": "deepgram/nova-3", "audio_duration": 120}
    assert costs.price_usage(version, stt)["cost_usd"] == pytest.approx(2 * 0.0058)
    tts = {"type": "tts_usage", "provider": "elevenlabs", "model": "eleven_flash_v2_5", "characters_count": 500}
    assert costs.price_usage(version, tts)["cost_usd"] == pytest.approx(0.02)


def test_unknown_model_and_rates_stay_unknown_and_mark_incomplete():
    mystery = {"type": "llm_usage", "provider": "acme", "model": "brand-new", "input_tokens": 10}
    result = costs.breakdown(call(), [mystery])
    assert result["lines"][0]["cost_usd"] is None and result["lines"][0]["unpriced"]
    livekit = next(line for line in result["lines"] if line["component"] == "livekit")
    assert livekit["cost_usd"] is None  # no published rate: unknown, never zero
    telephony = next(line for line in result["lines"] if line["component"] == "telephony")
    assert telephony["cost_usd"] == pytest.approx(2 * 0.0032)
    assert result["known_usd"] == pytest.approx(2 * 0.0032) and result["complete"] is False


def test_web_calls_have_no_telephony_and_unended_calls_are_pending():
    web = costs.breakdown(call("web"), [])
    assert [line["component"] for line in web["lines"]] == ["livekit"]
    live = costs.breakdown(call(seconds=None), [])
    assert all(line["unpriced"] == "call not ended" for line in live["lines"])


def test_version_is_chosen_by_call_date(monkeypatch):
    monkeypatch.setattr(costs, "PRICING", {"versions": [
        {"id": "old", "effective_from": "2026-01-01"}, {"id": "new", "effective_from": "2026-10-01"}]})
    assert costs.version_at(datetime(2026, 9, 30))["id"] == "old"
    assert costs.version_at(datetime(2026, 10, 2))["id"] == "new"


@pytest.mark.asyncio
async def test_recompute_sums_every_session(sessions):
    async with sessions() as db:
        row = Call(persona="", scenario="", status=CallStatus.completed, direction="web", duration_seconds=60)
        db.add(row)
        await db.flush()
        for sequence in (1, 2):
            db.add(CallTelemetryEvent(call_id=row.id, session_id=str(uuid.uuid4()), sequence=sequence, payload={
                "event": "usage_reported", "usage": [{"type": "tts_usage", "provider": "elevenlabs",
                                                      "model": "eleven_flash_v2_5", "characters_count": 1000}]}))
        await db.flush()
        await costs.recompute(db, row)
        tts = [line for line in row.cost_breakdown["lines"] if line["component"] == "tts"]
        assert len(tts) == 2 and row.cost_breakdown["known_usd"] == pytest.approx(0.08)
