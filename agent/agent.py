"""LiveKit worker for the AI Prank Caller (M2/M3).

Registered as agent "prank-caller". The backend dispatches a job per call
(see backend/app/livekit_dispatch.py) with job.metadata carrying the merged
master + per-call prompt, the friend's number, voice, and the hard duration
cap. This worker dials out over the Telnyx SIP trunk, runs the conversation
through Gemini Live, records the call to R2, reports transcript/status back to
the backend, and hangs up via its own tool or the duration cap.
"""

import asyncio
import json
import logging
import os

import httpx
from google.protobuf.duration_pb2 import Duration
from livekit import api
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    cli,
    function_tool,
    get_job_context,
)
from livekit.plugins import google

logger = logging.getLogger("prank-caller")

BACKEND_URL = os.environ.get("BACKEND_PUBLIC_URL", "http://localhost:8000")
AGENT_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "")
# How long the friend's phone rings before we give up (the library default is 30s).
RINGING_TIMEOUT_SECONDS = int(os.environ.get("RINGING_TIMEOUT_SECONDS", "45"))
# If the callee stays silent after answering, open the conversation after this long.
GREETING_WAIT_SECONDS = 4


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


async def start_recording(lk: api.LiveKitAPI, room_name: str, call_id: str) -> str:
    key = f"recordings/{call_id}.mp4"
    account = os.environ["R2_ACCOUNT_ID"]
    await lk.egress.start_room_composite_egress(
        api.RoomCompositeEgressRequest(
            room_name=room_name,
            audio_only=True,
            file_outputs=[
                api.EncodedFileOutput(
                    file_type=api.EncodedFileType.MP4,
                    filepath=key,
                    s3=api.S3Upload(
                        access_key=os.environ["R2_ACCESS_KEY_ID"],
                        secret=os.environ["R2_SECRET_ACCESS_KEY"],
                        bucket=os.environ["R2_BUCKET_NAME"],
                        endpoint=f"https://{account}.r2.cloudflarestorage.com",
                        region="auto",
                        force_path_style=True,
                    ),
                )
            ],
        )
    )
    return key


class PrankCallerAgent(Agent):
    def __init__(self, instructions: str, call_id: str) -> None:
        super().__init__(instructions=instructions)
        self._call_id = call_id

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


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    metadata = json.loads(ctx.job.metadata or "{}")
    call_id = metadata["call_id"]
    friend_phone_number = metadata["friend_phone_number"]
    prompt = metadata["prompt"]
    voice = metadata.get("voice", "default")
    max_duration_seconds = metadata.get("max_duration_seconds", 300)
    sip_trunk_id = metadata.get("sip_trunk_id") or os.environ.get("SIP_TRUNK_ID", "")
    outbound_number = metadata.get("outbound_number")

    logger.info("call %s: dialing %s via trunk %s", call_id, friend_phone_number, sip_trunk_id)

    lk = api.LiveKitAPI()
    try:
        try:
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
            sip_status = (getattr(e, "metadata", None) or {}).get("sip_status_code")
            logger.exception("call %s: dial failed (sip status %s)", call_id, sip_status)
            await report(call_id, status="failed")
            return

        recording_key = None
        if not os.environ.get("R2_ACCOUNT_ID"):
            logger.warning("call %s: R2 not configured, not recording", call_id)
        else:
            try:
                recording_key = await start_recording(lk, ctx.room.name, call_id)
            except Exception:
                logger.exception("call %s: recording failed to start", call_id)
    finally:
        await lk.aclose()

    await report(call_id, status="active")

    session = AgentSession(
        llm=google.beta.realtime.RealtimeModel(
            voice=voice if voice != "default" else "Puck",
            api_key=os.environ.get("GEMINI_API_KEY"),
        ),
    )

    @session.on("conversation_item_added")
    def _on_item(ev) -> None:
        role = "agent" if ev.item.role == "assistant" else "friend"
        text = ev.item.text_content
        if text:
            asyncio.create_task(
                report(call_id, transcript_role=role, transcript_text=text)
            )

    async def _enforce_duration_cap() -> None:
        # Warn the agent shortly before the cap so the reveal and recording
        # notice happen instead of the call being cut off mid-sentence.
        warn_at = max(max_duration_seconds - 25, max_duration_seconds * 0.75)
        await asyncio.sleep(warn_at)
        session.generate_reply(
            instructions="Time is almost up. Do the reveal now, mention the recording, say goodbye and hang up."
        )
        await asyncio.sleep(max_duration_seconds - warn_at)
        logger.info("call %s: hard duration cap reached, disconnecting", call_id)
        await ctx.room.disconnect()

    @session.on("close")
    def _on_close(_ev) -> None:
        ctx.shutdown(reason="session closed")

    async def _finish() -> None:
        cap_task.cancel()
        event = {"status": "completed"}
        if recording_key:
            event["recording_url"] = recording_key
        await report(call_id, **event)

    ctx.add_shutdown_callback(_finish)

    # Let the callee speak first ("Εμπρός;") like a real caller would. This also
    # lets the model hear a voicemail greeting before it says anything.
    callee_spoke = asyncio.Event()

    @session.on("user_state_changed")
    def _on_user_state(ev) -> None:
        if ev.new_state == "speaking":
            callee_spoke.set()

    cap_task = asyncio.create_task(_enforce_duration_cap())
    await session.start(agent=PrankCallerAgent(instructions=prompt, call_id=call_id), room=ctx.room)
    try:
        await asyncio.wait_for(callee_spoke.wait(), timeout=GREETING_WAIT_SECONDS)
    except asyncio.TimeoutError:
        await session.generate_reply(
            instructions="Greet the friend naturally and open the scenario."
        )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name="prank-caller"))
