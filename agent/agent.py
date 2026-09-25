"""LiveKit worker for AI Caller.

Registered as agent "prank-caller". The backend dispatches a job per call
(see backend/app/livekit_dispatch.py) with job.metadata carrying the merged
master + per-call prompt, the friend's number, voice, language and the hard
duration cap. This worker dials out over the Telnyx SIP trunk, runs the
conversation, records it to S3-compatible storage, reports transcript/status
back to the backend, and hangs up via its own tool or the duration cap.

Two engines (AGENT_ENGINE):
- "pipeline" (default): ElevenLabs Scribe realtime -> Gemini Flash-Lite ->
  ElevenLabs voice, with multilingual turn detection, preemptive generation,
  filler words when a reply is slow, and an opening line prepared during the ring.
- "realtime": Gemini Live speech-to-speech (the original engine; fallback).
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

from fillers import FillerPicker, normalize_language
from opening import prepare_opening, ready_opening
from voices import elevenlabs_voice, gemini_voice

logger = logging.getLogger("prank-caller")

BACKEND_URL = os.environ.get("BACKEND_PUBLIC_URL", "http://localhost:8000")
AGENT_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "")
# How long the friend's phone rings before we give up (the library default is 30s).
RINGING_TIMEOUT_SECONDS = int(os.environ.get("RINGING_TIMEOUT_SECONDS", "45"))
ENGINE = os.environ.get("AGENT_ENGINE", "pipeline")
# Realtime engine model.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-live")
# Pipeline engine: the "brain". Flash-Lite starts answering in ~0.45 s.
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-3.5-flash-lite")
# Realtime engine: how long a pause means the friend has finished talking.
REALTIME_SILENCE_MS = int(os.environ.get("REALTIME_SILENCE_MS", "500"))
# Say a filler word if the reply hasn't started this long after the friend stops talking.
FILLER_DELAY_SECONDS = 0.5
# If the callee stays silent after answering, open the conversation after this long.
GREETING_WAIT_SECONDS = 4
# If the callee spoke but no reply has started this long after, open the conversation anyway.
OPENING_FALLBACK_SECONDS = 5


async def report(call_id: str, **event) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{BACKEND_URL}/internal/calls/{call_id}/events",
                json=event,
                headers={"x-agent-token": AGENT_TOKEN},
            )
    except Exception:
        logger.exception("failed to report event %s for call %s", list(event), call_id)


# S3-compatible recording storage, named like the variables a Railway bucket exposes.
STORAGE_VARS = ("AWS_ENDPOINT_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_S3_BUCKET_NAME")


def storage_configured() -> bool:
    return all(os.environ.get(v) for v in STORAGE_VARS)


async def start_recording(lk: api.LiveKitAPI, room_name: str, call_id: str) -> str:
    key = f"recordings/{call_id}.mp4"
    await lk.egress.start_room_composite_egress(
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
    return key


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
    "el": "Χαιρέτα τον φίλο με φυσικό τρόπο, όπως στο τηλέφωνο, και ξεκίνα.",
    "other": "Greet the friend naturally, as on the phone, and start. Speak {language}.",
}
TIME_UP = {
    "el": "Ο χρόνος τελειώνει. Κάνε τώρα την αποκάλυψη, πες για την ηχογράφηση, αποχαιρέτα και κλείσε.",
    "other": "Time is almost up. Do the reveal now, mention the recording, say goodbye and hang up.",
}
# Realtime engine: pin Greek speech; other languages are auto-detected.
REALTIME_LANGUAGE = {"el": "el-GR"}


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
    ) -> None:
        super().__init__(instructions=instructions)
        self._call_id = call_id
        # The language being spoken right now; follows the friend if they switch.
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
        """Ends the call. Use this once the reveal is done or the scenario
        has run its course — never leave a call open indefinitely."""
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


def prewarm(proc: JobProcess) -> None:
    # Loaded once per worker process, not per call; only the pipeline engine needs it.
    if ENGINE == "pipeline":
        proc.userdata["vad"] = silero.VAD.load()


def build_session(ctx: JobContext, engine: str, voice: str, language: str) -> AgentSession:
    if engine == "pipeline":
        stt = elevenlabs.STT(
            model="scribe_v2_realtime",
            # Without a language hint Scribe hears Greek phone audio as Ukrainian (Cyrillic text).
            # Detection stays on so a friend who switches language is still followed.
            language_code=language,
            include_language_detection=True,
            # ElevenLabs decides when a sentence is finished. The plugin's default ("manual")
            # waits for a commit the session never sends, so no final transcript ever arrived
            # and the agent stayed silent for the whole call.
            server_vad={"vad_silence_threshold_secs": 0.3},
        )
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
                # version="v1" must be explicit: outside LiveKit Cloud hosting the default is the
                # local v1-mini model, which doesn't know Greek, so every turn waited max_delay.
                turn_detection=inference.TurnDetector(version="v1", local_fallback=False),
                endpointing={"mode": "dynamic", "min_delay": 0.2, "max_delay": 1.5},
                # Start writing and voicing the reply before the friend has fully finished.
                preemptive_generation={"enabled": True, "preemptive_tts": True},
            ),
        )
    return AgentSession(
        llm=google.beta.realtime.RealtimeModel(
            model=GEMINI_MODEL,
            voice=gemini_voice(voice),
            language=REALTIME_LANGUAGE.get(language),
            api_key=os.environ.get("GEMINI_API_KEY"),
            # Decide the friend has finished after a short pause, not Gemini's slower default.
            realtime_input_config=genai_types.RealtimeInputConfig(
                automatic_activity_detection=genai_types.AutomaticActivityDetection(
                    end_of_speech_sensitivity=genai_types.EndSensitivity.END_SENSITIVITY_HIGH,
                    silence_duration_ms=REALTIME_SILENCE_MS,
                ),
            ),
        ),
    )


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


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    metadata = json.loads(ctx.job.metadata or "{}")
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

    engine = ENGINE
    if engine == "pipeline" and not os.environ.get("ELEVEN_API_KEY"):
        logger.warning("call %s: ELEVEN_API_KEY missing, using the realtime engine", call_id)
        engine = "realtime"

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

    @session.on("conversation_item_added")
    def _on_item(ev) -> None:
        if getattr(ev.item, "type", None) != "message":  # e.g. agent handoffs
            return
        role = "agent" if ev.item.role == "assistant" else "friend"
        text = ev.item.text_content
        if text:
            asyncio.create_task(
                report(call_id, transcript_role=role, transcript_text=text)
            )

    @session.on("user_input_transcribed")
    def _on_transcribed(ev) -> None:
        # Follow the friend's language (fillers and greetings switch with them).
        lang = normalize_language(getattr(ev, "language", None))
        if ev.is_final and lang and lang != agent.language:
            logger.info("call %s: friend switched language %s -> %s", call_id, agent.language, lang)
            agent.language = lang

    log_latency(session, call_id)

    async def _enforce_duration_cap() -> None:
        # Warn the agent shortly before the cap so the reveal and recording
        # notice happen instead of the call being cut off mid-sentence.
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
        # Clean phone-line noise before transcription and turn detection hear it.
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(noise_cancellation=noise_cancellation.BVCTelephony()),
        ),
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
