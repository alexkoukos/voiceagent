"""Eval Suite V0.1 and text benchmark (ASTRA.md).

Drives the real receptionist agent (prompt, tools, pipeline LLM) in text mode against a
LOCAL backend and Postgres, then asserts on backend state: appointments, messages,
handoffs, outcome and the tools the worker called. No audio, phone or LiveKit room.

    docker compose up -d postgres            # repo root
    cd backend && INTERNAL_API_TOKEN=dev-agent ADMIN_API_TOKEN=dev-founder \\
        uv run --python 3.12 --with-requirements requirements.txt uvicorn app.main:app
    cd agent && INTERNAL_API_TOKEN=dev-agent ADMIN_API_TOKEN=dev-founder GEMINI_API_KEY=... \\
        uv run --python 3.12 --with-requirements requirements.txt python evals/run_evals.py

Options: --only EVAL-001,EVAL-004  --repeat 5 (benchmark)  --baseline <results file name>
Exit code 1 when any critical check fails or a pass rate drops below the baseline.
"""

import argparse
import asyncio
import json
import os
import random
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
RECOVERY_LINES = {"Συγγνώμη, μπορείτε να το πείτε ξανά;", "Sorry, could you say that again?"}
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
CRITICAL = {"new_appointments", "service_id", "weekday", "not_weekday_afternoon", "seeded_status",
            "seeded_weekday", "handoff_status_in", "messages", "no_reconfirm", "customer_not_staff",
            "no_language_switch_to"}


# --- checks (pure; unit-tested in test_evals.py) ---

def local(value: str, tz: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ZoneInfo(tz))


