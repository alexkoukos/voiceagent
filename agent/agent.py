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

Four engines (AGENT_ENGINE):
- "pipeline" (default): ElevenLabs Scribe realtime -> Gemini Flash-Lite ->
  ElevenLabs voice, with multilingual turn detection, preemptive generation,
  filler words when a reply is slow, and an opening line prepared during the ring.
- "realtime": Gemini Live speech-to-speech (the original engine; fallback).
- "text_pipeline": Deepgram transcription -> Gemini text model -> Gemini speech.
  Receptionist fallback when ElevenLabs is unavailable, so the model receives
  the same words the caller sees in the web transcript.
- "openai": OpenAI Realtime speech-to-speech (gpt-realtime-2.1). Needs OPENAI_API_KEY;
  without it the call uses "realtime".
"""

import asyncio
import difflib
import json
import logging
import os
import re
import uuid
from collections.abc import Callable

import httpx
from google.genai import types as genai_types
from google.protobuf.duration_pb2 import Duration
from livekit import api, rtc
from livekit.agents import inference, room_io, stt as livekit_stt, tts as livekit_tts
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    StopResponse,
    WorkerOptions,
    RunContext,
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
GEMINI_TTS_MODEL = os.environ.get("GEMINI_TTS_MODEL", "gemini-3.8-flash-lite-tts")
GEMINI_TTS_FALLBACK_MODEL = os.environ.get("GEMINI_TTS_FALLBACK_MODEL", "gemini-3.8-flash-tts")
# Realtime and OpenAI engines: how long a pause means the friend has finished talking.
REALTIME_SILENCE_MS = int(os.environ.get("REALTIME_SILENCE_MS", "400"))
# Turn detector (pipeline engine): the longest we wait after the caller stops before
# treating the turn as finished. Lower is snappier at the risk of cutting slow speech.
TURN_MAX_DELAY_MS = int(os.environ.get("TURN_MAX_DELAY_MS", "1000"))
# Say a filler word if the reply hasn't started this long after the friend stops talking.
FILLER_DELAY_SECONDS = 0.5
# At most one filler within this many seconds.
FILLER_GAP_SECONDS = 4
# Pipeline engine: pause that ends a transcribed segment, and the least wait after the caller
# stops before replying. Lower is snappier but splits normal-speed speech into fragments.
SCRIBE_SILENCE_SECS = float(os.environ.get("SCRIBE_SILENCE_SECS", "0.5"))
ENDPOINT_MIN_DELAY = float(os.environ.get("ENDPOINT_MIN_DELAY", "0.5"))
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

# A caller who swears directly at the agent gets one calm, polite reminder. Directed insults
# only — casual fillers ("γαμώτο", "ρε", bare "damn") are left alone. Used as a fallback when
# the call's metadata carries no profanity block (prank/professional flow), matching the backend.
DEFAULT_PROFANITY_PHRASES = {
    "el": ["μαλακα", "καριολη", "γαμησου", "να γαμηθεις", "αρχιδι", "πουστη", "ηλιθιε", "βλακα",
           "χαζε", "κωλοπαιδο"],
    "en": ["fuck you", "asshole", "bastard", "idiot", "moron", "dickhead", "shut up", "bitch"],
}
DEFAULT_PROFANITY_SCRIPT = {
    "el": "Παρακαλώ, ας μιλήσουμε ευγενικά· δεν χρειάζονται βρισιές. Πώς μπορώ να σας βοηθήσω;",
    "en": "Please, let's keep things polite — there's no need to swear. How can I help you?",
}


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
        self._last_filler_at = 0.0

    @function_tool
    async def delete_recording(self) -> str:
        """Deletes the recording and transcript of this call. Use it as soon as
        the friend asks for the recording to be deleted."""
        await report(self._call_id, delete_recording=True)
        return "recording and transcript will be deleted"

    @function_tool
    async def hang_up(self, context: RunContext, silent: bool = False) -> str:
        """Says a final goodbye and ends the call after it has played. Use silent=true only for voicemail."""
        # current_speech.wait_for_playout() waits for the tool itself to finish and
        # raises inside a function tool. Wait for the speech before this tool instead.
        context.disallow_interruptions()
        try:
            await asyncio.wait_for(context.wait_for_playout(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("call %s: timed out waiting for speech before hang-up", self._call_id)
        if silent:
            await get_job_context().room.disconnect()
            return "call ended"
        closing = "Ευχαριστούμε που καλέσατε. Γεια σας." if self.language == "el" else "Thank you for calling. Goodbye."
        if self._fillers is not None:
            handle = self.session.say(closing, allow_interruptions=False)
        else:
            quote = "Πες μόνο αυτή τη φράση" if self.language == "el" else "Say only this phrase"
            handle = self.session.generate_reply(instructions=f"{quote}: {closing}", allow_interruptions=False)
        room = get_job_context().room

        async def finish() -> None:
            try:
                # The tool must return before the new speech can finish playing.
                await asyncio.wait_for(handle.wait_for_playout(), timeout=15)
            except Exception:
                logger.exception("call %s: goodbye playout failed", self._call_id)
            await room.disconnect()

        asyncio.create_task(finish())
        return "The call is ending. Do not speak again."

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
            # One filler per turn: a reply started early and then restarted must not add a second.
            now = asyncio.get_event_loop().time()
            if not done and now - self._last_filler_at > FILLER_GAP_SECONDS:
                self._last_filler_at = now
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
        payload = dict(args or {})
        if name in ("book_appointment", "reschedule_appointment", "cancel_appointment"):
            payload.update(self._rc.confirmation_for_write())
        result = await self._rc.tool(name, payload)
        if name in ("book_appointment", "reschedule_appointment", "cancel_appointment") and not result.get("error"):
            self._rc.clear_confirmation()
        return json.dumps(result, ensure_ascii=False)

    @function_tool
    async def route_call(self, context: RunContext, intent: str, staff: str = "", department: str = "") -> str:
        """Call first, as soon as you know what the caller wants, and again if it changes.

        Args:
            intent: One of book, change, cancel, confirm, question, message, human, emergency, unclear, off_topic (not about the business, insults, nonsense or trolling: call it at once, before saying anything).
                book = they need a visit or treatment ("θέλω ραντεβού", "να φτιάξω/αλλάξω ένα δόντι", "με πονάει").
                change = only an appointment they ALREADY have ("να αλλάξω το ραντεβού μου", "να το μεταφέρω").
            staff: Who they asked for, in their words ("με τον Γιώργο", "τον γιατρό"); empty if nobody.
            department: The department they named, if any.
        """
        # The language never changes here: only "English mode", matched in code (check_language).
        result = await self._rc.tool("route_call", {
            "intent": intent, "staff": staff or None, "department": department or None,
        })
        if result.get("path") == "end_call":
            # Third off-topic / abusive turn (off_topic_limit): the backend decided to end the call.
            await context.wait_for_playout()
            self._rc.spawn(self._rc.end_with(result.get("say", "")))
            return json.dumps({"path": "end_call", "next": "The call is ending. Say nothing more."})
        return json.dumps(result, ensure_ascii=False)

    @function_tool
    async def check_availability(
        self, when: str, service_id: str = "", staff: str = "", appointment_id: str = "",
        after: str = "", before: str = "",
    ) -> str:
        """Finds free appointment times. Call it before offering any time, and again whenever
        the caller wants a different day or time ("νωρίτερα", "αργότερα", "την επόμενη μέρα").

        Args:
            when: The day the caller asked for, in their own words, e.g. "την Τρίτη το
                απόγευμα", "αύριο", "next Monday morning". Never convert it to a date yourself.
                For "νωρίτερα"/"αργότερα"/"την επόμενη μέρα" pass their words: they are taken
                relative to the day you just offered.
            service_id: The id of the service from the list of services, if known.
            staff: Who they want it with, in their words; empty for anyone free.
            appointment_id: When moving an existing appointment, its id.
            after: "αργότερα" / "later": the latest time you just offered (HH:MM); only later times come back.
            before: "νωρίτερα" / "earlier": the earliest time you just offered (HH:MM); only earlier times come back.
                To check one exact time, set both after and before to that time.
        """
        payload = {
            "when": when, "service_id": service_id or None, "staff": staff or None,
            "appointment_id": appointment_id or None, "after": after or None, "before": before or None,
        }
        # On the first inbound lookup, use the recognized caller turn itself. If STT
        # stopped before the day was spoken, the backend returns no_date instead of
        # accepting a day the model guessed (observed as "σήμερα" in a demo call).
        if (not getattr(self._rc, "_availability_checked", False)
                and self._rc.metadata.get("direction") in ("web", "inbound")
                and not appointment_id and self._rc._last_user_text):
            payload["when"] = self._rc._last_user_text
        reply = await self._tool("check_availability", payload)
        if json.loads(reply).get("date"):
            self._rc._availability_checked = True
        return reply

    @function_tool
    async def prepare_action(
        self, action: str, date: str = "", time: str = "", service_id: str = "",
        customer_name: str = "", staff: str = "", appointment_id: str = "",
    ) -> str:
        """Reads back trusted details and asks for a clear yes before booking, moving or cancelling.

        Args:
            action: book, reschedule or cancel.
            date: The offered date, for book or reschedule.
            time: The offered time, for book or reschedule.
            service_id: The service id, for book or reschedule.
            customer_name: The full name, for a new booking.
            staff: Same staff wording used in check_availability.
            appointment_id: From find_appointments, for reschedule or cancel.
        """
        # Realtime speech models can invent a surname even when the separate STT got it
        # right. A short, clearly spoken name in the latest caller turn wins over the
        # model's tool argument; the backend still reads it back and requires a yes.
        spoken_name = name_from_transcript(self._rc._last_user_text) if action == "book" else None
        prepared_name = spoken_name or customer_name
        result = await self._rc.tool("prepare_action", {
            "action": action, "date": date or None, "time": time or None,
            "service_id": service_id or None, "customer_name": prepared_name or None,
            "staff": staff or None, "appointment_id": appointment_id or None,
        })
        if result.get("confirmation_id"):
            self._rc.read_back(result, customer_name=prepared_name if action == "book" else None)
            return json.dumps({"next": "Wait for the caller to answer the spoken readback. Only a clear yes permits the action."})
        return json.dumps(result, ensure_ascii=False)

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
            "date": date, "time": time, "service_id": service_id,
            "customer_name": self._rc._prepared_name or customer_name,
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
    async def admin_login(self, pin: str) -> str:
        """Checks the PIN of a staff member calling from their registered mobile to change settings.
        Only when the prompt says this caller may change settings, after stop_recording.

        Args:
            pin: The digits they said.
        """
        return await self._tool("admin_login", {"pin": pin})

    @function_tool
    async def admin_change(self, request: str) -> str:
        """Plans a settings change (closure, leave, hours, a price, information) after admin_login.
        Read say_and_ask to them and wait for a clear yes or no.

        Args:
            request: What they asked for, in their own words.
        """
        return await self._tool("admin_change", {"request": request})

    @function_tool
    async def admin_confirm(self, yes: bool) -> str:
        """Applies (yes) or drops (no) the change admin_change read back. Say the returned `say`.

        Args:
            yes: True only for a clear yes.
        """
        return await self._tool("admin_confirm", {"yes": yes})

    @function_tool
    async def stop_recording(self) -> str:
        """Stops recording the call, when the caller doesn't want to be recorded. The call goes on."""
        if not self._rc.metadata.get("record"):
            return "this call is not recorded"
        await self._rc.stop_recording()
        return "recording stopped"

    async def greet(self, *, wait_for_playout: bool = False) -> None:
        instruction = self._rc.metadata.get("greeting_instruction")
        if instruction:
            await self.session.generate_reply(instructions=instruction)
        elif self._rc.engine in ("pipeline", "text_pipeline"):
            handle = self.session.say(self._rc.metadata["greeting"], add_to_chat_ctx=True)
            if wait_for_playout:
                await asyncio.wait_for(handle.wait_for_playout(), timeout=20)
        else:
            quote = "Πες ακριβώς αυτό" if self.language == "el" else "Say exactly this"
            await self.session.generate_reply(instructions=f"{quote}: {self._rc.metadata['greeting']}")


