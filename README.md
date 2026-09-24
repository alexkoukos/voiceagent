# AI Prank Caller — Level 1

Personal tool: calls a friend from a Greek 210 landline number and runs a prank scenario in natural Greek. See [PRD AI Prank Caller (Level 1).md](<PRD AI Prank Caller (Level 1).md>) for the full spec.

## Layout

- `backend/` — FastAPI + Postgres. Owns friends, calls, prompt templates, transcripts, recording metadata. Merges the master + per-call prompt and dispatches the LiveKit agent job.
  - `backend/config/master_prompt.md` — the fixed master system prompt (F2), edit directly, no code change needed.
- `agent/` — LiveKit worker (`prank-caller`). Picks up dispatched jobs, dials the friend over the DIDWW SIP trunk, runs the conversation through Gemini Live, enforces the hard duration cap, and hangs up via its own tool.
- `ios/` — SwiftUI app (M4, not started).

## Status

Scaffolding only — no external accounts wired up yet. Nothing here has been run end-to-end.

## Local setup

```bash
cp .env.example .env   # fill in LiveKit / Gemini / DIDWW / R2 keys as you get them
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
| M1 | 210 number on DIDWW (KYC) + SIP trunk into LiveKit Cloud | Not started |
| M2 | LiveKit agent calls a phone, speaks Greek via Gemini Live | Skeleton in `agent/agent.py`, untested |
| M3 | Prompt merge, hang-up tool, hard cap, recording, Postgres schema | Backend skeleton done; recording (F6) not yet implemented |
| M4 | SwiftUI app | Not started |
| M5 | Deploy + first real prank call | Not started |

## Not yet implemented

- Recording capture (LiveKit Egress) and upload to R2, plus the "recording with notice + delete on request" flow required by the PRD's guardrails.
- Live transcript streaming to the app (currently only persisted `TranscriptEntry` rows, no WebSocket push).
- Voice selection mapping (F8) beyond passing a raw voice name through to Gemini Live.
- Alembic migrations (`Base.metadata.create_all` is used for now — fine for local dev, not for a real deploy).
