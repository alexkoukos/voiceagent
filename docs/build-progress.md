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

## Piece 3 — live call monitor (2026-09-30)

Telemetry was stored but readable only as raw JSON pages. `/monitor` is a single
founder-only page, not a dashboard platform. It lists recent calls with reply count
and p50 reply time. For the selected call it polls every second while the call is
live and shows:

- the headline metric, caller stops speaking → agent starts speaking: the last
  reply, p50, p95 and a bar per reply (green < 1 s, amber < 2 s, red above)
- SDK stage timings per message: STT, end of turn, LLM first token, TTS first byte,
  end-to-end
- backend tool and transfer spans with outcome and duration
- one merged log: transcript, routing decisions and timing events (agent state
  changes are optional)

The page holds no data; the browser sends the founder key on every request and
keeps it in local storage on that device. The worker also writes readable,
content-free log lines (`call <id> reply after 1234 ms`, `... timings: ...`,
`... tool_request <name> returned in 180 ms`) so plain Railway log tailing shows the
same numbers.

Verification: backend suite 133 passed (including the new monitor tests on Postgres),
agent suite 58 passed. Not yet checked against a real call.

Limits: reply time is still the SDK-state proxy described in [telemetry.md](telemetry.md).
Transcript times are backend receive times and event times are worker clock times, so
the merged log order between the two is approximate. There is no cost data yet
(next piece).

## Piece 4: usage and cost per call (2026-09-30)

The worker reports per provider/model usage at session end. The backend prices it from
a central, dated price list into `calls.cost_breakdown` (migration `0028`). Unknown
rates stay unknown and mark the breakdown incomplete. `/monitor` shows a cost table per
call and a summary over a window of 24 h, 7 days or 30 days: calls, minutes, outcomes,
reply p50/p95/p99, stage timings, cost per call, per minute and per successful outcome.
See [telemetry.md](telemetry.md#cost-per-call).

## Piece 5: Eval Suite V0.1 (2026-10-01)

`agent/evals/`: ASTRA's ten scenarios plus EVAL-011 (no booking without a yes). They
run the real receptionist agent in text mode against a local backend, and critical
checks are on database state. CI workflow `evals.yml` (needs the `GEMINI_API_KEY`
secret). See [evals.md](evals.md).

First runs found seven real bugs, all fixed, each with a regression test:

1. **Extra LLM turn after tools that already spoke** (read-back, goodbye, end of call).
   Gemini returned empty completions (four retries, then an error) or said «Συγγνώμη,
   δεν σας άκουσα καλά» just before the read-back. Text engines now ask for no reply.
2. **Transfer announced but never started.** The backend said `handoff` and the model
   said «Μια στιγμή να σας συνδέσω» without calling `transfer_to_human`, so the caller
   waited for nobody. The worker now starts the transfer in code, and a second request
   cannot start a second transfer.
3. **Duplicate messages.** Each follow-up detail created a new message (three for one
   call). Now there is one message per call and recipient, updated in place, with the
   urgent alert sent once.
4. **The "English mode" line never played.** It was spoken while the old agent was still
   draining, which raised an error that was only logged. The switch now waits for the swap.
5. **Callback numbers without `+30`** when the model passed a spoken Greek number.
   Now stored in E.164 format.
6. **Parallel tool calls raced.** Gemini emits `check_availability` and `prepare_action`
   together; run concurrently, the read-back was rejected before the new offer was saved,
   and the caller's yes arrived before the late read-back. Backend calls now run one at a
   time, in order.
7. **Reschedule offered another dentist's times.** Availability for a move was "anyone
   free", but a move keeps the appointment's own dentist, so the move failed with
   `slot_taken` after the caller's yes. A move now offers only that dentist's times.
8. Harness issues found while building it: read-backs went through the LLM (production
   uses `say`), and a finished speech stayed "current" after an agent swap. Both fixed in
   the harness; neither affected production.

Seen but not fixed: Gemini once blocked a "say exactly this" read-back as
`PROHIBITED_CONTENT` (that is the `realtime` engine's path), and once produced a
malformed function call four times in a row. Repeat runs measure how often.
English read-backs say the service's Greek name ("for Έλεγχος").

## Piece 6: benchmark (2026-10-01)

`run_evals.py --repeat N --baseline <file>` reports pass rate per eval, critical
failures, turn and first-reply p50/p95/p99, tool error rate and cost per call. A lower
critical pass rate than the baseline exits 1. Voice latency comes from `/monitor` after
scripted audio calls, not from text runs.

Baseline, 2026-10-01 (10 text evals × 3, gemini-3.5-flash-lite, local backend):

| | First run | After fixes |
|---|---|---|
| Runs passed | 24/30 (80%) | 30/30 (100%) |
| Critical failures | 6 | 0 |
| First reply p50 / p95 / p99 | 699 / 1583 / 1926 ms | 693 / 1529 / 1797 ms |
| Whole turn p50 / p95 / p99 | 1314 / 2130 / 4131 ms | 1323 / 2093 / 2194 ms |
| Tool error rate | 8.1% | 5.6% |
| Known cost per call | $0.0216 | $0.0206 |

Text timings exclude STT, turn detection, TTS and telephony. Voice latency will be higher.
Costs are a lower bound: LiveKit minutes are unpriced. EVAL-010's Gemini
`MALFORMED_FUNCTION_CALL` appeared in 2 of 9 earlier runs and 0 of 3 here: still a known,
intermittent provider failure.

## Next pieces, in order

1. Deploy, then one real test call with `/monitor` open. Fill in the LiveKit rates in
   `pricing.json` from the invoice.
2. Scripted audio calls (`scripts/scripted_calls.py`) against the deployment for voice
   latency p50/p95 and EVAL-006.
3. Customer discovery: local material in `sales/` (ignored by git). Start conversations now.
