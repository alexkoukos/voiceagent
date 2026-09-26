# AI Caller — Voice Receptionist

Greek and English voice receptionist built on the original outbound caller: inbound routing, staff calendars, booking changes, messages, notifications, and an iOS call log. The current specification is the local `AI Voice Receptionist 2.0 PRD.md`; the original [Level 1 PRD](<PRD AI Prank Caller (Level 1).md>) describes the legacy outbound flow.

See [PRD_STATUS.md](PRD_STATUS.md) for the latest reliability improvements, verified behavior, and remaining acceptance work. Deployment notes below also include the original outbound setup.

## Layout

- `backend/` — FastAPI + Postgres. Owns friends, calls, prompt templates, transcripts, recording metadata. Merges the master + per-call prompt and dispatches the LiveKit agent job.
  - `backend/config/master_prompt.md` — the fixed master system prompt (F2), edit directly, no code change needed.
- `agent/` — LiveKit worker (`prank-caller`). Picks up dispatched jobs, dials the friend over the Telnyx SIP trunk, runs the conversation through Gemini Live, enforces the hard duration cap, and hangs up via its own tool.
- `ios/` — SwiftUI app (see `ios/README.md`).

## Status

Receptionist features are implemented, but pilot readiness still requires the live acceptance work in [PRD_STATUS.md](PRD_STATUS.md). Historical Level 1 verification included:

- Backend: end-to-end on real Postgres (Docker), including migrations, auth, the agent-event endpoint, WebSocket push, and recording/transcript deletion (R2 and LiveKit stubbed).
- Agent: imports and instantiates against the real `livekit-agents` 1.8.3 in its Docker image; the LiveKit request types it uses exist. The actual call flow is untested.
- iOS: builds and launches in the simulator against a local backend (templates load, auth works). The call flow itself hasn't been driven from the app.

For local backend regression tests (external providers are mocked):

```bash
uv run --python 3.12 --with-requirements backend/requirements.txt \
  --with pytest --with pytest-asyncio pytest -q backend/tests -c backend/pytest.ini
```

To include concurrency and transaction tests, start the local Docker Postgres and set `TEST_DATABASE_URL=postgresql+asyncpg://user:password@localhost:5433/voiceagent` for that command. Each test creates and removes its own random schema; application tables are untouched. Without that variable, Postgres tests are explicitly skipped.

## Local setup

```bash
cp .env.example .env   # fill in LiveKit / Gemini / Telnyx / R2 keys as you get them
docker compose up -d   # local Postgres on port 5433

cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
```

```bash
cd agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python agent.py dev
```

## Deploy on Railway (M5)

One Railway project with three services, all from this GitHub repo:

1. **Postgres:** New → Database → PostgreSQL. **Recordings:** `railway bucket create recordings --region ams`.
2. **backend:** New → GitHub repo → set **Root Directory** to `backend` (it builds the Dockerfile; migrations run on boot). In Settings, set the health check path to `/health`. Under Networking, generate a public domain. Variables:
   - `DATABASE_URL=${{Postgres.DATABASE_URL}}`
   - `APP_API_TOKEN`, `INTERNAL_API_TOKEN`, `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `SIP_TRUNK_ID`, `SIP_OUTBOUND_NUMBER`, `TELNYX_PUBLIC_KEY` (copy from `.env`)
   - Recording storage, referencing the bucket: `AWS_ENDPOINT_URL=${{recordings.ENDPOINT}}`, `AWS_S3_BUCKET_NAME=${{recordings.BUCKET}}`, `AWS_ACCESS_KEY_ID=${{recordings.ACCESS_KEY_ID}}`, `AWS_SECRET_ACCESS_KEY=${{recordings.SECRET_ACCESS_KEY}}`, `AWS_DEFAULT_REGION=auto`, `AWS_S3_URL_STYLE=virtual-host`
3. **agent:** New → same repo → **Root Directory** `agent`. No public domain needed. Variables:
   - `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `GEMINI_API_KEY`, `INTERNAL_API_TOKEN`, `SIP_TRUNK_ID`
   - `BACKEND_PUBLIC_URL=https://${{backend.RAILWAY_PUBLIC_DOMAIN}}`
   - The same six `AWS_*` storage variables as the backend

Railway no longer reads `railway.toml`, so these settings live on the services themselves. Keep the backend at one replica. Point the Telnyx webhook at `https://<backend domain>/webhooks/telnyx` and the app's Settings tab at the backend domain.

Stop the local agent (`python agent.py dev`) once the Railway one is up, or both will compete for jobs.

## Deploy on Fly.io (alternative)

Fly.io configs are in `backend/fly.toml` and `agent/fly.toml`; both services have a Dockerfile (Railway works too).

