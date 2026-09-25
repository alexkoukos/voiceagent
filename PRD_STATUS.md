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

## Remaining engineering work

1. **B1/B3: auditable offer and confirmation state.** The prompt requires returned slots and explicit readback/yes, but the backend does not persist an offer and bind the write to the caller's confirmation. Availability is rechecked, but verbal confirmation still depends on model behavior.
2. **B8: ambiguous external writes and atomic follow-up.** Google writes happen before the Postgres commit. A provider timeout or database failure can leave an event without a local appointment; deterministic provider event IDs and reconciliation are still needed. Appointment writes and their customer notification/call-outcome updates also use separate transactions, leaving a crash window.
3. **Retention: durable recording deletion.** Recording deletion retries are in-memory tasks, while the stored object key is cleared immediately. A restart or prolonged storage outage can lose cleanup work. Use a persistent deletion queue and retain its object key until confirmed deletion.
4. **Summary and notification recovery policy.** A failed summary-model request returns no summary but still finalizes the call. Provider delivery failures become visible `failed` rows after eight attempts and require a replay/operator workflow. Previously skipped notification rows are not automatically replayed by this change. The existing policy suppresses abandoned/off-topic business summaries, which is narrower than the PRD's “every call outcome” wording.
5. **G9 and language routing.** Outbound waitlist offers do not verify an existing appointment for the selected number. R7 first-turn language switching is intentionally disabled in the current agent to avoid the documented Greek transcription drift; this differs from the PRD's P1 requirement.

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
