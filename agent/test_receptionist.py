import json
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import agent as worker
from telemetry import CallTelemetry


@pytest.mark.asyncio
async def test_phone_handoff_waits_for_answer_and_falls_back_on_failure(monkeypatch):
    dial = AsyncMock()
    close_api = AsyncMock()
    monkeypatch.setattr(worker.api, "LiveKitAPI", lambda: SimpleNamespace(
        sip=SimpleNamespace(create_sip_participant=dial), aclose=close_api))
    monkeypatch.setattr(worker.asyncio, "sleep", AsyncMock())
    tool = AsyncMock()
    session = SimpleNamespace(interrupt=Mock(), aclose=AsyncMock())
    nobody_came = Mock()
    telemetry_events = []
    receiver = SimpleNamespace(metadata={"sip_trunk_id": "trunk", "outbound_number": "+302100000009"},
                               ctx=SimpleNamespace(room=SimpleNamespace(name="room")), call_id="call",
                               tool=tool, session=session, handed_off=False, _nobody_came=nobody_came,
                               telemetry=CallTelemetry("call", "pipeline", sink=telemetry_events.append))
    handoff = {"handoff_id": "handoff", "transfer_to": "tel:+306900000001",
               "timeout_seconds": 12, "transfer_failure": "collect_callback"}

    await worker.ReceptionistCall._sip_transfer(receiver, handoff)
    request = dial.await_args.args[0]
    assert request.wait_until_answered
    assert request.ringing_timeout.seconds == 12
    assert request.sip_call_to == "+306900000001"
    assert receiver.handed_off
    tool.assert_awaited_with("handoff_result", {"handoff_id": "handoff", "status": "joined"})
    session.aclose.assert_awaited_once()

    dial.reset_mock()
    tool.reset_mock()
    dial.side_effect = RuntimeError("no answer")
    receiver.handed_off = False
    await worker.ReceptionistCall._sip_transfer(receiver, handoff)
    assert not receiver.handed_off
    tool.assert_awaited_with("handoff_result", {"handoff_id": "handoff", "status": "failed"})
    nobody_came.assert_called_once_with("collect_callback")
    outcomes = [e for e in telemetry_events if e["event"] == "transfer_ended"]
    assert [e["outcome"] for e in outcomes] == ["joined", "failed"]
    assert all(e["duration_ms"] >= 0 and e["mode"] == "sip" for e in outcomes)
    assert all("transfer_to" not in e for e in telemetry_events)


@pytest.mark.asyncio
async def test_app_handoff_accepts_staff_already_in_room(monkeypatch):
    monkeypatch.setattr(worker.asyncio, "sleep", AsyncMock())
    listeners = {}
    room = SimpleNamespace(
        remote_participants={"caller": object(), "staff-join-early": object()},
        on=lambda event, callback: listeners.setdefault(event, callback),
        off=lambda event, callback: listeners.pop(event),
    )
    tool = AsyncMock()
    session = SimpleNamespace(interrupt=Mock(), generate_reply=AsyncMock(), aclose=AsyncMock())
    telemetry_events = []
    receiver = SimpleNamespace(ctx=SimpleNamespace(room=room), tool=tool, session=session,
                               language="el", handed_off=False,
                               telemetry=CallTelemetry("call", "pipeline", sink=telemetry_events.append))

    await worker.ReceptionistCall._wait_for_staff(receiver, {"handoff_id": "handoff", "timeout_seconds": 0.01})

    assert receiver.handed_off
    tool.assert_awaited_once_with("handoff_result", {"handoff_id": "handoff", "status": "joined"})
    assert listeners == {}
    assert telemetry_events[-1]["outcome"] == "joined"
    assert telemetry_events[-1]["mode"] == "app"


