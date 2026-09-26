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

## Changes after go-live (OP2, part), 2026-09-25

- Migration 0015: `config_versions` (published, pending, rejected; changed fields plus a full snapshot) and `admin_links` (SHA-256 of the token, expiry, revoke).
- Every change to hours, services, rules or the knowledge base is a version: the app's practice update, closures, the doctor's link and rollbacks. The first change also stores a baseline, so it can be undone too. A full practice update keeps closures.
- Founder API: `GET /practices/<id>/versions[?status=pending]`, `POST .../versions/<vid>/approve|reject|rollback`, `POST /practices/<id>/links` (optional `staff_id`, `hours`), `DELETE /practices/<id>/links`.
- Magic link page `/manage/<token>` (public, no-store, noindex): hours and closures apply after a confirm dialog; services, prices and FAQ go to the approval queue. A staff link only sets that person's leave.
- Tests: `backend/tests/test_config_changes.py`. Checked in a local server and headless Chromium at 390 px. Not yet built: OP2 by phone (caller ID + PIN) and by SMS.

## Built 2026-09-25 (later): ops, onboarding, security

Migrations 0016 and 0017. 66 backend tests; each part below has its own test file.

- **OP8 patient data:** `POST /practices/<id>/data/export|erase` (phone in the body), log in `data_requests` (hashed number). Erase refuses while an upcoming appointment exists.
- **OP9 alerts:** `alerts` table, `GET /alerts`, `POST /alerts/<id>/ack`; emailed/texted to `FOUNDER_EMAIL`/`FOUNDER_SMS`, unacknowledged after 30 min to `BACKUP_EMAIL`/`BACKUP_SMS`. Raised for emergencies, failed notifications, cost cap, spam blocks, wrong PINs, LiveKit/agent down.
- **OP10:** monthly cost cap (alert at 80%, refuse at 100%), blocked numbers (silent hang-up), 3 silent calls in 24 h auto-block.
- **OP7 offboarding:** stops answering, revokes links, cancels queued calls, emails the ##002# code, CSV export; recordings and transcripts purged 30 days later; `/reactivate` undoes it.
- **O1/O2/O4/O5 onboarding:** Google Places import (needs `GOOGLE_MAPS_API_KEY`), price list from photo/scan/PDF/website via Gemini (checked live on a Greek price list), both into the approval queue; forwarding codes.
- **OP2 by SMS and phone:** registered staff mobiles; Gemini parses, backend checks, reads back, applies on yes. Phone needs the practice PIN (3 tries), recording stops first and PIN digits are masked. SMS needs Telnyx messaging pointed at `/webhooks/telnyx`.
- **OP1:** LiveKit check every 5 min; agent probe job (off by default, `HEALTH_AGENT_CHECK_MINUTES`, OOM risk); `notifications.fallback_number` setting. The Telnyx "forward on failure" script (`scripts/setup_failover.py --apply`, needs the Greek DID) and the daily real test call (scheduler `test_call`/`check_test_call`) are now built; neither has run against a real number.
- **Language (owner's rule):** always Greek; English only on "English mode", matched in code.
- **Encryption at rest:** `app/crypto.py`, patient-data columns AES-GCM, phone numbers AES-SIV (lookups still work). Off until `DATA_ENCRYPTION_KEY` is set; then run `scripts/encrypt_existing.py`.
- **iOS:** Face ID on Ιστορικό and Γραμματεία (60 s re-lock, app switcher cover) and on approve/rollback/links/exports/PIN/offboarding; settings screens for all of the above.

## Remaining engineering work

- **O3 Google Calendar sign-in** is built (migration 0018, `app/google_oauth.py`, iOS "Ημερολόγια Google"). It needs a Google OAuth "Web application" client: set `GOOGLE_OAUTH_CLIENT_ID`/`_SECRET` and the redirect URI `<BACKEND_PUBLIC_URL>/oauth/google/callback`. Unverified apps allow 100 test users.
- **OP1:** `scripts/setup_failover.py` (dry run by default) sets Telnyx on-failure forwarding; run it with `--apply` once the Greek number and the practice's fallback mobile exist. The daily real test call is built: with `practice.notifications["test_call"]` = `{"enabled": true, "number": "+30…", "time": "HH:MM"}`, the scheduler places one real outbound call a day (purpose `test`, no business email) and raises a `test_call_failed` alert (OP9) if it does not connect. Off by default; not yet run against a real number.
- **Encryption:** on in production since 2026-09-25 (key in the owner's `~/.config/voiceagent/`); recordings are sealed by the scheduler after upload.
- **Push notifications** need a paid Apple developer account.
- **Agent health probe** stays off until the agent has more memory.
- **Rotate exposed keys before production (2026-09-26).** The Google Maps API key, Google OAuth client secret and client ID were shared in an assistant chat session on 2026-09-26 and are in use for testing only. Before any production/pilot use: regenerate the Google OAuth client secret and rotate/lock down the Maps API key (restrict it to the Places API and to a backend referrer/IP). This is in addition to the still-pending rotation of the LiveKit secret and Railway workspace token (exposed 2026-09-24, tracked in CLAUDE.md's "Security state").

## Remaining acceptance and onboarding work

- Run the M0 ten-call/calendar acceptance set, then the M1 thirty scripted voice calls; manually review routing and booking correctness. Backend tests are not a substitute for these voice evaluations.
- Measure real caller-to-agent latency, notification delivery within 60 seconds, hard duration limits, concurrent inbound admission, handoff timeout and recording refusal end to end. No voice-latency improvement is claimed by this backend pass.
- Verify deployed email/SMS configuration, recipient delivery, Calendar permissions and external-calendar synchronization. These services' current production configuration was not inspected in this pass.
- Complete Greek DID/forwarding setup, DPA/legal review and vendor residency/account checks before pilot acceptance. Existing repository notes identify these as onboarding dependencies; their external status was not reverified.
- Validate the iOS receptionist and handoff flows on device, including push entitlements. No iOS code changed in this pass.

## Onboarding, 2026-09-26

The 2.0 PRD file is kept local (gitignored) and was not available in the cloud session, so the IDs below are the ones the code and this file already cite. Check the PRD for onboarding items not listed here.

| PRD item | Status | Notes |
| --- | --- | --- |
| Vertical template -> practice | Done | `GET /verticals[/<id>]`, `POST /practices`. New: unknown `vertical` -> 422; a number already used by another active practice -> 409 `number_in_use` (inbound calls are matched by dialed number). iOS "Νέα επιχείρηση" (+ in Γραμματεία, and the empty state) posts the template with name, number, email and demo slug. |
| Staff (R2) | Done | `POST /practices/<id>/staff` now rejects unknown `service_ids` (422). iOS: add staff from the go-live checklist. |
| O1 Google profile, O2 price list, O4 approval queue | Done | Unchanged. Need `GOOGLE_MAPS_API_KEY` / `GEMINI_API_KEY`. |
| O3 calendars | Built, needs credentials | Checklist item `calendars`: every calendar of the practice and its active staff needs a doctor sign-in or the service account. Needs `GOOGLE_OAUTH_CLIENT_ID/SECRET` or `GOOGLE_SERVICE_ACCOUNT_JSON`. |
| O5 number and forwarding | Blocked on the Greek DID | Checklist `numbers` (required) and `forwarding` (optional, confirmed by hand after dialing the codes). `scripts/setup_inbound.py` still has to be run per number. |
| G1 AI disclosure | Done | A custom greeting must say it's a digital/AI assistant; the default one does. |
| G7 recording notice | Open (owner's decision) | Required checklist item: the greeting must say the call is recorded. No greeting says so yet (still testing). |
| G2 DPA | Record built, text needs a lawyer | `PUT /practices/<id>/onboarding` with `dpa: {signed_on, signed_by}`. The DPA text itself is not written. |
| Test call (M0) | Done | Counted from a completed inbound/web call, or confirmed by hand. |
| Notifications, OP1 fallback, OP9 alerts, OP8 encryption | Built, needs credentials | Shown as `not_configured` when SMTP, Telnyx SMS, `FOUNDER_*` or `DATA_ENCRYPTION_KEY` are missing. Email is required, the rest are warnings. |
| Go-live | Done | `GET /practices/<id>/onboarding` (checklist), `POST /practices/<id>/go-live` (409 lists what is missing). It records `live_at`; it does not stop calls, so the live demo practice keeps answering. |

- Migration 0019 adds `practices.onboarding` (JSON). Checked on a local Postgres 16: upgrade, downgrade, upgrade. Not deployed.
- Tests: `backend/tests/test_go_live.py` (5 unit, 2 Postgres). 85 tests pass against Postgres. Endpoints also checked over HTTP with a local server.
- iOS: `OnboardingView.swift` (new business, checklist, DPA, confirmations, staff) plus entries in Γραμματεία and Ρυθμίσεις. Not compiled (Linux session): build it in Xcode before installing.

## Deployment: move backend + Postgres to EU West (before Greek go-live), 2026-09-26

Follow-up for when the service goes live in Greece (Alex to do on return). Not started.

- Move `backend` and `Postgres` from Railway US West to EU West. The `agent` already runs in EU West (LiveKit region "Germany 2"); this puts the whole stack closer to Greek callers.
- Keep `backend` and `Postgres` co-located in the same region.
- Needs a Postgres data migration (dump/restore, or a Railway region move). Do it in a quiet window: live push holds subscribers in memory on a single backend instance, so the switch drops open connections.
- The backend URL (`backend-production-c085.up.railway.app`) may change; the iOS app and the agent both point at it and would need the new URL.
- Check whether the `recordings` S3 bucket should also move (latency, and EU/Greek data-residency rules for call recordings).

## Reproduce

From the repository root, with the local Docker Postgres running:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://user:password@localhost:5433/voiceagent \
uv run --python 3.12 --with-requirements backend/requirements.txt \
  --with pytest --with pytest-asyncio pytest -q backend/tests -c backend/pytest.ini
```

Tests create and drop random schemas. Leave `TEST_DATABASE_URL` unset for the unit/provider-response subset. Keep this pointed at a local test database.
