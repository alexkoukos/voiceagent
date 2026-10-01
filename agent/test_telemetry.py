import asyncio
import json
from types import SimpleNamespace as NS

import pytest
import httpx

from livekit.agents import AgentSession
from livekit.agents.voice.events import (
    AgentStateChangedEvent, ConversationItemAddedEvent, FunctionToolsExecutedEvent,
    UserStateChangedEvent,
)
from livekit.agents.llm import ChatMessage, FunctionCall, FunctionCallOutput

from telemetry import CallTelemetry, TelemetryUploader, observe_span, track_telemetry


def setup_tracker():
    records = []
    now = [0]
    tracker = CallTelemetry("test-call", "pipeline", sink=records.append,
                            clock=lambda: now[0], wall_clock=lambda: 123)
    return tracker, records, now


def user(tracker, old, new):
    tracker.user_state(UserStateChangedEvent(old_state=old, new_state=new))


def agent(tracker, old, new):
    tracker.agent_state(AgentStateChangedEvent(old_state=old, new_state=new))


def test_greeting_resume_and_duplicate_speech_do_not_create_false_latency():
    tracker, records, now = setup_tracker()
    agent(tracker, "listening", "speaking")  # greeting has no caller speech
    user(tracker, "listening", "speaking")
    user(tracker, "speaking", "listening")
    now[0] = 100_000_000
    user(tracker, "listening", "speaking")  # resumed speech invalidates old stop
    agent(tracker, "listening", "speaking")
    user(tracker, "speaking", "listening")
    now[0] = 350_000_000
    agent(tracker, "thinking", "speaking")
    agent(tracker, "speaking", "speaking")
    estimates = [r for r in records if r["event"] == "response_latency_estimate"]
    assert len(estimates) == 1
    assert estimates[0]["latency_ms"] == 250
    assert estimates[0]["accuracy"] == "proxy_not_handset_playback"
    assert len({r["turn_id"] for r in records if r["event"] == "caller_speech_started"}) == 2


def test_message_metrics_preserve_unknowns_and_exclude_content():
    tracker, records, _ = setup_tracker()
    user(tracker, "listening", "speaking")
    tracker.message(ConversationItemAddedEvent(item=ChatMessage(
        role="assistant", content=["PRIVATE PATIENT"], metrics={
            "llm_node_ttft": .25, "tts_node_ttfb": -1,
            "e2e_latency": float("nan"), "transcription_delay": 0,
        },
    )))
    assert records[-1]["latencies"] == {"llm_node_ttft_ms": 250, "transcription_delay_ms": 0}
    assert records[-1]["turn_id"] is None  # late SDK item isn't falsely correlated
    assert "PRIVATE" not in json.dumps(records, allow_nan=False)


def test_tools_do_not_log_arguments_or_outputs_or_claim_business_success():
    tracker, records, _ = setup_tracker()
    tracker.tools_completed(FunctionToolsExecutedEvent(
        function_calls=[FunctionCall(call_id="tool-1", name="book_appointment",
                                     arguments='{"name":"PRIVATE PATIENT"}')],
        function_call_outputs=[FunctionCallOutput(call_id="tool-1", is_error=False,
                                                  output="PRIVATE: no slots available")],
    ))
    assert records[0]["tool_call_id"] == "tool-1"
    assert records[0]["is_error"] is False
    assert "success" not in records[0]
    assert "PRIVATE" not in json.dumps(records)


def test_close_is_idempotent_and_envelope_is_ordered(monkeypatch):
    monkeypatch.setenv("AGENT_VERSION", "test-version")
    tracker, records, now = setup_tracker()
    tracker.emit("session_observation_started")
    now[0] = 12_345
    tracker.finish()
    tracker.finish()
    tracker.emit("late_event")
    assert [r["sequence"] for r in records] == [1, 2]
    assert records[-1]["elapsed_ns"] == 12_345
    assert records[-1]["observed_at_unix_ns"] == 123
    assert records[-1]["agent_version"] == "test-version"
    assert records[0]["session_id"] == records[1]["session_id"]


def test_sink_failure_does_not_escape():
    def broken(_record):
        raise RuntimeError("PRIVATE")
    tracker = CallTelemetry("call", "pipeline", sink=broken)
    tracker.emit("test")
    tracker.finish()
    assert tracker.closed


@pytest.mark.asyncio
async def test_real_session_event_registration_and_shutdown(monkeypatch):
    monkeypatch.delenv("INTERNAL_API_TOKEN", raising=False)
    session = AgentSession()
    callbacks = []
    tracker = track_telemetry(session, NS(add_shutdown_callback=callbacks.append), "call", "realtime")
    records = []
    tracker.sink = records.append
    session.emit("user_state_changed", UserStateChangedEvent(old_state="listening", new_state="speaking"))
    await callbacks[0]()
    assert [r["event"] for r in records] == ["caller_speech_started", "session_observation_ended"]


def test_span_keeps_original_turn_and_records_cancellation():
    tracker, records, now = setup_tracker()
    user(tracker, "listening", "speaking")
    turn = tracker.turn_id
    with pytest.raises(asyncio.CancelledError):
        with observe_span(tracker, "tool_request", tool_name="check_availability"):
            now[0] = 150_000_000
            user(tracker, "listening", "speaking")
            raise asyncio.CancelledError()
    assert records[-1]["outcome"] == "cancelled"
    assert records[-1]["duration_ms"] == 150
    assert records[-1]["turn_id"] == turn
    assert records[-1]["span_id"] == records[1]["span_id"]