def test_deepgram_transcript_uses_a_bounded_practice_glossary(monkeypatch):
    calls = []
    monkeypatch.setattr(worker.inference, "STT", lambda *args, **kwargs: calls.append((args, kwargs)))

    worker.provider_stt.deepgram("el", [" Κομμωτήριο   Αθηνά ", "Γιώργος", "γιώργος", "Ανδρικό κούρεμα"]
                                 + [f"Υπηρεσία {i}" for i in range(60)])
    args, kwargs = calls[-1]
    assert args == ("deepgram/nova-3",)
    assert kwargs["language"] == "el"
    assert kwargs["extra_kwargs"]["keyterm"][:3] == [
        "Κομμωτήριο Αθηνά", "Γιώργος", "Ανδρικό κούρεμα"]
    assert len(kwargs["extra_kwargs"]["keyterm"]) == 40

    worker.provider_stt.deepgram("en")
    assert calls[-1][1] == {"language": "en"}


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
        _prepared_phone = None

        async def tool(self, name, args):
            calls.append((name, args))
            return {"confirmation_id": "confirmation", "say": "Να επιβεβαιώσω;"}

        def read_back(self, result, *, customer_name=None, customer_phone=None):
            self._prepared_name = customer_name
            self._prepared_phone = customer_phone

    rc = Call()

    async def book_tool(name, args):
        calls.append((name, args))
        return '{}'

    receiver = SimpleNamespace(_rc=rc, _tool=book_tool, _spoken_already=lambda note: None)
    assert await worker.ReceptionistAgent.prepare_action.__wrapped__(
        receiver, action="book", date="2026-09-28", time="17:15", service_id="first_visit",
        customer_name="Ανδρέας Σταθόπουλος",
    ) is None  # the read-back speaks; no second LLM reply
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
    session = Session()

    async def say_fixed(text, **kwargs):  # pre-voiced audio unavailable: streamed as before
        return session.say(text, **kwargs)

    receiver = SimpleNamespace(
        _rc=SimpleNamespace(metadata={"greeting": greeting}, engine="pipeline", say_fixed=say_fixed),
        session=session, language="el",
    )
    await worker.ReceptionistAgent.greet(receiver, wait_for_playout=True)
    assert played == [greeting, "finished"]


@pytest.mark.asyncio
async def test_first_availability_lookup_uses_the_recognized_caller_day():
    sent = []

    async def call_tool(name, payload):
        sent.append(payload)
        return {"error": "no_date"} if len(sent) == 1 else {"date": "2026-09-29"}

    rc = SimpleNamespace(
        metadata={"direction": "web"}, _last_user_text="Θέλω κούρεμα με τον Νίκο",
        _availability_checked=False, tool=call_tool, _user_turn=1, _spoken_offer=None,
    )
    receiver = SimpleNamespace(_rc=rc, _speak=lambda result: result)
    await worker.ReceptionistAgent.check_availability.__wrapped__(
        receiver, when="σήμερα", service_id="haircut", staff="Νίκος",
    )
    assert sent[0]["when"] == "Θέλω κούρεμα με τον Νίκο"
    assert rc._availability_checked is False

    rc._last_user_text = "Την άλλη Τρίτη το απόγευμα"
    await worker.ReceptionistAgent.check_availability.__wrapped__(
        receiver, when="σήμερα", service_id="haircut", staff="Νίκος",
    )
    assert sent[1]["when"] == "Την άλλη Τρίτη το απόγευμα"
    assert rc._availability_checked is True


@pytest.mark.parametrize("engine,quiet", [("pipeline", True), ("text_pipeline", True),
                                          ("realtime", False)])
def test_tools_that_already_spoke_ask_text_engines_for_no_reply(engine, quiet):
    # EVAL-001: a note made Gemini reply empty (retried, then an error) or say
    # "Συγγνώμη, δεν σας άκουσα" before the read-back.
    receiver = SimpleNamespace(_rc=SimpleNamespace(engine=engine))
    result = worker.ReceptionistAgent._spoken_already(receiver, "note")
    assert result is None if quiet else result == "note"


@pytest.mark.asyncio
async def test_handoff_path_starts_the_transfer_in_code():
    # EVAL-009: the model said "Μια στιγμή να σας συνδέσω" but never called transfer_to_human.
    started = []

    async def tool(name, args):
        return {"path": "handoff", "target": "Δρ. Νίκος", "next": "call transfer_to_human"}

    async def start_handoff(target):
        started.append(target)
        return {"status": "waiting", "say": "stay on the line"}

    receiver = SimpleNamespace(_rc=SimpleNamespace(tool=tool, start_handoff=start_handoff))
    result = await worker.ReceptionistAgent.route_call.__wrapped__(receiver, None, intent="human", staff="τον γιατρό")
    assert started == ["Δρ. Νίκος"]
    assert '"transfer"' in result and "do not call transfer_to_human" in result


