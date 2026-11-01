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
    db.add(TranscriptEntry(call_id=call.id, role=TranscriptRole.friend, text=text))
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
