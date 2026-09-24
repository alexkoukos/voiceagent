"""Starts an outbound call by dispatching a LiveKit agent job.

The agent (see agent/agent.py) picks up the job, dials the friend through the
Telnyx SIP trunk, and gets the merged prompt + guardrails via job metadata.
This is a thin wrapper so routers/calls.py doesn't need to know LiveKit's API
shape; fill in the trunk/room details once M1 (the 210 number) is live.
"""

import json

from livekit import api

from app.config import get_settings


async def dispatch_call(
    *,
    call_id: str,
    friend_phone_number: str,
    merged_prompt: str,
    voice: str,
    max_duration_seconds: int,
) -> None:
    settings = get_settings()
    room_name = f"call-{call_id}"

    metadata = json.dumps(
        {
            "call_id": call_id,
            "friend_phone_number": friend_phone_number,
            "prompt": merged_prompt,
            "voice": voice,
            "max_duration_seconds": max_duration_seconds,
            "sip_trunk_id": settings.sip_trunk_id,
            "outbound_number": settings.sip_outbound_number,
        }
    )

    async with api.LiveKitAPI(
        settings.livekit_url,
        settings.livekit_api_key,
        settings.livekit_api_secret,
    ) as lk:
        # Dispatches the "prank-caller" agent (registered in agent/agent.py)
        # into a new room; the agent reads `metadata` to know who to dial and
        # what to say. Actual SIP INVITE happens inside the agent via
        # lk.sip.create_sip_participant once the trunk is provisioned.
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name="prank-caller",
                room=room_name,
                metadata=metadata,
            )
        )


async def end_call(call_id: str) -> None:
    settings = get_settings()
    async with api.LiveKitAPI(
        settings.livekit_url,
        settings.livekit_api_key,
        settings.livekit_api_secret,
    ) as lk:
        await lk.room.delete_room(api.DeleteRoomRequest(room=f"call-{call_id}"))