@pytest.mark.asyncio
async def test_second_handoff_request_does_not_start_another_transfer():
    tool = AsyncMock(return_value={"handoff_id": "h", "mode": "app", "timeout_seconds": 3})
    receiver = SimpleNamespace(_handoff_pending=False, tool=tool, spawn=lambda coro: coro.close(),
                               _wait_for_staff=lambda h: worker.asyncio.sleep(0))
    first = await worker.ReceptionistCall.start_handoff(receiver, "γιατρό")
    second = await worker.ReceptionistCall.start_handoff(receiver, "γιατρό")
    assert first["status"] == "waiting" and second["status"] == "already_transferring"
    tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_language_switch_waits_for_the_agent_swap(monkeypatch):
    # EVAL-007: speaking before update_agent's swap finished hit the draining old agent.
    monkeypatch.setattr(worker, "language_parts", lambda *args: {})
    real_sleep = worker.asyncio.sleep
    swapped = []

    class Session:
        _update_activity_atask = None

        def update_agent(self, agent):
            async def swap():
                await real_sleep(0.05)
                swapped.append(agent)
            self._update_activity_atask = worker.asyncio.create_task(swap())

    rc = SimpleNamespace(language="el", session=Session(), metadata={}, engine="pipeline", call_id="call",
                         make_agent=lambda language, parts: f"agent-{language}")
    await worker.ReceptionistCall.switch_language(rc, "en")
    assert swapped == ["agent-en"] and rc.language == "en"


@pytest.mark.asyncio
async def test_parallel_backend_tools_run_in_call_order(monkeypatch):
    # EVAL-004: a parallel prepare_action overtook check_availability's saved offer.
    order = []

    async def backend_post(path, payload):
        name = path.rsplit("/", 1)[-1]
        order.append(("start", name))
        await worker.asyncio.sleep(0.05 if name == "check_availability" else 0)
        order.append(("end", name))
        return {}

    monkeypatch.setattr(worker, "backend_post", backend_post)
    rc = worker.ReceptionistCall(SimpleNamespace(), {"call_id": "call"}, "pipeline")
    await worker.asyncio.gather(rc.tool("check_availability", {}), rc.tool("prepare_action", {}))
    assert order == [("start", "check_availability"), ("end", "check_availability"),
                     ("start", "prepare_action"), ("end", "prepare_action")]


def test_failed_reply_asks_the_caller_to_repeat_once_per_turn():
    # EVAL-003/010: Gemini's empty or malformed completions left the caller in silence.
    from livekit.agents import llm
    handlers, said = {}, []
    session = SimpleNamespace(on=lambda event, cb: handlers.setdefault(event, cb),
                              say=lambda text, **kwargs: said.append(text))
    rc = worker.ReceptionistCall(SimpleNamespace(), {"call_id": "call"}, "pipeline")
    rc.session = session
    rc.watch_llm_errors()
    fatal = SimpleNamespace(error=llm.LLMError(timestamp=0, label="google.LLM", error=RuntimeError("empty"), recoverable=False))
    retrying = SimpleNamespace(error=llm.LLMError(timestamp=0, label="google.LLM", error=RuntimeError("empty"), recoverable=True))
    handlers["error"](retrying)
    handlers["error"](fatal)
    handlers["error"](fatal)  # same caller turn: no second apology
    rc.heard_user("Ελένη Γεωργίου")
    handlers["error"](fatal)
    assert said == ["Συγγνώμη, μπορείτε να το πείτε ξανά;"] * 2


def test_realtime_engine_does_not_add_a_recovery_line():
    handlers = {}
    rc = worker.ReceptionistCall(SimpleNamespace(), {"call_id": "call"}, "realtime")
    rc.session = SimpleNamespace(on=lambda event, cb: handlers.setdefault(event, cb))
    rc.watch_llm_errors()
    assert handlers == {}


@pytest.mark.asyncio
async def test_llm_node_reports_the_first_words_once(monkeypatch):
    seen = []

    async def model(*_args):
        await worker.asyncio.sleep(0.02)
        yield SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=["route_call"]))
        yield SimpleNamespace(delta=SimpleNamespace(content="Την Τρίτη έχω στις δέκα."))
        yield SimpleNamespace(delta=SimpleNamespace(content=" Σας βολεύει;"))

    monkeypatch.setattr(worker.Agent.default, "llm_node", model)
    receiver = SimpleNamespace(_rc=SimpleNamespace(awaiting_pin=False,
                                                   telemetry=SimpleNamespace(answer_started=seen.append)))
    chunks = [c async for c in worker.ReceptionistAgent.llm_node(receiver, None, None, None)]
    assert len(chunks) == 3
    assert len(seen) == 1 and seen[0] >= 15  # model time to the answer text, the tool call excluded


