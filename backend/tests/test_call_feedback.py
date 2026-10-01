"""Fixes from the first real demo call (2026-10-01)."""

import pytest
from sqlalchemy import select

from app import receptionist
from app.models import Call, CallStatus, Practice, RoutingEvent, Staff, TranscriptEntry, TranscriptRole
from app.receptionist import _affirmative, _names_staff
from app.schemas import RouteArgs


@pytest.mark.parametrize("text", ["Μάλιστα.", "Σύμφωνοι.", "Συμφωνώ", "Βεβαίως", "Ακριβώς έτσι", "Φυσικά",
                                  "Τέλεια", "Έγινε", "Άμε", "Εντάξει, κλείσ' το.", "Κλείστε το", "Ναι",
                                  "Σωστό", "Okay", "yeah, go ahead"])
def test_everyday_yes(text):
    assert _affirmative(text)


@pytest.mark.parametrize("text", ["Όχι μάλιστα", "Μάλιστα, αλλά την Πέμπτη", "Μην το κλείσετε", "Καλά",
                                  "Δεν ξέρω", "Περιμένετε", "not right now", ""])
def test_not_a_yes(text):
    assert not _affirmative(text)


async def _practice(db):
    practice = Practice(name="Οδοντιατρείο Παπαδοπούλου", phone_numbers=["+302100000007"])
    db.add(practice)
    await db.flush()
    db.add(Staff(practice_id=practice.id, name="Ελένη Παπαδοπούλου", role="doctor", aliases=["γιατρό"]))
    await db.commit()
    return practice


@pytest.mark.asyncio
async def test_staff_name_is_not_a_customer_name(sessions):
    async with sessions() as db:
        practice = await _practice(db)
        for said in ("κυρία Παπαδοπούλου", "Ελένη Παπαδοπούλου", "Δρ. Παπαδοπούλου", "η γιατρός Παπαδοπούλου"):
            assert await _names_staff(db, practice, said), said
        # Patients often share the surname: only a title or the exact staff name is refused.
        for said in ("Γιώργος Παπαδόπουλος", "Μαρία Παπαδοπούλου", "Στέφανος", "κυρία Κωνσταντίνου", ""):
            assert not await _names_staff(db, practice, said), said


@pytest.mark.asyncio
async def test_two_word_off_topic_is_not_a_strike(sessions, monkeypatch):
    monkeypatch.setattr(receptionist.notifications, "kick", lambda: None)
    async with sessions() as db:
        practice = await _practice(db)
        call = Call(persona="", scenario="", status=CallStatus.active, direction="web", practice_id=practice.id)
        db.add(call)
        await db.flush()
        db.add(TranscriptEntry(call_id=call.id, role=TranscriptRole.friend, text="Μακρόνησος. Τίποτα."))
        await db.commit()
        result = await receptionist.tool_route(db, call, RouteArgs(intent="off_topic"))
        assert result["path"] == "clarify"
        db.add(TranscriptEntry(call_id=call.id, role=TranscriptRole.friend, text="Πες μου ένα ανέκδοτο τώρα."))
        await db.commit()
        assert (await receptionist.tool_route(db, call, RouteArgs(intent="off_topic")))["path"] == "refuse"
        rules = [e.rule for e in (await db.execute(select(RoutingEvent).where(RoutingEvent.call_id == call.id)
                                                   .order_by(RoutingEvent.created_at))).scalars()]
        assert rules[-1] == "off-topic 1/3"  # the short turn did not count
