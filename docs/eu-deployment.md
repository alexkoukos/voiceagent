# Moving everything to the EU

Goal: every hop of a call (phone network, LiveKit, agent, backend, database, recordings, models) runs in or near Frankfurt/Amsterdam. Two reasons: latency (each agent→backend tool call currently crosses the Atlantic) and GDPR (Greek patients' data is stored in the US today).

## Where things are now (checked 2026-10-30)

| Piece | Now | Target | Who changes it |
|---|---|---|---|
| Agent (Railway) | EU West (per CLAUDE.md; check in dashboard) | EU West Amsterdam | — |
| Backend (Railway) | **US West** | EU West Amsterdam | you (Railway) |
| Postgres (Railway) | **US West** | EU West Amsterdam, same region as backend | you (Railway) |
| Recordings bucket (Railway, Tigris) | **unknown**; buckets default to `sjc` (San Jose) and the region cannot be changed | new bucket in `ams` + copy | you + `scripts/copy_bucket.py` |
| LiveKit Cloud project `test-yw591kbj` | data region **unknown** (fixed at project creation) | "European Union (Frankfurt)" | you (dashboard) |
| LiveKit SIP inbound URI | global `<id>.sip.livekit.cloud` | `<id>.eu.sip.livekit.cloud` | you (Telnyx portal) |
| LiveKit outbound trunk | no `destination_country` | `de` | you (dashboard / `lk`) |
| LiveKit Inference (Deepgram fallback) | not restricted | "inference region restriction" on | you (dashboard) |
| Telnyx credential connection | media anchor "Latency" (automatic) | Frankfurt | you or me, with your OK |
| Telnyx number | one **US** test number | Greek DID (pending) | you |
| Gemini (agent LLM, Gemini TTS fallback, call summaries, onboarding) | AI Studio `generativelanguage.googleapis.com`: global, **no residency guarantee** | Vertex AI `europe-west3` (Frankfurt); Gemini 3.5 Flash-Lite is listed there | me (code, behind a flag) + you (enable Vertex AI on the Google Cloud project) |
| ElevenLabs Scribe + TTS | global API | EU residency is **Enterprise-only** (`api.eu.residency.elevenlabs.io`) | stays global for now; covered by their DPA |
| Google Calendar | global | — (Google DPA) | — |

## Order (one quiet evening, ~1–2 h, short downtime in step 2)

### 1. Prepare (no downtime)
1. Back up the production database: `pg_dump` the Railway Postgres to a local file (Railway → Postgres → Connect → public URL). Keep it with the encryption key in `~/.config/voiceagent/backups`.
2. Check and write down in the dashboards: the agent's region, the bucket's region (bucket → Settings), and the LiveKit project's data region (Project settings).
3. Create a new Railway bucket in **ams**. Don't connect it yet.

### 2. Backend and database to EU West (downtime ≈ volume migration time)
1. Railway → Postgres → Settings → Scale/Regions → **EU West (Amsterdam)**. Railway migrates the volume automatically; the database is down until it finishes. The connection variables stay the same.
2. Railway → backend → same setting → **EU West (Amsterdam)**. Keep **one** replica (live updates and the scheduler are in memory).
3. The public backend URL does not change with the region, so the agent and the iOS app need no update.
4. Check: `GET /health`, open `/monitor`, list a practice's calls in the app.

Rollback: switch the regions back (another migration), or restore the dump into a fresh US Postgres.

### 3. Recordings bucket
1. Copy (dry run first, then `--apply`; it skips what is already there, so rerun it right before the switch):
   ```sh
   cd backend
   SRC_ENDPOINT=... SRC_KEY_ID=... SRC_SECRET=... SRC_BUCKET=... \
   DST_ENDPOINT=... DST_KEY_ID=... DST_SECRET=... DST_BUCKET=... \
   uv run --python 3.12 --with-requirements requirements.txt python scripts/copy_bucket.py
   ```
2. Point the backend's and the agent's `AWS_*` variables at the new bucket (`${{<new bucket>.*}}` references). Both services use them: the agent for egress uploads, the backend for sealing, playback and deletion.
3. Run the copy once more, play an old recording in the app, make a test call and check its recording.
4. Delete the old bucket after a week.

Rollback: point `AWS_*` back to the old bucket; it still has everything up to the switch.

### 4. LiveKit
1. If the project's data region is **not** the EU: create a new project with "European Union (Frankfurt)". It cannot be changed on the existing one. Then update `LIVEKIT_URL`/`LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` on backend and agent, and recreate the trunks:
   - inbound: `SIP_ALLOWED_ADDRESSES=<Telnyx signalling IPs> python backend/scripts/setup_inbound.py <numbers>` (phase0 branch);
   - outbound: recreate the Telnyx outbound trunk and set `SIP_TRUNK_ID`.
   This is also the moment to rotate the LiveKit secret that was exposed in September.
2. Outbound trunk: set `destination_country` to `de`.
3. Project settings → Data and privacy → turn on the inference region restriction.

### 5. Telnyx
1. Credential connection → change the anchor site from "Latency" to **Frankfurt**.
2. Point the inbound connection at `<sip-subdomain>.eu.sip.livekit.cloud` (`;transport=tls` where supported).
3. Later: the Greek DID on the same connection.

### 6. Gemini on Vertex AI in Frankfurt (separate change, measured first)
Same model, regional endpoint. Needs Vertex AI enabled on the Google Cloud project (`ai-receptionist-59557`) and a service account with the "Vertex AI User" role. I add it behind `GEMINI_VERTEX_LOCATION=europe-west3`. Before switching, I measure time-to-first-token from the EU agent on both endpoints. Your rule: switch only if Vertex is not slower.

## How we check it worked
- Tool round trip (`tool_request_ended` in `/monitor`): today it includes a transatlantic TLS handshake; expected to drop by roughly 0.3–0.5 s.
- 30 scripted calls before and after: answer delay p50/p95 and first-sound p50/p95 (the latency gate in MIGRATION_PLAN.md).
- No data in US regions: Railway services and bucket show EU, LiveKit data region EU, Gemini requests to `europe-west3-aiplatform.googleapis.com`.

## Still outside the EU after this
- ElevenLabs (STT and TTS): global processing unless you move to their Enterprise EU residency.
- Meta Muse, if it is ever adopted: no EU region documented.

Sources: [Railway regions](https://docs.railway.com/deployments/regions.md), [Railway buckets](https://docs.railway.com/storage-buckets), [LiveKit EU data residency](https://docs.livekit.io/deploy/admin/regions/data-residency.md), [Vertex AI data residency](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/data-residency), [ElevenLabs data residency](https://elevenlabs.io/docs/overview/administration/data-residency).
