# Migration plan

Companion to [AUDIT.md](AUDIT.md). Finding ids (P0-1, P1-20, …) refer to it.

## Ground rules for every phase

- **Latency bar:** answer delay median ≤ 1.1 s, p95 ≤ 1.45 s; cost ≤ $0.04/min. "Answer delay" = `answer_latency_estimate` (caller stops → first real LLM text). Every phase also reports `response_latency_estimate` (first sound) and per-stage timings, so a change that moves time into TTS or the network still shows up.
- **Latency gate:** ≥ 30 scripted audio calls before and after, same scenario set, same region. A regression of more than 50 ms on the median or 100 ms on p95 needs your explicit OK.
- **Correctness gate:** the full text eval suite at `--repeat 3`, zero critical failures, plus the new invariant tests. From phase 2 on, the parallel audio harness too.
- **Shipping:** each phase is its own PR (or a few), small commits, one concern per commit, no co-author or "Generated with" lines. Every new behaviour sits behind an env flag whose default is the current behaviour until the gate passes.
- **Languages:** nothing in these phases changes the call language rules (Greek first, "English mode" matched in code). Where a phase touches language settings (STT language pins), it says so and needs your OK.

## Suggested order and why it differs from yours

| # | Phase | Why here |
|---|---|---|
| 0 | Safety fixes (P0s) | Small, high value, and they change the gates every later phase is measured against. |
| 1 | Cleanup + provider interfaces | As you proposed. No behaviour change, makes 3–5 mechanical. |
| 2 | Eval harness (parallel, 8 kHz, DB-scored, dashboard) | **Moved before the STT swap.** Greek isn't a validated Muse language; without telephony-band audio evals you can't tell whether Muse is better or worse. |
| 3 | EU move + latency quick wins | Lowest risk, largest latency gain (0.3–0.9 s on tool turns). Also a GDPR blocker. Banks headroom before the riskier swaps. |
| 4 | STT: Muse (shadow, then flag) | Needs 1 (interface, SDK upgrade) and 2 (harness). |
| 5 | LLM bake-off and swap | Needs 1 and 2. Easier to judge after 3 removed network noise. |
| 6 | Vertical config | Independent; easier once prompts are data. |
| 7 | State graph for business logic | Last, because it reshapes the backend and should be checked against a full harness. |
| T | Twilio trunk | Independent of everything else. Runs whenever the regulatory bundle is approved. |

---

## Phase 0 — Safety fixes

**Changes**
1. Strict yes-detector (P0-1): short reply, yes-form at the start or alone, no `?`, no change/hesitation/time words; anything else → `confirmation_required` with a hint to ask again. Table-driven tests with the nine failing phrases from the audit and the existing yes/no tables.
2. Confirmations single-use and bound to the transcript (P1-1): atomic `pending → accepted`; the worker reports the caller turn synchronously and sends its id; the backend checks it is the latest caller entry after the readback.
3. Protected lookup by spoken number (P0-2): name match before disclosure, no service name for a non-caller-ID match, audit log entry.
4. Remove the legacy outbound dialer endpoint and UI (P0-3). Add a Telnyx outbound voice profile allowlist (Greece only) — that one is a console step for you.
5. Admin changes (P0-5): PIN on SMS, per-practice lockout across calls, transcript-based yes for `admin_confirm`, PIN captured from the transcript in code, never through the LLM.
6. LiveKit inbound trunk restricted to carrier IPs + TLS (P1-9).
7. Fixed safety lines spoken verbatim (P1-4): emergency, busy, time-up, handoff failure via `say_fixed`.

**Files:** `backend/app/receptionist.py`, `backend/app/admin_changes.py`, `backend/app/routers/calls.py`, `backend/app/main.py`, `agent/agent.py`, `backend/scripts/setup_inbound.py`, `ios/PrankCaller/*` (dialer tab), tests in `backend/tests/` and `agent/test_receptionist.py`.

**Verify:** new unit tests for invariants 1–3; full text evals ×3 (EVAL-011/012/015 especially); a manual call per fix. One new eval: "yes + correction" (`«Ναι ναι, αλλάξτε την ώρα»` must not book).

**Latency:** none expected. The transcript-binding step adds one synchronous transcript POST per caller turn (~10–20 ms once the backend is in the EU, ~150 ms before; done in parallel with the LLM, not before it).

**Rollback:** revert the PR. The dialer removal has no data migration in this phase (tables dropped in phase 1).

---

## Phase 1 — Cleanup and provider interfaces

