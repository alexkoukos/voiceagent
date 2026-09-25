"""LiveKit worker for AI Caller.

Registered as agent "prank-caller". It runs two kinds of calls:
- Receptionist (2.0): an inbound phone call (dispatched by LiveKit's SIP dispatch rule,
  with no metadata; the backend finds the practice by the dialed number) or a web demo
  (dispatched by the backend with mode "receptionist"). The agent greets first and books
  through backend tools (check_availability, book_appointment).
- Outbound (Level 1): the backend dispatches a job per call
(see backend/app/livekit_dispatch.py) with job.metadata carrying the merged
master + per-call prompt, the friend's number, voice, language and the hard
duration cap. This worker dials out over the Telnyx SIP trunk, runs the
conversation, records it to S3-compatible storage, reports transcript/status
back to the backend, and hangs up via its own tool or the duration cap.

Three engines (AGENT_ENGINE):
- "pipeline" (default): ElevenLabs Scribe realtime -> Gemini Flash-Lite ->
  ElevenLabs voice, with multilingual turn detection, preemptive generation,
  filler words when a reply is slow, and an opening line prepared during the ring.
- "realtime": Gemini Live speech-to-speech (the original engine; fallback).
- "openai": OpenAI Realtime speech-to-speech (gpt-realtime-2.1). Needs OPENAI_API_KEY;
  without it the call uses "realtime".
"""

import asyncio
import json
import logging
import os

import httpx
from google.genai import types as genai_types
from google.protobuf.duration_pb2 import Duration
from livekit import api
from livekit.agents import inference, room_io
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    StopResponse,
    WorkerOptions,
    cli,
    function_tool,
    get_job_context,
)
from livekit.plugins import elevenlabs, google, noise_cancellation, silero
from livekit.agents.voice.turn import TurnHandlingOptions

from fillers import FillerPicker
from opening import prepare_opening, ready_opening
from voices import elevenlabs_voice, gemini_voice, openai_voice

logger = logging.getLogger("prank-caller")

BACKEND_URL = os.environ.get("BACKEND_PUBLIC_URL", "http://localhost:8000")
AGENT_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "")
# How long the friend's phone rings before we give up (the library default is 30s).
RINGING_TIMEOUT_SECONDS = int(os.environ.get("RINGING_TIMEOUT_SECONDS", "45"))
ENGINE = os.environ.get("AGENT_ENGINE", "pipeline")
if ENGINE == "openai":
    # Plugins must register at import time on the main process; import it only when used,
    # since every worker process pays for what's imported and memory is tight on Railway.
    from livekit.plugins import openai
    from openai.types.beta.realtime.session import InputAudioTranscription, TurnDetection
# Realtime engine model.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-live")
# OpenAI engine model; "gpt-realtime-2.1-mini" costs about a third.
OPENAI_REALTIME_MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1")
# Pipeline engine: the "brain". Flash-Lite starts answering in ~0.45 s.
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-3.5-flash-lite")
# Realtime and OpenAI engines: how long a pause means the friend has finished talking.
REALTIME_SILENCE_MS = int(os.environ.get("REALTIME_SILENCE_MS", "500"))
# Say a filler word if the reply hasn't started this long after the friend stops talking.
FILLER_DELAY_SECONDS = 0.5
# If the callee stays silent after answering, open the conversation after this long.
GREETING_WAIT_SECONDS = 4
# Noise filter on the friend's audio before any model hears it; "off" to compare recognition without it.
NOISE_CANCELLATION = os.environ.get("NOISE_CANCELLATION", "on") != "off"
# If the callee spoke but no reply has started this long after, open the conversation anyway.
OPENING_FALLBACK_SECONDS = 5


async def backend_post(path: str, payload: dict, timeout: float = 10) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{BACKEND_URL}{path}", json=payload, headers={"x-agent-token": AGENT_TOKEN})
        r.raise_for_status()
        return r.json()


