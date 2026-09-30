# Call telemetry

## Enable

Apply migrations through `0027` before deploying the worker. The worker uses the
existing `BACKEND_PUBLIC_URL` and `INTERNAL_API_TOKEN`. Without a token it emits
local JSON logs only. Set `AGENT_VERSION` to the deployed commit or release ID
(at most 128 characters; letters, digits, underscore, dot, colon and hyphen).

No provider credentials or real call are needed for the automated tests. Live-call
acceptance and deployment are still separate steps.

## Watch live

Open `<backend>/monitor` and enter the founder API key (`ADMIN_API_TOKEN`). It follows
the newest call by default and refreshes every second while that call is live. It shows
reply times (last, p50, p95), stage timings, tool/transfer spans, and a merged log of
transcript, routing and timing events. Data comes from `GET /monitor/api/calls` and
`GET /monitor/api/calls/{call_id}?after=<cursor>`; both require the founder key.

In worker logs, search for `reply after`, `timings:` and `tool_request` for the same
numbers as plain text.

## Retrieve

`GET /internal/calls/{call_id}/telemetry` requires the founder's `x-api-key`.
Worker keys, practice keys, dialer keys and unauthenticated requests cannot read it.

The response has `events`, `next_after`, and `has_more`. Pass `after=next_after`
to read the next page. The default `limit` is 200; the maximum is 500. A cursor
remains usable for later polling even when `has_more` is false. Cursors follow
ingestion order. Reconstruct worker order using `(session_id, sequence)` and
`elapsed_ns`, not arrival time. Sequence gaps can indicate missing events.

Each record includes the backend receive time and original worker timestamps.
Nanosecond timestamps are JSON integers: clients such as JavaScript must use a
lossless integer parser to retain every digit. Their precision is not a claim of
nanosecond timing accuracy. Monotonic times are comparable within one session only.

## Ingest and retry

`POST /internal/calls/{call_id}/telemetry` requires `x-agent-token` and accepts
`{"events": [...]}` with 1–50 validated events. Unknown fields, unsupported schema
versions, non-finite/negative measurements and mismatched call IDs are rejected.
The response reports `inserted` and `discarded` counts. A duplicate
`(call_id, session_id, sequence)` keeps the original event unchanged.

The worker sends in the background with a 1,000-event memory queue and batches
of at most 50. It retries transient HTTP/network failures up to three attempts;
permanent client errors are not retried. Shutdown drains for at most five seconds.
Overflow, exhausted retries and shutdown losses are counted in worker logs.

Durability begins when the database transaction commits. This is best-effort
delivery before acknowledgement: a worker crash or sustained outage can lose
events. JSON worker logs remain a second source, subject to the host's log-retention
policy. There is no disk spool or guaranteed crash recovery in this increment.

## What timings mean

- SDK speech-state response latency is a proxy for when the caller hears audio.
  It does not measure network transit or handset playback.
- SDK message metrics keep their original message IDs. They are not assigned to
  whichever local caller turn happens to be active when the callback arrives.
- Tool request spans measure the worker's HTTP round trip, including backend work.
  They have generated `span_id` values, distinct from SDK `tool_call_id` values.
  `returned` means the request returned without an error field; it is not proof of
  a successful business outcome. Exceptions and cancellation get separate outcomes.
- Transfer spans cover SIP or in-app handoff attempts. SIP timing includes the
  existing 2.5-second announcement pause and waits for answer. Outcomes distinguish
  `joined`, `failed`, `unanswered`, cancellation and unexpected errors. A failed
  transfer's existing fallback still runs; this does not prove that a message or
  callback was subsequently completed.
- Session observation end is the worker/session ending, which may precede the
  end of a conversation handed to a human.

No transcript, tool arguments, tool output, destination number or exception text
is included in these events. Existing application logs have separate behavior.

## Retention

Database telemetry is removed by the existing daily scheduler after 30 days from
call creation, or earlier under a practice's transcript-retention setting.
Offboarded practices follow the existing 30-day purge. Scheduler operation is
required for automatic cleanup. Expired calls reject late ingestion by discarding
the batch, so retries cannot recreate expired history.

Caller-data erasure and call recording/transcript deletion also remove telemetry
and block further uploads for that call. Physical call deletion cascades to the
telemetry table. These actions do not erase copies already present in host logs.
