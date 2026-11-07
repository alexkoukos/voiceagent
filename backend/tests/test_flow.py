"""The state-machine flow (app/flow.py): whole conversations, turn by turn, against Postgres."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import flow
from app.models import Appointment, Call, Message, Practice, TranscriptEntry, TranscriptRole

HOURS = {d: [["09:00", "13:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}
SERVICES = [{"id": "checkup", "name": "Έλεγχος", "duration_minutes": 30},
            {"id": "cleaning", "name": "Καθαρισμός", "duration_minutes": 30}]


async def setup(db, *, caller="+306900000001"):
    practice = Practice(name="Test", timezone="Europe/Athens", hours=HOURS, services=SERVICES,
                        rules={"min_notice_minutes": 0}, language="el")
    db.add(practice)
    await db.flush()
    call = Call(practice_id=practice.id, language="el", caller_number=caller, direction="inbound")
    db.add(call)
    await db.commit()
    return practice, call


async def say(db, call, text, **understood):
    """The caller's line is stored first, as the worker does before each turn."""
    db.add(TranscriptEntry(call_id=call.id, role=TranscriptRole.caller, text=text))
    await db.commit()
    return await flow.turn(db, await db.get(Call, call.id), text, understood)


def next_weekday() -> str:
    day = datetime.now(ZoneInfo("Europe/Athens")).date() + timedelta(days=1)
    while day.weekday() > 4:
        day += timedelta(days=1)
    return day.isoformat()


@pytest.mark.asyncio
async def test_a_booking_from_hello_to_booked(sessions):
    async with sessions() as db:
        _, call = await setup(db)
        day = next_weekday()
        out = await say(db, call, "Θέλω ένα ραντεβού.", intent="book")
        assert out["say"].startswith("Για ποια υπηρεσία;")
        out = await say(db, call, "Καθαρισμό.", intent="none", service="cleaning")
        assert out["say"] == "Ποια μέρα θα σας βόλευε;"
        out = await say(db, call, day, intent="none", day=day)
        assert "έχουμε ελεύθερα στις εννέα" in out["say"]
        out = await say(db, call, "Στις δέκα.", intent="none", time="10:00")
        assert out["say"] == "Ποιο είναι το ονοματεπώνυμό σας;"
        out = await say(db, call, "Γιώργος Παπαδόπουλος.", intent="none", name="Γιώργος Παπαδόπουλος")
        assert out.get("readback") and "στις δέκα το πρωί" in out["say"] and out["say"].endswith("Σωστά;")
        out = await say(db, call, "Ναι, σωστά.", intent="none")
        assert out["say"].startswith("Έγινε, το ραντεβού σας κλείστηκε")
        call = await db.get(Call, call.id)
        appt = await db.get(Appointment, call.appointment_id)
        assert appt.service_id == "cleaning" and appt.customer_name == "Γιώργος Παπαδόπουλος"
        assert [a[0] for a in out["actions"]] == ["book_appointment"]


@pytest.mark.asyncio
async def test_a_correction_at_the_readback_stays_a_booking(sessions):
    async with sessions() as db:
        _, call = await setup(db)
        day = next_weekday()
        other = (datetime.fromisoformat(day) + timedelta(days=7)).date().isoformat()
        await say(db, call, "Ραντεβού για έλεγχο.", intent="book", service="checkup", day=day)
        await say(db, call, "Στις δέκα.", intent="none", time="10:00")
        out = await say(db, call, "Ελένη Γεωργίου.", intent="none", name="Ελένη Γεωργίου")
        assert out.get("readback")
        # The model reads the correction as a change of an existing appointment.
        out = await say(db, call, "Όχι, άλλη μέρα ήθελα.", intent="change", day=other)
        assert "έχουμε ελεύθερα" in out["say"]
        out = await say(db, call, "Η πρώτη ώρα.", intent="none", time="09:00")
        assert out.get("readback") and "Ελένη Γεωργίου" in out["say"]
        out = await say(db, call, "Ναι, σωστά.", intent="none")
        assert out["say"].startswith("Έγινε, το ραντεβού σας κλείστηκε")
        appt = await db.get(Appointment, (await db.get(Call, call.id)).appointment_id)
        assert appt.starts_at.astimezone(ZoneInfo("Europe/Athens")).date().isoformat() == other


