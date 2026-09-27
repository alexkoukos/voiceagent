from scripts.scripted_calls import check


def test_booking_scenario_checks_the_requested_local_time():
    scenario = {"name": "booking", "expect": {"time_local": "17:00"}}
    call = {"id": "call", "status": "completed", "outcome": "booked", "appointment": {"starts_at": "2026-09-29T13:45:00Z"}}
    assert check(scenario, call)["ok"] is False

    call["appointment"]["starts_at"] = "2026-09-29T14:00:00Z"
    assert check(scenario, call)["ok"] is True


def test_incomplete_and_failed_calls_never_pass_empty_expectations():
    for call in ({}, {"status": "active"}, {"status": "failed", "outcome": "error"}):
        assert not check({"name": "empty", "expect": {}}, call)["ok"]