async def report(call_id: str, _retries: int = 0, **event) -> None:
    """Posts a call event. The final record is sent with retries and backoff."""
    for attempt in range(_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.post(
                    f"{BACKEND_URL}/internal/calls/{call_id}/events",
                    json=event,
                    headers={"x-agent-token": AGENT_TOKEN},
                )
                r.raise_for_status()
                return
        except Exception:
            if attempt == _retries:
                logger.exception("failed to report event %s for call %s", list(event), call_id)
            else:
                await asyncio.sleep(min(2 ** attempt, 10))


# S3-compatible recording storage, named like the variables a Railway bucket exposes.
STORAGE_VARS = ("AWS_ENDPOINT_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_S3_BUCKET_NAME")


def storage_configured() -> bool:
    return all(os.environ.get(v) for v in STORAGE_VARS)


async def start_recording(lk: api.LiveKitAPI, room_name: str, call_id: str) -> str:
    key, _ = await start_recording_with_id(lk, room_name, call_id)
    return key


async def start_recording_with_id(lk: api.LiveKitAPI, room_name: str, call_id: str) -> tuple[str, str]:
    key = f"recordings/{call_id}.mp4"
    info = await lk.egress.start_room_composite_egress(
        api.RoomCompositeEgressRequest(
            room_name=room_name,
            audio_only=True,
            file_outputs=[
                api.EncodedFileOutput(
                    file_type=api.EncodedFileType.MP4,
                    filepath=key,
                    s3=api.S3Upload(
                        access_key=os.environ["AWS_ACCESS_KEY_ID"],
                        secret=os.environ["AWS_SECRET_ACCESS_KEY"],
                        bucket=os.environ["AWS_S3_BUCKET_NAME"],
                        endpoint=os.environ["AWS_ENDPOINT_URL"],
                        region=os.environ.get("AWS_DEFAULT_REGION", "auto"),
                        force_path_style=os.environ.get("AWS_S3_URL_STYLE") == "path",
                    ),
                )
            ],
        )
    )
    return key, info.egress_id


def dial_failure_reason(e: Exception) -> str:
    """Map a failed dial to what the app shows: no_answer, declined, unreachable or error."""
    code = getattr(e, "sip_status_code", None)
    if code in (486, 600, 603):  # busy / declined
        return "declined"
    if code in (404, 410, 484, 604):  # number doesn't exist
        return "unreachable"
    if code in (408, 480, 487) or getattr(e, "status", None) == 408 or "timed out" in str(e):
        return "no_answer"
    return "error"


GREETING = {
    "el": "Χαιρέτα ευγενικά, όπως στο τηλέφωνο, και ξεκίνα.",
    "other": "Greet them politely, as on the phone, and start. Speak {language}.",
}
TIME_UP = {
    "el": "Ο χρόνος τελειώνει. Κλείσε σύντομα τη συζήτηση, αποχαιρέτα ευγενικά και κλείσε.",
    "other": "Time is almost up. Wrap up briefly, say goodbye politely and hang up.",
}
# Realtime engine: pin the call's language (calls are only Greek or English).
REALTIME_LANGUAGE = {"el": "el-GR", "en": "en-US"}


def _for_language(texts: dict[str, str], language: str, language_name: str) -> str:
    return texts["el"] if language == "el" else texts["other"].format(language=language_name)


class PrankCallerAgent(Agent):
    def __init__(
        self,
        *,
        instructions: str,
        call_id: str,
        language: str,
        language_name: str,
        opening_task: "asyncio.Task | None" = None,
        fillers: bool = False,
        **agent_kwargs,
    ) -> None:
        # agent_kwargs: per-agent model parts (llm/stt) and chat_ctx, for a language switch.
        super().__init__(instructions=instructions, **agent_kwargs)
        self._call_id = call_id
        # The call's language (Greek or English); it stays the same for the whole call.
        self.language = language
        self._language_name = language_name
        self._opening_task = opening_task
        self._opened = False
        self._fillers = FillerPicker() if fillers else None

    @function_tool
    async def delete_recording(self) -> str:
        """Deletes the recording and transcript of this call. Use it as soon as
        the friend asks for the recording to be deleted."""
        await report(self._call_id, delete_recording=True)
        return "recording and transcript will be deleted"

    @function_tool
    async def hang_up(self) -> str:
        """Ends the call. Use this once the call has done its job or the other
        person wants to stop — never leave a call open indefinitely."""
        job_ctx = get_job_context()
        await job_ctx.room.disconnect()
        return "call ended"

    async def _play_opening(self) -> bool:
        opening = await ready_opening(self._opening_task)
        if opening is None:
            return False
        self.session.say(opening.text, audio=opening.audio(), add_to_chat_ctx=True)
        return True

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        # The friend's first words ("Εμπρός;"): answer with the line prepared during the ring.
        if self._opened:
            return
        self._opened = True
        if await self._play_opening():
            raise StopResponse()

    async def open_after_silence(self) -> None:
        """The friend picked up but hasn't said anything: open the conversation ourselves."""
        if self._opened:
            return
        self._opened = True
        if not await self._play_opening():
            await self.session.generate_reply(
                instructions=_for_language(GREETING, self.language, self._language_name)
            )

    async def llm_node(self, chat_ctx, tools, model_settings):
        # Filler words: if the reply hasn't started after FILLER_DELAY_SECONDS, start it with
        # a short "Ε…" / "Κοίτα…" so the friend isn't left in silence. It's part of the reply
        # itself, so it always plays before the answer.
        stream = Agent.default.llm_node(self, chat_ctx, tools, model_settings)
        if self._fillers is None or not self._opened:
            async for chunk in stream:
                yield chunk
            return
        it = stream.__aiter__()
        first = asyncio.ensure_future(it.__anext__())
        try:
            done, _ = await asyncio.wait({first}, timeout=FILLER_DELAY_SECONDS)
            if not done:
                yield self._fillers.pick(self.language) + " "
            try:
                yield await first
            except StopAsyncIteration:
                return
            async for chunk in it:
                yield chunk
        finally:
            if not first.done():
                first.cancel()


class ReceptionistAgent(PrankCallerAgent):
    """Answers a practice's phone. Routing, dates, free times, bookings and messages all
    come from the backend; the model only passes on what the caller said."""

    def __init__(self, *, call: "ReceptionistCall", **kwargs) -> None:
        super().__init__(**kwargs)
        self._rc = call
        # The receptionist speaks first; the callee-speaks-first logic doesn't apply.
        self._opened = True

    async def _tool(self, name: str, args: dict | None = None) -> str:
        result = await self._rc.tool(name, args or {})
        return json.dumps(result, ensure_ascii=False)

    @function_tool
    async def route_call(self, intent: str, staff: str = "", department: str = "") -> str:
        """Call first, as soon as you know what the caller wants, and again if it changes.

        Args:
            intent: One of book, change, cancel, confirm, question, message, human, emergency, unclear.
            staff: Who they asked for, in their words ("με τον Γιώργο", "τον γιατρό"); empty if nobody.
            department: The department they named, if any.
        """
        # The call's language is fixed (Greek, or English for foreign numbers): no switching.
        return await self._tool("route_call", {
            "intent": intent, "staff": staff or None, "department": department or None,
        })

    @function_tool
    async def check_availability(self, when: str, service_id: str = "", staff: str = "", appointment_id: str = "") -> str:
        """Finds free appointment times. Call it before offering any time.

        Args:
            when: The day the caller asked for, in their own words, e.g. "την Τρίτη το
                απόγευμα", "αύριο", "next Monday morning". Never convert it to a date yourself.
            service_id: The id of the service from the list of services, if known.
            staff: Who they want it with, in their words; empty for anyone free.
            appointment_id: When moving an existing appointment, its id.
        """
        return await self._tool("check_availability", {
            "when": when, "service_id": service_id or None, "staff": staff or None,
            "appointment_id": appointment_id or None,
        })

    @function_tool
    async def book_appointment(
        self, date: str, time: str, service_id: str, customer_name: str, customer_phone: str = "",
        staff: str = "", name_uncertain: bool = False,
    ) -> str:
        """Books the appointment. Only after reading the details back and the caller said yes.

        Args:
            date: The date exactly as check_availability returned it (YYYY-MM-DD).
            time: One of the free_times check_availability returned (HH:MM).
            service_id: The service id.
            customer_name: The caller's full name.
            customer_phone: Their phone number if they gave a different one; empty to use caller ID.
            staff: Who it's with, as passed to check_availability; empty for anyone free.
            name_uncertain: True if you're not sure you got the surname right.
        """
        return await self._tool("book_appointment", {
            "date": date, "time": time, "service_id": service_id, "customer_name": customer_name,
            "customer_phone": customer_phone or None, "staff": staff or None, "name_uncertain": name_uncertain,
        })

    @function_tool
    async def find_appointments(self, phone: str = "") -> str:
        """Finds the caller's upcoming appointments, before changing, cancelling or confirming one.

        Args:
            phone: The number they booked with, if different from the one they're calling from.
        """
        return await self._tool("find_appointments", {"phone": phone or None})

    @function_tool
    async def reschedule_appointment(self, appointment_id: str, date: str, time: str) -> str:
        """Moves an appointment to a new free time, after reading it back and a clear yes.

        Args:
            appointment_id: From find_appointments.
            date: The new date as check_availability returned it (YYYY-MM-DD).
            time: One of the free_times (HH:MM).
        """
        return await self._tool("reschedule_appointment", {"appointment_id": appointment_id, "date": date, "time": time})

    @function_tool
    async def cancel_appointment(self, appointment_id: str) -> str:
        """Cancels an appointment, after the caller confirmed which one and said yes.

        Args:
            appointment_id: From find_appointments.
        """
        return await self._tool("cancel_appointment", {"appointment_id": appointment_id})

    @function_tool
    async def confirm_appointment(self, appointment_id: str) -> str:
        """Marks an appointment as confirmed by the caller (e.g. on a reminder call).

        Args:
            appointment_id: From find_appointments or the reminder details.
        """
        return await self._tool("confirm_appointment", {"appointment_id": appointment_id})

    @function_tool
    async def take_message(
        self, caller_name: str, reason: str, callback_number: str = "", best_time: str = "",
        urgent: bool = False, for_whom: str = "",
    ) -> str:
        """Saves a message for the business; they call the caller back.

        Args:
            caller_name: The caller's name.
            reason: What it's about, in a few plain, non-medical words.
            callback_number: Number to call back; empty to use the number they're calling from.
            best_time: When they'd like to be called back.
            urgent: True if it's urgent.
            for_whom: Who the message is for, if anyone in particular.
        """
        return await self._tool("take_message", {
            "caller_name": caller_name, "reason": reason, "callback_number": callback_number or None,
            "best_time": best_time, "urgent": urgent, "for_whom": for_whom or None,
        })

    @function_tool
    async def transfer_to_human(self, target: str = "") -> str:
        """Connects the caller to a person. Only when route_call said handoff.

        Args:
            target: Who they want, in their words; empty for whoever is available.
        """
        return json.dumps(await self._rc.start_handoff(target), ensure_ascii=False)

    @function_tool
    async def add_to_waitlist(self, when: str, service_id: str, customer_name: str) -> str:
        """Puts the caller on the waitlist when no time suits them; they're called if a slot frees up.

        Args:
            when: From which day, in the caller's words.
            service_id: The service id.
            customer_name: The caller's name.
        """
        return await self._tool("add_to_waitlist", {"when": when, "service_id": service_id, "customer_name": customer_name})

    @function_tool
    async def stop_recording(self) -> str:
        """Stops recording the call, when the caller doesn't want to be recorded. The call goes on."""
        await self._rc.stop_recording()
        return "recording stopped"

    async def greet(self) -> None:
        instruction = self._rc.metadata.get("greeting_instruction")
        if instruction:
            await self.session.generate_reply(instructions=instruction)
        elif self._rc.engine == "pipeline":
            self.session.say(self._rc.metadata["greeting"], add_to_chat_ctx=True)
        else:
            quote = "Πες ακριβώς αυτό" if self.language == "el" else "Say exactly this"
            await self.session.generate_reply(instructions=f"{quote}: {self._rc.metadata['greeting']}")


def prewarm(proc: JobProcess) -> None:
    # Loaded once per worker process, not per call; only the pipeline engine needs it.
    if "pipeline" in (ENGINE, RECEPTIONIST_ENGINE):
        proc.userdata["vad"] = silero.VAD.load()


def build_session(ctx: JobContext, engine: str, voice: str, language: str, vocabulary: list[str] | None = None) -> AgentSession:
    if engine == "pipeline":
        stt = scribe_stt(language, vocab_terms(language, vocabulary))
        tts = elevenlabs.TTS(voice_id=elevenlabs_voice(voice), model="eleven_flash_v2_5")
        # Open the connections now, while the phone rings, not on the first reply.
        for part in (stt, tts):
            try:
                part.prewarm()
            except Exception:
                logger.warning("could not prewarm %s", type(part).__name__)
        return AgentSession(
            stt=stt,
            llm=google.LLM(
                model=LLM_MODEL,
                api_key=os.environ.get("GEMINI_API_KEY"),
                temperature=0.9,
                thinking_config=genai_types.ThinkingConfig(thinking_level="minimal"),
            ),
            tts=tts,
            vad=ctx.proc.userdata.get("vad") or silero.VAD.load(),
            turn_handling=TurnHandlingOptions(
                # Understands when someone has finished a sentence, in any language,
                # instead of waiting for a fixed silence. Runs on LiveKit Cloud, so the
                # worker doesn't load a local model (that process ran out of memory on Railway).
                # Outside LiveKit Cloud hosting this resolves to the local v1-mini model, which
                # doesn't know Greek, so Greek turns end on max_delay. version="v1" (cloud) got
                # the job process OOM-killed on Railway on 2026-09-25; don't retry it blindly.
                turn_detection=inference.TurnDetector(local_fallback=False),
                endpointing={"mode": "dynamic", "min_delay": 0.2, "max_delay": 1.5},
                # Start writing and voicing the reply before the friend has fully finished.
                preemptive_generation={"enabled": True, "preemptive_tts": True},
            ),
        )
    return AgentSession(**language_parts(engine, voice, language, vocabulary))


def scribe_stt(language: str, keyterms: list[str] | None = None):
    return elevenlabs.STT(
        keyterms=keyterms or None,
        model="scribe_v2_realtime",
        # Without a language hint Scribe hears Greek phone audio as Ukrainian (Cyrillic text).
        language_code=language,
        # ElevenLabs decides when a sentence is finished. The plugin's default ("manual")
        # waits for a commit the session never sends, so no final transcript ever arrived
        # and the agent stayed silent for the whole call.
        server_vad={"vad_silence_threshold_secs": 0.3},
    )


# Everyday Greek the transcriber should expect; the business adds its own names and services.
GREEK_VOCABULARY = [
    "ρε", "μωρέ", "κομπλέ", "γαμώτο", "άσ' το", "θα 'ρθω", "κάνα", "τίποτα", "εντάξει", "μπορείς",
    "απογευματάκι", "πρωινό", "ραντεβουδάκι", "ρε φίλε", "έλα", "λέγε", "άντε", "οκ", "ναι ρε",
]


def vocab_terms(language: str, vocabulary: list[str] | None) -> list[str]:
    """Everyday Greek plus the business's names, split into words: Scribe realtime rejects
    the whole session if any keyterm is over 20 characters."""
    terms = list(GREEK_VOCABULARY if language == "el" else [])
    for phrase in vocabulary or []:
        terms += [phrase] if len(phrase) <= 20 else phrase.split()
    return [t for t in dict.fromkeys(terms) if 2 <= len(t) <= 20][:100]


def language_parts(engine: str, voice: str, language: str, vocabulary: list[str] | None = None) -> dict:
    """The parts of a session that are pinned to one language. A receptionist call that
    switches language (R7) hands over to a new agent built with these."""
    words = vocab_terms(language, vocabulary)
    if engine == "pipeline":
        return {"stt": scribe_stt(language, words)}
    if engine == "openai":
        return {"llm": openai.realtime.RealtimeModel(
            model=OPENAI_REALTIME_MODEL,
            voice=openai_voice(voice),
            # The language hint only steers the transcript; the model hears the audio itself.
            input_audio_transcription=InputAudioTranscription(model="gpt-4o-transcribe", language=language),
            turn_detection=TurnDetection(
                type="server_vad", silence_duration_ms=REALTIME_SILENCE_MS, prefix_padding_ms=300,
            ),
            api_key=os.environ.get("OPENAI_API_KEY"),
        )}
    return {"llm": google.beta.realtime.RealtimeModel(
        model=GEMINI_MODEL,
        voice=gemini_voice(voice),
        language=REALTIME_LANGUAGE.get(language),
        # Transcribe only the call's language. Left on auto-detect, Greek came back as
        # Italian/Spanish fragments and swear words were rewritten ("Γαμώτο" -> "Σταματήστε").
        input_audio_transcription=genai_types.AudioTranscriptionConfig(
            language_codes=[REALTIME_LANGUAGE.get(language, "en-US")], custom_vocabulary=words or None,
        ),
        api_key=os.environ.get("GEMINI_API_KEY"),
        # Decide the friend has finished after a short pause, not Gemini's slower default.
        realtime_input_config=genai_types.RealtimeInputConfig(
            automatic_activity_detection=genai_types.AutomaticActivityDetection(
                end_of_speech_sensitivity=genai_types.EndSensitivity.END_SENSITIVITY_HIGH,
                silence_duration_ms=REALTIME_SILENCE_MS,
            ),
        ),
    )}


def log_latency(session: AgentSession, call_id: str) -> None:
    """One log line per reply: how long each stage took, to tune against real numbers."""
    last_user: dict = {}

    @session.on("conversation_item_added")
    def _on_item(ev) -> None:
        if getattr(ev.item, "type", None) != "message":  # e.g. agent handoffs
            return
        m = getattr(ev.item, "metrics", None) or {}
        if ev.item.role == "user":
            last_user.clear()
            last_user.update(m)
            return
        stages = {
            "end_of_turn": last_user.get("end_of_turn_delay"),
            "transcript": last_user.get("transcription_delay"),
            "llm": m.get("llm_node_ttft"),
            "tts": m.get("tts_node_ttfb"),
        }
        parts = ", ".join(f"{k} {v * 1000:.0f}ms" for k, v in stages.items() if v is not None)
        e2e = m.get("e2e_latency")
        # The realtime engine doesn't report these; the pipeline engine does.
        if e2e is not None or parts:
            logger.info("call %s latency: %s%s", call_id,
                        f"end of speech -> first audio {e2e * 1000:.0f}ms" if e2e is not None else "",
                        f" ({parts})" if parts else "")


# Receptionist calls listen with ElevenLabs Scribe pinned to the call's language: on casual
# Greek with slang and swearing it was near perfect where Gemini Live's own ear mixed in
# Italian/Spanish (tested 2026-09-25). "realtime" goes back to Gemini Live.
RECEPTIONIST_ENGINE = os.environ.get("RECEPTIONIST_ENGINE", "pipeline")


def pick_engine(call_id: str, receptionist: bool = False) -> str:
    engine = RECEPTIONIST_ENGINE if receptionist else ENGINE
    if engine == "pipeline" and not os.environ.get("ELEVEN_API_KEY"):
        logger.warning("call %s: ELEVEN_API_KEY missing, using the realtime engine", call_id)
        engine = "realtime"
    if engine == "openai" and not os.environ.get("OPENAI_API_KEY"):
        logger.warning("call %s: OPENAI_API_KEY missing, using the realtime engine", call_id)
        engine = "realtime"
    return engine


def track_transcript(session: AgentSession, call_id: str) -> None:
    @session.on("conversation_item_added")
    def _on_item(ev) -> None:
        if getattr(ev.item, "type", None) != "message":  # e.g. agent handoffs
            return
        role = "agent" if ev.item.role == "assistant" else "friend"
        text = ev.item.text_content
        if text:
            # What each side said, to judge recognition and language from the logs.
            logger.info("call %s %s: %s", call_id, role, text)
            asyncio.create_task(
                report(call_id, transcript_role=role, transcript_text=text)
            )


def room_options() -> room_io.RoomOptions:
    # Clean phone-line noise before transcription and turn detection hear it.
    return room_io.RoomOptions(
        audio_input=room_io.AudioInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony() if NOISE_CANCELLATION else None,
        ),
    )


