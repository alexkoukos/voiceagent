# Moving everything to the EU

Goal: every hop of a call (phone network, LiveKit, agent, backend, database, recordings, models) runs in or near Frankfurt/Amsterdam, for latency and GDPR.

## Where things are now (checked 2026-10-30 with the Railway CLI and the Telnyx API)

| Piece | Now | Target | Status |
|---|---|---|---|
| Agent, backend, Postgres (Railway) | EU West, Amsterdam (`europe-west4`) | same | done (older notes saying "US West" are out of date) |
| Recordings bucket (Railway, Tigris) | `ams` | same | done |
| Telnyx credential connections | media anchor Frankfurt (was "Latency") | same | done 2026-10-30; undo by setting it back to "Latency" |
| LiveKit Cloud project `test-yw591kbj` | data region not visible from the API | "European Union (Frankfurt)" | check in the dashboard (Project settings); it can't be changed on an existing project |
| LiveKit SIP inbound URI | global `<id>.sip.livekit.cloud` | `<id>.eu.sip.livekit.cloud` | Telnyx portal: the connection's SIP URI |
| LiveKit outbound trunk | no `destination_country` | `de` | dashboard or `lk` |
| LiveKit Inference (Deepgram fallback) | not restricted | inference region restriction on | dashboard → Data and privacy |
| Gemini (agent model, summaries, onboarding) | AI Studio, global, no residency | Vertex AI `europe-west3` | code ready behind `GEMINI_VERTEX_LOCATION`; needs Vertex AI enabled (below) and a measurement |
| ElevenLabs Scribe + TTS | global | EU residency is Enterprise-only | stays global for now (DPA) |
| Telephony provider | Telnyx, US test number | Twilio Greek 21x number later | Phase T |

## Remaining steps

### LiveKit
1. Check the project's data region. If it isn't the EU, create a new project in "European Union (Frankfurt)", update `LIVEKIT_URL`/`LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` on backend and agent, rotate the secret exposed in September, and recreate the inbound trunk (`setup_inbound.py`, phase0 branch) and the outbound trunk.
2. Point Telnyx (later Twilio) at `<sip-subdomain>.eu.sip.livekit.cloud`; set the outbound trunk's `destination_country` to `de`; turn on the inference region restriction.

### Gemini on Vertex AI (Frankfurt)
1. Enable Vertex AI on the project that already holds the Gemini key and billing, and give the existing service account access:
   ```sh
   gcloud services enable aiplatform.googleapis.com --project gen-lang-client-0414182517
   gcloud projects add-iam-policy-binding gen-lang-client-0414182517 \
     --member="serviceAccount:receptionist@ai-receptionist-59557.iam.gserviceaccount.com" \
     --role="roles/aiplatform.user" --condition=None
   ```
2. Measure from the agent (EU) with `agent/scripts/compare_gemini_endpoints.py`. Switch only if Vertex is not slower.
3. To switch: set `GEMINI_VERTEX_LOCATION=europe-west3` and `GEMINI_VERTEX_PROJECT=gen-lang-client-0414182517` on backend and agent; the agent also needs `GOOGLE_SERVICE_ACCOUNT_JSON`. Rollback: unset `GEMINI_VERTEX_LOCATION`.

### Recordings (only if a bucket ever has to move)
A bucket's region is fixed at creation. `backend/scripts/copy_bucket.py` copies everything to a new bucket (dry run by default); then point `AWS_*` on backend and agent at it.

## How we check it worked
- Gemini first token from the agent on both endpoints (comparison script), then 30 scripted calls with the faster one.
- 30 scripted calls before and after: answer delay p50/p95 and first-sound p50/p95 (the latency gate in MIGRATION_PLAN.md).
- No data in US regions: LiveKit data region EU, Gemini requests to `europe-west3-aiplatform.googleapis.com`.

## Still outside the EU after this
- ElevenLabs (STT and TTS): global processing unless you move to their Enterprise EU residency.
- Meta Muse, if it is ever adopted: no EU region documented.

Sources: [Railway regions](https://docs.railway.com/deployments/regions.md), [Railway buckets](https://docs.railway.com/storage-buckets), [LiveKit EU data residency](https://docs.livekit.io/deploy/admin/regions/data-residency.md), [Vertex AI data residency](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/data-residency), [ElevenLabs data residency](https://elevenlabs.io/docs/overview/administration/data-residency).
