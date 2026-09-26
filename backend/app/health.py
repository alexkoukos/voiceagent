"""Synthetic checks (PRD OP1): alert within minutes when calls could not be answered.

Inbound calls reach the backend only through a running agent, so a dead agent is otherwise
invisible. Every few minutes LiveKit's API is asked for rooms; every HEALTH_AGENT_CHECK_MINUTES
a no-op job is dispatched and the agent must report back within a minute.
"""

import asyncio
import logging
import secrets
from datetime import datetime, timedelta

from livekit import api

from app import alerts
from app.config import get_settings

logger = logging.getLogger(__name__)

LIVEKIT_EVERY = timedelta(minutes=5)
AGENT_REPLY_WITHIN = timedelta(seconds=60)

_last_livekit: datetime | None = None
_last_agent: datetime | None = None
# token -> (dispatched at, answered)
_pending: dict[str, list] = {}


def acknowledge(token: str) -> bool:
    entry = _pending.get(token)
    if entry is None:
        return False
    entry[1] = True
    return True


async def _livekit_ok() -> str | None:
    s = get_settings()
    try:
        async with api.LiveKitAPI(s.livekit_url, s.livekit_api_key, s.livekit_api_secret) as lk:
            await asyncio.wait_for(lk.room.list_rooms(api.ListRoomsRequest()), timeout=10)
        return None
    except Exception as e:
        return repr(e)[:300]


async def _dispatch_probe(token: str) -> None:
    s = get_settings()
    async with api.LiveKitAPI(s.livekit_url, s.livekit_api_key, s.livekit_api_secret) as lk:
        await lk.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(
            agent_name=s.agent_name, room=f"health-{token[:12]}", metadata=f'{{"health_check": "{token}"}}'))


async def check(db, now: datetime | None = None) -> None:
    global _last_livekit, _last_agent
    s = get_settings()
    if not (s.livekit_url and s.livekit_api_key):
        return
    now = now or datetime.utcnow()
    hour = now.strftime("%Y-%m-%dT%H")

    # Probes that were not answered in time.
    for token, (sent, answered) in list(_pending.items()):
        if answered:
            del _pending[token]
        elif now - sent > AGENT_REPLY_WITHIN:
            del _pending[token]
            await alerts.raise_alert(db, None, "agent_down", "The call agent did not answer a test job",
                                     "Inbound calls are going to the fallback number (if set) or failing. "
                                     "Check the agent service on Railway.", dedupe_key=f"agent_down:{hour}")

    if _last_livekit is None or now - _last_livekit >= LIVEKIT_EVERY:
        _last_livekit = now
        error = await _livekit_ok()
        if error:
            await alerts.raise_alert(db, None, "livekit_down", "LiveKit is not answering", error,
                                     dedupe_key=f"livekit_down:{hour}")
            return

    every = s.health_agent_check_minutes
    if every and (_last_agent is None or now - _last_agent >= timedelta(minutes=every)):
        _last_agent = now
        token = secrets.token_urlsafe(16)
        try:
            await _dispatch_probe(token)
            _pending[token] = [now, False]
        except Exception as e:
            logger.warning("health probe dispatch failed: %r", e)
