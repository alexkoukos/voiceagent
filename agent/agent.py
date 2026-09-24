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
    def __init__(self, instructions: str) -> None:
        super().__init__(instructions=instructions)

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
                )
            )
        except Exception:
            logger.exception("call %s: dial failed", call_id)
            await report(call_id, status="failed")
            return

        recording_key = None
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
        await asyncio.sleep(max_duration_seconds)
        logger.info("call %s: hard duration cap reached, disconnecting", call_id)
        await ctx.room.disconnect()

    async def _finish() -> None:
        # bucket is private; the backend serves playback via its own access
        event = {"status": "completed"}
        if recording_key:
            event["recording_url"] = recording_key
        await report(call_id, **event)

    ctx.add_shutdown_callback(_finish)

    cap_task = asyncio.create_task(_enforce_duration_cap())
    try:
        await session.start(agent=PrankCallerAgent(instructions=prompt), room=ctx.room)
        await session.generate_reply(
            instructions="Greet the friend naturally and open the scenario."
        )
    except Exception:
        cap_task.cancel()
        raise


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name="prank-caller"))
