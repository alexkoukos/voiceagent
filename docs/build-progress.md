# Astra execution record

The operating brief is preserved verbatim in [ASTRA.md](../ASTRA.md).
The next milestone is a controlled pilot that delivers measurable value and converts to payment.

## Piece 1 — session telemetry foundation (2026-09-30)

The existing FastAPI/Postgres backend handles business tools, booking, call outcomes,
and routing. The LiveKit Python worker runs receptionist and legacy outbound calls,
with pipeline and realtime providers. Existing latency visibility consists of text
logs and a receptionist median. This increment adds structured JSON log records
without changing the call flow or requiring a migration.

- Each event has a schema version, call ID, generated session ID, sequence number,
  engine, and agent version (`AGENT_VERSION`; `unknown` when unset).
- UTC epoch nanoseconds record when the worker observed the event; elapsed
  nanoseconds use a monotonic clock. Resolution does not imply nanosecond accuracy.
- Caller speech transitions have locally generated turn IDs. SDK message metrics
  use message IDs; tool completions use provider tool call IDs. Late message and
  tool events deliberately have no inferred turn ID.
- SDK message metrics expose available STT/transcription delay, turn detection,
  LLM TTFT, TTS TTFB, playback and end-to-end timings in milliseconds. Missing,
  negative and non-finite values are omitted, never fabricated as zero.
- State-based response latency is explicitly an estimate. It excludes an unmatched
  greeting, clears stale stops when speech resumes, and counts a response once.
- Tool events record execution error status only: a non-error tool response is
  not proof of a successful booking or other business outcome.
- No transcripts, message content, tool arguments/outputs, or exception text enter
  the new telemetry. Existing logging remains separate.

Success criteria: actual SDK event compatibility, deterministic timing checks,
interruption/resumed-speech checks, content exclusion, safe sink failure, and
idempotent shutdown. Run the agent suite with the same dependencies as agent CI.

Validation: all 51 agent tests passed locally, including six new telemetry tests.

### Limits

These are worker session observations, not telephony call start/end or verified
handset playback. The log collector must retain the JSON message to preserve this
history; durable database storage and a retrieval API are not implemented yet.
Provider failure before telemetry attachment and worker crashes can leave missing
events. No tool duration, cost total, or benchmark claim is made by this increment.
Live provider acceptance remains outstanding. No deployment is part of this change.

## Piece 2 — durable telemetry and timing spans (2026-09-30)

Implemented the authenticated ingestion endpoint, Postgres event table and
migration `0027`, duplicate protection, and founder-only cursor-based retrieval.
The worker now uploads through a bounded background queue and flushes on shutdown.
Receptionist backend tool calls and SIP/in-app transfers have monotonic timing
spans, including error and cancellation outcomes. Existing transfer fallbacks stay
in place. Call-data erasure and retention include telemetry and reject late writes.

The database is durable after acknowledgement; delivery from worker memory remains
best effort during crashes or extended outages. Provider playback is still an
estimate and tool spans are worker/backend timings, not provider-internal spans.
Deployment, live-call verification and guaranteed crash recovery are not completed.
See [telemetry.md](telemetry.md) for setup, retrieval, semantics and limits.

Verification: full backend suite passed (129 tests), full agent suite passed
(56 tests), and an isolated Postgres database passed fresh migrations, downgrade
to `0026`, and re-upgrade to `0027`. Additional worker-to-API and tool cancellation
checks passed in the final targeted runs: all 15 backend telemetry tests and all
12 agent telemetry tests passed. This includes sending the actual worker's event
payloads through the API to Postgres and reading back the ordered history.

The full suite also exposed and fixed an existing missing `TranscriptRole` import
in call-event handling, and froze a waitlist regression test's clock so its fixed
fixture date remains valid after September 28.

## Next pieces, in order

1. Capture provider usage and centralize versioned pricing. Preserve source usage
   and historical rates; report missing costs as unknown.
2. Implement Eval Suite V0.1 using the ten scenarios in ASTRA.md, starting with
   booking state assertions and existing booking tests. Keep audio/language cases
   separately identifiable when they require live providers.
3. Build repeatable benchmarks with p50/p95/p99, sample counts, correctness and cost.
4. Use measured failures to improve the demo while beginning customer discovery.

Do not postpone customer conversations until all four pieces are complete.