def prewarm(proc: JobProcess) -> None:
    # Loaded once per worker process, not per call; both text pipelines need it.
    if any(engine in ("pipeline", "text_pipeline") for engine in (ENGINE, RECEPTIONIST_ENGINE)):
        proc.userdata["vad"] = silero.VAD.load()


def build_session(ctx: JobContext, engine: str, voice: str, language: str, vocabulary: list[str] | None = None) -> AgentSession:
    if engine in ("pipeline", "text_pipeline"):
        stt = (pipeline_stt(language, vocab_terms(language, vocabulary)) if engine == "pipeline"
               else caller_stt(language))
        tts = pipeline_tts(voice) if engine == "pipeline" else gemini_tts(voice)
        # Open the connections now, while the phone rings, not on the first reply.
        for part in ((stt, tts) if engine == "pipeline" else (stt,)):
            if prewarm_part := getattr(part, "prewarm", None):
                try:
                    prewarm_part()
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
                # ElevenLabs uses the turn detector. Deepgram's final transcript ends
                # a fallback turn, so the text model responds to that exact transcript.
                # Avoid loading a local turn model in the fallback worker: it previously
                # exhausted Railway's memory.
                turn_detection=(inference.TurnDetector(local_fallback=False)
                                if engine == "pipeline" else "stt"),
                endpointing=({"mode": "dynamic", "min_delay": ENDPOINT_MIN_DELAY,
                              "max_delay": TURN_MAX_DELAY_MS / 1000}
                             if engine == "pipeline" else {"mode": "fixed", "min_delay": ENDPOINT_MIN_DELAY}),
                # A cough or a one-word "ναι" mid-reply shouldn't cut the agent off; if it was
                # a false alarm, carry on where it stopped.
                interruption={"min_duration": 0.6, "min_words": 2, "resume_false_interruption": True,
                              "false_interruption_timeout": 1.5},
                # Prepare the text early, but do not voice it over a caller who is still speaking.
                preemptive_generation={"enabled": True, "preemptive_tts": False},
            ),
        )
    return AgentSession(**language_parts(engine, voice, language, vocabulary))


