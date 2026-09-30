"""Content-free session telemetry. Times are observations, not handset measurements."""

import asyncio
import json
import logging
import math
import os
import time
import uuid

import httpx

logger = logging.getLogger("voice.telemetry")

# Explicit allowlist: never serialize SDK events, messages, or tool outputs wholesale.
LATENCIES = (
    "transcription_delay", "end_of_turn_delay", "on_user_turn_completed_delay",
    "llm_node_ttft", "tts_node_ttfb", "e2e_latency", "playback_latency",
)


class CallTelemetry:
    def __init__(self, call_id, engine, *, sink=None, clock=time.perf_counter_ns,
                 wall_clock=time.time_ns):
        self.call_id = call_id
        self.engine = engine
        self.session_id = str(uuid.uuid4())
        self.agent_version = os.getenv("AGENT_VERSION", "unknown")
        self.clock = clock
        self.wall_clock = wall_clock
        self.sink = sink or self._log
        self.started_ns = clock()
        self.sequence = 0
        self.closed = False
        self.turn_id = None
        self.speech_end_ns = None

    @staticmethod
    def _log(record):
        logger.info(json.dumps(record, separators=(",", ":"), allow_nan=False))

    def emit(self, event, **fields):
        if self.closed:
            return
        self.sequence += 1
        record = {
            "schema_version": 1, "event": event, "call_id": self.call_id,
            "session_id": self.session_id, "agent_version": self.agent_version,
            "engine": self.engine, "sequence": self.sequence,
            "observed_at_unix_ns": self.wall_clock(),
            "elapsed_ns": self.clock() - self.started_ns,
            "turn_id": self.turn_id, **fields,
        }
        try:
            self.sink(record)
        except Exception:
            # Telemetry must not break a call; do not include potentially sensitive errors.
            logger.warning("telemetry sink failed")

    def attach(self, session):
        session.on("user_state_changed", self.user_state)
        session.on("agent_state_changed", self.agent_state)
        session.on("conversation_item_added", self.message)
        session.on("function_tools_executed", self.tools_completed)
        session.on("close", lambda _ev: self.finish())
        self.emit("session_observation_started")

    def user_state(self, ev):
        if ev.new_state == "speaking" and ev.old_state != "speaking":
            self.turn_id = str(uuid.uuid4())
            self.speech_end_ns = None
            self.emit("caller_speech_started", source="sdk_state")
        elif ev.old_state == "speaking" and ev.new_state != "speaking":
            self.speech_end_ns = self.clock()
            self.emit("caller_speech_stopped", source="sdk_state")

    def agent_state(self, ev):
        self.emit("agent_state_changed", old_state=ev.old_state, new_state=ev.new_state)
        if ev.new_state == "speaking" and ev.old_state != "speaking":
            if self.speech_end_ns is not None:
                latency_ms = (self.clock() - self.speech_end_ns) / 1_000_000
                self.emit(
                    "response_latency_estimate", latency_ms=latency_ms,
                    source="sdk_state", accuracy="proxy_not_handset_playback",
                )
                self.speech_end_ns = None
                # Human-readable line for plain log tailing; no content.
                logger.info("call %s reply after %.0f ms", self.call_id, latency_ms)

    def message(self, ev):
        item = ev.item
        if getattr(item, "type", None) != "message":
            return
        metrics = getattr(item, "metrics", None) or {}
        values = {
            name + "_ms": value * 1000
            for name in LATENCIES
            if isinstance(value := metrics.get(name), (int, float))
            and not isinstance(value, bool) and math.isfinite(value) and value >= 0
        }
        # SDK items can arrive late (especially after interruption). Do not assign
        # their timings to the currently speaking caller's locally inferred turn.
        self.emit("message_metrics", turn_id=None, message_id=item.id,
                  role=item.role, source="sdk_message_metrics", latencies=values)
        if values:
            logger.info("call %s %s timings: %s", self.call_id, item.role,
                        " ".join(f"{name[:-3]}={value:.0f}" for name, value in values.items()))

    def tools_completed(self, ev):
        for call, output in ev.zipped():
            self.emit("tool_completed", turn_id=None, tool_call_id=call.call_id,
                      tool_name=call.name, is_error=output.is_error,
                      source="sdk_tool_batch")

    def finish(self):
        self.emit("session_observation_ended")
        self.closed = True

    def start_span(self, kind, **fields):
        span = {"span_id": str(uuid.uuid4()), "turn_id": self.turn_id, **fields}
        started = self.clock()
        self.emit(kind + "_started", **span)
        return started, span

    def end_span(self, kind, span, outcome):
        started, fields = span
        duration_ms = (self.clock() - started) / 1_000_000
        self.emit(kind + "_ended", **fields, outcome=outcome, duration_ms=duration_ms)
        logger.info("call %s %s %s %s in %.0f ms", self.call_id, kind,
                    fields.get("tool_name") or fields.get("mode", ""), outcome, duration_ms)