def plain(text: str) -> str:
    """Lowercase, no accents, final ς as σ (like the backend's booking._plain)."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn").replace("ς", "σ")


def subsequence(wanted: list[str], called: list[str]) -> bool:
    it = iter(called)
    return all(name in it for name in wanted)


def check(expect: dict, obs: dict, critical_all: bool = False) -> list[dict]:
    """obs: outcome, new_appointments, seeded, tools [(name, args, result)], agent_text,
    handoffs, messages, timezone."""
    tz = obs.get("timezone", "Europe/Athens")
    new = obs.get("new_appointments", [])
    names = [name for name, _args, _result in obs.get("tools", [])]
    results = []

    def add(name, passed, detail):
        results.append({"check": name, "passed": bool(passed), "detail": detail,
                        "critical": critical_all or name in CRITICAL})

    for name, want in expect.items():
        if name == "outcome":
            add(name, obs.get("outcome") == want, f"got {obs.get('outcome')}")
        elif name == "new_appointments":
            add(name, len(new) == want, f"got {len(new)}")
        elif name == "service_id":
            add(name, bool(new) and all(a["service_id"] == want for a in new), [a["service_id"] for a in new])
        elif name == "weekday":
            days = [WEEKDAYS[local(a["starts_at"], tz).weekday()] for a in new]
            add(name, bool(days) and all(d == want for d in days), days)
        elif name == "not_weekday_afternoon":
            bad = [a for a in new if WEEKDAYS[local(a["starts_at"], tz).weekday()] == want
                   and local(a["starts_at"], tz).hour >= 14]
            add(name, bool(new) and not bad, [a["starts_at"] for a in new])
        elif name == "customer_name_contains":  # a string, or a list of accepted spellings
            options = [want] if isinstance(want, str) else want
            add(name, bool(new) and all(any(plain(o) in plain(a["customer_name"]) for o in options) for a in new),
                [a["customer_name"] for a in new])
        elif name == "seeded_status":
            seeded = obs.get("seeded") or {}
            add(name, seeded.get("status") == want, f"got {seeded.get('status')}")
        elif name == "seeded_weekday":
            seeded = obs.get("seeded") or {}
            day = WEEKDAYS[local(seeded["starts_at"], tz).weekday()] if seeded.get("starts_at") else None
            add(name, day == want, f"got {day}")
        elif name == "tools_in_order":
            add(name, subsequence(want, names), names)
        elif name == "tools_called":
            add(name, all(tool in names for tool in want), names)
        elif name == "tools_not_called":
            add(name, not any(tool in names for tool in want), names)
        elif name == "agent_said_all":  # each item: a fragment, or a list of accepted forms
            text = obs.get("agent_text", "").lower()
            missing = [f for f in want if not any(o.lower() in text for o in ([f] if isinstance(f, str) else f))]
            add(name, not missing, missing)
        elif name == "handoff_status_in":
            statuses = [h["status"] for h in obs.get("handoffs", [])]
            closed = any(tool == "route_call" and isinstance(result, dict) and result.get("hours_state") == "closed"
                         for tool, _args, result in obs.get("tools", []))
            if closed:  # after hours the correct path is a message, never a transfer
                add(name, not statuses and "transfer_to_human" not in names, f"closed now; handoffs {statuses}")
            else:
                add(name, bool(statuses) and statuses[-1] in want, statuses)
        elif name == "messages":
            add(name, len(obs.get("messages", [])) == want, f"got {len(obs.get('messages', []))}")
        elif name == "no_reconfirm":  # the first clear yes was accepted
            rejected = [t for t, _a, r in obs.get("tools", []) if isinstance(r, dict)
                        and r.get("error") == "confirmation_required"]
            add(name, not rejected, rejected)
        elif name == "customer_not_staff":
            bad = [a["customer_name"] for a in new
                   if any(plain(w) in plain(a["customer_name"]) for w in want)]
            add(name, bool(new) and not bad, [a["customer_name"] for a in new])
        elif name == "no_language_switch_to":
            switched = [a for t, a, _r in obs.get("tools", []) if t == "set_language" and a.get("language") == want]
            add(name, not switched, switched)
        elif name == "first_route_intent_in":
            intents = [args.get("intent") for tool, args, _ in obs.get("tools", []) if tool == "route_call"]
            add(name, bool(intents) and intents[0] in want, intents)
        else:
            add(name, False, "unknown check")
    return results


def percentile(values, share):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(share * (len(ordered) - 1)))]


def summarize(runs: list[dict]) -> dict:
    """Benchmark numbers across all runs: pass rates, latency spread, tool errors, cost."""
    by_eval: dict[str, list[dict]] = {}
    for run in runs:
        by_eval.setdefault(run["id"], []).append(run)
    turns = [ms for run in runs for ms in run.get("turn_ms", [])]
    first = [ms for run in runs for ms in run.get("first_reply_ms", [])]
    tools = [t for run in runs for t in run.get("tools", [])]
    costs = [run["cost_usd"] for run in runs if run.get("cost_usd") is not None]
    spread = lambda values: {"n": len(values), "p50": percentile(values, .5),  # noqa: E731
                             "p95": percentile(values, .95), "p99": percentile(values, .99)}
    return {
        "runs": len(runs),
        "success_rate": sum(r["passed"] for r in runs) / len(runs) if runs else None,
        "critical_failures": sum(1 for r in runs if not r["critical_passed"]),
        "per_eval": {eid: {"pass_rate": sum(r["passed"] for r in rs) / len(rs),
                           "critical_pass_rate": sum(r["critical_passed"] for r in rs) / len(rs), "runs": len(rs)}
                     for eid, rs in sorted(by_eval.items())},
        "turn_ms": spread(turns), "first_reply_ms": spread(first),
        "tool_calls": len(tools),
        "tool_error_rate": (sum(1 for t in tools if isinstance(t[2], dict) and t[2].get("error")) / len(tools)
                            if tools else None),
        "recoveries": sum(run.get("recoveries", 0) for run in runs),
        "cost_usd_per_call": sum(costs) / len(costs) if costs else None,
        "cost_known_calls": len(costs),
    }


def regressions(current: dict, baseline: dict) -> list[str]:
    found = []
    for eid, now in current["per_eval"].items():
        before = baseline.get("per_eval", {}).get(eid)
        if before and now["critical_pass_rate"] < before["critical_pass_rate"]:
            found.append(f"{eid}: critical pass rate {before['critical_pass_rate']:.0%} -> {now['critical_pass_rate']:.0%}")
        elif before and now["pass_rate"] < before["pass_rate"]:
            found.append(f"{eid}: pass rate {before['pass_rate']:.0%} -> {now['pass_rate']:.0%} (non-critical)")
    return found


# --- harness ---

class FakeRoom:
    name = "eval"

    def __init__(self):
        self.remote_participants = {}
        self.disconnected = False

    def on(self, *_args):
        pass

    def off(self, *_args):
        pass

    async def disconnect(self):
        self.disconnected = True


class FakeCtx:
    def __init__(self):
        self.room = FakeRoom()
        self.proc = SimpleNamespace(userdata={})
        self.callbacks = []

    def add_shutdown_callback(self, callback):
        self.callbacks.append(callback)

    def shutdown(self, reason=""):
        pass


def seed_date(weekday: str, tz: str) -> date:
    today = datetime.now(ZoneInfo(tz)).date()
    target = WEEKDAYS.index(weekday)
    day = today + timedelta(days=2)
    while day.weekday() != target:
        day += timedelta(days=1)
    return day


class Harness:
    def __init__(self, http, worker, founder_key: str):
        self.http = http
        self.worker = worker
        self.founder = {"x-api-key": founder_key}
        self.practice = None

    async def create_practice(self):
        fixture = json.loads((HERE / "practice.json").read_text(encoding="utf-8"))
        staff = fixture.pop("staff")
        suffix = random.randrange(10**6, 10**7)
        fixture.update(slug=f"eval-{suffix}", phone_numbers=[f"+30210{suffix}"])
        r = await self.http.post("/practices", headers=self.founder, json=fixture)
        r.raise_for_status()
        self.practice = r.json()
        for member in staff:
            (await self.http.post(f"/practices/{self.practice['id']}/staff", headers=self.founder,
                                  json=member)).raise_for_status()

    async def appointments(self) -> list[dict]:
        r = await self.http.get(f"/practices/{self.practice['id']}/appointments", headers=self.founder)
        r.raise_for_status()
        return r.json()

    async def run(self, spec: dict) -> dict:
        worker, pid, tz = self.worker, self.practice["id"], self.practice.get("timezone", "Europe/Athens")
        caller = f"+3069{random.randrange(10**7, 10**8)}"
        seeded_id = None
        if seed := spec.get("seed_appointment"):
            # Earlier evals in this practice may hold the slot: take the next free one that morning.
            hour, minute = map(int, seed["time"].split(":"))
            for step in range(12):
                total = hour * 60 + minute + 15 * step
                r = await self.http.post(f"/practices/{pid}/appointments", headers=self.founder, json={
                    "date": seed_date(seed["days_ahead_weekday"], tz).isoformat(),
                    "time": f"{total // 60:02d}:{total % 60:02d}", "service_id": seed["service_id"],
                    "customer_name": seed["customer_name"], "customer_phone": caller})
                if r.status_code != 409:
                    break
            r.raise_for_status()
            seeded_id = r.json()["id"]
        before = {a["id"] for a in await self.appointments()}

        r = await self.http.post("/internal/inbound", headers={"x-agent-token": worker.AGENT_TOKEN}, json={
            "dialed_number": self.practice["phone_numbers"][0], "caller_number": caller, "forwarding_reason": None})
        r.raise_for_status()
        metadata = r.json()
        call_id = metadata["call_id"]

        ctx = FakeCtx()
        worker.get_job_context = lambda: ctx
        # The production receptionist engine's code paths (read-backs via say(), fillers),
        # with text in place of STT/TTS; telemetry is tagged text_eval.
        worker.language_parts = lambda *_args, **_kwargs: {}
        rc = worker.ReceptionistCall(ctx, metadata, "pipeline")
        tools: list[tuple] = []
        original_tool = rc.tool

        async def recording_tool(name, args):
            result = await original_tool(name, args)
            tools.append((name, args, result))
            return result
        rc.tool = recording_tool

        session = worker.AgentSession(llm=worker.text_llm())
        rc.session = session
        agent_lines: list[tuple[float, str]] = []
        session.on("conversation_item_added", lambda ev: agent_lines.append((time.perf_counter(), ev.item.text_content or ""))
                   if getattr(ev.item, "role", None) == "assistant" else None)
        await worker.report(call_id, status="active")
        rc.agent = rc.make_agent(rc.language)
        worker.track_transcript(session, call_id, rc.engine, room=None, caller_identity=None,
                                language=lambda: rc.language)
        rc.telemetry = worker.track_telemetry(session, ctx, call_id, "text_eval")
        rc.watch_llm_errors()
        await session.start(agent=rc.agent)
        started = time.perf_counter()
        turn_ms, first_reply_ms, error, recoveries = [], [], None, 0
        try:
            await rc.agent.greet()
            await self.settle(rc, session)
            pending = list(spec["lines"])
            while pending:
                line = pending.pop(0)
                if ctx.room.disconnected:
                    break
                rc.heard_user(line)
                rc.check_emergency(line)
                rc.check_profanity(line)
                rc.check_language(line)
                await self.settle(rc, session)
                if worker.wants_language(line):
                    # Production interrupts the model's reply to "English mode": the caller
                    # only hears the fixed switch line, which settle() has just played.
                    continue
                seen, t0 = len(agent_lines), time.perf_counter()
                await asyncio.wait_for(session.run(user_input=line), timeout=60)
                turn_ms.append((time.perf_counter() - t0) * 1000)
                await self.settle(rc, session)
                replies = [at for at, text in agent_lines[seen:] if text.strip()]
                if replies:
                    first_reply_ms.append((replies[0] - t0) * 1000)
                if any(text.strip() in RECOVERY_LINES for _, text in agent_lines[seen:]):
                    # The reply failed and the agent asked to repeat: a real caller would.
                    recoveries += 1
                    if recoveries <= 2:
                        pending.insert(0, line)
        except Exception as e:  # a crash is a failed eval, not a crashed suite
            error = f"{type(e).__name__}: {e}"
        finally:
            for callback in ctx.callbacks:
                await callback()
            await session.aclose()
            await worker.report(call_id, status="completed", _retries=3,
                                flags=sorted(rc.flags & {"tool_error"}))

        detail = await self.finished(call_id)
        after = await self.appointments()
        new = [a for a in after if a["id"] not in before and a["call_id"] == call_id]
        seeded = next((a for a in after if a["id"] == seeded_id), None)
        cost = (await self.http.get(f"/monitor/api/calls/{call_id}", headers=self.founder)).json()["call"].get("cost_breakdown")
        obs = {"outcome": detail.get("outcome"), "new_appointments": new, "seeded": seeded, "tools": tools,
               "agent_text": "\n".join(text for _, text in agent_lines), "handoffs": detail.get("handoffs", []),
               "messages": detail.get("messages", []), "timezone": tz}
        checks = check(spec["expect"], obs, spec.get("critical_all", False))
        if error:
            checks.append({"check": "no_crash", "passed": False, "critical": True, "detail": error})
        return {
            "id": spec["id"], "title": spec["title"], "call_id": call_id,
            "passed": all(c["passed"] for c in checks),
            "critical_passed": all(c["passed"] for c in checks if c["critical"]),
            "checks": checks, "duration_s": round(time.perf_counter() - started, 1),
            "turn_ms": turn_ms, "first_reply_ms": first_reply_ms, "tools": tools, "recoveries": recoveries,
            "outcome": obs["outcome"], "cost_usd": cost.get("known_usd") if cost else None,
            "transcript": [e["role"] + ": " + e["text"] for e in detail.get("transcript_entries", [])],
        }

    settle_timeouts = 0

    @classmethod
    async def settle(cls, rc, session, timeout=30):
        """Wait until background tool work (read-backs, handoff waits) and speech are done.
        A text-mode speech can stay unfinished; waiting 90 s three times once outlasted the
        backend's 5-minute offer window (EVAL-012, 2026-10-01). Timeouts are counted."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        quiet = 0
        while loop.time() < deadline and quiet < 2:
            pending = [t for t in rc._tasks if not t.done()]
            swap = getattr(session, "_update_activity_atask", None)  # a language switch in progress
            if swap is not None and not swap.done():
                pending.append(swap)
            speech = session.current_speech
            if speech is not None and speech.done():
                speech = None  # after an agent swap the finished speech can stay "current"
            if pending:
                quiet = 0
                await asyncio.wait(pending, timeout=max(0.1, deadline - loop.time()))
            elif speech is not None:
                quiet = 0
                await asyncio.wait_for(speech.wait_for_playout(), timeout=max(0.1, deadline - loop.time()))
            else:
                quiet += 1
                await asyncio.sleep(0.4)
        if quiet < 2:
            cls.settle_timeouts += 1
            print(f"settle timed out after {timeout} s", file=sys.stderr)

    async def finished(self, call_id: str) -> dict:
        path = f"/practices/{self.practice['id']}/calls/{call_id}"
        detail = {}
        for _ in range(60):
            detail = (await self.http.get(path, headers=self.founder)).json()
            if detail.get("outcome"):
                break
            await asyncio.sleep(1)
        return detail