def gemini_tts(voice: str) -> livekit_tts.TTS:
    """Gemini's voice: the text pipeline's only voice, and the pipeline's spare."""
    models = list(dict.fromkeys((GEMINI_TTS_MODEL, GEMINI_TTS_FALLBACK_MODEL)))
    return livekit_tts.FallbackAdapter([
        google.beta.GeminiTTS(model=model, voice_name=gemini_voice(voice),
                              api_key=os.environ.get("GEMINI_API_KEY"))
        for model in models
    ], max_retry_per_tts=1)


def pipeline_tts(voice: str) -> livekit_tts.TTS:
    """ElevenLabs, with Gemini's voice taking over the moment it fails mid-call (credits run
    out, outage): the call starting fine doesn't keep it from going silent later. No retry on
    ElevenLabs, so the caller waits one failed attempt, not three; a background probe brings it
    back for the next reply if it was a blip. The output keeps ElevenLabs' rate, so only
    Gemini's audio is resampled."""
    eleven = elevenlabs.TTS(voice_id=elevenlabs_voice(voice), model="eleven_flash_v2_5")
    if not os.environ.get("GEMINI_API_KEY"):
        return eleven
    return livekit_tts.FallbackAdapter([eleven, gemini_tts(voice)], max_retry_per_tts=0,
                                       sample_rate=eleven.sample_rate)


