# AI Prank Caller — Claude notes

See [README.md](README.md) for layout, setup and deploy, and the PRD for the spec.

## Checkpoint (2026-09-24)

Code-complete through M6, latest commit `c094926`. Nothing has run against real LiveKit / Gemini / Telnyx / R2.

**Done and verified**
- Backend on real Postgres (Docker): migrations, auth, agent-event endpoint, WebSocket push, delete-on-request (R2/LiveKit stubbed).
- `POST /webhooks/telnyx` ([backend/app/routers/webhooks.py](backend/app/routers/webhooks.py)): ed25519 signature check when `TELNYX_PUBLIC_KEY` is set (valid → 200, tampered → 401). It only logs; it changes no call state.
- Agent imports and instantiates on `livekit-agents` 1.8.3 in Docker.
- iOS builds and launches in the simulator; templates load.

**Not done / blocked**
- M1: 210 number on Telnyx + SIP trunk into LiveKit. Needs the user in Greece.
- `.env` exists locally (git-ignored, copied from `.env.example`) but keys are not filled in.
- Agent call flow (SIP dial, Gemini Greek, hang-up tool, hard cap) is untested.
- iOS call flow has not been driven from the app.
- Not deployed to Fly.io (M5).
- Open questions: Gemini Greek quality over phone audio, latency, 210 caller ID on Greek mobiles.

## Working notes
- Python 3.14 breaks venv/ensurepip here; run backend code with `uv run --python 3.12 --with-requirements backend/requirements.txt ...`.
- Run a single backend instance (live push keeps subscribers in memory).
- Never commit `.env`.
