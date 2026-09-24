# AI Prank Caller — Level 1

Personal tool: calls a friend from a Greek 210 landline number and runs a prank scenario in natural Greek. See [PRD AI Prank Caller (Level 1).md](<PRD AI Prank Caller (Level 1).md>) for the full spec.

## Layout

- `backend/` — FastAPI + Postgres. Owns friends, calls, prompt templates, transcripts, recording metadata. Merges the master + per-call prompt and dispatches the LiveKit agent job.
  - `backend/config/master_prompt.md` — the fixed master system prompt (F2), edit directly, no code change needed.
- `agent/` — LiveKit worker (`prank-caller`). Picks up dispatched jobs, dials the friend over the Telnyx SIP trunk, runs the conversation through Gemini Live, enforces the hard duration cap, and hangs up via its own tool.
- `ios/` — SwiftUI app (see `ios/README.md`).

## Status

Code-complete and committed, but never run against real LiveKit/Gemini/Telnyx/R2. What has been verified:

- Backend: end-to-end on real Postgres (Docker), including migrations, auth, the agent-event endpoint, WebSocket push, and recording/transcript deletion (R2 and LiveKit stubbed).
- Agent: imports and instantiates against the real `livekit-agents` 1.8.3 in its Docker image; the LiveKit request types it uses exist. The actual call flow is untested.
- iOS: builds for the simulator with `xcodebuild`; not run yet.

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

## Deploy (M5)

Fly.io configs are in `backend/fly.toml` and `agent/fly.toml`; both services have a Dockerfile (Railway works too).

```bash
# backend (runs `alembic upgrade head` on boot)
cd backend && fly launch --no-deploy --copy-config
fly postgres create && fly postgres attach <pg-app>   # sets DATABASE_URL
fly secrets set APP_API_TOKEN=... INTERNAL_API_TOKEN=... LIVEKIT_URL=... LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=... \
  SIP_TRUNK_ID=... SIP_OUTBOUND_NUMBER=... R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_BUCKET_NAME=...
fly deploy

# agent worker (no public port)
cd ../agent && fly launch --no-deploy --copy-config
fly secrets set LIVEKIT_URL=... LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=... GEMINI_API_KEY=... INTERNAL_API_TOKEN=... \
  BACKEND_PUBLIC_URL=https://<backend-app>.fly.dev SIP_TRUNK_ID=... R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_BUCKET_NAME=...
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

Set `APP_API_TOKEN` and `INTERNAL_API_TOKEN` in `.env`; the app needs the same `APP_API_TOKEN` in its Settings tab.

## Known limits

- Gemini's Greek quality over phone audio, latency, and the 210 caller ID on Greek mobiles are unmeasured (PRD open questions).
- Voice choice is one of five Gemini voices, labelled male/female in the app.
- Live push keeps subscribers in memory, so it needs a single backend instance.