@pytest.mark.asyncio
async def test_emergency_line_is_spoken_word_for_word(monkeypatch):
    """The 166/112 line is said verbatim, not paraphrased by the model (audit P1-4)."""
    script = "Αυτό ακούγεται επείγον. Καλέστε αμέσως το 166 ή το 112."
    spoken = SimpleNamespace(wait_for_playout=AsyncMock())
    session = SimpleNamespace(interrupt=Mock(), say=Mock(return_value=spoken), generate_reply=Mock())
    ctx = SimpleNamespace(room=SimpleNamespace(name="room"))
    rc = worker.ReceptionistCall(ctx, {"call_id": "c", "language": "el", "emergency": {
        "enabled": True, "phrases": ["δεν αναπνέει"], "script": {"el": script, "en": "Call 112."}}}, "text_pipeline")
    rc.session = session
    rc.tool = AsyncMock(return_value={})
    rc.check_emergency("Βοήθεια, ο πατέρας μου δεν αναπνέει!")
    await asyncio.gather(*rc._tasks)
    session.say.assert_called_once()
    assert session.say.call_args.args[0] == script
    rc.tool.assert_awaited_once_with("emergency", {})
    assert "take_message" in session.generate_reply.call_args.kwargs["instructions"]
    assert script not in session.generate_reply.call_args.kwargs["instructions"]


@pytest.mark.parametrize("said,digits", [
    ("τέσσερα, πέντε, έξι, ένα, δύο, μηδέν", "456120"),
    ("Ο κωδικός είναι 4 5 6 1 2 0", "456120"),
    ("Εννιά οχτώ εφτά τρία τρεις μία", "987331"),
    ("four five six one two zero", "456120"),
    ("Oh, it's 4 5 6", "456"),
    ("Δεν τον έχω ακόμα", ""),
])
def test_spoken_digits(said, digits):
    assert worker.spoken_digits(said) == digits


@pytest.mark.asyncio
async def test_pin_is_taken_in_code_and_never_reaches_the_model():
    rc = SimpleNamespace(awaiting_pin=True, tool=AsyncMock(return_value={"ok": True}), engine="pipeline",
                         answer_readback=AsyncMock(return_value=False))
    agent = worker.ReceptionistAgent(call=rc, instructions="x", call_id="c", language="el")
    waiting = worker.llm.ChatMessage(role="user", content=["Ένα λεπτό να τον βρω"])
    await agent.on_user_turn_completed(None, waiting)
    assert rc.awaiting_pin and waiting.text_content == "Ένα λεπτό να τον βρω"
    rc.tool.assert_not_awaited()

    said = worker.llm.ChatMessage(role="user", content=["Είναι τέσσερα οκτώ δύο ένα"])
    preemptive = worker.llm.ChatContext([worker.llm.ChatMessage(role="user", content=["Είναι 4 8 2 1"])])
    worker.mask_pin(preemptive)
    assert worker.spoken_digits(preemptive.items[-1].text_content) == ""
    await agent.on_user_turn_completed(None, said)
    rc.tool.assert_awaited_once_with("admin_login", {"pin": "4821"})
    assert not rc.awaiting_pin
    assert worker.spoken_digits(said.text_content) == "" and "ok" in said.text_content


@pytest.mark.parametrize("engine,direct", [("pipeline", True), ("text_pipeline", True), ("realtime", False)])
def test_a_reply_the_backend_worded_is_spoken_without_a_second_model_round(engine, direct):
    said = []
    rc = SimpleNamespace(engine=engine, telemetry=None)
    receiver = SimpleNamespace(_rc=rc, session=SimpleNamespace(say=said.append))
    line = "Την Τρίτη 3 Νοεμβρίου έχουμε ελεύθερα στις εννέα ή στις εννέα και μισή το πρωί. Σας βολεύει κάποια;"
    out = worker.ReceptionistAgent._speak(receiver, {"date": "2026-11-03", "free_times": ["09:00", "09:30"], "say": line})
    if direct:
        assert said == [line]
        assert out.reply_required is False
        # The model keeps what was offered (for prepare_action) and knows it was said.
        assert json.loads(out.output) == {"date": "2026-11-03", "free_times": ["09:00", "09:30"], "already_said": line}
    else:
        assert said == [] and json.loads(out)["say"] == line
    # Without a worded line the model replies as before.
    assert worker.ReceptionistAgent._speak(receiver, {"error": "no_date"}) == '{"error": "no_date"}'


