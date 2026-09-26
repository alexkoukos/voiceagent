from types import SimpleNamespace

import pytest

import agent as worker


def test_name_from_recent_transcript():
    assert worker.name_from_transcript("Αντρέας Αντετοκούμπο") == "Αντρέας Αντετοκούμπο"
    assert worker.name_from_transcript("Όχι! Λένε Γιάννη Αντετοκούμπο.") == "Γιάννη Αντετοκούμπο"
    assert worker.name_from_transcript("Ναι σωστά") is None
    assert worker.name_from_transcript("Στις πέντε και τέταρτο") is None
    assert worker.name_from_transcript("Εντάξει πάμε") is None


@pytest.mark.asyncio
async def test_readback_and_booking_use_the_transcribed_name():
    calls = []

    class Call:
        _last_user_text = "Αντρέας Αντετοκούμπο"
        _prepared_name = None

        async def tool(self, name, args):
            calls.append((name, args))
            return {"confirmation_id": "confirmation", "say": "Να επιβεβαιώσω;"}

        def read_back(self, result, *, customer_name=None):
            self._prepared_name = customer_name

    rc = Call()

    async def book_tool(name, args):
        calls.append((name, args))
        return '{}'

    receiver = SimpleNamespace(_rc=rc, _tool=book_tool)
    await worker.ReceptionistAgent.prepare_action.__wrapped__(
        receiver, action="book", date="2026-09-28", time="17:15", service_id="first_visit",
        customer_name="Ανδρέας Σταθόπουλος",
    )
    assert calls[0][1]["customer_name"] == "Αντρέας Αντετοκούμπο"
    assert rc._prepared_name == "Αντρέας Αντετοκούμπο"

    await worker.ReceptionistAgent.book_appointment.__wrapped__(
        receiver, date="2026-09-28", time="17:15", service_id="first_visit",
        customer_name="Ανδρέας Σταθόπουλος",
    )
    assert calls[1][1]["customer_name"] == "Αντρέας Αντετοκούμπο"

    rc._last_user_text = "Όχι! Λένε Γιάννη Αντετοκούμπο."
    await worker.ReceptionistAgent.prepare_action.__wrapped__(
        receiver, action="book", date="2026-09-28", time="17:15", service_id="first_visit",
        customer_name="Γιάννης Σταθόπουλος",
    )
    assert calls[2][1]["customer_name"] == "Γιάννη Αντετοκούμπο"


@pytest.mark.asyncio
async def test_web_pipeline_greeting_waits_for_playout():
    played = []

    class Handle:
        async def wait_for_playout(self):
            played.append("finished")

    class Session:
        def say(self, text, *, add_to_chat_ctx):
            played.append(text)
            return Handle()

    greeting = "Οδοντιατρείο Παπαδοπούλου. For English, say English."
    receiver = SimpleNamespace(
        _rc=SimpleNamespace(metadata={"greeting": greeting}, engine="pipeline"),
        session=Session(), language="el",
    )
    await worker.ReceptionistAgent.greet(receiver, wait_for_playout=True)
    assert played == [greeting, "finished"]