class ReceptionistCall:
    """Everything about one receptionist call that outlives a single agent (a language
    switch replaces the agent): the room, recording, handoffs, emergencies, latency."""

    def __init__(self, ctx: JobContext, metadata: dict, engine: str) -> None:
        self.ctx = ctx
        self.metadata = metadata
        self.engine = engine
        self.call_id = metadata["call_id"]
        self.language = metadata.get("language", "el")
        self.session: AgentSession | None = None
        self.agent: ReceptionistAgent | None = None
        self.caller_identity: str | None = None
        self.egress_id: str | None = None
        self.recording_key: str | None = None
        self.flags: set[str] = set()
        self.latencies: list[float] = []
        self.handed_off = False
        self._emergency_said = False
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def tool(self, name: str, args: dict) -> dict:
        try:
            result = await backend_post(f"/internal/calls/{self.call_id}/tools/{name}", args)
        except Exception:
            logger.exception("call %s: tool %s failed", self.call_id, name)
            self.flags.add("tool_error")
            result = {"error": "tool_error"}
        logger.info("call %s tool %s %s -> %s", self.call_id, name, args, result)
        return result

    def make_agent(self, language: str, parts: dict | None = None) -> ReceptionistAgent:
        prompts = self.metadata.get("prompts") or {}
        return ReceptionistAgent(
            call=self, instructions=prompts.get(language) or self.metadata["prompt"], call_id=self.call_id,
            language=language, language_name="Greek" if language == "el" else "English",
            fillers=self.engine == "pipeline", **(parts or {}),
        )

    # --- emergency (R5): a deterministic phrase match on what the caller said ---

    def check_emergency(self, text: str) -> None:
        em = self.metadata.get("emergency") or {}
        if self._emergency_said or not em.get("enabled"):
            return
        plain = _plain(text)
        if any(_plain(p) in plain for p in em.get("phrases", [])):
            self._emergency_said = True
            script = em["script"]["el" if self.language == "el" else "en"]
            logger.warning("call %s: emergency phrase heard", self.call_id)
            self.spawn(self.tool("emergency", {}))
            self.session.interrupt()
            quote = "Πες αμέσως ακριβώς αυτό" if self.language == "el" else "Say exactly this right now"
            then = ("Μετά κράτα επείγον μήνυμα (take_message με urgent true)." if self.language == "el"
                    else "Then take an urgent message (take_message with urgent true).")
            self.session.generate_reply(instructions=f"{quote}: {script} {then}")

    # --- recording (G7) ---

    async def start_recording(self) -> None:
        if not self.metadata.get("record") or not storage_configured():
            return
        lk = api.LiveKitAPI()
        try:
            self.recording_key, self.egress_id = await start_recording_with_id(lk, self.ctx.room.name, self.call_id)
        except Exception:
            logger.exception("call %s: recording failed to start", self.call_id)
        finally:
            await lk.aclose()

    async def stop_recording(self) -> None:
        self.flags.add("recording_refused")
        if self.egress_id:
            lk = api.LiveKitAPI()
            try:
                await lk.egress.stop_egress(api.StopEgressRequest(egress_id=self.egress_id))
            except Exception:
                logger.exception("call %s: could not stop recording", self.call_id)
            finally:
                await lk.aclose()
        # The backend deletes whatever was uploaded.
        await report(self.call_id, flags=["recording_refused"])

    # --- handoff (R6, W1, C6, C7) ---

    async def start_handoff(self, target: str) -> dict:
        result = await self.tool("transfer_to_human", {"target": target or None})
        if result.get("error"):
            return result
        if result.get("mode") == "sip":
            self.spawn(self._sip_transfer(result))
            return {"status": "transferring", "say": "Tell the caller you're putting them through now."}
        self.spawn(self._wait_for_staff(result))
        return {"status": "waiting",
                "say": "Tell the caller you're trying to reach them and to stay on the line. Don't call more tools."}

    async def _sip_transfer(self, h: dict) -> None:
        await asyncio.sleep(2.5)  # let the "putting you through" line play
        lk = api.LiveKitAPI()
        try:
            await lk.sip.transfer_sip_participant(api.TransferSIPParticipantRequest(
                participant_identity=self.caller_identity, room_name=self.ctx.room.name,
                transfer_to=h["transfer_to"], play_dialtone=True,
            ))
            self.handed_off = True
            await self.tool("handoff_result", {"handoff_id": h["handoff_id"], "status": "transferred"})
        except Exception:
            logger.exception("call %s: SIP transfer failed", self.call_id)
            await self.tool("handoff_result", {"handoff_id": h["handoff_id"], "status": "failed"})
            self._nobody_came()
        finally:
            await lk.aclose()

    async def _wait_for_staff(self, h: dict) -> None:
        joined = asyncio.Event()

        def _on_join(p) -> None:
            if p.identity.startswith("staff-join-"):
                joined.set()

        self.ctx.room.on("participant_connected", _on_join)
        try:
            await asyncio.wait_for(joined.wait(), timeout=h.get("timeout_seconds", 20))
        except asyncio.TimeoutError:
            await self.tool("handoff_result", {"handoff_id": h["handoff_id"], "status": "unanswered"})
            self._nobody_came()
            return
        finally:
            self.ctx.room.off("participant_connected", _on_join)
        await self.tool("handoff_result", {"handoff_id": h["handoff_id"], "status": "joined"})
        self.handed_off = True
        # A person took over: the agent steps out and leaves them to talk. The room and its
        # recording go on until they hang up.
        self.session.interrupt()
        text = "Σας συνδέω τώρα." if self.language == "el" else "I'm connecting you now."
        await self.session.generate_reply(instructions=(
            f"Πες μόνο: {text}" if self.language == "el" else f"Say only: {text}"))
        await asyncio.sleep(2)
        await self.session.aclose()

    def _nobody_came(self) -> None:
        text = ("Κανείς δεν μπόρεσε να απαντήσει. Ζήτα συγγνώμη και κράτα επείγον μήνυμα (take_message με urgent "
                "true) για να τον καλέσουν πίσω." if self.language == "el" else
                "Nobody could pick up. Apologise and take an urgent message (take_message with urgent true) so "
                "they call back.")
        self.session.generate_reply(instructions=text)

    # --- latency (median end of caller speech -> agent speaking) ---

    def track_latency(self) -> None:
        last_end: list[float] = []

        @self.session.on("user_state_changed")
        def _user(ev) -> None:
            if ev.old_state == "speaking" and ev.new_state != "speaking":
                last_end[:] = [asyncio.get_event_loop().time()]

        @self.session.on("agent_state_changed")
        def _agent(ev) -> None:
            if ev.new_state == "speaking" and last_end:
                self.latencies.append(asyncio.get_event_loop().time() - last_end[0])
                last_end.clear()

    def latency_ms_median(self) -> int | None:
        if not self.latencies:
            return None
        xs = sorted(self.latencies)
        return int(xs[len(xs) // 2] * 1000)


def _plain(s: str) -> str:
    """Lowercase, no accents: matches the backend's phrase matching."""
    import unicodedata
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn").replace("ς", "σ")


async def say_busy_and_leave(ctx: JobContext, engine: str, busy: dict) -> None:
    """All lines busy (G8): one line, then hang up."""
    session = build_session(ctx, engine, busy.get("voice", "default"), busy.get("language", "el"))
    await session.start(agent=Agent(instructions="Say only the line you are given, then stop."), room=ctx.room)
    quote = "Πες ακριβώς αυτό" if busy.get("language") == "el" else "Say exactly this"
    await session.generate_reply(instructions=f"{quote}: {busy['busy_line']}")
    await asyncio.sleep(6)
    await ctx.room.disconnect()
    ctx.shutdown(reason="lines busy")


async def run_receptionist(ctx: JobContext, metadata: dict) -> None:
    """Inbound phone call (no metadata: ask the backend which practice was dialed), web
    demo, or an outbound reminder / waitlist call (metadata has dial_number)."""
    dialing = bool(metadata.get("dial_number"))
    caller = None
    if not dialing:
        caller = await ctx.wait_for_participant()
    if "call_id" not in metadata:
        dialed = caller.attributes.get("sip.trunkPhoneNumber", "")
        caller_number = caller.attributes.get("sip.phoneNumber") or None
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(f"{BACKEND_URL}/internal/inbound", headers={"x-agent-token": AGENT_TOKEN},
                                      json={"dialed_number": dialed, "caller_number": caller_number})
            if r.status_code == 429:
                await say_busy_and_leave(ctx, pick_engine("busy", receptionist=True), r.json())
                return
            r.raise_for_status()
            metadata = r.json()
        except Exception:
            logger.exception("inbound call to %s from %s: no practice, hanging up", dialed, caller_number)
            await ctx.room.disconnect()
            ctx.shutdown(reason="no practice")
            return

    rc = ReceptionistCall(ctx, metadata, pick_engine(metadata["call_id"], receptionist=True))
    call_id = rc.call_id
    max_duration_seconds = metadata.get("max_duration_seconds", 300)
    logger.info("call %s: receptionist (%s, %s), engine %s, language %s",
                call_id, metadata.get("practice_id"), metadata.get("direction"), rc.engine, rc.language)
    session = build_session(ctx, rc.engine, metadata.get("voice", "default"), rc.language, metadata.get("vocabulary"))
    rc.session = session

    if dialing:
        lk = api.LiveKitAPI()
        try:
            await lk.sip.create_sip_participant(api.CreateSIPParticipantRequest(
                sip_trunk_id=metadata.get("sip_trunk_id") or os.environ.get("SIP_TRUNK_ID", ""),
                sip_call_to=metadata["dial_number"], sip_number=metadata.get("outbound_number"),
                room_name=ctx.room.name, participant_identity=f"customer-{call_id}", participant_name="Customer",
                wait_until_answered=True, ringing_timeout=Duration(seconds=RINGING_TIMEOUT_SECONDS),
            ))
        except Exception as e:
            reason = dial_failure_reason(e)
            logger.warning("call %s: reminder not connected (%s): %s", call_id, reason, e)
            await report(call_id, status="failed", end_reason=reason, _retries=3)
            try:
                await lk.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
            except Exception:
                pass
            await lk.aclose()
            ctx.shutdown(reason=f"dial {reason}")
            return
        await lk.aclose()
        rc.caller_identity = f"customer-{call_id}"
    else:
        rc.caller_identity = caller.identity

    await rc.start_recording()
    await report(call_id, status="active")

    agent = rc.make_agent(rc.language)
    rc.agent = agent
    track_transcript(session, call_id)
    log_latency(session, call_id)
    rc.track_latency()

    @session.on("conversation_item_added")
    def _heard(ev) -> None:
        if getattr(ev.item, "type", None) == "message" and ev.item.role == "user" and ev.item.text_content:
            rc.check_emergency(ev.item.text_content)

    @session.on("user_input_transcribed")
    def _partial(ev) -> None:
        # The pipeline engine hears words before the turn ends: react to an emergency sooner.
        if getattr(ev, "is_final", False) and getattr(ev, "transcript", ""):
            rc.check_emergency(ev.transcript)

    async def _enforce_duration_cap() -> None:
        warn_at = max(max_duration_seconds - 25, max_duration_seconds * 0.75)
        await asyncio.sleep(warn_at)
        if not rc.handed_off:
            session.generate_reply(instructions=_for_language(TIME_UP, rc.language, "English"))
        await asyncio.sleep(max_duration_seconds - warn_at)
        if rc.handed_off:
            return
        rc.flags.add("over_duration")
        logger.info("call %s: hard duration cap reached, disconnecting", call_id)
        await ctx.room.disconnect()

    cap_task = asyncio.create_task(_enforce_duration_cap())

    @session.on("close")
    def _on_close(_ev) -> None:
        ctx.shutdown(reason="session closed")

    async def _finish() -> None:
        cap_task.cancel()
        event = {"status": "completed", "flags": sorted(rc.flags & {"over_duration", "recording_refused", "tool_error"})}
        if rc.recording_key:
            # Sent even when the caller refused recording: the backend then deletes the partial file.
            event["recording_url"] = rc.recording_key
        if rc.latency_ms_median() is not None:
            event["latency_ms_median"] = rc.latency_ms_median()
        await report(call_id, _retries=5, **event)

    ctx.add_shutdown_callback(_finish)
    await session.start(agent=agent, room=ctx.room, room_options=room_options())
    if dialing:
        # Let the customer say "Εμπρός;" first (and hear a voicemail greeting before speaking).
        spoke = asyncio.Event()
        session.on("user_state_changed", lambda ev: spoke.set() if ev.new_state == "speaking" else None)
        try:
            await asyncio.wait_for(spoke.wait(), timeout=GREETING_WAIT_SECONDS)
            await asyncio.sleep(0.8)
        except asyncio.TimeoutError:
            pass
    await agent.greet()


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    metadata = json.loads(ctx.job.metadata or "{}")
    if metadata.get("mode") == "receptionist" or "call_id" not in metadata:
        await run_receptionist(ctx, metadata)
        return
    call_id = metadata["call_id"]
    friend_phone_number = metadata.get("friend_phone_number", "")
    prompt = metadata["prompt"]
    voice = metadata.get("voice", "default")
    language = metadata.get("language", "el")
    language_name = metadata.get("language_name", "Greek")
    max_duration_seconds = metadata.get("max_duration_seconds", 300)
    sip_trunk_id = metadata.get("sip_trunk_id") or os.environ.get("SIP_TRUNK_ID", "")
    outbound_number = metadata.get("outbound_number")
    # Test mode: skip the phone call and talk to whoever joins the room.
    test_no_dial = bool(metadata.get("test_no_dial"))

    engine = pick_engine(call_id)

    # Everything that doesn't need the friend happens while the phone rings:
    # connections open and the opening line gets written and voiced.
    session = build_session(ctx, engine, voice, language)
    opening_task = None
    if engine == "pipeline":
        opening_task = asyncio.create_task(prepare_opening(
            prompt=prompt, language=language, language_name=language_name,
            voice_id=elevenlabs_voice(voice), llm_model=LLM_MODEL,
        ))

    logger.info("call %s: engine %s, language %s, dialing %s via trunk %s",
                call_id, engine, language, friend_phone_number, sip_trunk_id)

    lk = api.LiveKitAPI()
    try:
        try:
            if test_no_dial:
                await ctx.wait_for_participant()
            else:
                await lk.sip.create_sip_participant(
                    api.CreateSIPParticipantRequest(
                        sip_trunk_id=sip_trunk_id,
                        sip_call_to=friend_phone_number,
                        sip_number=outbound_number,
                        room_name=ctx.room.name,
                        participant_identity=f"friend-{call_id}",
                        participant_name="Friend",
                        wait_until_answered=True,
                        ringing_timeout=Duration(seconds=RINGING_TIMEOUT_SECONDS),
                    )
                )
        except Exception as e:
            if opening_task:
                opening_task.cancel()
            reason = dial_failure_reason(e)
            if reason == "error":
                logger.exception("call %s: dial failed", call_id)
            else:
                # Expected outcomes of calling a phone, not bugs: one line, no traceback.
                logger.warning("call %s: not connected (%s): %s", call_id, reason, e)
            await report(call_id, status="failed", end_reason=reason)
            # Close the room so the job exits instead of idling until LiveKit times it out.
            try:
                await lk.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
            except Exception:
                logger.warning("call %s: could not delete room after failed dial", call_id)
            ctx.shutdown(reason=f"dial {reason}")
            return

        recording_key = None
        if test_no_dial:
            logger.info("call %s: test mode, not recording", call_id)
        elif not storage_configured():
            logger.warning("call %s: recording storage not configured, not recording", call_id)
        else:
            try:
                recording_key = await start_recording(lk, ctx.room.name, call_id)
            except Exception:
                logger.exception("call %s: recording failed to start", call_id)
    finally:
        await lk.aclose()

    await report(call_id, status="active")

    agent = PrankCallerAgent(
        instructions=prompt, call_id=call_id, language=language, language_name=language_name,
        opening_task=opening_task, fillers=engine == "pipeline",
    )

    track_transcript(session, call_id)
    log_latency(session, call_id)

    async def _enforce_duration_cap() -> None:
        # Warn the agent shortly before the cap so it wraps up and says goodbye
        # instead of the call being cut off mid-sentence.
        warn_at = max(max_duration_seconds - 25, max_duration_seconds * 0.75)
        await asyncio.sleep(warn_at)
        session.generate_reply(instructions=_for_language(TIME_UP, agent.language, language_name))
        await asyncio.sleep(max_duration_seconds - warn_at)
        logger.info("call %s: hard duration cap reached, disconnecting", call_id)
        await ctx.room.disconnect()

    @session.on("close")
    def _on_close(_ev) -> None:
        ctx.shutdown(reason="session closed")

    async def _finish() -> None:
        cap_task.cancel()
        if opening_task:
            opening_task.cancel()
        event = {"status": "completed"}
        if recording_key:
            event["recording_url"] = recording_key
        await report(call_id, **event)

    ctx.add_shutdown_callback(_finish)

    # Let the callee speak first ("Εμπρός;") like a real caller would. This also
    # lets the agent hear a voicemail greeting before it says anything.
    callee_spoke = asyncio.Event()

    @session.on("user_state_changed")
    def _on_user_state(ev) -> None:
        if ev.new_state == "speaking":
            callee_spoke.set()

    cap_task = asyncio.create_task(_enforce_duration_cap())
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_options(),
    )
    try:
        await asyncio.wait_for(callee_spoke.wait(), timeout=GREETING_WAIT_SECONDS)
    except asyncio.TimeoutError:
        await agent.open_after_silence()
        return
    # Safety net: if the friend's "Εμπρός;" never becomes a finished turn, open anyway
    # instead of leaving them in silence.
    await asyncio.sleep(OPENING_FALLBACK_SECONDS)
    await agent.open_after_silence()


if __name__ == "__main__":
    # AGENT_NAME lets a local test worker run without taking real calls from production.
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint, prewarm_fnc=prewarm,
        agent_name=os.environ.get("AGENT_NAME", "prank-caller"),
        # Each standby process holds its own copy of the models; keep few so the worker
        # fits in a small container (MAX_CONCURRENT_CALLS is 1 by default anyway).
        num_idle_processes=int(os.environ.get("NUM_IDLE_PROCESSES", "1")),
    ))
