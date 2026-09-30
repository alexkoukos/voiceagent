"""Bounded, content-free telemetry contract shared by ingestion and retrieval."""

from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import Practice

TELEMETRY_RETENTION_DAYS = 30


async def telemetry_expired(db, call):
    now = datetime.utcnow()
    days = TELEMETRY_RETENTION_DAYS
    if call.practice_id:
        practice = await db.get(Practice, call.practice_id)
        if practice:
            days = min(days, practice.retention_transcripts_days)
            if practice.offboarded_at and practice.offboarded_at <= now - timedelta(days=30):
                days = 0
    return call.created_at < now - timedelta(days=days)

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
Milliseconds = Annotated[float, Field(ge=0, le=86_400_000, allow_inf_nan=False)]
LatencyName = Literal[
    "transcription_delay_ms", "end_of_turn_delay_ms", "on_user_turn_completed_delay_ms",
    "llm_node_ttft_ms", "tts_node_ttfb_ms", "e2e_latency_ms", "playback_latency_ms",
]


class TelemetryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    event: Literal[
        "session_observation_started", "session_observation_ended",
        "caller_speech_started", "caller_speech_stopped", "agent_state_changed",
        "response_latency_estimate", "message_metrics", "tool_completed",
        "tool_request_started", "tool_request_ended", "transfer_started", "transfer_ended",
    ]
    call_id: Identifier
    session_id: UUID
    agent_version: Identifier
    engine: Literal["pipeline", "text_pipeline", "realtime", "openai"]
    sequence: Annotated[int, Field(ge=1, le=2_147_483_647)]
    observed_at_unix_ns: Annotated[int, Field(ge=0, le=9_223_372_036_854_775_807)]
    elapsed_ns: Annotated[int, Field(ge=0, le=9_223_372_036_854_775_807)]
    turn_id: UUID | None = None
    source: Literal["sdk_state", "sdk_message_metrics", "sdk_tool_batch", "worker_http", "worker_transfer"] | None = None
    old_state: Literal["initializing", "idle", "listening", "thinking", "speaking"] | None = None
    new_state: Literal["initializing", "idle", "listening", "thinking", "speaking"] | None = None
    accuracy: Literal["proxy_not_handset_playback"] | None = None
    latency_ms: Milliseconds | None = None
    duration_ms: Milliseconds | None = None
    message_id: Identifier | None = None
    role: Literal["user", "assistant", "system", "developer", "tool"] | None = None
    latencies: dict[LatencyName, Milliseconds] | None = None
    tool_call_id: Identifier | None = None
    tool_name: Identifier | None = None
    is_error: bool | None = None
    span_id: UUID | None = None
    handoff_id: Identifier | None = None
    mode: Literal["sip", "app"] | None = None
    outcome: Literal["returned", "error", "cancelled", "joined", "failed", "unanswered"] | None = None

    @model_validator(mode="after")
    def required_event_fields(self):
        requirements = {
            "response_latency_estimate": ("latency_ms", "accuracy"),
            "message_metrics": ("message_id", "role", "latencies"),
            "tool_completed": ("tool_call_id", "tool_name", "is_error"),
            "tool_request_started": ("span_id", "tool_name"),
            "tool_request_ended": ("span_id", "tool_name", "duration_ms", "outcome"),
            "transfer_started": ("span_id", "handoff_id", "mode"),
            "transfer_ended": ("span_id", "handoff_id", "mode", "duration_ms", "outcome"),
            "agent_state_changed": ("old_state", "new_state"),
        }
        if any(getattr(self, name) is None for name in requirements.get(self.event, ())):
            raise ValueError("Missing fields for telemetry event")
        return self


class TelemetryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: Annotated[list[TelemetryEvent], Field(min_length=1, max_length=50)]