def pipeline_stt(language: str, keyterms: list[str] | None = None) -> livekit_stt.STT:
    """Scribe, with Deepgram taking over for the rest of the call if Scribe fails (it ends
    the session on quota_exceeded). One quick retry covers a dropped connection; the words
    said while it failed are lost, so the caller may have to repeat them."""
    return livekit_stt.FallbackAdapter([scribe_stt(language, keyterms), caller_stt(language)],
                                       max_retry_per_stt=1, retry_interval=0.5)


def scribe_stt(language: str, keyterms: list[str] | None = None):
    return elevenlabs.STT(
        keyterms=keyterms or None,
        model="scribe_v2_realtime",
        # Without a language hint Scribe hears Greek phone audio as Ukrainian (Cyrillic text).
        language_code=language,
        # ElevenLabs decides when a sentence is finished. The plugin's default ("manual")
        # waits for a commit the session never sends, so no final transcript ever arrived
        # and the agent stayed silent for the whole call.
        # 0.3 s cut normal-speed sentences into fragments at every short pause.
        server_vad={"vad_silence_threshold_secs": SCRIBE_SILENCE_SECS},
    )


# Everyday Greek the transcriber should expect; the business adds its own names and services.
GREEK_VOCABULARY = [
    "ρε", "μωρέ", "κομπλέ", "γαμώτο", "άσ' το", "θα 'ρθω", "κάνα", "τίποτα", "εντάξει", "μπορείς",
    "απογευματάκι", "πρωινό", "ραντεβουδάκι", "ρε φίλε", "έλα", "λέγε", "άντε", "οκ", "ναι ρε",
    "English", "ίνγκλις", "ένγκλις", "αγγλικά",
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
        return {"stt": pipeline_stt(language, words)}
    if engine == "text_pipeline":
        return {"stt": caller_stt(language)}
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
        # Gemini Live understands Greek well but its own transcript of it doesn't: even pinned
        # to el-GR it wrote Portuguese/Spanish/German fragments ("diepes", "né"). The caller's
        # words come from the STT below instead (see CallerTurns).
        input_audio_transcription=None,
        api_key=os.environ.get("GEMINI_API_KEY"),
        # Decide the friend has finished after a short pause, not Gemini's slower default.
        realtime_input_config=genai_types.RealtimeInputConfig(
            automatic_activity_detection=genai_types.AutomaticActivityDetection(
                end_of_speech_sensitivity=genai_types.EndSensitivity.END_SENSITIVITY_HIGH,
                silence_duration_ms=REALTIME_SILENCE_MS,
            ),
        ),
    ), "stt": caller_stt(language)}


def caller_stt(language: str):
    """Deepgram STT for the text pipeline and realtime call transcript.
    Deepgram nova-3 via LiveKit Inference was the best streaming option on a real Greek
    phone recording (2026-09-25); Speechmatics, Cartesia and Gemini Transcribe Live garbled
    more, AssemblyAI has no Greek."""
    return inference.STT("deepgram/nova-3", language=language)


class CallerTurns:
    """Calls `on_turn(text)` once per caller turn. The realtime engine takes the text from the
    STT (Deepgram), joining its final segments until Gemini commits the turn; if the STT
    fails or stays silent, it falls back to Gemini's own text. Other engines use the turn text."""

    LATE_SECONDS = 1.5  # how long to wait for the STT when Gemini's turn arrives first

    def __init__(self, session: AgentSession, engine: str, on_turn) -> None:
        self._on_turn = on_turn
        self._stt = engine == "realtime"
        self._parts: list[str] = []
        self._late: asyncio.TimerHandle | None = None
        session.on("conversation_item_added", self._item)
        if self._stt:
            session.on("user_input_transcribed", self._final)
            session.on("error", self._error)

    def _item(self, ev) -> None:
        item = ev.item
        if getattr(item, "type", None) != "message" or item.role != "user":
            return
        if not self._stt:
            if item.text_content:
                self._on_turn(item.text_content)
        elif self._parts:
            self._flush()
        elif self._late is None:
            self._late = asyncio.get_running_loop().call_later(self.LATE_SECONDS, self._fallback, item.text_content)

    def _final(self, ev) -> None:
        text = (getattr(ev, "transcript", "") or "").strip()
        # Gemini still sends its own transcript through this event; only its events carry an item_id.
        if not getattr(ev, "is_final", False) or not text or getattr(ev, "item_id", None):
            return
        self._parts.append(text)
        if self._late is not None:  # Gemini's turn already arrived: this is its text
            self._late.cancel()
            self._late = None
            self._flush()

    def _fallback(self, text: str | None) -> None:
        self._late = None
        if text:
            self._on_turn(text)

    def _flush(self) -> None:
        text = " ".join(self._parts)
        self._parts.clear()
        self._on_turn(text)

    def _error(self, ev) -> None:
        if "STT" in type(getattr(ev, "source", None)).__name__:
            logger.warning("caller STT failed, using Gemini's transcript: %s", getattr(ev, "error", ev))
            self._stt = False


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


# Credits left below which a warning is logged, and below which a call doesn't start on
# ElevenLabs at all (~1500 is about one 5-minute call).
ELEVEN_LOW_CREDITS = int(os.environ.get("ELEVEN_LOW_CREDITS", "5000"))
ELEVEN_MIN_CREDITS = int(os.environ.get("ELEVEN_MIN_CREDITS", "1500"))
ELEVEN_CHECK_SECONDS = 2.5


