import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "evals"))
from run_evals import check, regressions, subsequence, summarize  # noqa: E402

TUESDAY_10 = "2026-10-06T07:00:00"  # UTC -> 10:00 in Athens
TUESDAY_17 = "2026-10-06T14:00:00"


def appt(starts_at=TUESDAY_10, **fields):
    return {"starts_at": starts_at, "service_id": "checkup", "customer_name": "Γιώργος Παπαδόπουλος",
            "status": "booked", **fields}


def test_booking_checks_use_practice_timezone_and_db_state():
    obs = {"outcome": "booked", "new_appointments": [appt()],
           "tools": [("route_call", {"intent": "book"}, {}), ("check_availability", {}, {}),
                     ("prepare_action", {}, {}), ("book_appointment", {}, {})]}
    results = check({"outcome": "booked", "new_appointments": 1, "weekday": "tue", "service_id": "checkup",
                     "customer_name_contains": "παπαδόπουλ",
                     "tools_in_order": ["check_availability", "book_appointment"],
                     "first_route_intent_in": ["book"]}, obs)
    assert all(r["passed"] for r in results), results
    assert {r["check"] for r in results if r["critical"]} == {"new_appointments", "weekday", "service_id"}


def test_double_booking_and_forbidden_afternoon_fail_critically():
    obs = {"new_appointments": [appt(), appt(TUESDAY_17)]}
    results = {r["check"]: r for r in check({"new_appointments": 1, "not_weekday_afternoon": "tue"}, obs)}
    assert not results["new_appointments"]["passed"] and results["new_appointments"]["critical"]
    assert not results["not_weekday_afternoon"]["passed"]


def test_critical_all_and_unknown_checks():
    results = check({"tools_not_called": ["book_appointment"], "nonsense": 1},
                    {"tools": [("book_appointment", {}, {})]}, critical_all=True)
    assert [r["passed"] for r in results] == [False, False]
    assert all(r["critical"] for r in results)


def test_subsequence_requires_order():
    assert subsequence(["a", "b"], ["x", "a", "y", "b"])
    assert not subsequence(["b", "a"], ["a", "b"])
    assert subsequence(["a", "a"], ["a", "x", "a"]) and not subsequence(["a", "a"], ["a"])


def test_summary_and_baseline_regressions():
    runs = [
        {"id": "E1", "passed": True, "critical_passed": True, "turn_ms": [100, 300], "first_reply_ms": [90],
         "tools": [("t", {}, {"ok": True}), ("t", {}, {"error": "x"})], "cost_usd": 0.01},
        {"id": "E1", "passed": False, "critical_passed": False, "turn_ms": [200], "first_reply_ms": [],
         "tools": [], "cost_usd": None},
    ]
    stats = summarize(runs)
    assert stats["success_rate"] == 0.5 and stats["critical_failures"] == 1
    assert stats["turn_ms"] == {"n": 3, "p50": 200, "p95": 300, "p99": 300}
    assert stats["tool_error_rate"] == 0.5 and stats["cost_usd_per_call"] == 0.01
    baseline = {"per_eval": {"E1": {"pass_rate": 1.0, "critical_pass_rate": 1.0}}}
    assert regressions(stats, baseline) == ["E1: critical pass rate 100% -> 50%"]


def test_eval_file_is_consistent():
    data = json.loads((Path(__file__).resolve().parent / "evals" / "evals.json").read_text())
    ids = [e["id"] for e in data["evals"]]
    assert len(ids) == len(set(ids)) and {f"EVAL-{n:03d}" for n in range(1, 11)} <= set(ids)
    for spec in data["evals"]:
        if spec["mode"] == "text":
            assert spec["lines"] and spec["expect"]
            assert "unknown check" not in json.dumps(check(spec["expect"], {}))
