# AI Caller — Claude notes

See [README.md](README.md) for layout, setup and deploy, and the PRD for the spec.

## Current work — read first (2026-10-30)

Refactor in phases, approved by the owner. Plan and findings: `AUDIT.md`, `MIGRATION_PLAN.md` (PR #45, branch `audit/plan`). Older sections below are history; where they disagree, this section wins.

**Owner's decisions (binding):**
- Priorities: lowest latency, leanest codebase. Remove anything the receptionist doesn't need. Keep whichever models measure best; never swap a model or provider that isn't clearly better.
- Latency bar: answer delay median ≤ 1.1 s, p95 ≤ 1.45 s; cost ≤ $0.04/min. No regression without the owner's explicit OK. Gate: ≥ 30 scripted audio calls before/after, plus text evals ×3 with zero critical failures.
- Invariants: (1) never book/move/cancel without the caller's explicit yes, checked against the caller's own transcript; (2) only offered slots; (3) the backend decides what's true and speaks the readback; (4) evals pass/fail on DB state.
- Git: small commits, one concern each, **no "Co-Authored-By" or "Generated with" lines** in commits or PRs. Merging is the owner's.
- Language rules unchanged unless the owner asks (Greek first; "English mode" matched in code).
- Muse Voice Transcribe: 4.0 spike first (Greek on 8 kHz audio). Only if it works: shadow mode, then switch only if it beats Scribe on accuracy (names, days, times, yes/no) AND latency. Otherwise drop Muse entirely.
- LLM bake-off: only Gemini Flash-Lite (incumbent) vs Claude Haiku 4.5. Muse Spark only off the hot path (summaries), and only if it adds no complexity.
- No LangGraph: a plain Python state machine in the backend (plan option B), no new dependencies.
- Call cap 12 minutes. Doctors only for now (vertical config deferred), but no dental wording in new code.
- Spoken-number lookups: name match before confirming anything exists, no details before an SMS code to the number on file. Admin changes by SMS off; phone only, transcript-based yes.
- Phase order: 0 → 1 → 2. The owner said there is no hackathon deadline to plan around (2026-10-30).
- `main` branch protection requires Backend tests, Agent tests and iOS build (and up-to-date branches). CodeQL was removed from the required checks on 2026-10-30, with the owner's OK: code scanning is off on the private repo, so it could never pass.

**Status:**
- Phase 0: merged (PR #46, 2026-10-30). Still to do on Railway: set `MAX_CALL_DURATION_SECONDS=720` (it is 300), set `SMS_FROM` for SMS codes, run `setup_inbound.py` with the carrier's signalling IPs, and run the 30-call voice latency check.
- EU: PR #47 (`deploy/eu`), runbook `docs/eu-deployment.md`. Railway backend, Postgres and agent are already in EU West Amsterdam; recordings bucket is in `ams`. Telnyx media anchor switched to Frankfurt on both credential connections (2026-10-30; undo = "Latency"). Gemini on Vertex AI `europe-west3` is coded behind `GEMINI_VERTEX_LOCATION` (off); the owner must first run the two `gcloud` commands in the runbook, then measure with `agent/scripts/compare_gemini_endpoints.py` from Railway.
- LiveKit (2026-10-30): switched to a new LiveKit project with rotated API keys. Railway backend and agent point at it via `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`; both redeployed successfully. The new project has one outbound trunk, copied from the previous active one (same Telnyx address, numbers and credentials) plus `destination_country=de`; its ID lives in Railway's `SIP_TRUNK_ID`. It has no inbound trunk and no dispatch rule — the old project never had them either, so inbound SIP is still unwired. The old LiveKit project still exists; delete it once everything is verified. Still pending (owner): enable the inference region restriction in the new project (dashboard → Data and privacy).
- Phase 1a (remove the prank era): PR #49 (`phase1/cleanup`). Migration 0031 drops `friends` / `prompt_templates` and the dialer-only call columns; the database was backed up first (2026-10-30 18:03, encrypted in `~/.config/voiceagent/backups/`, checked with a full restore). `AGENT_ENGINE` is no longer read (`RECEPTIONIST_ENGINE` is). Text evals ×3: 45/45, no regression against Phase 0. Agent name `prank-caller` and the bundle id are unchanged.
- Next: Phase 1b (SDK upgrade to the latest 1.8.x, its own latency check) and 1c (STT/LLM/TTS provider modules), then the Phase 2 harness, per `MIGRATION_PLAN.md`.

**Needs the owner's machine or accounts (a cloud/Slack session can't do these):** Railway changes, `gcloud`, Telnyx/LiveKit dashboards, real phone calls, the Muse spike (needs a Meta Model API key and recordings or synthetic 8 kHz Greek audio), and anything using the local `.env` or `~/.config/voiceagent` keys. A cloud session can write code, run unit tests and text evals (if `GEMINI_API_KEY` is available), and open PRs.

**Open questions for the owner:** Meta Model API key for the Muse spike; LiveKit status above; confirm that after a correct SMS code the appointment details may be read back (implemented that way).

## Direction change (2026-09-25, not yet deployed)
- Calls are now professional, not pranks: no reveal, never mentions Alexandros. It still says it's an AI if asked directly, and deletes the recording on request.
- Receptionist language (2026-09-25, owner's rule): every call starts in Greek, whatever the caller's number. English only when the caller says "English mode" (also "ίνγκλις", "αγγλικά"); "Greek mode"/"ελληνικά" switches back. The agent matches this in code on the Deepgram transcript (`wants_language`), never the model: Gemini Live misheard casual Greek as another language and refused with "I only speak Greek and English". That refusal line is gone. Voices: every call (Greek or English) uses only Aria (female `Kore`, the default) or Fatsis (male `Zubenelgenubi`), in `agent/voices.py` (owner, 2026-11-05). The English premade voices sounded foreign/Cypriot in Greek. Male/female toggle: iOS Settings (`PUT /practices/<id>/voice`) and per call on the web demo page. Slower ElevenLabs models sound no better for this and add 0.7–1.1 s per reply.
- Still in testing, so no up-front "this call is recorded" notice. Add one (or turn recording off) before real use: Greek law generally requires it.

## 2.0 receptionist (2026-09-25, feature implementation)
Current reliability fixes and remaining acceptance gaps are tracked in `PRD_STATUS.md`; feature presence does not mean every PRD requirement has been verified.
Spec: `AI Voice Receptionist 2.0 PRD.md`. Migrations 0012 + 0013. Nothing has run with a real voice yet.
- **Backend modules:** `booking.py` (dates, slots, per-staff calendars, book/reschedule/cancel under an advisory lock, idempotent per call+slot+service+staff), `routing.py` (R1-R9 from `practices.routing_rules`, every decision in `routing_events`), `receptionist.py` (call start, metadata, all agent tools, reminder/waitlist calls), `notifications.py` (outbox + worker: SMTP email, Telnyx SMS, APNs push; unset channels remain pending), `finalize.py` (outcome, Gemini summary, cost, one email per call), `scheduler.py` (20:00 digest, monthly report, reminders, retention, stale calls), `metrics.py`, `gcal.py`, `texts.py`.
- **Setup:** `GET /verticals/<id>` gives a template (`config/verticals/*.json`); POST it to `/practices` with name, numbers, `notifications.emails`; staff via `/practices/<id>/staff`. Emergency rule is on for health verticals.
- **Agent** (`run_receptionist`, `ReceptionistCall`): inbound SIP (no metadata), web demo, outbound reminder/waitlist (`dial_number`). Tools: route_call, check_availability, book/find/reschedule/cancel/confirm_appointment, take_message, transfer_to_human (in-app join waits `timeout_seconds`, or SIP transfer), add_to_waitlist, stop_recording. Emergency phrases are matched in code on the caller's words. A language switch hands over to a new agent with a model pinned to that language. Busy practice (G8) -> one line and hang up.
- **Web:** `/demo/<slug>` page, `/demo/<slug>/widget.js` "Call us" button (W3).
- **Tests:** `backend/tests` (12 unit tests); e2e against local Postgres covered staff routing, anyone-free, find-before-change, ask-twice handoff, clarify-then-message, language switch, waitlist offer on cancel, reminders, digest/monthly dedupe, metrics. `agent/scripts/scripted_calls.py` + `scenarios.el.json` = W4 harness (needs a deployed backend + worker, ELEVEN_API_KEY, APP_API_TOKEN).
- **iOS:** new "Γραμματεία" tab (calls with outcome filters, detail with summary/routing/transcript/recording/review, messages, appointments, metrics, handoff join/listen via LiveKit Swift SDK). Push code is in, but a free Apple team can't get push: the app falls back to live updates while open. DEBUG launch arg `-startTab 2`.
- **Listening (2026-09-25):** Gemini Live's own transcription heard casual Greek as Italian/Spanish and rewrote swear words, even with `language_codes=["el-GR"]` pinned. ElevenLabs Scribe v2 pinned to `el` was near perfect on slang and swearing. So receptionist calls use the `pipeline` engine (`RECEPTIONIST_ENGINE`, default pipeline; `realtime` goes back to Gemini Live). Scribe realtime rejects the session if any keyterm is over 20 characters (`vocab_terms` splits them). About 1.5–1.8 s from end of speech to first audio (turn detection waits up to 1.5 s: no Greek model). Calls never switch language. Fillers are polite and at most one per turn. Before each pipeline call the agent sends a ~1-credit ElevenLabs TTS request; if it fails (no credits, down, >2.5 s) the call uses Gemini Live instead (2026-09-25: the account ran out and calls were silent). On Gemini Live the caller's transcript lines come from Deepgram nova-3 (LiveKit Inference), not Gemini: Gemini's own transcript of Greek was Portuguese/Spanish/German fragments while its replies were right. Gemini still emits its transcript events (they carry an `item_id`); `CallerTurns` ignores those.
- **Live on Railway:** demo practice "Οδοντιατρείο Παπαδοπούλου" (slug only in the local scratch notes, not here), two dentists each with a Google Calendar created by the service account `receptionist@ai-receptionist-59557` and shared with the owner. Telnyx balance was $0.79 on 2026-09-25; Greek mobiles cost ~$0.37/min from the US number.
- **Needs from the user:** SMTP + Telnyx SMS credentials, Google service account, Greek DID, run `backend/scripts/setup_inbound.py`, a lawyer for the DPA (G2).

## Checkpoint (2026-09-25, end of session)

**Live on Railway now**
- Agent: `AGENT_ENGINE=realtime` (Gemini `gemini-3.8-live`), `REALTIME_SILENCE_MS=400`, `NUM_IDLE_PROCESSES=0`, `NOISE_CANCELLATION=off`. Each turn is logged as `call <id> friend|agent: <text>`: read those first after a test call.
- Backend: master prompt cut to speaking style + safety, with a strict "only the call's language" rule (dropping it made Gemini drift into Spanish/Chinese). The app sends one free-text description in `scenario`; persona/context/reveal are optional (older builds still work). Migrations 0009 (fold presets into one text) and 0010 (delete built-in presets) are deployed; only user-saved presets remain. Not confirmed on live data.
- iOS (installed on the iPhone 2026-09-25): one textbox, preset chips that fill it, "Αποθήκευση ως σενάριο". Default voice is male (`Puck`).

**What we learned**
- Agent jobs were OOM-killed (exit -9) on Railway: idle worker ~530 MB, and every extra process is ~400 MB. `NUM_IDLE_PROCESSES=0` fixed it for the one call since (adds ~2 s before dialing). The real fix is more memory, or hosting the agent on LiveKit Cloud (which also enables the cloud turn detector).
- Pipeline engine problems: Scribe needs `server_vad` (manual commit never finalises mid-call) and `language_code` (otherwise Greek comes back as Cyrillic "Ukrainian"). Even then it garbles Greek phone speech ("σακούλα" for "σ' ακούω"). The local turn detector has no Greek, so every turn waited `max_delay`. Replies took ~1.5–2.5 s.
- Recognition test on a real recording (streamed 4x speed): Scribe v2 was mostly right with key errors; Gemini 3.5 Transcribe Live and 3.8 Live's input transcript were garbage (no language hint given in the test). On a real call, Gemini 3.8 Live understood Greek fine until the language drift. Recordings are mixed mono-in-stereo (both voices on both channels).
- Call from my number (Greek number shown on a US trunk) fails. Likely Greek anti-spoofing blocks international calls that show a +30 number. Needs a Greek route (the 210 number). Leave the toggle off.

**Next**
1. Test call on the current setup; read the `friend:`/`agent:` log lines.
2. OpenAI engine (`AGENT_ENGINE=openai`): the user tested it (2026-09-25) and its Greek was as bad as Gemini's. Decision: stay on Gemini. The code stays but is off. English works well on both; Greek is weak in the models themselves, not our setup.
3. Move the agent to LiveKit Cloud hosting, or give it more memory.

## Checkpoint (2026-09-24)

Code-complete through M6. First real call worked end to end (2026-09-24) on a **US Telnyx number** (the LiveKit outbound trunk is set to TCP): dial → answer → Gemini speaks Greek → transcript → hang-up.
- Recordings go to the Railway bucket `recordings` (S3-compatible, `AWS_*` variables referencing `${{recordings.*}}`); verified with a real egress on 2026-09-25.
- Calls that don't connect get `end_reason` (no_answer / declined / unreachable / error); the app shows it in Greek with a retry button.
- The app is Greek-only and named "AI Caller" on the home screen; internal ids (`com.alekos.prankcaller`, agent `prank-caller`) are unchanged on purpose.
- iOS installs from the CLI: `xcodebuild ... DEVELOPMENT_TEAM=<personal team> -allowProvisioningUpdates` then `xcrun devicectl device install app`. Free team: the app expires after 7 days.
- The user's own mobile never rings (408 after the ring window); another Greek mobile does. Cause unknown — likely the carrier or a spam filter on that line, not our code.
- LiveKit has a duplicate, unused outbound trunk.
- Never put phone numbers, trunk IDs or keys in committed files (the repo is private now, but was public).

**Done and verified**
- Backend on real Postgres (Docker): migrations, auth, agent-event endpoint, WebSocket push, delete-on-request (R2/LiveKit stubbed).
- `POST /webhooks/telnyx` ([backend/app/routers/webhooks.py](backend/app/routers/webhooks.py)): ed25519 signature check when `TELNYX_PUBLIC_KEY` is set (valid → 200, tampered → 401). It only logs; it changes no call state.
- Agent imports and instantiates on `livekit-agents` 1.8.3 in Docker.
- iOS builds and launches in the simulator; templates load.

**Not done / blocked**
- M1: 210 number on Telnyx + SIP trunk into LiveKit. Needs the user in Greece.
- `.env` is filled for LiveKit, Gemini, Telnyx and the SIP trunk; R2 keys are missing.
- Voicemail hang-up and the callee-speaks-first greeting are written but not yet tested on a real call; the hard duration cap is untested.
- iOS call flow has not been driven from the app.
- Deployed on Railway (project `zonal-enjoyment`): services `backend` (root `backend`, health `/health`), `agent` (root `agent`) and `Postgres`. Backend at `https://backend-production-c085.up.railway.app`. Railway ignores `railway.toml` now; service settings are set on the services (via dashboard or GraphQL API). Don't run the local agent at the same time — both would take jobs.
- Open questions: Gemini Greek quality over phone audio, latency, 210 caller ID on Greek mobiles.

**Voice pipeline (2026-09-25)**
- Agent has two engines (`AGENT_ENGINE`): `pipeline` (ElevenLabs Scribe v2 realtime -> gemini-3.5-flash-lite -> ElevenLabs Flash v2.5, LiveKit Cloud turn detector, fillers, opening line pre-rendered with eleven_v3 during the ring) and `realtime` (Gemini Live). Railway ran `pipeline` from 2026-09-25 but Scribe mangled Greek phone audio; since 2026-09-25 it runs `realtime` again (`REALTIME_SILENCE_MS=400`) (new ElevenLabs key with `text_to_speech` + `speech_to_text`; `voices_read` isn't needed). Not yet tested on a real call; fall back with `AGENT_ENGINE=realtime`.
- The agent runs in Railway EU West (LiveKit region "Germany 2"); backend + Postgres stay in US West together. Before the Greek go-live, move backend + Postgres to EU West too (co-located) — tracked in `PRD_STATUS.md` ("Deployment: move backend + Postgres to EU West").
- Don't use `livekit-plugins-turn-detector` on Railway: its local model process gets OOM-killed and takes the worker down. Use `inference.TurnDetector(local_fallback=False)`. Off LiveKit Cloud hosting it resolves to the local v1-mini model, which has no Greek, so Greek turns end on `max_delay`. Forcing `version="v1"` (cloud) got every call OOM-killed (exit -9) on 2026-09-25.
- Test without phoning: local worker with `AGENT_NAME=prank-caller-test`, dispatch with metadata `test_no_dial: true` (skips dial and recording). Realtime engine measured ~2 s from end of speech to reply.
- Call language comes from the friend's phone prefix (`backend/app/languages.py`); non-Greek calls use `config/master_prompt.en.md`.

**Call from my number**
- `OWN_CALLER_NUMBER` (the owner's Telnyx-verified number) is set on Railway and added to the LiveKit trunk's numbers. The app shows a "Call from my number" toggle when `/options` says it's available. Not yet tested on a real call.

**Security state**
- All routes except `/health` need `x-api-key` (app) or `x-agent-token` (agent); `/docs` and `/openapi.json` are off unless `ENABLE_DOCS=true`; the Telnyx webhook rejects unsigned requests and returns 503 when no key is set.
- iOS keeps the API key in the Keychain (this device only).
- The LiveKit secret and the Railway workspace token were pasted in chat on 2026-09-24: rotate both.

## Working notes
- Live call timings and log: `<backend>/monitor` with the founder key (`ADMIN_API_TOKEN`); see `docs/telemetry.md`.
- Python 3.14 breaks venv/ensurepip here; run backend code with `uv run --python 3.12 --with-requirements backend/requirements.txt ...`.
- Run a single backend instance (live push keeps subscribers in memory).
- Never commit `.env`.