async def elevenlabs_credits(client: httpx.AsyncClient) -> tuple[int, bool] | None:
    """Credits left this period and whether billing may go past the limit; None when the key
    can't read them (no user_read), the request fails or the answer has no numbers, which
    means "use the TTS probe instead"."""
    try:
        r = await client.get("https://api.elevenlabs.io/v1/user/subscription",
                             headers={"xi-api-key": os.environ["ELEVEN_API_KEY"]}, timeout=1.2)
        if r.status_code != 200:
            if r.status_code not in (401, 403):
                logger.warning("ElevenLabs subscription check failed (%s)", r.status_code)
            return None
        sub = r.json()
        remaining = int(sub["character_limit"]) - int(sub["character_count"])
    except Exception as e:
        logger.warning("ElevenLabs subscription check failed: %r", e)
        return None
    # Usage-based billing goes past the limit instead of refusing.
    overage = bool(sub.get("can_extend_character_limit") and sub.get("allowed_to_extend_character_limit"))
    return remaining, overage


async def elevenlabs_usable() -> bool:
    """Reads the credits left when the key has user_read; out of credits means a call that
    would go silent, so it starts on the fallback engine. Without user_read it sends a tiny
    paid TTS request (~1 credit) instead: ElevenLabs refuses it with quota_exceeded when the
    account is out of credits. A 1-character text is let through even at 0 credits, so the
    probe is two. Both together stay within ELEVEN_CHECK_SECONDS."""
    try:
        return await asyncio.wait_for(_elevenlabs_usable(), ELEVEN_CHECK_SECONDS)
    except Exception as e:  # includes the timeout
        logger.warning("ElevenLabs check failed: %r", e)
        return False


async def _elevenlabs_usable() -> bool:
    async with httpx.AsyncClient(timeout=ELEVEN_CHECK_SECONDS) as client:
        if (credits := await elevenlabs_credits(client)) is not None:
            remaining, overage = credits
            logger.info("ElevenLabs credits: %d left%s", remaining, " (billing past the limit)" if overage else "")
            if remaining < ELEVEN_MIN_CREDITS and not overage:
                logger.warning("ElevenLabs credits below %d, not using ElevenLabs", ELEVEN_MIN_CREDITS)
                return False
            if remaining < ELEVEN_LOW_CREDITS:
                logger.warning("ElevenLabs credits low: %d left (top up)", remaining)
            return True
        r = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{elevenlabs_voice('default')}",
            params={"output_format": "mp3_22050_32"},
            headers={"xi-api-key": os.environ["ELEVEN_API_KEY"]},
            json={"text": "ok", "model_id": "eleven_flash_v2_5"},
        )
    if r.status_code != 200:
        logger.warning("ElevenLabs unusable (%s): %s", r.status_code, r.text[:200])
        return False
    return True


async def pick_engine(call_id: str, receptionist: bool = False) -> str:
    engine = RECEPTIONIST_ENGINE if receptionist else ENGINE
    fallback = "text_pipeline" if receptionist and os.environ.get("GEMINI_API_KEY") else "realtime"
    if engine == "pipeline" and not os.environ.get("ELEVEN_API_KEY"):
        logger.warning("call %s: ELEVEN_API_KEY missing, using %s", call_id, fallback)
        engine = fallback
    # Out of credits (or ElevenLabs down) would mean a silent call.
    if engine == "pipeline" and not await elevenlabs_usable():
        logger.warning("call %s: ElevenLabs unusable, using %s", call_id, fallback)
        engine = fallback
    if engine == "openai" and not os.environ.get("OPENAI_API_KEY"):
        logger.warning("call %s: OPENAI_API_KEY missing, using the realtime engine", call_id)
        engine = "realtime"
    return engine


async def publish_web_transcript(
    room: rtc.Room, role: str, text: str, caller_identity: str, language: str,
) -> None:
    """Publish final CallerTurns text, not the realtime model's raw events."""
    if role == "friend" and language == "el":
        # Gemini's fallback transcript can turn Greek audio into Spanish/Italian text.
        # Keep an explicit "English" request so the caller can change language.
        if not re.search(r"[\u0370-\u03ff\u1f00-\u1fff]", text) and wants_language(text) != "en":
            return
    if not room.isconnected():
        return
    identity = caller_identity if role == "friend" else room.local_participant.identity
    participant = room.remote_participants.get(identity) if role == "friend" else room.local_participant
    track_sid = ""
    if participant:
        track_sid = next((p.sid for p in participant.track_publications.values()
                          if p.source == rtc.TrackSource.SOURCE_MICROPHONE), "")
    try:
        await room.local_participant.publish_transcription(rtc.Transcription(
            participant_identity=identity,
            track_sid=track_sid,
            segments=[rtc.TranscriptionSegment(
                id=uuid.uuid4().hex, text=text, start_time=0, end_time=0,
                language=language, final=True,
            )],
        ))
    except Exception:
        logger.exception("could not publish web transcript")


