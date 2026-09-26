from datetime import datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app import booking
from app.booking import hours_state, match_staff
from app.prompts import default_greeting
from app.routing import PROFANITY_SCRIPT, is_emergency, is_profanity
from app.texts import customer_sms

ATH = ZoneInfo("Europe/Athens")


def practice(**kw):
    base = dict(name="Οδοντιατρείο Δοκιμή", timezone="Europe/Athens", language="el", greeting="",
                hours={"mon": [["09:00", "14:00"], ["17:00", "21:00"]]}, rules={}, vertical="dentist",
                routing_rules={}, knowledge_base={"Διεύθυνση": "Σόλωνος 10"}, phone_numbers=["+302100000001"])
    return SimpleNamespace(**(base | kw))


def staff(name, role="staff", aliases=()):
    return SimpleNamespace(name=name, role=role, aliases=list(aliases), bookable=True)


def test_match_staff_greek_endings_and_roles():
    people = [staff("Γιώργος Παπαδάκης"), staff("Μαρία Νικολάου", "doctor", ["η γιατρός"])]
    assert match_staff(people, "με τον Γιώργο").name == "Γιώργος Παπαδάκης"
    assert match_staff(people, "τον κύριο Παπαδάκη").name == "Γιώργος Παπαδάκης"
    assert match_staff(people, "με τη Μαρία").name == "Μαρία Νικολάου"
    assert match_staff(people, "τη γιατρό").name == "Μαρία Νικολάου"
    assert match_staff(people, "με τον Κώστα") is None


def test_hours_state():
    p = practice()
    assert hours_state(p, datetime(2026, 9, 28, 10, tzinfo=ATH))[0] == "open"
    state, nxt = hours_state(p, datetime(2026, 9, 28, 15, tzinfo=ATH))
    assert state == "break" and nxt == datetime(2026, 9, 28, 17, tzinfo=ATH)
    state, nxt = hours_state(p, datetime(2026, 9, 28, 22, tzinfo=ATH))
    assert state == "closed" and nxt == datetime(2026, 10, 5, 9, tzinfo=ATH)
    assert hours_state(practice(rules={"holidays": ["2026-09-28"]}), datetime(2026, 9, 28, 10, tzinfo=ATH))[0] == "closed"


def test_emergency_phrases():
    p = practice()
    assert is_emergency(p, "Ο πατέρας μου ΔΕΝ ΑΝΑΠΝΕΈΙ καλά")
    assert is_emergency(p, "έχω πόνο στο στήθος... πόνος στο στήθος")
    assert not is_emergency(p, "θέλω ραντεβού για καθαρισμό")
    assert not is_emergency(practice(vertical="barber"), "δεν αναπνέει")


def test_profanity_directed_insults():
    # Directed insults/curses, accents and case folded, trigger the polite reminder.
    assert is_profanity("είσαι μαλάκας ρε", "el")
    assert is_profanity("ΓΑΜΉΣΟΥ", "el")
    assert is_profanity("να γαμηθείς", "el")
    assert is_profanity("you idiot", "en")
    assert is_profanity("fuck you", "en")
    assert is_profanity("just shut up", "en")


def test_profanity_leaves_casual_talk_alone():
    # Casual fillers and normal appointment phrasing are never flagged.
    assert not is_profanity("γαμώτο με πονάει το δόντι", "el")
    assert not is_profanity("ρε συ, θέλω ένα ραντεβού", "el")
    assert not is_profanity("θέλω ραντεβού για καθαρισμό", "el")
    assert not is_profanity("damn it, I forgot the time", "en")
    assert not is_profanity("I'd like to book an appointment", "en")


def test_profanity_script_has_both_languages():
    assert set(PROFANITY_SCRIPT) == {"el", "en"}
    assert PROFANITY_SCRIPT["el"] and PROFANITY_SCRIPT["en"]


def test_greeting_is_only_the_assistant_line():
    p = practice()
    assert default_greeting(p, language="el") == ("Οδοντιατρείο Δοκιμή. Είμαι ο ψηφιακός βοηθός, "
                                                    "πώς μπορώ να σας βοηθήσω; For English, say English.")
    assert default_greeting(p, language="en") == "I'm a digital assistant, how can I help you?"
    assert default_greeting(practice(greeting="Γεια σας!"), language="el") == "Γεια σας!"


def test_customer_sms():
    appt = {"date_spoken": "Τρίτη 29 Σεπτεμβρίου", "time": "17:30", "service": "Έλεγχος"}
    text = customer_sms(practice(), "booked", appt, "el")
    assert "17:30" in text and "Σόλωνος 10" in text and "+302100000001" in text


@pytest.mark.asyncio
async def test_equal_time_bounds_check_one_exact_slot(monkeypatch):
    p = practice(
        id="mock-barber",
        services=[{"id": "haircut", "name": "Κούρεμα", "duration_minutes": 30}],
        hours={"tue": [["16:00", "20:00"]]}, calendar_id=None,
    )
    monkeypatch.setattr(booking, "staff_of", AsyncMock(return_value=[]))
    slots = {
        datetime(2026, 9, 29, hour, minute, tzinfo=ATH): []
        for hour, minute in [(16, 45), (17, 0), (17, 15)]
    }
    monkeypatch.setattr(booking, "availability", AsyncMock(return_value=slots))

    result = await booking.check_availability(
        None, p, "την Τρίτη το απόγευμα", "haircut",
        datetime(2026, 9, 25, 12, tzinfo=ATH), after=time(17), before=time(17),
    )

    assert result["free_times"] == ["17:00"]
