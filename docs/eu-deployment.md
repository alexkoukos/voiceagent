# Moving everything to the EU

Goal: every hop of a call (phone network, LiveKit, agent, backend, database, recordings, models) runs in or near Frankfurt/Amsterdam, for latency and GDPR.

## Where things are now (checked 2026-10-30 with the Railway CLI and the Telnyx API)

| Piece | Now | Target | Status |
|---|---|---|---|
| Agent, backend, Postgres (Railway) | EU West, Amsterdam (`europe-west4`) | same | done (older notes saying "US West" are out of date) |
| Recordings bucket (Railway, Tigris) | `ams` | same | done |
| Telnyx credential connections | media anchor Frankfurt (was "Latency") | same | done 2026-10-30; undo by setting it back to "Latency" |
| LiveKit Cloud project | new project, keys rotated, Railway `LIVEKIT_URL`/`LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` updated | "European Union (Frankfurt)" | new project created 2026-10-30 (region can't change on an existing project); backend + agent redeployed. Old project still exists — delete once verified. Confirm the new project's data region in Project settings |
| LiveKit SIP inbound URI | no inbound trunk, no dispatch rule | `<id>.eu.sip.livekit.cloud` once wired | not set up: the new project has neither (the old one never had them either); wire inbound SIP when needed |
| LiveKit outbound trunk | one trunk, `destination_country=de`, ID in `SIP_TRUNK_ID` | same | done 2026-10-30; copied from the previous active trunk (same Telnyx address, numbers, credentials) |
| LiveKit Inference (Deepgram fallback) | not restricted | inference region restriction on | pending (owner): dashboard → Data and privacy |
| Gemini (agent model, summaries, onboarding) | AI Studio, global, no residency | Vertex AI multi-region `eu` | **live since 2026-10-30** (`GEMINI_VERTEX_LOCATION=eu`). `europe-west3/4/1/9` and `europe-north1` don't serve `gemini-3.5-flash-lite` (404) |
| ElevenLabs Scribe + TTS | global | EU residency is Enterprise-only | stays global for now (DPA) |
| Telephony provider | Telnyx, US test number | Twilio Greek 21x number later | Phase T |

## Remaining steps

### LiveKit
Done 2026-10-30: created a new LiveKit project (an existing project's data region can't be changed) and rotated its API keys, then updated `LIVEKIT_URL`/`LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` on backend and agent; both redeployed successfully. Recreated the single outbound trunk by copying the previous active one (same Telnyx address, numbers and credentials) and set its `destination_country=de`; its ID is in Railway's `SIP_TRUNK_ID`. The new project has no inbound trunk and no dispatch rule — the old project never had them either.

Remaining:
1. Confirm the new project's data region is "European Union (Frankfurt)" in Project settings.
2. Turn on the inference region restriction (dashboard → Data and privacy).
3. Delete the old LiveKit project once everything is verified.
4. If inbound SIP is ever needed, create an inbound trunk + dispatch rule (`setup_inbound.py`, phase0 branch) and point Telnyx (later Twilio) at `<sip-subdomain>.eu.sip.livekit.cloud`.

### Gemini on Vertex AI (Frankfurt)
1. Enable Vertex AI on the project that already holds the Gemini key and billing, and give the existing service account access:
   ```sh
   gcloud services enable aiplatform.googleapis.com --project gen-lang-client-0414182517
   gcloud projects add-iam-policy-binding gen-lang-client-0414182517 \
     --member="serviceAccount:receptionist@ai-receptionist-59557.iam.gserviceaccount.com" \
     --role="roles/aiplatform.user" --condition=None
   ```
2. Measure from the agent (EU) with `agent/scripts/compare_gemini_endpoints.py`. Switch only if Vertex is not slower.
3. To switch: set `GEMINI_VERTEX_LOCATION=eu` and `GEMINI_VERTEX_PROJECT=gen-lang-client-0414182517` on backend and agent; the agent also needs `GOOGLE_SERVICE_ACCOUNT_JSON`. Rollback: unset `GEMINI_VERTEX_LOCATION`.

### Recordings (only if a bucket ever has to move)
A bucket's region is fixed at creation. `backend/scripts/copy_bucket.py` copies everything to a new bucket (dry run by default); then point `AWS_*` on backend and agent at it.

## How we check it worked
- Gemini first token from the agent on both endpoints (comparison script), then 30 scripted calls with the faster one.
- 30 scripted calls before and after: answer delay p50/p95 and first-sound p50/p95 (the latency gate in MIGRATION_PLAN.md).
- No data in US regions: LiveKit data region EU, Gemini requests to `eu-aiplatform.googleapis.com`.

## Measured (2026-10-30, from the Railway agent, 20 requests each, alternating)
Time to first token of `gemini-3.5-flash-lite`: AI Studio p50 517 ms / p95 610 ms; Vertex `eu` p50 400 ms / p95 504 ms. Vertex EU is ~110 ms faster, so both backend and agent were switched.

## Still outside the EU after this
- Gemini Live (realtime engine, the last fallback) and Gemini TTS (the `text_pipeline` fallback voice) still use the AI Studio key.
- ElevenLabs (STT and TTS): global processing unless you move to their Enterprise EU residency.
- Meta Muse, if it is ever adopted: no EU region documented.

Sources: [Railway regions](https://docs.railway.com/deployments/regions.md), [Railway buckets](https://docs.railway.com/storage-buckets), [LiveKit EU data residency](https://docs.livekit.io/deploy/admin/regions/data-residency.md), [Vertex AI data residency](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/data-residency), [ElevenLabs data residency](https://elevenlabs.io/docs/overview/administration/data-residency).