@pytest.mark.asyncio
async def test_the_model_never_books_without_the_callers_own_yes(sessions):
    async with sessions() as db:
        _, call = await setup(db)
        day = next_weekday()
        await say(db, call, "Ραντεβού για έλεγχο.", intent="book", service="checkup")
        await say(db, call, day, intent="none", day=day)
        await say(db, call, "Στις εννέα.", intent="none", time="09:00")
        out = await say(db, call, "Μαρία Κ.", intent="none", name="Μαρία Κ.")
        assert out.get("readback")
        # The model's reading says yes, the caller's words don't: nothing is booked.
        out = await say(db, call, "Ναι, αλλά την Πέμπτη.", intent="none", day="την Πέμπτη")
        call = await db.get(Call, call.id)
        assert call.appointment_id is None and "confirm_pending" not in [a[0] for a in out["actions"]]


@pytest.mark.asyncio
async def test_small_talk_gets_the_models_answer_then_the_next_question(sessions):
    async with sessions() as db:
        _, call = await setup(db)
        out = await say(db, call, "Καλησπέρα, τι κάνετε;", intent="smalltalk")
        assert out == {**out, "reply": "model"} and not out.get("then")
        await say(db, call, "Θέλω ραντεβού για έλεγχο.", intent="book", service="checkup")
        out = await say(db, call, "Α, πού βρίσκεστε;", intent="question")
        assert out["reply"] == "model" and out["then"] == "Ποια μέρα θα σας βόλευε;"


@pytest.mark.asyncio
async def test_goodbye_ends_the_call(sessions):
    async with sessions() as db:
        _, call = await setup(db)
        out = await say(db, call, "Ευχαριστώ, γεια σας.", intent="smalltalk", goodbye=True)
        assert out["end_call"] is True


@pytest.mark.asyncio
async def test_a_time_that_is_not_free_gets_another_offer(sessions):
    async with sessions() as db:
        _, call = await setup(db)
        day = next_weekday()
        await say(db, call, "Ραντεβού για έλεγχο.", intent="book", service="checkup")
        await say(db, call, day, intent="none", day=day)
        out = await say(db, call, "Στις πέντε το απόγευμα.", intent="none", time="17:00")
        assert out["say"].startswith("Στις πέντε το απόγευμα δεν είναι ελεύθερα.")
        assert "έχουμε ελεύθερα" in out["say"]


@pytest.mark.asyncio
async def test_a_message_is_taken_with_name_and_reason(sessions):
    async with sessions() as db:
        _, call = await setup(db)
        out = await say(db, call, "Θέλω να αφήσω ένα μήνυμα.", intent="message")
        assert out["say"] == "Τι θέλετε να τους πω;"
        out = await say(db, call, "Να με πάρει ο γιατρός για τις εξετάσεις.", intent="none",
                        reason="να τον πάρει ο γιατρός για τις εξετάσεις")
        assert out["say"] == "Με ποιο όνομα να αφήσω το μήνυμα;"
        out = await say(db, call, "Νίκος Βλάχος.", intent="none", name="Νίκος Βλάχος")
        assert out["say"].startswith("Το μήνυμα καταγράφηκε")
        from sqlalchemy import select
        m = (await db.execute(select(Message).where(Message.call_id == call.id))).scalar_one()
        assert m.caller_name == "Νίκος Βλάχος" and m.callback_number == "+306900000001"


