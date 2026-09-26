from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.booking import hours_state, match_staff
from app.prompts import default_greeting
from app.routing import is_emergency
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
