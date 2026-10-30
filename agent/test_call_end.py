import asyncio
from types import SimpleNamespace

import pytest

import agent as worker


class Playout:
    def __init__(self):
        self.done = asyncio.Event()

    async def wait_for_playout(self):
        await self.done.wait()


class Context:
    def __init__(self, calls):
        self.calls = calls

    def disallow_interruptions(self):
        pass

    async def wait_for_playout(self):
        self.calls.append("previous speech finished")


def room(calls):
    class Room:
        async def disconnect(self):
            calls.append("disconnected")
    return SimpleNamespace(room=Room())


@pytest.mark.asyncio
async def test_hang_up_says_the_prepared_goodbye_word_for_word(monkeypatch):
    played = Playout()
    calls = []

    async def say_fixed(text, *, allow_interruptions):
        calls.append((text, allow_interruptions))
        return played

    monkeypatch.setattr(worker, "get_job_context", lambda: room(calls))
    receiver = SimpleNamespace(_rc=SimpleNamespace(engine="pipeline", say_fixed=say_fixed), language="en",
                               _call_id="call", _spoken_already=lambda note: note)
    result = await worker.ReceptionistAgent.hang_up.__wrapped__(receiver, Context(calls))
    assert result.startswith("The call is ending")
    assert calls == ["previous speech finished", ("Thank you for calling. Goodbye.", False)]
    played.done.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls[-1] == "disconnected"


@pytest.mark.asyncio
async def test_hang_up_on_realtime_asks_the_model_for_the_goodbye(monkeypatch):
    played = Playout()
    calls = []

    class Session:
        def generate_reply(self, *, instructions, allow_interruptions):
            calls.append((instructions, allow_interruptions))
            return played

    monkeypatch.setattr(worker, "get_job_context", lambda: room(calls))
    receiver = SimpleNamespace(_rc=SimpleNamespace(engine="realtime"), session=Session(), language="en",
                               _call_id="call", _spoken_already=lambda note: note)
    await worker.ReceptionistAgent.hang_up.__wrapped__(receiver, Context(calls))
    assert calls[-1] == ("Say only this phrase: Thank you for calling. Goodbye.", False)


def test_agent_listens_only_to_the_caller_in_a_handoff_room():
    options = worker.room_options(caller_identity="customer-123")
    assert options.participant_identity == "customer-123"