@pytest.mark.asyncio
async def test_a_web_call_without_a_number_is_asked_for_one(sessions):
    async with sessions() as db:
        _, call = await setup(db, caller=None)
        day = next_weekday()
        await say(db, call, "Ραντεβού για έλεγχο.", intent="book", service="checkup")
        await say(db, call, day, intent="none", day=day)
        await say(db, call, "Στις εννέα.", intent="none", time="09:00")
        out = await say(db, call, "Άννα Ρ.", intent="none", name="Άννα Ρ.")
        assert out["say"] == "Σε ποιο τηλέφωνο μπορούμε να σας βρούμε;"
        out = await say(db, call, "6912345678", intent="none", phone="6912345678")
        assert out.get("readback") and "6 9 1 2 3 4 5 6 7 8" in out["say"]


@pytest.mark.asyncio
async def test_cancel_finds_the_callers_appointment_and_reads_it_back(sessions):
    async with sessions() as db:
        practice, call = await setup(db)
        from app import booking
        day = datetime.fromisoformat(next_weekday()).date()
        await booking.book(db, practice, day=day, start_time="11:00", service_id="checkup",
                           customer_name="Ελένη Μ.", customer_phone="+306900000001", call_id=None,
                           now=datetime.now(ZoneInfo("Europe/Athens")))
        await db.commit()
        out = await say(db, call, "Θέλω να ακυρώσω το ραντεβού μου.", intent="cancel")
        assert out.get("readback") and out["say"].startswith("Βρήκα το ραντεβού σας")
        out = await say(db, call, "Ναι.", intent="none")
        assert out["say"].startswith("Το ραντεβού ακυρώθηκε")
        # Saying it again doesn't start a new cancellation (EVAL-005).
        out = await say(db, call, "Ναι, ακυρώστε το.", intent="cancel")
        assert out["say"].startswith("Το ραντεβού έχει ήδη ακυρωθεί")


@pytest.mark.asyncio
async def test_an_emergency_the_model_heard_is_routed_and_becomes_an_urgent_message(sessions):
    async with sessions() as db:
        _, call = await setup(db)
        out = await say(db, call, "Βοήθεια! Ο πατέρας μου έχει πόνο στο στή", intent="emergency")
        assert "112" in out["say"] and out["say"].endswith("Με ποιο όνομα να αφήσω το μήνυμα;")
        call = await db.get(Call, call.id)
        assert {"emergency", "urgent"} <= set(call.flags or [])
        out = await say(db, call, "Γιάννης Μακρής.", intent="none", name="Γιάννης Μακρής")
        assert out["say"].startswith("Το μήνυμα καταγράφηκε")
        from sqlalchemy import select
        m = (await db.execute(select(Message).where(Message.call_id == call.id))).scalar_one()
        assert m.urgent and "πατέρας" in m.reason


