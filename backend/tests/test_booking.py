from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.booking import free_slots, in_part_of_day, resolve_date, say_date

ATH = ZoneInfo("Europe/Athens")
# Thursday
TODAY = date(2026, 9, 24)


def r(phrase):
    return resolve_date(phrase, TODAY)


def test_relative_days():
    assert r("αύριο").day == date(2026, 9, 25)
    assert r("Μεθαύριο το πρωί").day == date(2026, 9, 26)
    assert r("μεθαύριο το πρωί").part_of_day == "morning"
    assert r("σήμερα").day == TODAY
    assert r("tomorrow afternoon").day == date(2026, 9, 25)


def test_weekdays():
    assert r("την Τρίτη").day == date(2026, 9, 29)
    assert r("την Πέμπτη").day == date(2026, 10, 1)  # same weekday as today -> next week
    assert r("την Παρασκευή το απόγευμα").day == date(2026, 9, 25)
    assert r("την Παρασκευή το απόγευμα").part_of_day == "afternoon"
    assert r("την άλλη Τρίτη").day == date(2026, 9, 29)
    assert r("την άλλη Παρασκευή").day == date(2026, 10, 2)
    assert r("Σαββάτου").day == date(2026, 9, 26)
    assert r("next Monday").day == date(2026, 9, 28)


def test_next_week_and_explicit_dates():
    assert r("την άλλη εβδομάδα").day == date(2026, 9, 28)
    assert r("15/10").day == date(2026, 10, 15)
    assert r("15 Οκτωβρίου").day == date(2026, 10, 15)
    assert r("3 Ιανουαρίου").day == date(2027, 1, 3)
    assert r("2026-11-02").day == date(2026, 11, 2)
    # "5.30" is a time, not a date
    assert r("αύριο στις 5.30").day == date(2026, 9, 25)
    assert r("κάποια στιγμή").day is None


def test_say_date():
    assert say_date(date(2026, 10, 13)) == "Τρίτη 13 Οκτωβρίου"


def practice(**rules):
    return SimpleNamespace(
        timezone="Europe/Athens",
        hours={"mon": [["09:00", "12:00"], ["17:00", "19:00"]]},
        rules={"min_notice_minutes": 0, **rules},
    )


def test_free_slots_respects_hours_busy_and_buffer():
    day = date(2026, 9, 28)  # Monday
    now = datetime(2026, 9, 24, 12, tzinfo=ATH)
    busy = [(datetime(2026, 9, 28, 10, 0, tzinfo=ATH), datetime(2026, 9, 28, 10, 30, tzinfo=ATH))]
    slots = [s.strftime("%H:%M") for s in free_slots(practice(slot_step_minutes=30), day, 30, busy, now)]
    assert slots == ["09:00", "09:30", "10:30", "11:00", "11:30", "17:00", "17:30", "18:00", "18:30"]
    slots = [s.strftime("%H:%M") for s in free_slots(practice(slot_step_minutes=30, buffer_minutes=15), day, 30, busy, now)]
    assert "09:30" not in slots and "10:30" not in slots
    # 45-minute visit must end by closing time
    slots = [s.strftime("%H:%M") for s in free_slots(practice(slot_step_minutes=15), day, 45, [], now)]
    assert slots[-1] == "18:15" and "11:15" in slots and "11:30" not in slots


def test_free_slots_closed_holiday_notice_and_horizon():
    now = datetime(2026, 9, 28, 9, 50, tzinfo=ATH)
    assert free_slots(practice(), date(2026, 9, 29), 30, [], now) == []  # Tuesday: closed
    assert free_slots(practice(holidays=["2026-10-05"]), date(2026, 10, 5), 30, [], now) == []
    assert free_slots(practice(max_days_ahead=7), date(2026, 10, 12), 30, [], now) == []
    slots = free_slots(practice(min_notice_minutes=60), date(2026, 9, 28), 30, [], now)
    assert slots[0].strftime("%H:%M") == "11:00"


def test_part_of_day():
    assert in_part_of_day(datetime(2026, 9, 28, 17, 0, tzinfo=ATH), "afternoon")
    assert not in_part_of_day(datetime(2026, 9, 28, 9, 0, tzinfo=ATH), "afternoon")


def test_relative_to_last_offer():
    offered = date(2026, 9, 29)
    assert resolve_date("νωρίτερα", TODAY, offered).day == offered
    assert resolve_date("πιο αργά το απόγευμα", TODAY, offered).part_of_day == "afternoon"
    assert resolve_date("την επόμενη μέρα", TODAY, offered).day == date(2026, 9, 30)
    assert resolve_date("την άλλη μέρα", TODAY, offered).day == date(2026, 9, 30)
    assert resolve_date("νωρίτερα", TODAY).day is None  # nothing offered yet
    assert resolve_date("την Πέμπτη νωρίτερα", TODAY, offered).day == date(2026, 10, 1)


def test_relative_time_words():
    from app.booking import relative_time
    assert relative_time("πιο νωρίς") == "earlier"
    assert relative_time("Νωρίτερα γίνεται;") == "earlier"
    assert relative_time("κάτι πιο αργά") == "later"
    assert relative_time("αργότερα") == "later"
    assert relative_time("later please") == "later"
    assert relative_time("την Τρίτη") is None
