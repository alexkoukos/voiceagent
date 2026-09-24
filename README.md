# AI Prank Caller — Level 1

Personal tool: calls a friend from a Greek 210 landline number and runs a prank scenario in natural Greek. See [PRD AI Prank Caller (Level 1).md](<PRD AI Prank Caller (Level 1).md>) for the full spec.

## Layout

- `backend/` — FastAPI + Postgres. Owns friends, calls, prompt templates, transcripts, recording metadata. Merges the master + per-call prompt and dispatches the LiveKit agent job.
  - `backend/config/master_prompt.md` — the fixed master system prompt (F2), edit directly, no code change needed.
- `agent/` — LiveKit worker (`prank-caller`). Picks up dispatched jobs, dials the friend over the Telnyx SIP trunk, runs the conversation through Gemini Live, enforces the hard duration cap, and hangs up via its own tool.
- `ios/` — SwiftUI app (M4, not started).

## Status

Code-complete scaffold, committed but never run against real LiveKit/Gemini/Telnyx/R2. The backend was exercised end to end on SQLite with LiveKit stubbed; the iOS sources type-check but haven't run in a simulator.

## Local setup

```bash
cp .env.example .env   # fill in LiveKit / Gemini / Telnyx / R2 keys as you get them
docker compose up -d   # local Postgres

cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

```bash
cd agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python agent.py dev
```

## Milestones (from the PRD)

| Milestone | Deliverable | Status |
| --- | --- | --- |
| M1 | 210 number on Telnyx (KYC) + SIP trunk into LiveKit Cloud | You: start KYC |
| M2 | LiveKit agent calls a phone, speaks Greek via Gemini Live | Written, untested |
| M3 | Prompt merge, hang-up, hard cap, recording, Postgres schema | Written, backend tested on SQLite |
| M4 | SwiftUI app | Written, type-checks (`ios/README.md`) |
| M5 | Deploy + first real prank call | Not started |

Set `APP_API_TOKEN` and `INTERNAL_API_TOKEN` in `.env`; the app needs the same `APP_API_TOKEN` in its Settings tab.

## Not yet implemented

- WebSocket push for the live transcript (the app polls once a second).
- Deleting a recording does not delete its transcript.
- Voice selection is just a Gemini voice name (F8).
- Alembic migrations (`create_all` is used for now; fine locally, not for a real deploy).