@pytest.mark.asyncio
async def test_the_demo_call_of_2026_11_05(sessions):
    """A web call (no caller number), replayed with the fixes: a vague yes to three times is
    asked which one, a short phone number is asked again, and «Όχι» at the end hangs up."""
    async with sessions() as db:
        _, call = await setup(db, caller=None)
        day = next_weekday()
        out = await say(db, call, "Θα ήθελα να μου πείτε τι υπηρεσίες προσφέρετε.", intent="question")
        assert out["reply"] == "model"
        out = await say(db, call, "Ναι, θα ήθελα να κλείσω ένα ραντεβού.", intent="book")
        assert out["say"].startswith("Για ποια υπηρεσία;")
        out = await say(db, call, "Θέλαμε για καθαρισμό.", intent="none", service="cleaning")
        assert out["say"] == "Ποια μέρα θα σας βόλευε;"
        out = await say(db, call, "Αύριο.", intent="none", day=day)
        assert "έχουμε ελεύθερα στις εννέα" in out["say"]
        # The model chose 09:00 for the caller.
        out = await say(db, call, "Μας βολεύει κάποια, ναι ή μια άλλη.", intent="none", time="09:00")
        assert out["say"].startswith("Ποια ώρα σας βολεύει, στις εννέα")
        out = await say(db, call, "Την πρώτη.", intent="none", time="09:00")
        assert out["say"] == "Ποιο είναι το ονοματεπώνυμό σας;"
        out = await say(db, call, "Μαξ Παπαμάκης.", intent="none", name="Μαξ Παπαμάκης")
        assert out["say"] == "Σε ποιο τηλέφωνο μπορούμε να σας βρούμε;"
        # The model wrote 695050 for six digits.
        out = await say(db, call, "Εξήντα πέντε πενήντα πενήντα.", intent="none", phone="695050")
        assert out["say"].startswith("Δεν το έπιασα ολόκληρο.")
        out = await say(db, call, "Εξήντα εννιά σαράντα τέσσερα, εκατόν είκοσι τρία, τετρακόσια πενήντα έξι.",
                        intent="none", phone="6944123456")
        assert out.get("readback") and "Μαξ Παπαμάκης" in out["say"]
        out = await say(db, call, "Μάλιστα.", intent="none")
        assert out["say"].startswith("Έγινε, το ραντεβού σας κλείστηκε")
        appt = await db.get(Appointment, (await db.get(Call, call.id)).appointment_id)
        assert appt.customer_phone == "+306944123456" and appt.service_id == "cleaning"
        assert appt.starts_at.astimezone(ZoneInfo("Europe/Athens")).strftime("%H:%M") == "09:00"
        out = await say(db, call, "Όχι, ευχαριστώ.", intent="none")
        assert out.get("end_call")


@pytest.mark.asyncio
async def test_yes_to_a_single_offered_time_takes_it(sessions):
    async with sessions() as db:
        _, call = await setup(db)
        day = next_weekday()
        await say(db, call, "Ραντεβού για έλεγχο.", intent="book", service="checkup", day=day)
        call = await db.get(Call, call.id)
        call.flow = {**call.flow, "offer": {"date": day, "free_times": ["11:00"]}}
        await db.commit()
        out = await say(db, call, "Ναι.", intent="none")
        assert out["say"] == "Ποιο είναι το ονοματεπώνυμό σας;"
        assert (await db.get(Call, call.id)).flow["time"] == "11:00"


def test_phone_numbers_are_read_from_the_callers_words():
    assert flow.spoken_phone("Έξι μία έξι μία έξι εννιά έξι εννιά έξι εννιά.") == "6161696969"
    assert flow.spoken_phone("Εξήντα πέντε πενήντα πενήντα.") == "655050"
    assert flow.spoken_phone("εξήντα εννιά, τριακόσια είκοσι πέντε, δεκαπέντε, μηδέν οχτώ") == "6932515" + "08"
    assert flow.spoken_phone("Το κινητό μου είναι 6944 123 456") == "6944123456"
    assert flow.spoken_phone("διακόσια δέκα, εξακόσια") == "210600"
    assert flow.valid_phone("6944123456", "el") and flow.valid_phone("2101234567", "el")
    assert flow.valid_phone("306944123456", "el")
    assert not flow.valid_phone("655050", "el") and not flow.valid_phone("694412345", "el")
    assert flow.valid_phone("4155550100", "en")


def test_a_time_counts_only_when_the_caller_named_or_picked_one():
    for text in ("Στις δέκα.", "Την πρώτη.", "Η δεύτερη ώρα.", "Στη μία και μισή.", "Όποια θέλετε.",
                 "Στις 10:30", "Εννιά και τέταρτο", "Ό,τι πιο νωρίς", "The first one", "At one"):
        assert flow.names_a_time(text), text
    for text in ("Μας βολεύει κάποια, ναι ή μια άλλη.", "Ναι.", "Ναι, την Τρίτη.", "Τη Δευτέρα.",
                 "Καλά.", "Yes please", "One of them"):
        assert not flow.names_a_time(text), text
