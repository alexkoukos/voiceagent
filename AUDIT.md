# Audit — AI receptionist (Phase 1, read-only)

Date: 2026-10-30. Branch audited: `main` at `f422681`. Nothing in the code was changed.

Scope: `agent/` (LiveKit worker), `backend/` (FastAPI + Postgres), `ios/`, CI, docs, git history.
Unmerged work on other branches (`codex/pilot-readiness` +4 commits, `origin/codex/tenant-isolation-connection` +1) was not audited.

Notation: **P0** = broken or unsafe, fix before any real patient call. **P1** = important. **P2** = nice to have.
Effort: **S** < 1 day, **M** 1–3 days, **L** > 3 days.

---

## 1. What the stack really is (checked against the code)

| Layer | What runs | Where in code |
|---|---|---|
| Telephony | Telnyx SIP trunk → LiveKit SIP inbound trunk + dispatch rule (`inbound-` rooms). Outbound and SIP transfers use one trunk id from env (`SIP_TRUNK_ID`). | [backend/scripts/setup_inbound.py](backend/scripts/setup_inbound.py), [agent/agent.py:1461](agent/agent.py#L1461) |
| Worker | `livekit-agents==1.8.3`, one process per job, agent name `prank-caller`. | [agent/requirements.txt](agent/requirements.txt), [agent/agent.py:1957](agent/agent.py#L1957) |
| STT | ElevenLabs Scribe v2 realtime, pinned `language_code=el`, server VAD 0.7 s silence, keyterms. Deepgram nova-3 (LiveKit Inference) on the fallback `text_pipeline` engine. | [agent/agent.py:820](agent/agent.py#L820), [agent/agent.py:915](agent/agent.py#L915) |
| Turn detection | Greek + Scribe: Scribe's end-of-speech, `min_delay=0`. English: LiveKit cloud turn detector. Silero VAD for barge-in. | [agent/agent.py:792-815](agent/agent.py#L792-L815) |
| LLM | `google.LLM("gemini-3.5-flash-lite")`, thinking `minimal`, **temperature 0.9**. Preemptive generation on, no preemptive TTS. | [agent/agent.py:767-774](agent/agent.py#L767-L774) |
| TTS | ElevenLabs `eleven_flash_v2_5`, PCM 24 kHz, native Greek voices. Fixed lines (greeting, goodbye) pre-rendered with `eleven_multilingual_v2` and cached on disk. | [agent/agent.py:750](agent/agent.py#L750), [agent/opening.py:126](agent/opening.py#L126) |
| Engines | `pipeline` (above, default), `text_pipeline` (Deepgram → Gemini → Gemini TTS, chosen automatically if ElevenLabs fails a pre-call probe), `realtime` (Gemini Live), `openai` (OpenAI Realtime, off). | [agent/agent.py:1036](agent/agent.py#L1036) |
| Backend | FastAPI, async SQLAlchemy, Alembic migrations 0001–0030, in-process scheduler + notification worker, in-memory pub/sub. One instance only. | [backend/app/main.py](backend/app/main.py) |
| Data | Postgres; patient fields AES-GCM/AES-SIV when `DATA_ENCRYPTION_KEY` is set; recordings in an S3-compatible bucket, sealed (encrypted) by the scheduler after upload. | [backend/app/crypto.py](backend/app/crypto.py), [backend/app/storage.py](backend/app/storage.py) |
| Hosting | Railway. Agent in EU West; **backend and Postgres in US West** (per PRD_STATUS.md). | — |
| Telemetry | Worker → `POST /internal/calls/{id}/telemetry` (content-free events), `/monitor` (founder key), `/demo-dashboard/{token}` (shareable). | [agent/telemetry.py](agent/telemetry.py), [backend/app/routers/demo_dashboard.py](backend/app/routers/demo_dashboard.py) |

The "answer delay" on the demo dashboard (your 1.1 s / 1.45 s bar) is `answer_latency_estimate`: from the SDK's "caller stopped speaking" (VAD) to the **first non-filler LLM text chunk** (or the moment a readback is queued). It does not include TTS first byte, network or playout. `response_latency_estimate` ("first sound") is the closer proxy for what the caller hears. Every phase below reports both, so the bar stays comparable ([agent/telemetry.py:86-102](agent/telemetry.py#L86-L102)).

CLAUDE.md is out of date in places (it says the repo is public, it is private; it describes Railway running `realtime`, the code defaults to `pipeline`). Treat the code as the source of truth.

## 2. One inbound call, end to end

```
Caller ── PSTN ── Telnyx ── SIP INVITE ──> LiveKit SIP (inbound trunk, no IP/auth restriction)
                                              │ dispatch rule → room "inbound-<id>", agent "prank-caller"
                                              ▼
Worker process (agent.py entrypoint)
 1. ctx.connect(); wait_for_participant()                                   agent.py:1612
 2. POST /internal/inbound {dialed, caller, forwarding_reason}              agent.py:1620
      backend: practice by number → blocked? cost cap? admission lock →
      Call row, routing events, prompt (+both languages), greeting           receptionist.py:156-294
      429 → say busy line, hang up.  Any other error → silent hang-up.      agent.py:1624-1633
 3. pick_engine: ~1-credit ElevenLabs TTS probe (≤2.5 s)                    agent.py:1015-1049
 4. build_session (Scribe STT, Flash-Lite, ElevenLabs TTS, Silero VAD)      agent.py:777
 5. start egress recording → S3 bucket (plain MP4, sealed later)           agent.py:1415
 6. report status=active; session.start; say pre-rendered greeting         agent.py:1674-1750

Per turn (pipeline engine):
 caller audio → (BVCTelephony if NOISE_CANCELLATION=on) → Scribe WS → final transcript
   → CallerTurns → heard_user / check_emergency / check_profanity / check_language   agent.py:1689-1695
   → Gemini Flash-Lite (prompt + history + 17 tools)
       └─ tool call → ReceptionistCall.tool (one at a time, lock)                   agent.py:1285
            → new httpx client → POST {backend}/internal/calls/{id}/tools/<name>    agent.py:112
                 backend decides (routing.py, booking.py, receptionist.py), writes routing_events
            → LLM runs again on the tool result
   → ElevenLabs Flash TTS (pronunciation aliases, "!"→".") → room
 Transcript lines → POST /internal/calls/{id}/events (fire-and-forget)              agent.py:1082-1098

Booking (the guarded write):
 check_availability(when=caller's words) → backend resolves date, free slots
     (DB + Google freeBusy + iCal), stores the offer as routing_events kind=offer   receptionist.py:602-635
 prepare_action(...) → backend checks the slot was offered (≤5 min), builds the
     Greek readback, stores kind=confirmation                                      receptionist.py:687-759
 worker speaks the readback verbatim (session.say) and arms the confirmation        agent.py:1248-1272
 caller answers → next tool call carries {confirmation_id, confirmation_text=last
     caller transcript} (added by worker code, not by the LLM)                      agent.py:488-495, 1274
 book_appointment → _confirmed: yes-detector + details match the stored readback   receptionist.py:762-786
     → booking.book under a per-practice advisory lock, re-checks availability,
       idempotency key, Google event with deterministic id                          booking.py:629-717

Hang-up: hang_up tool (fixed goodbye) or duration cap (300 s) or repetition guard
 → shutdown callback reports completed + recording key + median latency             agent.py:1721-1729
 → backend: costs, routing log, finalize (Gemini summary, one email), queued calls   internal.py:84-140
```

## 3. Findings

### P0 — fix before any real patient call

**P0-1. The "yes" check accepts corrections and questions (invariant 1).** [backend/app/receptionist.py:653-662](backend/app/receptionist.py#L653-L662) — Effort **S**
`_affirmative` is a bag-of-words test: any yes-word anywhere and no word from a short negative list. I ran it on realistic replies to a readback; all of these return `True` and would book/move/cancel:
`«Ναι ναι, αλλάξτε την ώρα»`, `«Σωστά, όμως θέλω Τρίτη»`, `«Ναι, μόνο που θέλω άλλη ώρα»`, `«Ναι; Ποια ώρα είπατε;»`, `«Σωστά κατάλαβα ότι είναι Τρίτη;»`, `«Περιμένετε, ναι»`, `«μισό λεπτό, ναι»`, `«Εντάξει, για Πέμπτη μάλλον»`, `«ok actually make it 10»`.
Why it matters: this is the only gate between the LLM and a write. Fix: accept only short replies (≤ ~6 words) that start with or consist of a yes-form, contain no question mark, and contain no change/hesitation words (`όμως, μόνο, περίμενε/περιμένετε, μισό, στάσου, άλλαξ-, αλλάξ-, κάν' το, μάλλον, actually, instead, change, make it`, any weekday/time/number). Everything else returns "ask again". Add the list above as a regression table.

**P0-2. Anyone can hear and change another patient's appointment by saying a phone number.** [backend/app/receptionist.py:852-866](backend/app/receptionist.py#L852-L866), [agent/agent.py:618-625](agent/agent.py#L618-L625) — Effort **M**
`find_appointments(phone=...)` takes any number the caller says, returns name, date, time and **service** (for a doctor this is health data), and marks those appointments as "found", which is all `cancel`/`reschedule` need besides a "yes". The calling number itself is also spoofable (see P1-9: the LiveKit inbound trunk accepts INVITEs from anyone). Fix: for a number other than the caller ID, require the caller to state the full name and match it before anything is disclosed or changeable; never speak the service for a non-caller-ID match; optionally SMS a code to that number before a change. Log every lookup by spoken number.

**P0-3. The prank-era outbound dialer is still live.** [backend/app/routers/calls.py:57-88](backend/app/routers/calls.py#L57-L88), [backend/app/dispatcher.py](backend/app/dispatcher.py), [backend/app/livekit_dispatch.py](backend/app/livekit_dispatch.py), [agent/agent.py:1807-1952](agent/agent.py#L1807-L1952) — Effort **M**
`POST /calls` with the shared `APP_API_TOKEN` (stored on the phone) dials **any number in the world** with a free-text persona/scenario, optionally showing the owner's own number (`from_own_number`). There is no destination allowlist, no premium-rate block and no per-day cap beyond `MAX_CONCURRENT_CALLS`. One leaked app token = toll fraud and harassment calls from your trunk. The receptionist does not need it: reminders/waitlist/test calls go through `receptionist.queue_*` and only dial customers with a booked appointment. Remove the endpoint, the dialer UI tab and the outbound entrypoint (details in P2-1). Also set a Telnyx outbound voice profile with a destination allowlist (Greece only).

**P0-4. EU health-adjacent data is stored in the US.** PRD_STATUS.md "Deployment"; [docs/telemetry.md](docs/telemetry.md) — Effort **M**
Backend and Postgres run in Railway US West; the recordings bucket region is unverified. Transcripts, names, phone numbers, call summaries and recordings of Greek patients are therefore transferred to and stored in the US. This is already tracked, but it is a hard blocker for real calls, not a nice-to-have. It is also the single biggest latency win (P1-12). Move backend + Postgres + bucket to EU West together; sign DPAs with Google (Gemini, Calendar), ElevenLabs, LiveKit, Telnyx, Railway and (if adopted) Meta; record them in a processor list.

**P0-5. Settings can be changed by spoofed SMS, brute-forced PINs, or the LLM's own "yes".** [backend/app/admin_changes.py:295-317](backend/app/admin_changes.py#L295-L317), [backend/app/receptionist.py:1087-1148](backend/app/receptionist.py#L1087-L1148) — Effort **M**
- SMS changes (closures, hours, prices) are authenticated only by the sender number. Numeric sender IDs can be spoofed on grey routes. A fake "closed all next week" silently empties the calendar.
- Phone changes need the PIN, but the 3-try limit is **per call** ([receptionist.py:1091](backend/app/receptionist.py#L1091)). Redialling gives unlimited tries; a 4-digit PIN falls in a few thousand calls (alerts fire, nothing blocks).
- `admin_confirm(yes: bool)` applies the change on the **model's** boolean, not on the caller's transcript — the same weakness invariant 1 forbids for bookings.
- The PIN is spoken into the LLM context (sent to Google) through `admin_login(pin)`.
Fix: require the PIN (or a per-staff code) on SMS too; lock admin access per practice/number after N failures across calls with a cool-down; decide `admin_confirm` with the same transcript yes-check as bookings; capture PIN digits in code from the STT transcript (like "English mode") and never pass them through the LLM.

### P1 — important

**Invariants**

**P1-1. Confirmations are reusable and not tied to the stored transcript.** [backend/app/receptionist.py:762-786](backend/app/receptionist.py#L762-L786) — **S**
`_confirmed` sets `path="accepted"` but never checks that the event was still `pending`, so one confirmation can authorize several writes within 4 minutes (idempotency limits the damage for identical slots only). The backend also trusts `confirmation_text` as sent by the worker; it never checks it against the caller's `TranscriptEntry` rows. Fix: single-use (`pending` → `accepted` atomically), and require that `confirmation_text` equals the latest caller transcript row created after the confirmation event (report the transcript line synchronously before the tool call, or send its id).

**P1-2. On `realtime`/`openai` engines the readback is paraphrased by the model (invariant 3).** [agent/agent.py:1256-1260](agent/agent.py#L1256-L1260) — **S**
Pipeline engines use `session.say(result["say"])` (exact text). Realtime engines use `generate_reply("Say exactly this: …")`, which the model may reword or "correct". Either forbid writes on those engines or keep them off for receptionist calls (today `RECEPTIONIST_ENGINE=realtime` would enable it).

**P1-3. The model, not the backend, speaks what was booked and which times are free.** [backend/app/receptionist.py:842](backend/app/receptionist.py#L842), [backend/app/booking.py:507-536](backend/app/booking.py#L507-L536) — **S**
After a write, the LLM composes "κλείστηκε για …" from tool JSON; offers are also voiced by the LLM from `free_times`. Invariant 2 still holds for the write, but the caller can hear a wrong day/time. Return a backend-built `say` for the post-write confirmation and speak it verbatim like the readback; optionally the same for the first offer of each day.

**P1-4. Safety-critical fixed lines go through the LLM.** [agent/agent.py:1377-1391](agent/agent.py#L1377-L1391) (emergency), [1395-1411](agent/agent.py#L1395-L1411) (profanity), [1707](agent/agent.py#L1707) (time-up), [1526-1537](agent/agent.py#L1526-L1537) (handoff failure), [1595-1603](agent/agent.py#L1595-L1603) (busy line) — **S**
The 166/112 emergency script is spoken via `generate_reply("Πες αμέσως ακριβώς αυτό: …")`. It should be `say_fixed(script)` (pre-rendered at call start), then the model continues.

**P1-5. Test coverage of the invariants is thin.** — **M**
- Invariant 1: one end-to-end test ([backend/tests/test_confirmation.py](backend/tests/test_confirmation.py)) plus a yes/no word table ([backend/tests/test_call_feedback.py:12-28](backend/tests/test_call_feedback.py#L12-L28)) that has no "yes + correction" or question cases (hence P0-1). No test that a confirmation is single-use, expires, or survives a new `check_availability` in between; no test for reschedule/cancel confirmation paths separately; no test that `confirmation_text` is the caller's turn after the readback (agent side, [agent/agent.py:1274](agent/agent.py#L1274)).
- Invariant 2: covered for book; not for reschedule with a slot from another staff member's offer, nor for the 5-minute offer window edge.
- Invariant 3: nothing asserts that the readback is spoken verbatim (an agent-side test with a fake session can).
- Invariant 4: text evals score on DB state (good), but run once per PR (`--repeat 1`) on a non-deterministic model, with no fixed clock, and 2 of 15 cases are audio-only and skipped ([.github/workflows/evals.yml:73](.github/workflows/evals.yml#L73)).

**Security**

**P1-6. The founder key sits in `localStorage` on a page that shares its origin with public pages; no CSP.** [backend/app/templates/monitor.html:144](backend/app/templates/monitor.html#L144) — **S**
`/monitor` stores `ADMIN_API_TOKEN` (full access to every practice and patient record) in `localStorage`. The same origin serves the landing page, demo pages, the embeddable widget and a static asset tree; any XSS there reads the key. No `Content-Security-Policy` header anywhere. Fix: keep the key in memory only (or an HttpOnly session cookie scoped to `/monitor`), add a strict CSP, ideally serve admin pages from a separate host.

**P1-7. No rate limiting anywhere.** (no limiter in `backend/app`) — **M**
Public, unauthenticated, paid or sensitive: `POST /demo/{slug}/session` (starts a paid call), `POST /demo-dashboard/{token}/call/session`, `POST /leads`, `/manage/{token}` (magic links), `/recordings/{token}`, and SIP inbound itself. The only brakes are the global concurrency cap (1) and the practice's monthly cost cap. Add per-IP and per-token limits (and per-caller-number limits for inbound).

**P1-8. Demo dashboard token in the URL.** [backend/app/routers/demo_dashboard.py:106-127](backend/app/routers/demo_dashboard.py#L106-L127) — **S**
Good: 256-bit random, only the SHA-256 stored, expiry/revoke, `no-store`, `no-referrer`, numbers only, demo-only practices. Remaining risk: the token is in every request path, so it lands in Railway access logs and browser history, and the link also starts paid calls. Fix: on first visit exchange it for an HttpOnly cookie and redirect to a token-free URL; redact `/demo-dashboard/*` paths in logs.

**P1-9. LiveKit inbound trunk accepts INVITEs from anyone.** [backend/scripts/setup_inbound.py:28-29](backend/scripts/setup_inbound.py#L28-L29) — **S**
No `allowed_addresses` and no auth. Anyone who learns the LiveKit SIP URI can place calls into the agent with a forged `From` (caller ID), which feeds the "known caller" prompt and `find_appointments` (P0-2), and can trigger SIP transfers that cost money. Restrict to Telnyx (and later Twilio) signalling IP ranges and/or digest auth; use `;transport=tls`.

**P1-10. Webhook validation.** [backend/app/routers/webhooks.py](backend/app/routers/webhooks.py) — **S**
Telnyx: ed25519 + 5-minute timestamp window, fails closed without a key: correct. Missing: replay protection (dedupe on event id). Twilio: there is nothing yet. Note that Twilio **Elastic SIP Trunking does not use webhooks** for calls, so "Twilio signature validation" only applies if you add Twilio status callbacks or SMS; the real control for the trunk is the IP ACL / credential list (P1-9).

**P1-11. A competitor's website copy is publicly served.** [backend/app/main.py:63](backend/app/main.py#L63), `backend/app/templates/landing-assets/fonio/` — **S**
The `fonio.ai` replica (their logo, team photo, videos, fonts; 2.5 MB) is inside the static mount, so it is live at `/landing-assets/fonio/index.html` on the production domain. You chose to keep it versioned; hosting it publicly is a different thing and a legal/brand risk. Move it out of the served directory (or out of the repo).

**Patient data / GDPR**

**P1-12. Personal data stored in plain text and outside retention.** — **M**
- `routing_events.value` is a plain `String` ([backend/app/models.py:551](backend/app/models.py#L551)). It holds the confirmation JSON with the patient's **name and phone** ([receptionist.py:752-756](backend/app/receptionist.py#L752-L756)), the caller's literal reply (`confirmation_answer`, [receptionist.py:785](backend/app/receptionist.py#L785)), the caller number on `known_caller` ([routing.py:278](backend/app/routing.py#L278)) and appointment offers.
- Retention ([backend/app/scheduler.py:146-160](backend/app/scheduler.py#L146-L160)) deletes recordings, transcripts and telemetry only. Call summaries (Gemini-written, may contain health details), messages, routing events, notification bodies and alerts are kept forever. Customers/appointments need their own policy (e.g. N months after the last visit).
- What is stored per call: caller number, customer name, appointment (service = health data for doctors), transcript (both sides), Gemini summary, recording (inbound; web demo is not recorded), routing log, telemetry (content-free), notification copies (email/SMS bodies).
- Who can read it: founder key (everything), practice keys (one practice, enforced by `require_admin_token` + composite FKs), magic links (hours/closures/services only), the worker token (any call by id).
- Recording disclosure is now in the greeting ([backend/app/prompts.py:150-163](backend/app/prompts.py#L150-L163)). DPA, processor list and a DPIA (Art. 35, health data at scale) are still open.

**P1-13. The PIN and every utterance go to the LLM provider.** Covered in P0-5 for the PIN; for the rest, document it in the processor list and prefer zero-retention endpoints (Meta offers ZDR at parity price; check Gemini's paid-tier terms).

**Reliability**

**P1-14. Backend unreachable mid-call leaves the caller with nothing.** [agent/agent.py:1293-1303](agent/agent.py#L1293-L1303) — **M**
Every tool fails to `{"error": "tool_error"}`; the prompt says "take a message", but `take_message` is a backend tool too. Fix: on two consecutive tool errors, transfer to the practice's fallback number (put it in the call metadata at call start) or record the message locally and replay it to the backend later.

**P1-15. Inbound setup failure is a silent hang-up.** [agent/agent.py:1629-1633](agent/agent.py#L1629-L1633) — **S**
If `/internal/inbound` fails (backend down, deploy), the patient hears nothing and is dropped. Telnyx failover only covers SIP-level failure. Fix: say a fixed "please call X / we'll call you back" line (pre-rendered per number) or SIP-transfer to a fallback number from worker config.

**P1-16. No mid-call fallback for STT/LLM/TTS; LLM outage ends in a hang-up.** [agent/agent.py:777-817](agent/agent.py#L777-L817), [1206-1226](agent/agent.py#L1206-L1226), [1108-1129](agent/agent.py#L1108-L1129) — **M**
ElevenLabs health is probed once before the call; there is no `FallbackAdapter` on the pipeline's STT, LLM or TTS. If Gemini fails, each turn says "Συγγνώμη, μπορείτε να το πείτε ξανά;" and after three identical lines `guard_repetition` hangs up on the patient. Fix: LiveKit `llm.FallbackAdapter` / `stt.FallbackAdapter` / `tts.FallbackAdapter` behind the new provider interfaces, and route a repeated failure to handoff/message, never to a silent disconnect.

**P1-17. Hard 5-minute call cap.** [backend/app/config.py:89](backend/app/config.py#L89), [agent/agent.py:1703-1713](agent/agent.py#L1703-L1713) — **S**
A slow elderly caller booking with a spelled surname can exceed 5 minutes and is disconnected mid-booking (warning via the LLM). Make it per practice (10–15 min), and near the cap take a message instead of hanging up.

**P1-18. Concurrency is capped at one call, and the shape of the system does not scale yet.** — **M**
- `MAX_CONCURRENT_CALLS=1` globally ([backend/app/config.py:90](backend/app/config.py#L90)) gates web demos ([backend/app/routers/demo.py:118](backend/app/routers/demo.py#L118)); practices default to 2.
- Each job is a ~400 MB process; idle processes were turned off for OOM reasons (CLAUDE.md).
- One backend instance (in-memory pub/sub and scheduler), default SQLAlchemy pool (5 + 10 overflow, [backend/app/database.py:8](backend/app/database.py#L8)).
- `booking.book/reschedule/cancel` hold a per-practice advisory lock **across Google Calendar HTTP calls** (8 s timeout each, [backend/app/booking.py:580-588](backend/app/booking.py#L580-L588), [backend/app/gcal.py:81](backend/app/gcal.py#L81)). Twenty parallel callers to one practice serialize behind the slowest Google call; the worker gives up after 10 s.
- `report()` and every tool call open a new HTTP client.

**P1-19. LLM temperature 0.9 on a tool-calling receptionist.** [agent/agent.py:772](agent/agent.py#L772) — **S**
High temperature increases wrong tool choices and argument drift. Use 0.2–0.4 and measure with the evals.

**Latency hotspots in the turn loop**

**P1-20. Each tool call pays a fresh TLS handshake across the Atlantic.** [agent/agent.py:112-116](agent/agent.py#L112-L116) — **S** (code) + P0-4 (region)
`backend_post` creates an `httpx.AsyncClient` per call. Agent (EU West) → backend (US West) is ~140–160 ms RTT; TCP + TLS + request ≈ 3 RTT ≈ 0.4–0.5 s per tool call before any backend work. A booking turn often chains 2 tools (e.g. `route_call` then `check_availability`). One shared client per worker (keep-alive, HTTP/2) cuts this to ~1 RTT now, and moving the backend to the EU cuts that to ~10–20 ms. Estimated saving on tool turns: 0.3–0.9 s. This is the largest single latency item in the codebase.

**P1-21. Availability makes sequential Google calls, each with a new client.** [backend/app/booking.py:438-444](backend/app/booking.py#L438-L444), [557-570](backend/app/booking.py#L557-L570), [backend/app/gcal.py:80-86](backend/app/gcal.py#L80-L86) — **M**
One `freeBusy` per staff member per day, one after another, new `httpx.AsyncClient` (TLS) each time, plus a DB session per OAuth token lookup. When the requested day is full, `_next_free_days` walks up to 14 days × staff. Two dentists, no slot on the asked day → up to ~30 sequential Google calls (multi-second). Fix: one `freeBusy` request for all calendars and the whole 14-day window (the API accepts multiple `items` and a range), a shared client, and a short per-call cache.

**P1-22. Tool-chained turns cost an extra LLM round trip each.** [agent/agent.py:497-522](agent/agent.py#L497-L522), [backend/config/receptionist_prompt.md](backend/config/receptionist_prompt.md) — **M**
"Call route_call first" means a typical first turn is LLM → route_call → LLM → check_availability → LLM. Each hop adds TTFT (~0.45 s on Flash-Lite per the code comment) + tool RTT. Options: let the backend route implicitly inside `check_availability`/`find_appointments`, or run `route_call` in code from the transcript (it is already partly deterministic), so the model makes one tool call per turn.

**P1-23. Per-call setup costs.** [agent/agent.py:1015-1049](agent/agent.py#L1015-L1049), [backend/app/receptionist.py:262](backend/app/receptionist.py#L262) — **S**
The ElevenLabs probe adds up to 2.5 s (typically 0.2–0.5 s) before the greeting and costs credits each call; cache the result per worker for ~60 s. Metadata builds the full prompt in both languages on every call (cheap now, but it does two staff/booking reads).

### P2 — nice to have

**P2-1. Prank-era code and leftovers to remove.** — **M**
Backend: `routers/calls.py` POST + legacy GET/WS paths not used by practice calls, `routers/friends.py`, `routers/templates.py`, `dispatcher.start_call`, `livekit_dispatch.py`, `prompts.build_call_prompt` + `config/master_prompt*.md`, `languages.language_for_phone`, `own_caller_number`/`from_own_number`, `Friend`/`PromptTemplate` models (drop with a new migration; keep old migrations), sparring (`routers/sparring.py`, `templates/sparring.html`, `config/sparring_prompt.md`, `sparring_token`; it also hardcodes `agent_name="prank-caller"` at [sparring.py:78](backend/app/routers/sparring.py#L78)).
Agent: outbound prank entrypoint ([agent.py:1807-1952](agent/agent.py#L1807-L1952)), `PrankCallerAgent` callee-speaks-first/opening logic, `opening.prepare_opening` and its AI-disclosure prompt, filler logic in `llm_node` (receptionist passes `fillers=False`, [agent.py:1310](agent/agent.py#L1310)) and non-Greek/English filler lists, `heckle_while_talking`, the OpenAI engine and `openai` extra, unused voice maps (`OPENAI_VOICES`, prank voice labels), duplicate `CallerTurns` instance ([agent.py:1098](agent/agent.py#L1098) and [1695](agent/agent.py#L1695)).
iOS: the "Κλήση" tab (`NewCallView`, `DialPadView`, `LiveCallView`, friends/templates in `APIClient`, legacy `HistoryView`).
Repo: `PRD AI Prank Caller (Level 1).md`, both `fly.toml` files (Railway is the host), `agent/scripts/scenarios.barber.el.json` if unused, stale CLAUDE.md sections. Keep bundle id / agent name unless you decide otherwise (renaming the agent name needs a coordinated LiveKit dispatch-rule change).

**P2-2. Vertical config is half done.** [backend/config/verticals/](backend/config/verticals/), [backend/app/routing.py:21](backend/app/routing.py#L21), [agent/agent.py:499-507](agent/agent.py#L499-L507) — **M**
Templates already carry hours, services, routing rules and knowledge base. Still hardcoded: the receptionist prompt (dental examples: «να φτιάξω ένα δόντι»), tool docstrings with dental examples, `HEALTH_VERTICALS`, emergency phrases/script, staff titles (`TITLES`), the "ο γιατρός" wording in routing, STT vocabulary (`GREEK_VOCABULARY`), off-topic lines, intent examples. Covered in the migration plan.

**P2-3. Possibly real phone number in tests and history.** `+306907626384` in [backend/app/schemas.py:21](backend/app/schemas.py#L21) and [backend/tests/test_api_e2e.py:348](backend/tests/test_api_e2e.py#L348) (since 2026-09-24). If it is a real person's mobile, replace it with `+3069000000xx`; history rewrite is optional now that the repo is private. No API keys, private keys or `.env` files were found in history (pattern scan of all 296 commits).

**P2-4. Dependencies.** `livekit-agents` is pinned to 1.8.3; `livekit-plugins-meta` exists from the 1.8.7 line (1.8.10 released 2026-10-28), so the STT swap needs an SDK upgrade first. The code reaches into a private SDK attribute (`_update_activity_atask`, [agent/agent.py:1327](agent/agent.py#L1327)) that an upgrade can break. Pending Dependabot branches exist. `datetime.utcnow()` deprecation warnings throughout.

**P2-5. Small correctness items.** Confirmation window (4 min) is shorter than offer window (5 min) — fine, but not documented; `tool_confirm` (reminder confirmation) needs no transcript yes; `check_availability` overrides `when` with the last caller turn on the first lookup ([agent/agent.py:551-554](agent/agent.py#L551-L554)), which breaks if the day was said two turns earlier.

## 4. Readiness for an N-caller eval harness

What exists: a text harness that drives the real `ReceptionistAgent` with scripted lines against a local backend and scores DB state ([agent/evals/run_evals.py](agent/evals/run_evals.py)), 15 cases, critical vs warning checks, `--repeat` benchmarking, CI on PRs; and a WebRTC script that speaks scripted lines with TTS into the demo room ([agent/scripts/scripted_calls.py](agent/scripts/scripted_calls.py)). Both run one call at a time.

What has to change for N LLM-driven simulated patients in parallel, scored on DB state, with a live dashboard:

1. **Caller simulator.** An LLM persona per scenario (goal, hidden facts like name/phone/preferred day, behaviour: confused, interrupts, goes silent, emergency) that hears the agent (via the published transcript or its own STT) and speaks via TTS. Scripted lines remain for deterministic cases.
2. **Telephony-realistic audio.** WebRTC is 48 kHz Opus; phones are 8 kHz G.711. To compare STT providers honestly the simulator must band-limit to 8 kHz μ-law (or call over a real SIP trunk for a smaller subset).
3. **Isolation.** One fresh practice per run (DB-only calendar, no notifications), unique caller numbers, seeded appointments per scenario, so parallel runs never compete for the same slot unless the scenario wants that.
4. **Admission bypass for evals.** A founder-only `POST /internal/evals/sessions` that creates the call and returns a room token without the public demo's global cap (1) and IP limits; eval practices get their own caps.
5. **Frozen clock.** `utcnow()` is called directly across `receptionist.py`/`booking.py`; "την άλλη Τρίτη" resolves differently on different days. Inject a clock (per call, from metadata) so runs are reproducible.
6. **Scoring.** Reuse `check()` from `run_evals.py`; add expectations for emergency flag/notification, message fields, handoff rows, no write without a yes, latency budget per turn. No LLM judge for pass/fail.
7. **Capacity.** N concurrent worker jobs need N × ~400 MB (or LiveKit Cloud agent hosting), DB pool sizing, and calendar writes outside the advisory lock (P1-18).
8. **Run records + live dashboard.** Tables `eval_runs`/`eval_cases` (status, call id, pass/fail per check, p50/p95 answer and first-sound latency, cost). The existing `/monitor` telemetry already has per-call timings; a `/evals` page can poll the run tables the same way.
9. **Cost guard.** A run budget and a hard cap on concurrent simulated callers.

## 5. Decisions you made that I would flag (once each)

- **Muse Voice Transcribe for Greek.** It streams (WebSocket, `ENDPOINTING` mode with `speechStart`/partials/`speechEnd`/`speechComplete`), costs $3 per 1,000 minutes vs Scribe's $0.0065/min in `pricing.json`, and LiveKit has an official plugin. But **Greek is not one of its 25 validated languages** (Meta's docs list them; Greek is trained "among 70+" but unverified), it accepts only 16 or 24 kHz input (phone audio is upsampled from 8 kHz, no telephony robustness claim), and `language_bias`/`keywords` "nudge rather than constrain". Your confirmation gate (invariant 1) reads the STT text, so an STT regression is a safety regression, not just a quality one. Plan it as shadow-first, then flag.
- **Meta LLM for the hot path.** The Meta Model API's text models are the Muse Spark reasoning models. Third-party measurements put TTFT at ~0.9–3.5 s, against ~0.45 s for Flash-Lite in this code base, and their list price is several times higher. It may still win on Greek; the bake-off will tell. Expect it to lose on latency.
- **LangGraph in the audio path.** The framework itself is cheap (low milliseconds per step in memory), but the usual graph shape (router node → tool node → responder node) adds an LLM round trip per hop, ~0.4–1 s each, which breaks the 1.1 s bar. The backend is already a state machine spread over `routing_events`. A deterministic graph in the backend with no LLM nodes gives you the structure without the latency.

Sources: [Meta speech-to-text docs](https://dev.meta.ai/docs/speech-to-text), [Meta Muse Voice Transcribe blog](https://dev.meta.ai/resources/blog/meet-muse-voice-transcribe-streaming-speech-to-text), [Meta research post](https://research.meta.ai/blog/introducing-muse-voice-transcribe), [LiveKit Meta plugin](https://docs.livekit.io/reference/python/livekit/plugins/meta/index.html), [livekit-plugins-meta on PyPI](https://pypi.org/project/livekit-plugins-meta/), [LiteLLM Meta provider](https://docs.litellm.ai/docs/providers/meta), [llm-stats Meta models](https://llm-stats.com/providers/meta), [Twilio Greece regulatory guidelines](https://www.twilio.com/guidelines/gr/regulatory), [LiveKit Twilio trunk guide](https://docs.livekit.io/sip/quickstarts/configuring-twilio-trunk), [LiveKit secure trunking](https://docs.livekit.io/telephony/features/secure-trunking.md).