def track_transcript(
    session: AgentSession, call_id: str, engine: str, *, room: rtc.Room | None = None,
    caller_identity: str | None = None, language: Callable[[], str] | None = None,
) -> None:
    def _line(role: str, text: str) -> None:
        # What each side said, to judge recognition and language from the logs.
        logger.info("call %s %s: %s", call_id, role, text)
        asyncio.create_task(report(call_id, transcript_role=role, transcript_text=text))
        if room is not None and caller_identity and language:
            asyncio.create_task(publish_web_transcript(room, role, text, caller_identity, language()))

    @session.on("conversation_item_added")
    def _on_item(ev) -> None:
        # Not handoffs; the caller's lines come from CallerTurns.
        if getattr(ev.item, "type", None) == "message" and ev.item.role == "assistant" and ev.item.text_content:
            _line("agent", ev.item.text_content)

    CallerTurns(session, engine, lambda text: _line("friend", text))


# If the agent says roughly the same thing this many times in a row it's stuck (looping on
# nonsense or a broken tool): end the call instead of leaving the friend with a broken bot.
REPEAT_HANGUP_COUNT = 3
# How alike two consecutive agent turns must be to count as the same utterance.
REPEAT_SIMILARITY = 0.9


def guard_repetition(session: AgentSession, ctx: JobContext, call_id: str) -> None:
    """Hang up if the agent repeats itself ~3 times in a row."""
    state = {"last": "", "count": 0}

    @session.on("conversation_item_added")
    def _on_item(ev) -> None:
        item = ev.item
        if getattr(item, "type", None) != "message" or item.role != "assistant":
            return
        text = _plain((item.text_content or "").strip())
        if not text:
            return
        if state["last"] and difflib.SequenceMatcher(None, state["last"], text).ratio() >= REPEAT_SIMILARITY:
            state["count"] += 1
        else:
            state["count"] = 1
        state["last"] = text
        if state["count"] >= REPEAT_HANGUP_COUNT:
            logger.warning("call %s: agent repeated itself %d times, hanging up", call_id, state["count"])
            asyncio.create_task(ctx.room.disconnect())

    return


def room_options(web: bool = False, caller_identity: str | None = None) -> room_io.RoomOptions:
    # Clean noise before transcription and turn detection hear it: the telephony model for
    # 8 kHz phone lines, the full-band one for browser (web demo) microphones.
    nc = None
    if NOISE_CANCELLATION:
        nc = noise_cancellation.BVC() if web else noise_cancellation.BVCTelephony()
    kwargs = {"participant_identity": caller_identity} if caller_identity else {}
    if web:
        # Web calls publish final lines from track_transcript. RoomIO would also forward
        # Gemini's unreliable raw user transcripts to the browser.
        kwargs["text_output"] = False
    return room_io.RoomOptions(audio_input=room_io.AudioInputOptions(noise_cancellation=nc), **kwargs)


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
        self._profanity_said = False
        self._switching_language = False
        self._tasks: set[asyncio.Task] = set()
        self._user_turn = 0
        self._last_user_text = ""
        self._confirmation_id: str | None = None
        self._confirmation_floor = 0
        self._confirmation_armed = False
        self._prepared_name: str | None = None
        self._availability_checked = False

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def heard_user(self, text: str) -> None:
        self._user_turn += 1
        self._last_user_text = text

    def read_back(self, result: dict, *, customer_name: str | None = None) -> None:
        self._confirmation_id = result["confirmation_id"]
        self._confirmation_armed = False
        self._prepared_name = customer_name
        if self.engine in ("pipeline", "text_pipeline"):
            handle = self.session.say(result["say"], allow_interruptions=False)
        else:
            quote = "Πες ακριβώς αυτό" if self.language == "el" else "Say exactly this"
            handle = self.session.generate_reply(instructions=f"{quote}: {result['say']}", allow_interruptions=False)

        async def arm() -> None:
            try:
                await asyncio.wait_for(handle.wait_for_playout(), timeout=20)
                self._confirmation_floor = self._user_turn
                self._confirmation_armed = True
            except Exception:
                logger.exception("call %s: confirmation readback failed", self.call_id)

        self.spawn(arm())

    def confirmation_for_write(self) -> dict:
        if self._confirmation_id and self._confirmation_armed and self._user_turn > self._confirmation_floor:
            return {"confirmation_id": self._confirmation_id, "confirmation_text": self._last_user_text}
        return {}

    def clear_confirmation(self) -> None:
        self._confirmation_id = None
        self._confirmation_armed = False
        self._prepared_name = None

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

    async def switch_language(self, language: str) -> None:
        # The backend authorizes and logs R7; only then replace the model's STT
        # language hint (or realtime model) along with the prompt.
        if language not in ("el", "en") or language == self.language or self.session is None:
            return
        await asyncio.sleep(0.2)  # let route_call return before replacing its agent
        parts = language_parts(self.engine, self.metadata.get("voice", "default"), language,
                               self.metadata.get("vocabulary"))
        self.language = language
        self.agent = self.make_agent(language, parts)
        self.session.update_agent(self.agent)

    async def end_with(self, line: str) -> None:
        """Say one closing line (not interruptible), then hang up."""
        await asyncio.sleep(0.1)
        try:
            if self.engine in ("pipeline", "text_pipeline"):
                handle = self.session.say(line, allow_interruptions=False)
            else:
                quote = "Πες ακριβώς αυτό" if self.language == "el" else "Say exactly this"
                handle = self.session.generate_reply(instructions=f"{quote}: {line}", allow_interruptions=False)
            await asyncio.wait_for(handle.wait_for_playout(), timeout=15)
        except Exception:
            logger.exception("call %s: closing line failed", self.call_id)
        await self.ctx.room.disconnect()

    # --- language: Greek, English only on "English mode" (matched here, never the model) ---

    def check_language(self, text: str) -> None:
        wanted = wants_language(text)
        if wanted and wanted != self.language and not self._switching_language:
            self._switching_language = True
            self.spawn(self._set_language(wanted))

    async def _set_language(self, language: str) -> None:
        try:
            result = await self.tool("set_language", {"language": language})
            if not result.get("ok"):
                return
            logger.info("call %s: %s mode", self.call_id, language)
            self.session.interrupt()
            await self.switch_language(language)
            line = LANGUAGE_MODE_LINE[language]
            if self.engine in ("pipeline", "text_pipeline"):
                self.session.say(line, add_to_chat_ctx=True)
            else:
                quote = "Πες ακριβώς αυτό" if language == "el" else "Say exactly this"
                self.session.generate_reply(instructions=f"{quote}: {line}")
        except Exception:
            logger.exception("call %s: language switch failed", self.call_id)
        finally:
            self._switching_language = False

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

    # --- profanity: one calm, polite reminder when the caller swears at the agent ---

    def check_profanity(self, text: str) -> None:
        # Falls back to built-in defaults so it also works on the prank/professional flow,
        # where the metadata carries no profanity block.
        pr = self.metadata.get("profanity") or {}
        if self._profanity_said or not pr.get("enabled", True):
            return
        key = "el" if self.language == "el" else "en"
        phrases = pr.get("phrases") or (DEFAULT_PROFANITY_PHRASES["el"] + DEFAULT_PROFANITY_PHRASES["en"])
        script = (pr.get("script") or DEFAULT_PROFANITY_SCRIPT)[key]
        plain = _plain(text)
        if any(_plain(p) in plain for p in phrases):
            # Only once: after this, repeated abuse falls through to the model's off_topic strikes.
            self._profanity_said = True
            logger.info("call %s: profanity heard, one polite reminder", self.call_id)
            self.session.interrupt()
            quote = "Πες αμέσως ακριβώς αυτό" if self.language == "el" else "Say exactly this right now"
            self.session.generate_reply(instructions=f"{quote}: {script}")

    # --- recording (G7) ---

    async def start_recording(self) -> None:
        # The practice can turn recording off (G7): then no egress starts at all.
        if not self.metadata.get("record") or self.metadata.get("recording_enabled") is False:
            logger.info("call %s: recording off for this call", self.call_id)
            return
        if not storage_configured():
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