class observe_span:
    """A timing scope that records cancellation and can end before session shutdown."""

    def __init__(self, telemetry, kind, **fields):
        self.telemetry = telemetry
        self.kind = kind
        self.fields = fields
        self.finished = False

    def __enter__(self):
        if self.telemetry:
            self.span = self.telemetry.start_span(self.kind, **self.fields)
        return self

    def finish(self, outcome):
        if self.telemetry and not self.finished:
            self.telemetry.end_span(self.kind, self.span, outcome)
        self.finished = True

    def __exit__(self, exc_type, _exc, _tb):
        outcome = "returned" if exc_type is None else (
            "cancelled" if issubclass(exc_type, asyncio.CancelledError) else "error")
        self.finish(outcome)


class TelemetryUploader:
    """Bounded best-effort transport. Accepted batches are durable in Postgres.

    Never await network I/O on a speech callback. Retries reuse the exact batch IDs.
    During a long outage records can be lost; counters and JSON logs expose that.
    """

    def __init__(self, call_id, url, token, *, client=None, capacity=1000):
        self.url = f"{url.rstrip('/')}/internal/calls/{call_id}/telemetry"
        self.token = token
        self.client = client
        self.queue = asyncio.Queue(maxsize=capacity)
        self.task = None
        self.stopping = False
        self.dropped = 0
        self.uploaded = 0
        self.inflight = 0

    def start(self):
        self.task = asyncio.create_task(self.run())

    def enqueue(self, record):
        if self.stopping:
            self.dropped += 1
            return
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped == 1:
                logger.warning("telemetry queue full; records remain in worker logs")

    async def send(self, client, batch):
        for attempt in range(3):
            try:
                response = await client.post(self.url, headers={"x-agent-token": self.token},
                                             json={"events": batch}, timeout=3.0)
                if response.is_success:
                    return True
                if response.status_code < 500 and response.status_code not in (408, 429):
                    # A bad schema/key will not improve by immediately retrying it.
                    logger.warning("telemetry upload rejected: status=%s", response.status_code)
                    return False
            except httpx.HTTPError:
                pass
            if attempt < 2:
                await asyncio.sleep(0.2 * (2 ** attempt))
        return False

    async def run(self):
        owned = self.client is None
        client = self.client or httpx.AsyncClient()
        try:
            while True:
                first = await self.queue.get()
                batch = [first]
                while len(batch) < 50 and not self.queue.empty():
                    batch.append(self.queue.get_nowait())
                self.inflight = len(batch)
                try:
                    if await self.send(client, batch):
                        self.uploaded += len(batch)
                    else:
                        self.dropped += len(batch)
                        logger.warning("telemetry batch not persisted; records remain in worker logs")
                finally:
                    for _ in batch:
                        self.queue.task_done()
                    # Keep inflight count on cancellation for close()'s loss report.
                self.inflight = 0
        finally:
            if owned:
                await client.aclose()

    async def close(self, timeout=5.0):
        if self.stopping:
            return
        self.stopping = True
        try:
            await asyncio.wait_for(self.queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            if self.task:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            self.dropped += self.queue.qsize() + self.inflight
            logger.info("telemetry delivery finished: acknowledged=%s lost=%s",
                        self.uploaded, self.dropped)


def track_telemetry(session, ctx, call_id, engine):
    url = os.getenv("BACKEND_PUBLIC_URL", "http://localhost:8000")
    token = os.getenv("INTERNAL_API_TOKEN", "")
    uploader = TelemetryUploader(call_id, url, token) if token else None
    if uploader:
        uploader.start()

    def sink(record):
        CallTelemetry._log(record)
        if uploader:
            uploader.enqueue(record)

    telemetry = CallTelemetry(call_id, engine, sink=sink)
    telemetry.attach(session)

    async def finish():
        telemetry.finish()
        if uploader:
            await uploader.close()

    # Also handles shutdown where no session close event was delivered.
    ctx.add_shutdown_callback(finish)
    return telemetry
