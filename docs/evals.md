# Evals and benchmarks

Evals answer "does it work correctly?". Benchmarks answer "how fast, how reliably, at
what cost?". Production telemetry (`/monitor`) answers "what actually happened?".

## Eval Suite V0.1

Definitions: [agent/evals/evals.json](../agent/evals/evals.json). Runner:
[agent/evals/run_evals.py](../agent/evals/run_evals.py).

Each text eval drives the real `ReceptionistAgent` (production prompt from the backend,
the same tools, `text_llm()`, the `pipeline` engine's code paths) through a scripted
caller, one line per turn, against a local backend and Postgres. Pass/fail comes from
backend state, not from an LLM judge:

- appointments created (exactly N, service, weekday, never in a forbidden slot)
- the seeded appointment's status and day after a reschedule/cancel
- handoff attempted and its result; messages saved (exactly N)
- the tools the worker called, in order, and the call outcome

Checks on database state are **critical**: any critical failure makes the run exit 1.
Wording checks (outcome, tools, what the agent said) are warnings.

| ID | Case | Mode |
|---|---|---|
| EVAL-001 | New patient booking, golden path | text |
| EVAL-002 | Hours, prices, services | text |
| EVAL-003 | Caller changes their mind mid-booking | text |
| EVAL-004 | Reschedule existing appointment | text |
| EVAL-005 | Cancel existing appointment | text |
| EVAL-006 | Fast Greek + barge-in | audio only |
| EVAL-007 | Greek/English switching + difficult name | text (recognition part: audio) |
| EVAL-008 | Requested time unavailable | text |
| EVAL-009 | Transfer with failed-transfer fallback | text |
| EVAL-010 | Ambiguous caller needing clarification | text |
| EVAL-011 | No booking without the caller's yes | text, all checks critical |

Not covered by text evals: speech recognition, turn detection, barge-in, TTS, Gemini
Live (`realtime`). Use `agent/scripts/scripted_calls.py` (WebRTC, synthetic caller voice)
for those, against a deployed backend and worker.

### Run locally

```sh
docker compose up -d postgres
docker exec voiceagent-postgres-1 psql -U user -d voiceagent -c "CREATE DATABASE evals"
cd backend
export DATABASE_URL=postgresql+asyncpg://user:password@localhost:5433/evals
export INTERNAL_API_TOKEN=dev-agent ADMIN_API_TOKEN=dev-founder GEMINI_API_KEY=...
uv run --python 3.12 --with-requirements requirements.txt alembic upgrade head
uv run --python 3.12 --with-requirements requirements.txt uvicorn app.main:app --port 8765 &
cd ../agent
uv run --python 3.12 --with-requirements requirements.txt python evals/run_evals.py \
    --backend http://localhost:8765 [--only EVAL-001,EVAL-009] [--repeat 3] [--baseline evals/results/<file>.json]
```

The runner refuses a non-local backend unless `--allow-remote` is given, because it
creates a practice and calls. Results are written to `agent/evals/results/` (ignored by
git) with transcripts, tool calls, checks, timings and cost. Eval calls also appear in
`/monitor` with engine `text_eval`.

In CI, `.github/workflows/evals.yml` runs the suite on pull requests touching `agent/` or
`backend/` and on demand. It needs the repository secret `GEMINI_API_KEY`; without it the
job is skipped with a notice.

### Adding an eval

Capture failure → reproduce as an eval → see it fail → fix → run the eval → run the full
suite → deploy. Prefer database-state checks. Use the scripts of real calls that went wrong
(fictional names only).

## Benchmarks

`--repeat N` turns the suite into a benchmark. The summary reports pass rate per eval,
critical failures, turn time and time to first reply (p50/p95/p99), tool error rate, and
known cost per call. Pass `--baseline` with an earlier results file to compare: a lower
critical pass rate is a regression and exits 1; a lower non-critical pass rate is reported.

Text timings are LLM + tool round trips only (no STT, turn detection, TTS or network to
the caller). For perceived voice latency, run scripted audio calls against a deployment
and read reply p50/p95/p99 and stage timings in `/monitor` (summary window).

Never judge from one run: the LLM is not deterministic. Use at least `--repeat 3` before
comparing versions.