def print_report(runs: list[dict], stats: dict) -> None:
    for run in runs:
        mark = "PASS" if run["passed"] else ("FAIL" if not run["critical_passed"] else "WARN")
        print(f"{mark:4}  {run['id']}  {run['title']}  ({run['duration_s']} s, outcome={run['outcome']})")
        for c in run["checks"]:
            if not c["passed"]:
                print(f"        {'CRITICAL ' if c['critical'] else ''}{c['check']}: {c['detail']}")
    fmt = lambda s: "  ".join(f"{k} {v:.0f}" if isinstance(v, float) else f"{k} {v}" for k, v in s.items())  # noqa: E731
    print(f"\nruns {stats['runs']}  success {stats['success_rate']:.0%}  critical failures {stats['critical_failures']}")
    print(f"turn ms: {fmt(stats['turn_ms'])}")
    print(f"first reply ms: {fmt(stats['first_reply_ms'])}")
    print(f"failed replies recovered by asking to repeat: {stats['recoveries']}")
    if stats["tool_error_rate"] is not None:
        print(f"tool calls {stats['tool_calls']}  tool error rate {stats['tool_error_rate']:.1%}")
    if stats["cost_usd_per_call"] is not None:
        print(f"known cost per call ${stats['cost_usd_per_call']:.4f} ({stats['cost_known_calls']} calls)")