def name_from_transcript(text: str) -> str | None:
    """Extract only a standalone full name or an explicit name correction."""
    if len(text) > 100:
        return None
    name = text.strip().strip(" .,!?;:·…")
    without_negation = re.sub(r"^(?:όχι|οχι|λάθος|λαθος|no|wrong)[\s,.!?;:·]+", "", name, flags=re.I)
    without_intro = re.sub(
        r"^(?:με λένε|λένε|λέγομαι|ονομάζομαι|το όνομά μου είναι|"
        r"my name is|i am|i'm)[\s]+", "", without_negation, flags=re.I,
    ).strip(" .,!?;:·…")
    explicit_name = without_intro != without_negation
    name = without_intro
    words = name.split()
    if not 2 <= len(words) <= 3:
        return None
    if any(not re.fullmatch(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", word) for word in words):
        return None
    if not explicit_name and not all(word[0].isupper() for word in words):
        return None
    if any(_plain(word) in {"ναι", "οχι", "σωστα", "πεντε", "τεταρτο", "ενταξει",
                            "yes", "no", "correct", "okay"}
           for word in words):
        return None
    return " ".join(words)


async def say_busy_and_leave(ctx: JobContext, engine: str, busy: dict) -> None:
    """All lines busy (G8): one line, then hang up."""
    session = build_session(ctx, engine, busy.get("voice", "default"), busy.get("language", "el"))
    await session.start(agent=Agent(instructions="Say only the line you are given, then stop."), room=ctx.room)
    quote = "Πες ακριβώς αυτό" if busy.get("language") == "el" else "Say exactly this"
    handle = session.generate_reply(instructions=f"{quote}: {busy['busy_line']}", allow_interruptions=False)
    await asyncio.wait_for(handle.wait_for_playout(), timeout=15)
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
                await say_busy_and_leave(ctx, await pick_engine("busy", receptionist=True), r.json())
                return
            r.raise_for_status()
            metadata = r.json()
        except Exception:
            logger.exception("inbound call to %s from %s: no practice, hanging up", dialed, caller_number)
            await ctx.room.disconnect()
            ctx.shutdown(reason="no practice")
            return

    rc = ReceptionistCall(ctx, metadata, await pick_engine(metadata["call_id"], receptionist=True))
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
    track_transcript(
        session, call_id, rc.engine,
        room=ctx.room if metadata.get("direction") == "web" else None,
        caller_identity=rc.caller_identity,
        language=lambda: rc.language,
    )
    log_latency(session, call_id)
    guard_repetition(session, ctx, call_id)
    rc.track_latency()

    def _heard(text: str) -> None:
        rc.heard_user(text)
        rc.check_emergency(text)
        rc.check_profanity(text)
        rc.check_language(text)

    CallerTurns(session, rc.engine, _heard)

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
    web = metadata.get("direction") == "web"
    if web:
        # Finish the opening before listening: an early browser-mic turn can interrupt
        # Gemini's greeting and make it start the same sentence a second time.
        session.input.set_audio_enabled(False)
    await session.start(agent=agent, room=ctx.room, room_options=room_options(
        web=web, caller_identity=rc.caller_identity,
    ))
    if dialing:
        # Let the customer say "Εμπρός;" first (and hear a voicemail greeting before speaking).
        spoke = asyncio.Event()
        session.on("user_state_changed", lambda ev: spoke.set() if ev.new_state == "speaking" else None)
        try:
            await asyncio.wait_for(spoke.wait(), timeout=GREETING_WAIT_SECONDS)
            await asyncio.sleep(0.8)
        except asyncio.TimeoutError:
            pass
    try:
        await agent.greet(wait_for_playout=web)
    finally:
        if web:
            session.input.set_audio_enabled(True)


# "English" as the Greek transcriber writes it too ("ίνγκλις", "ένγκλις",
# "αγγλικά"); accents are folded by _plain. "Greek" / "ελληνικά" switches back.
ENGLISH_MODE = ("english", "inglis", "ινγκλ", "ενγκλ", "ιγκλ", "εγκλ", "ινγλ", "ιγγλ", "εγγλ", "αγγλικ")
GREEK_MODE = ("greek", "γκρικ", "ελληνικ")
LANGUAGE_MODE_LINE = {
    "en": "English mode. How can I help you?",
    "el": "Ελληνικά. Πώς μπορώ να σας βοηθήσω;",
}


def wants_language(text: str) -> str | None:
    plain = _plain(text)
    if any(w in plain for w in ENGLISH_MODE):
        return "en"
    if any(w in plain for w in GREEK_MODE):
        return "el"
    return None


async def entrypoint(ctx: JobContext) -> None:
    metadata = json.loads(ctx.job.metadata or "{}")
    if metadata.get("health_check"):
        # Backend's synthetic check (OP1): answer and leave without joining the room.
        try:
            await backend_post("/internal/health/agent", {"token": metadata["health_check"]})
        finally:
            ctx.shutdown(reason="health check")
        return
    await ctx.connect()

    if metadata.get("mode") == "receptionist" or "call_id" not in metadata:
        try:
            await run_receptionist(ctx, metadata)
        except BaseException:
            # E.g. the worker is shutting down for a deploy, or the caller left before joining:
            # tell the backend so the call doesn't hold a line. Ignored if it already ended.
            if metadata.get("call_id"):
                await asyncio.shield(report(metadata["call_id"], status="failed", end_reason="error"))
            raise
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

    engine = await pick_engine(call_id)

    # Everything that doesn't need the friend happens while the phone rings:
    # connections open and the opening line gets written and voiced.
    session = build_session(ctx, engine, voice, language)
    opening_task = None
    if engine == "pipeline":
        opening_task = asyncio.create_task(prepare_opening(
            prompt=prompt, language=language, language_name=language_name,
            voice_id=elevenlabs_voice(voice), llm_model=LLM_MODEL,
        ))

    agent = PrankCallerAgent(
        instructions=prompt, call_id=call_id, language=language, language_name=language_name,
        opening_task=opening_task, fillers=engine == "pipeline",
    )

    track_transcript(session, call_id, engine)
    log_latency(session, call_id)
    guard_repetition(session, ctx, call_id)

    @session.on("close")
    def _on_close(_ev) -> None:
        ctx.shutdown(reason="session closed")

    # Let the callee speak first ("Εμπρός;") like a real caller would. This also
    # lets the agent hear a voicemail greeting before it says anything.
    callee_spoke = asyncio.Event()

    @session.on("user_state_changed")
    def _on_user_state(ev) -> None:
        if ev.new_state == "speaking":
            callee_spoke.set()

    logger.info("call %s: engine %s, language %s, dialing %s via trunk %s",
                call_id, engine, language, friend_phone_number, sip_trunk_id)

    # Warm up the model connection during the ring: start the session concurrently with the
    # dial so the (Gemini) cold start is hidden behind the ringing, not paid after pickup.
    # The agent only greets once the callee speaks or open_after_silence runs, so nothing is
    # said before someone answers.
    warm_task = asyncio.create_task(session.start(
        agent=agent, room=ctx.room,
        room_options=room_options(caller_identity=None if test_no_dial else f"friend-{call_id}"),
    ))

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
            warm_task.cancel()
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

    async def _enforce_duration_cap() -> None:
        # Warn the agent shortly before the cap so it wraps up and says goodbye
        # instead of the call being cut off mid-sentence.
        warn_at = max(max_duration_seconds - 25, max_duration_seconds * 0.75)
        await asyncio.sleep(warn_at)
        session.generate_reply(instructions=_for_language(TIME_UP, agent.language, language_name))
        await asyncio.sleep(max_duration_seconds - warn_at)
        logger.info("call %s: hard duration cap reached, disconnecting", call_id)
        await ctx.room.disconnect()

    async def _finish() -> None:
        cap_task.cancel()
        if opening_task:
            opening_task.cancel()
        event = {"status": "completed"}
        if recording_key:
            event["recording_url"] = recording_key
        await report(call_id, **event)

    ctx.add_shutdown_callback(_finish)

    cap_task = asyncio.create_task(_enforce_duration_cap())
    # The warm-up usually finishes during the ring; make sure it's done before we rely
    # on the session for the greeting.
    await warm_task
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