@pytest.mark.asyncio
async def test_uploader_retries_same_batch_and_drains():
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        assert request.headers["x-agent-token"] == "test-token"
        return httpx.Response(503 if len(requests) == 1 else 200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        upload = TelemetryUploader("call", "http://test", "test-token", client=client)
        upload.enqueue({"sequence": 1})
        upload.enqueue({"sequence": 2})
        upload.start()
        await upload.close()
    assert requests[0] == requests[1] == {"events": [{"sequence": 1}, {"sequence": 2}]}
    assert upload.uploaded == 2
    assert upload.dropped == 0
    assert upload.task.done()


@pytest.mark.asyncio
async def test_uploader_is_bounded_and_shutdown_cancels_stalled_network():
    entered = asyncio.Event()
    async def handle(_request):
        entered.set()
        await asyncio.Event().wait()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        upload = TelemetryUploader("call", "http://test", "token", client=client, capacity=1)
        upload.enqueue({"sequence": 1})
        upload.enqueue({"sequence": 2})
        assert upload.dropped == 1
        upload.start()
        await entered.wait()
        upload.enqueue({"sequence": 3})
        await upload.close(timeout=0.01)
    assert upload.dropped == 3
    assert upload.task.done()


@pytest.mark.asyncio
async def test_uploader_does_not_retry_schema_rejection():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(422)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        upload = TelemetryUploader("call", "http://test", "token", client=client)
        upload.enqueue({"sequence": 1})
        upload.start()
        await upload.close()
    assert len(requests) == 1
    assert upload.dropped == 1


@pytest.mark.asyncio
async def test_tracking_wires_upload_and_flushes_final_event(monkeypatch):
    requests = []
    def handle(request):
        requests.extend(json.loads(request.content)["events"])
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token")
        monkeypatch.setenv("BACKEND_PUBLIC_URL", "http://test")
        monkeypatch.setattr("telemetry.httpx.AsyncClient", lambda: client)
        callbacks = []
        tracker = track_telemetry(AgentSession(), NS(add_shutdown_callback=callbacks.append), "call", "pipeline")
        tracker.finish()
        await callbacks[0]()
    assert [r["event"] for r in requests] == ["session_observation_started", "session_observation_ended"]


@pytest.mark.asyncio
async def test_backend_tool_error_and_cancellation_emit_timing(monkeypatch):
    import agent as worker
    from unittest.mock import AsyncMock

    tracker, records, _ = setup_tracker()
    receiver = NS(telemetry=tracker, call_id="call", flags=set())
    receiver._tool_request = lambda name, args: worker.ReceptionistCall._tool_request(receiver, name, args)
    receiver._tool_lock = asyncio.Lock()
    monkeypatch.setattr(worker, "backend_post", AsyncMock(return_value={"error": "unavailable"}))
    assert (await worker.ReceptionistCall.tool(receiver, "book_appointment", {"name": "PRIVATE"}))["error"] == "unavailable"
    assert records[-1]["outcome"] == "error"
    monkeypatch.setattr(worker, "backend_post", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await worker.ReceptionistCall.tool(receiver, "book_appointment", {})
    assert records[-1]["outcome"] == "cancelled"
    assert "PRIVATE" not in json.dumps(records)


def test_readable_log_lines_carry_timings_but_no_content(caplog):
    tracker, _records, now = setup_tracker()
    caplog.set_level("INFO", logger="voice.telemetry")
    user(tracker, "listening", "speaking")
    user(tracker, "speaking", "listening")
    now[0] = 1_234_000_000
    agent(tracker, "thinking", "speaking")
    item = ChatMessage(role="assistant", content=["secret words"], metrics={"llm_node_ttft": 0.4})
    tracker.message(ConversationItemAddedEvent(item=item))
    assert "call test-call reply after 1234 ms" in caplog.text
    assert "call test-call assistant timings: llm_node_ttft=400" in caplog.text
    assert "secret words" not in caplog.text


def test_usage_report_keeps_numbers_only_and_runs_once():
    from livekit.agents.metrics import LLMModelUsage, TTSModelUsage
    tracker, records, _ = setup_tracker()
    tracker.session = NS(usage=NS(model_usage=[
        LLMModelUsage(provider="google", model="gemini-3.8-live", input_audio_tokens=900, output_audio_tokens=300),
        TTSModelUsage(provider="elevenlabs", model="flash"),  # all zero: skipped
    ]))
    tracker.finish()
    tracker.finish()
    usage = [r for r in records if r["event"] == "usage_reported"]
    assert len(usage) == 1
    assert usage[0]["usage"] == [{"type": "llm_usage", "provider": "google", "model": "gemini-3.8-live",
                                  "input_audio_tokens": 900, "output_audio_tokens": 300}]
    assert records[-1]["event"] == "session_observation_ended"


def test_usage_report_survives_missing_session():
    tracker, records, _ = setup_tracker()
    tracker.finish()
    assert [r["event"] for r in records] == ["session_observation_ended"]


def test_answer_latency_skips_fillers_and_counts_once_per_turn():
    tracker, records, now = setup_tracker()
    user(tracker, "listening", "speaking")
    user(tracker, "speaking", "listening")
    now[0] = 400_000_000
    agent(tracker, "thinking", "speaking")  # the filler: first sound only
    now[0] = 2_100_000_000
    tracker.answer_started(650.0)
    tracker.answer_started(300.0)  # second model call in the same turn: TTFT only
    answers = [r for r in records if r["event"] == "answer_latency_estimate"]
    assert [a["latency_ms"] for a in answers] == [2100]
    assert answers[0]["accuracy"] == "proxy_text_ready_not_playback"
    assert [r["latency_ms"] for r in records if r["event"] == "model_ttft"] == [650.0, 300.0]
    assert [r["latency_ms"] for r in records if r["event"] == "response_latency_estimate"] == [400]