def dump_tasks() -> None:
    """kill -USR1 <pid>: print what every asyncio task is waiting on (debugging a hung eval)."""
    for task in asyncio.all_tasks():
        print(f"--- {task.get_name()}", file=sys.stderr)
        task.print_stack(limit=6, file=sys.stderr)


async def main() -> int:
    import faulthandler
    import signal
    asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, dump_tasks)
    faulthandler.register(signal.SIGUSR2, all_threads=True)  # works even if the loop is blocked
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", default="http://localhost:8000")
    parser.add_argument("--only", default="")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--baseline", default="")
    parser.add_argument("--allow-remote", action="store_true",
                        help="Allow a non-local backend (creates a test practice there).")
    args = parser.parse_args()
    if urlparse(args.backend).hostname not in ("localhost", "127.0.0.1") and not args.allow_remote:
        sys.exit("Refusing a non-local backend: evals create practices and calls. Use --allow-remote to override.")
    for name in ("INTERNAL_API_TOKEN", "ADMIN_API_TOKEN", "GEMINI_API_KEY"):
        if not os.environ.get(name):
            sys.exit(f"Set {name}.")
    os.environ["BACKEND_PUBLIC_URL"] = args.backend
    os.environ["ELEVEN_API_KEY"] = ""  # text mode: no pre-voiced audio (and no TTS spend)
    os.environ.setdefault("AGENT_VERSION", "eval")
    sys.path.insert(0, str(HERE.parent))
    import httpx
    import agent as worker

    specs = json.loads((HERE / "evals.json").read_text(encoding="utf-8"))["evals"]
    wanted = {s.strip() for s in args.only.split(",") if s.strip()}
    specs = [s for s in specs if not wanted or s["id"] in wanted]
    for spec in specs:
        if spec["mode"] != "text":
            print(f"SKIP  {spec['id']}  {spec['title']}  (audio: {', '.join(spec.get('audio_scenarios', []))})")
    runs = []
    async with httpx.AsyncClient(base_url=args.backend, timeout=30) as http:
        harness = Harness(http, worker, os.environ["ADMIN_API_TOKEN"])
        await harness.create_practice()
        for _ in range(args.repeat):
            for spec in specs:
                if spec["mode"] == "text":
                    print(f"...   {spec['id']}", flush=True)
                    runs.append(await harness.run(spec))
    stats = summarize(runs)
    print_report(runs, stats)
    out = HERE / "results" / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    stats["harness_settle_timeouts"] = Harness.settle_timeouts
    print(f"harness settle timeouts: {Harness.settle_timeouts}")
    out.write_text(json.dumps({"agent_version": os.environ["AGENT_VERSION"], "llm": worker.LLM_MODEL,
                               "stats": stats, "runs": runs}, ensure_ascii=False, indent=1, default=str))
    print(f"results: {out}")
    failed = stats["critical_failures"] > 0
    if args.baseline:
        # Only earlier result files: evals/results/<name>.json
        name = Path(args.baseline).name
        known = {p.name: p for p in (HERE / "results").glob("*.json")}
        if name not in known:
            sys.exit(f"Baseline must be a file in {HERE / 'results'}: {name}")
        found = regressions(stats, json.loads(known[name].read_text())["stats"])
        for line in found:
            print("REGRESSION", line)
        failed = failed or any("non-critical" not in line for line in found)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