@pytest.mark.asyncio
async def test_a_plain_yes_to_the_readback_is_carried_out_without_the_model():
    calls, said, added, replies = [], [], [], []

    async def tool(name, args):
        calls.append((name, dict(args)))
        if not args.get("execute"):
            return {"yes": args["confirmation_text"].startswith("Ναι")}
        return {"booked": True, "say": "Έγινε, το ραντεβού σας κλείστηκε για την Τρίτη 3 Νοεμβρίου στις εννέα το πρωί."}

    rc = worker.ReceptionistCall(SimpleNamespace(), {"call_id": "call"}, "pipeline")
    rc.tool = tool
    rc.session = SimpleNamespace(say=said.append, _conversation_item_added=added.append,
                                 generate_reply=lambda **kw: replies.append(kw))
    agent = SimpleNamespace(_chat_ctx=SimpleNamespace(items=[]))

    # Not a readback answer: nothing happens here.
    assert await rc.answer_readback(agent, SimpleNamespace(text_content="Ναι.")) is False
    rc._confirmation_id, rc._confirmation_armed = "conf-1", True

    # Not a plain yes: the model answers, and the caller's line is left to the SDK.
    assert await rc.answer_readback(agent, SimpleNamespace(text_content="Όχι, την Τετάρτη.")) is False
    assert added == [] and said == []

    # A plain yes: the line is stored first, then the backend books and words the reply.
    yes = SimpleNamespace(text_content="Ναι, σωστά.")
    assert await rc.answer_readback(agent, yes) is True
    assert added == [yes] and agent._chat_ctx.items == [yes]
    assert calls[-1] == ("confirm_pending", {"confirmation_id": "conf-1", "confirmation_text": "Ναι, σωστά.", "execute": True})
    assert said == ["Έγινε, το ραντεβού σας κλείστηκε για την Τρίτη 3 Νοεμβρίου στις εννέα το πρωί."]
    assert rc._confirmation_id is None and replies == []


@pytest.mark.asyncio
async def test_a_refused_readback_answer_goes_back_to_the_model():
    replies, said = [], []

    async def tool(name, args):
        return {"yes": True} if not args.get("execute") else {"booked": False, "error": "slot_taken"}

    rc = worker.ReceptionistCall(SimpleNamespace(), {"call_id": "call"}, "pipeline")
    rc.tool = tool
    rc._confirmation_id, rc._confirmation_armed = "conf-1", True
    rc.session = SimpleNamespace(say=said.append, _conversation_item_added=lambda m: None,
                                 generate_reply=lambda **kw: replies.append(kw))
    assert await rc.answer_readback(SimpleNamespace(_chat_ctx=SimpleNamespace(items=[])),
                                    SimpleNamespace(text_content="Ναι.")) is True
    assert said == [] and "slot_taken" in replies[0]["instructions"]
    assert rc._confirmation_id == "conf-1"  # still pending: the model may offer another time


@pytest.mark.asyncio
async def test_the_same_offer_is_spoken_once_per_caller_turn():
    line = "Την Πέμπτη 19 Νοεμβρίου έχουμε ελεύθερα στις εννέα και μισή το πρωί. Σας βολεύει;"

    async def call_tool(name, payload):
        return {"date": "2026-11-19", "free_times": ["09:30"], "say": line}

    rc = SimpleNamespace(metadata={"direction": "inbound"}, _last_user_text="", _availability_checked=True,
                         tool=call_tool, _user_turn=3, _spoken_offer=None)
    receiver = SimpleNamespace(_rc=rc, _speak=lambda result: result)
    check = worker.ReceptionistAgent.check_availability.__wrapped__
    assert (await check(receiver, when="την Πέμπτη"))["say"] == line
    assert "say" not in await check(receiver, when="2026-11-19")  # looping in the same turn
    rc._user_turn = 4
    assert (await check(receiver, when="την Πέμπτη"))["say"] == line  # the caller asked again