```bash
# backend (runs `alembic upgrade head` on boot)
cd backend && fly launch --no-deploy --copy-config
fly postgres create && fly postgres attach <pg-app>   # sets DATABASE_URL
fly secrets set APP_API_TOKEN=... INTERNAL_API_TOKEN=... LIVEKIT_URL=... LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=... \
  SIP_TRUNK_ID=... SIP_OUTBOUND_NUMBER=... AWS_ENDPOINT_URL=... AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_S3_BUCKET_NAME=...
fly deploy

# agent worker (no public port)
cd ../agent && fly launch --no-deploy --copy-config
fly secrets set LIVEKIT_URL=... LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=... GEMINI_API_KEY=... INTERNAL_API_TOKEN=... \
  BACKEND_PUBLIC_URL=https://<backend-app>.fly.dev SIP_TRUNK_ID=... AWS_ENDPOINT_URL=... AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_S3_BUCKET_NAME=...
fly deploy
```

Then set the app's Settings tab to the backend URL and `APP_API_TOKEN`. The WebSocket push keeps its subscribers in memory, so run a single backend instance.

## Milestones (from the PRD)

| Milestone | Deliverable | Status |
| --- | --- | --- |
| M1 | 210 number on Telnyx + SIP trunk into LiveKit Cloud | You (needs you in Greece for the number) |
| M2 | LiveKit agent calls a phone, speaks Greek via Gemini Live | Written, untested |
| M3 | Prompt merge, hang-up, hard cap, recording, schema + migrations | Done, backend tested on Postgres |
| M4 | SwiftUI app | Builds, not run |
| M5 | Deploy + first real prank call | Configs written, not deployed |
| M6 | Concurrent calls: queue, active-calls list | Done (limit is a setting); more SIP channels are a Telnyx purchase |

Set `APP_API_TOKEN` and `INTERNAL_API_TOKEN` in `.env`; the app needs the same `APP_API_TOKEN` in its Settings tab.

## Behaviour worth knowing

- **Hard cap:** the backend clamps every call to `MAX_CALL_DURATION_SECONDS` (default 300) whatever the app asks for. About 25 seconds before the cap the agent is told to do the reveal and mention the recording, then the call is cut.
- **Concurrent calls (M6):** `MAX_CONCURRENT_CALLS` (default 1) is how many calls run at once. Extra calls are queued and start automatically when a line frees up; a queued call can be cancelled. The History tab lists active calls. Raise the limit after buying more SIP channels from Telnyx.
- **Delete on request:** if the friend asks, the agent calls its `delete_recording` tool. The transcript is deleted immediately and the recording is deleted once it finishes uploading (the backend retries for about 5 minutes). Same retry applies to deleting from the app.
- **Friend numbers** must be in international format (`+306...`).
- Two example templates ("Wrong order", "Fake call from the university") are seeded by migration 0002.

## Voice engine

The agent has two engines, picked with `AGENT_ENGINE`:

- **`pipeline`** (default): ElevenLabs Scribe v2 realtime hears the friend (90+ languages, follows them if they switch), `gemini-3.5-flash-lite` answers (`LLM_MODEL`), ElevenLabs Flash v2.5 speaks. LiveKit's multilingual turn detector decides when the friend has finished, the reply is prepared before they fully stop, a short filler («Ε…», «Κοίτα…») covers any reply that takes longer than 0.5 s, and the opening line is written and voiced (expressive `eleven_v3`) while the phone is still ringing. Needs `ELEVEN_API_KEY` with the `text_to_speech`, `speech_to_text` and `user_read` permissions (`user_read` lets it check the credits left before a call; `voices_read` isn't needed); without it the agent falls back to `realtime`.
- **`realtime`**: Gemini Live (`GEMINI_MODEL`) does everything. Simpler, but slower to notice the friend has finished (about 2 s from end of speech to reply in tests) and its transcription is weaker.

Both engines clean the phone audio with LiveKit's telephony noise cancellation. The call language starts from the friend's phone prefix (`backend/app/languages.py`) and can be changed per call in the app.

Every reply logs a `latency:` line (pipeline engine) with the end-of-turn, LLM and voice timings.

To try the agent without phoning anyone, run a local worker under another name (`AGENT_NAME=prank-caller-test python agent.py dev`) and dispatch a job whose metadata has `"test_no_dial": true`; the agent then talks to whoever joins the room.

## Known limits

- Gemini's Greek quality over phone audio, latency, and the 210 caller ID on Greek mobiles are unmeasured (PRD open questions).
- Talks through Gemini Live `gemini-3.8-live` (override with `GEMINI_MODEL`). Voice is one of five Gemini voices, labelled male/female in the app; "default" is Kore.
- Live push keeps subscribers in memory, so it needs a single backend instance.
