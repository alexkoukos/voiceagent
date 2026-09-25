import asyncio
from types import SimpleNamespace

import pytest

import agent as worker


class Playout:
    def __init__(self):
        self.done = asyncio.Event()

    async def wait_for_playout(self):
        await self.done.wait()


@pytest.mark.asyncio
async def test_hang_up_waits_for_spoken_goodbye(monkeypatch):
    played = Playout()
    calls = []

    class Session:
        def say(self, text, *, allow_interruptions):
            calls.append((text, allow_interruptions))
            return played

    class Context:
        def disallow_interruptions(self):
            pass

        async def wait_for_playout(self):
            calls.append("previous speech finished")

    class Room:
        async def disconnect(self):
            calls.append("disconnected")

    monkeypatch.setattr(worker, "get_job_context", lambda: SimpleNamespace(room=Room()))
    receiver = SimpleNamespace(session=Session(), _fillers=object(), language="en", _call_id="call")
    result = await worker.PrankCallerAgent.hang_up.__wrapped__(receiver, Context())
    assert result.startswith("The call is ending")
    assert calls == ["previous speech finished", ("Thank you for calling. Goodbye.", False)]
    played.done.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls[-1] == "disconnected"


def test_agent_listens_only_to_the_caller_in_a_handoff_room():
    options = worker.room_options(caller_identity="customer-123")
    assert options.participant_identity == "customer-123"