**1a. Remove the prank era** (P2-1). Separate commits for backend routes, models + migration `0031_drop_legacy_outbound`, agent outbound entrypoint, sparring, OpenAI engine, opening/fillers, iOS tab, docs. Archive the Level 1 PRD, delete the `fly.toml` files, update CLAUDE.md.

**1b. SDK upgrade** `livekit-agents` 1.8.3 → current 1.8.x (≥ 1.8.7 for `livekit-plugins-meta`). Replace the private `_update_activity_atask` access ([agent/agent.py:1327](agent/agent.py#L1327)) with the public API or a guarded fallback. Own commit, own latency gate.

**1c. Provider interfaces** in `agent/providers/`:

```python
class SttProvider(Protocol):
    name: str
    def build(self, language: str, keyterms: list[str]) -> stt.STT: ...
    turn_detection: Literal["stt", "semantic"]     # Scribe/Muse: "stt" (server endpointing)
    endpointing_min_delay: float                   # 0 when the provider endpoints itself
    def healthy(self) -> Awaitable[bool]: ...      # cached per worker, replaces the per-call probe

class LlmProvider(Protocol):
    name: str
    def build(self, *, temperature: float) -> llm.LLM: ...

class TtsProvider(Protocol):
    name: str
    def build(self, voice: str, language: str) -> tts.TTS: ...
```

Registry chosen by env: `STT_PROVIDER=scribe` (default) `| muse | deepgram`, `STT_FALLBACK=deepgram`; `LLM_PROVIDER=gemini-flash-lite` (default); `TTS_PROVIDER=elevenlabs`. `build_session` becomes provider-agnostic and wraps each part in LiveKit's `FallbackAdapter` when a fallback is configured (P1-16). The engine names stay for one release as aliases.

**Files:** `agent/agent.py` (shrinks by ~40%), new `agent/providers/{stt,llm,tts}.py`, `agent/requirements.txt`, `backend/app/{models,prompts,config,dispatcher}.py`, `backend/app/routers/{calls,friends,templates,sparring}.py`, migration, `ios/`, docs, tests.

**Verify:** all unit tests; text evals ×3; 30 scripted audio calls: the default configuration must be byte-identical in behaviour (same models, same settings), so latency must match within noise. iOS CI build.

**Latency:** 0 by design for 1a/1c. 1b can move it either way: measured separately.

**Rollback:** revert per commit. The table-drop migration has a downgrade that recreates the empty tables; take a DB backup first.

---

## Phase 2 — Eval harness

**Changes** (see AUDIT.md §4)
1. `backend`: injectable clock per call (`now` from metadata for eval practices, real time otherwise); founder-only `POST /internal/evals/sessions` that bypasses the public demo cap; `eval_runs`/`eval_cases` tables; per-practice concurrency caps for eval practices; booking writes take the advisory lock only around the DB write, with Google calls done before (re-check inside the lock) (P1-18).
2. `agent/evals/sim/`: LLM-driven caller (persona, goal, hidden facts, behaviours: book, reschedule, cancel, price question, confused, emergency, interruption, silence), TTS voice, audio band-limited to 8 kHz μ-law, joins the room with the eval token, listens via the agent's published transcript.
3. Orchestrator: N parallel callers (default 10), each on a fresh practice, budget cap, writes run records.
4. Scoring: reuse `check()`; add `emergency_flagged`, `message_fields`, `no_write_without_yes`, `turn_latency_budget`. No LLM judge for pass/fail.
5. `/evals` page (founder auth, in-memory key): runs, per-case status, pass/fail per check, p50/p95 answer and first-sound latency, cost; polls like `/monitor`.
6. Capacity: run the eval worker on LiveKit Cloud agent hosting or a larger instance; raise DB pool.

**Files:** `backend/app/{receptionist,booking,routers/internal}.py`, new `backend/app/routers/evals.py`, `backend/app/templates/evals.html`, migration, `agent/evals/sim/*`, `agent/evals/run_parallel.py`, `.github/workflows/evals.yml` (text suite stays on PRs; the audio suite on demand / nightly).

**Verify:** the harness itself: a known-bad build (e.g. phase 0 yes-detector reverted) must fail; the current build must pass; 3 identical runs must give the same pass/fail on deterministic cases.

**Latency:** none on production calls. The advisory-lock change shortens write latency under contention.

**Rollback:** harness is additive; the lock change is one commit.

---

## Phase 3 — EU region and latency quick wins

**Changes**
1. Move backend + Postgres + recordings bucket to EU West (P0-4); dump/restore in a quiet window; update the backend URL in the agent and iOS app.
2. One shared `httpx.AsyncClient` per worker process (keep-alive, HTTP/2) for tools, events and telemetry (P1-20).
3. Calendar: one `freeBusy` call for all calendars over the whole search window, shared client, short cache (P1-21).
4. Cache the ElevenLabs health probe per worker (P1-23).
5. Temperature 0.9 → 0.3 (P1-19), behind `LLM_TEMPERATURE`, gated on the evals.
6. Optional, separate flag: fold `route_call` into the first business tool or run it in code (P1-22).

**Files:** Railway config (you), `agent/agent.py`, `agent/telemetry.py`, `backend/app/{gcal,booking}.py`, `ios` default URL.

**Verify:** text evals ×3; parallel audio harness; 30-call latency gate. Expected: tool-turn answer delay −0.3 to −0.9 s; non-tool turns unchanged.

**Latency:** improves. Temperature change: none.

**Rollback:** keep the US database read-only for a week after the move; each code change is its own commit/flag.

---

## Phase 4 — STT: Muse Voice Transcribe

What the docs say: WebSocket streaming at `wss://api.meta.ai/v1/asr/realtime`; modes `PUSH_TO_TALK` (default), `ENDPOINTING` (server emits `speechStart`, partials, `speechEnd`, `speechComplete`) and `DIARIZATION`; mono PCM16 at 24 kHz (preferred) or 16 kHz only; `keywords` and `languageBias`; 256 concurrent streams; 60-minute session cap; zero data retention at the same price; $3 per 1,000 minutes. The LiveKit plugin uses server endpointing at 24 kHz and needs continuous audio including silence. **Greek is not in the 25 validated languages.**

So it can stream and endpoint; the open questions are Greek accuracy on 8 kHz phone audio, endpointing delay versus Scribe's 0.7 s, and whether `languageBias` accepts Greek at all.

**Step 4.0 — spike (half a day).** Send 10 real Greek call recordings (8 kHz, resampled to 24 kHz) through the API; check that Greek is accepted and roughly intelligible. If not, stop here and keep Scribe (and say so in the hackathon pitch: Muse behind a flag, used for English mode).

**Step 4.1 — shadow mode.** `STT_SHADOW=muse`: the same caller audio is also streamed to Muse; its finals and endpoint times are logged next to Scribe's (content in the transcript store, timings in telemetry). No effect on the call. Compare WER on names, days, times and yes/no replies (these drive invariant 1), and endpoint delay.

**Step 4.2 — flag.** `STT_PROVIDER=muse` with `STT_FALLBACK=scribe` via `FallbackAdapter`. Turn detection `stt`, `min_delay=0`, keyterms → `keywords`, language pin → `languageBias=["Greek"]`. Changing the language pin is a language setting: needs your OK.

If Muse cannot stream for some reason (e.g. Greek only works in push-to-talk mode), the fallback design is LiveKit's `stt.StreamAdapter` with Silero VAD: VAD ends the turn, the utterance is sent as one request. Expect +0.2–0.5 s per turn on top of VAD silence, which likely breaks the bar; I would not ship that for the hot path.

**Files:** `agent/providers/stt.py`, `agent/agent.py` (shadow tap), `agent/requirements.txt` (`livekit-plugins-meta`), `backend/config/pricing.json` (new version with Muse rate), telemetry fields.

**Verify:** shadow comparison over ≥ 100 Greek turns from real or harness calls; then the parallel harness and the 30-call latency gate with Muse on. Ship only if critical eval pass rate is ≥ Scribe's and yes/no recognition is no worse.

**Latency:** shadow: ~0 (one extra WebSocket, off the critical path). Flag on: unknown until measured; Meta advertises ~0.16 s finalisation with adaptive delay.

**Cost:** −$0.0035/min if it replaces Scribe; +$0.003/min during shadow.

**Rollback:** `STT_PROVIDER=scribe`. No data migration.

---

## Phase 5 — LLM bake-off and swap

**Candidates (3):**

| Model | Why | Expected TTFT* | Est. LLM cost/min** |
|---|---|---|---|
| Gemini 3.5 Flash-Lite (incumbent) | Known Greek quality and tool behaviour here | ~0.45 s (code comment, measured in this project) | ~$0.010 (list $0.30/$2.50 per 1M) |
| Meta Muse Spark (latest, lowest reasoning effort) | Hackathon; function calling, prompt caching, OpenAI-compatible and Anthropic-compatible endpoints | 0.9–3.5 s (third-party, varies by version) | ~$0.040 at $1.25/$4.25; much lower if the budget tier applies (prices conflict across sources, verify on the console) |
| Claude Haiku 4.5 | Strong tool-calling reliability and Greek; good control check | to measure | ~$0.033 list, much lower with prompt caching ($1/$5 per 1M) |

\* Measure all three from the EU worker with the real prompt.
\** Assumption: ~6 LLM calls per minute, ~5k input + ~100 output tokens each (Greek prompts tokenize long), no caching. Prices from `pricing.json` and provider pages; re-check before deciding.

**Method:** one `LlmProvider` per candidate; the text eval suite ×5 per model with a fixed clock; tool-calling metrics from the logs (wrong tool, malformed args, `confirmation_required` retries, `check_availability_first` rejections); Greek quality by a blind native review of 30 turns per model (you or a Greek speaker; this is for quality, not pass/fail); TTFT p50/p95 from telemetry; cost from `usage_reported`.

**Recommendation going in:** keep Flash-Lite unless a candidate is at least as good on critical evals and within the latency bar. Muse Spark is a reasoning model and is unlikely to meet 1.1 s; if it wins on Greek, it can still be used off the hot path (call summaries in `finalize.py`, admin-change parsing, onboarding extraction), which keeps it in the hackathon story without touching latency.

**Files:** `agent/providers/llm.py`, `agent/evals/run_evals.py` (`--llm` option), `backend/config/pricing.json`, optionally `backend/app/finalize.py`.

**Verify / latency / rollback:** evals ×5 per model, 30-call gate for the winner, `LLM_PROVIDER` flag to roll back.

---

## Phase 6 — Vertical config

**Changes:** move into `backend/config/verticals/<id>.json` (validated by a Pydantic schema):
- prompt fragments: role line, intent examples (book/change), tool hints that are now dental examples;
- booking rules: slot step, buffers, notice, max days ahead, default services;
- vocabulary for STT keyterms;
- routing/handoff: emergency enabled, phrases, script, after-hours rules, staff titles, "who" wording ("ο γιατρός", "ο φυσικοθεραπευτής", "η ρεσεψιόν");
- off-topic and closing lines.
The master prompt keeps only speaking style and safety. Tool docstrings become neutral, with vertical examples injected into the prompt instead. Add `doctor` and `hotel` templates. Practice-level overrides stay in the practice row.

**Files:** `backend/config/verticals/*.json`, new `backend/app/verticals.py`, `backend/app/{prompts,routing,receptionist}.py`, `backend/config/receptionist_prompt*.md`, `agent/agent.py` (docstrings, vocabulary).

**Verify:** a golden test that the dentist prompt is unchanged in meaning (diff review), text evals ×3 on dentist, and a small eval set per new vertical (book + question + emergency where enabled).

**Latency:** prompt length may change; keep the per-vertical prompt ≤ the current size. Rollback: revert; the practice rows are untouched.

---

## Phase 7 — State graph for the booking flow

**Measured/estimated cost of putting a graph in the audio path:**

| Option | Per-turn added latency | Verdict |
|---|---|---|
| A. LangGraph as the agent's LLM (LiveKit `LLMAdapter`), single LLM node + tool nodes | framework ~1–5 ms per step in memory; +5–20 ms per step with a Postgres checkpointer (EU) | Fits the budget **only** if the graph keeps one LLM call per turn. |
| A'. Usual graph shape: router LLM node → tool node → responder LLM node | +1 LLM round trip per extra LLM node: ~0.4–1.0 s | Breaks the 1.1 s bar. |
| B. Deterministic graph in the backend (no LLM nodes); the worker's tools trigger transitions | ~1–5 ms per tool call (in-process, state in Postgres you already write) | **Recommended.** |
| C. LiveKit's own tasks/workflows for conversation stages | ~0 | Good for prompt scoping per stage; can combine with B. |

The A numbers need confirming with a micro-benchmark before any commitment (step 7.0: wrap the current agent in a one-node graph, 30 calls, compare).

Honest view: today's booking logic is already a state machine, just implicit (offers, confirmations and found ids rebuilt from `routing_events` on every call). Making it explicit has real value: one place for allowed transitions (`intent → service → day → offered → name → readback → confirmed → written`), invariants enforced as guards, easy unit tests, a diagram. LangGraph adds a dependency (`langgraph`, `langchain-core`) for something a ~200-line Python state machine does equally well. If you want LangGraph for the hackathon, use it as in option B, with no LLM nodes, and keep the LLM in LiveKit's loop.

**Changes (option B):** `backend/app/flow/` with the graph (nodes = states, edges guarded by invariants), per-call state persisted in a `call_flow_state` table instead of reconstructing from `routing_events` (which stays as the audit log); tools become thin calls into the graph; each tool result tells the model the allowed next steps. Optionally (C) the worker switches prompt/tool sets per stage.

**Files:** new `backend/app/flow/*`, `backend/app/receptionist.py` (tools delegate), migration, tests.

**Verify:** unit tests per transition and guard; text evals ×3; parallel harness; 30-call gate. Target: ≤ 15 ms p95 added per tool call.

**Rollback:** `FLOW_ENGINE=legacy` keeps the current tool implementations for one release.

---

## Phase T — Twilio Elastic SIP trunk (Greek 21x number)

**Facts that shape it:** a Greek local number needs a Twilio regulatory bundle with proof of identity and a **Greek address within the number's area** (no PO box), and carriers now enforce address verification. Elastic SIP Trunking has no call webhooks: the security controls are the trunk's IP ACL / credential list and TLS, not request signatures. LiveKit documents the setup: origination URI = your LiveKit SIP URI with `;transport=tls`.

**Changes**
1. Config, not code, picks the trunk: `practices.telephony = {"provider": "telnyx"|"twilio", "inbound_numbers": [...], "outbound_trunk_id": "...", "caller_id": "..."}`; `build_metadata` sends the right `sip_trunk_id`/`outbound_number` per practice (today one env value serves everyone, [backend/app/receptionist.py:286-291](backend/app/receptionist.py#L286-L291)).
2. `setup_inbound.py` gains a `--provider twilio` mode: a second LiveKit inbound trunk restricted to Twilio's signalling IPs + digest auth, same dispatch rule, and a LiveKit outbound trunk for transfers/reminders via Twilio.
3. Inbound handling is already trunk-agnostic: the backend finds the practice by `sip.trunkPhoneNumber`. Add the provider to `calls` for cost reporting and a Twilio rate version in `pricing.json`.
4. If Twilio SMS or status callbacks are added later: validate `X-Twilio-Signature` (HMAC-SHA1 of URL + params with the auth token), fail closed, reject replays.

**Files:** `backend/app/{models,receptionist,schemas}.py`, migration, `backend/scripts/setup_inbound.py`, `backend/config/pricing.json`, docs.

**Verify:** inbound call to the 21x number (you, real phone), SIP transfer to a staff mobile, a reminder call, Telnyx calls unchanged; harness smoke test over SIP for 5 calls if a test number is available.

**Latency:** none on the turn loop; call setup may differ by carrier (measure ring → greeting).

**Rollback:** remove the number from the practice config; the Telnyx trunk is untouched.

---

## Open questions before Phase 2 (implementation)

1. **Muse and Greek:** knowing Greek is not a validated language, do you still want Muse as a candidate for Greek calls, or as Scribe's fallback / English-mode STT only? (This decides whether phase 4 is a swap or an add-on.)
2. **Phase order:** OK to run the eval harness (2) and the EU move (3) before the STT swap?
3. **EU move:** may I plan the Railway region move now (you run it), and is the recordings bucket region known?
4. **Dialer removal:** confirm the legacy outbound dialer and the "Κλήση" iOS tab can go, including the `friends`/`prompt_templates` tables (drop with a migration after a backup). Keep the agent name `prank-caller` and bundle id as they are?
5. **Sparring page:** remove it, or keep it as an internal tool behind the founder key?
6. **Spoken-number lookups (P0-2):** which check do you want for a number that isn't the caller ID: full-name match, SMS code, or both?
7. **Admin by SMS (P0-5):** require the PIN in the SMS, or turn SMS changes off for now?
8. **LLM bake-off:** OK with the three candidates (Flash-Lite, Muse Spark, Claude Haiku 4.5)? Is there a Meta Model API key with access to the budget tier?
9. **LangGraph:** do you want LangGraph specifically (for the hackathon), or is an explicit Python state machine acceptable if it's equivalent?
10. **Call cap:** what maximum call length per practice (I'd suggest 12 minutes)?
11. **Verticals:** which second and third verticals should the config prove out first (doctor + physio, or doctor + small hotel)?
12. **Possibly real number** `+306907626384` in tests: is it a real person's number?
