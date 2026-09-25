# Receptionist reliability review — 2026-09-25

The receptionist 2.0 PRD is the current target. This pass improves existing backend behavior around P0 booking, notifications, and call reporting. It does not establish pilot readiness or the PRD's real-call accuracy and latency targets.

## Implemented and verified

| PRD area | Change | Evidence |
| --- | --- | --- |
| B2/B5: correct availability and rescheduling | Exclude the appointment's own Google event by ID while preserving separate overlapping events; handle pagination, recurring instances and all-day events. Keep buffers across midnight. | Calendar response tests and Postgres rescheduling/buffer tests. |
| B6: efficient slot search | Skip database and calendar busy queries on closed days, holidays and dates outside the booking window; stop alternative-date searches at the booking horizon. | Query spy confirms no busy lookup for closed/out-of-window dates. |
| B5/B7: failure handling | An assigned calendar requires credentials. Remote appointments cannot silently be cancelled locally while Calendar is unavailable. Calendar rollback returns a structured tool error instead of accessing expired ORM state. | Tests cover missing credentials, unchanged appointment state, and rollback in all three write tools. |
| B2/B8: concurrency and retries | Refresh appointments after taking the practice lock. Repeated cancellation and same-slot reschedule are idempotent; already-deleted Google events accept 404/410. Cancellation retries offer a waitlist slot once. | Simultaneous booking test, retry tests, Google error-status tests, waitlist deduplication test. |
| B5: lookup before change | Availability cannot exclude an appointment the call has not previously found. | Agent-tool regression test. |
| Notifications: transaction safety | Use Postgres conflict handling for the notification key only. A duplicate no longer discards call outcomes or other queued messages, and unrelated integrity failures propagate. | Postgres duplicate and concurrent business-notification tests. |
| Notifications: delivery | Send up to five notifications concurrently, with one row lock and commit per delivery. Missing credentials leave messages pending and do not consume provider attempts. | Bounded concurrency, unique delivery, missing-configuration recovery and provider-failure tests. |
| Call reporting | Serialize finalization per call and recover unfinished terminal calls from the scheduler. Stale calls are committed before finalization. Retention's daily marker advances only after commit. | Concurrent finalization and scheduler recovery tests; transaction ordering reviewed. |
| Success metrics | Count notification timeliness per expected call, including failed, pending and absent messages. Additional recipients do not inflate success. Count appointments in SQL rather than loading every ID. | Mixed sent/pending/failed/missing case correctly reports 25%, not 100%. |

36 tests pass: 17 unit/provider-response tests and 19 isolated Postgres tests. External Calendar, SMS, email, summary generation, and voice calls were not exercised against live providers. Existing `datetime.utcnow()` deprecation warnings remain. No schema migration is required. Changes are local and have not been deployed.

Calendar behavior follows Google's [Events list API](https://developers.google.com/workspace/calendar/api/v3/reference/events/list); notification deduplication uses SQLAlchemy's [Postgres conflict handling](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#insert-on-conflict-upsert).

## Closures and staff leave (OP3), 2026-09-25

- `practice.rules.closures`: dated ranges for the whole business, or with `staff_id` for one person's leave. No migration: it lives in the existing `rules` JSON.
- Booking offers no slots on those days; `hours_state` finds the next opening after a long closure; `check_availability` returns `business_closed` or `staff_away` with spoken dates, and next free days are searched from the end of the closure.
- The agent's prompt lists current and upcoming closures, and both prompt files tell it to say the dates and offer the next free day or a message.
- `GET/POST /practices/<id>/closures`, `DELETE /practices/<id>/closures/<closure_id>`. A new closure returns the booked appointments inside it (`to_rebook`) and emails them to the business once.
- Tests: `backend/tests/test_closures.py` (slot blocking, next opening, agent output, endpoint). 50 tests pass. Not deployed; no real call yet. OP3's "the doctor says it by phone or SMS" belongs to OP2 and is not built.

## Remaining engineering work

Items 1 to 3 and G9 of the earlier list were addressed in `95694eb` (offer + readback state via `prepare_action`, deterministic Google event IDs, durable `recording_deletions` queue, fallback summaries, and the G9 check on waitlist calls). Still open:

1. **R7 language switching** stays off on purpose: the agent never switches language mid-call (owner's decision).
2. **P0 features with no code yet:** onboarding imports and config versions (O1 to O6, with draft/publish/rollback), OP1 failover (needs Telnyx), and OP2 changes after go-live by phone PIN, SMS or magic link.
3. **P1 features with no code yet:** OP8 patient data export/delete, OP10 monthly cost cap and spam blocking, OP7 offboarding export.

## Remaining acceptance and onboarding work

- Run the M0 ten-call/calendar acceptance set, then the M1 thirty scripted voice calls; manually review routing and booking correctness. Backend tests are not a substitute for these voice evaluations.
- Measure real caller-to-agent latency, notification delivery within 60 seconds, hard duration limits, concurrent inbound admission, handoff timeout and recording refusal end to end. No voice-latency improvement is claimed by this backend pass.
- Verify deployed email/SMS configuration, recipient delivery, Calendar permissions and external-calendar synchronization. These services' current production configuration was not inspected in this pass.
- Complete Greek DID/forwarding setup, DPA/legal review and vendor residency/account checks before pilot acceptance. Existing repository notes identify these as onboarding dependencies; their external status was not reverified.
- Validate the iOS receptionist and handoff flows on device, including push entitlements. No iOS code changed in this pass.

## Reproduce

From the repository root, with the local Docker Postgres running:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://user:password@localhost:5433/voiceagent \
uv run --python 3.12 --with-requirements backend/requirements.txt \
  --with pytest --with pytest-asyncio pytest -q backend/tests -c backend/pytest.ini
```

Tests create and drop random schemas. Leave `TEST_DATABASE_URL` unset for the unit/provider-response subset. Keep this pointed at a local test database.
