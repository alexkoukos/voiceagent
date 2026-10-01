"""Internal sparring page: /sparring/<SPARRING_TOKEN> lets the team argue with the agent
in the browser, swearing both ways (backend/config/sparring_prompt.md).

Web only on purpose: there is no way to point it at a phone number, so the insults only
ever reach whoever opened the page. No recording; the transcript is kept like any call.
"""

import json
import secrets
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from livekit import api
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import receptionist
from app.config import get_settings
from app.database import get_db
from app.dispatcher import active_count
from app.models import Call, CallStatus
from app.routers.demo import PAGE
from app.schemas import VOICE_BY_GENDER

router = APIRouter(prefix="/sparring", tags=["sparring"])
PROMPT = (Path(__file__).resolve().parents[2] / "config" / "sparring_prompt.md").read_text(encoding="utf-8")
MAX_SECONDS = 300


def _check(token: str) -> None:
    expected = get_settings().sparring_token
    if not expected or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=404, detail="Not found")


class SparringIn(BaseModel):
    gender: Literal["female", "male"] | None = None


@router.get("/{token}", response_class=HTMLResponse)
async def page(token: str, embed: bool = False):
    _check(token)
    return PAGE.substitute(
        lang="el", page_class="embed" if embed else "", name="Καβγάς με τον Μπάμπη",
        eyebrow="ΕΣΩΤΕΡΙΚΗ ΔΟΚΙΜΗ", subtitle="Βρίσε τον και θα σου τα πει κι αυτός. Πες «στοπ» για να σταματήσει.",
        call="Ξεκίνα τον καβγά", hang_up="Τέλος",
        note="Μόνο από τον browser, χωρίς ηχογράφηση. Χωρίς ρατσισμό, απειλές ή αληθινά πρόσωπα.",
        s_ready="Έτοιμος", s_connecting="Σύνδεση…", s_live="Σε καβγά", s_ended="Τέλος",
        s_busy="Όλες οι γραμμές είναι απασχολημένες. Δοκίμασε σε λίγο.",
        s_error="Κάτι πήγε στραβά. Δοκίμασε ξανά.", s_mic_error="Δώσε πρόσβαση στο μικρόφωνο και δοκίμασε ξανά.",
        transcript_title="Ο καβγάς", transcript_hint="Οι φράσεις εμφανίζονται μόλις ολοκληρωθούν.",
        transcript_empty="Ο καβγάς θα εμφανιστεί εδώ.", you="Εσύ", assistant="Μπάμπης",
        new_messages="Νέες φράσεις ↓", voice_label="Φωνή", female="Γυναικεία", male="Ανδρική",
        default_gender="male",
    )


@router.post("/{token}/session")
async def session(token: str, payload: SparringIn | None = None, db: AsyncSession = Depends(get_db)):
    _check(token)
    settings = get_settings()
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('global-demo-admission'))"))
    if await active_count(db) >= settings.max_concurrent_calls:
        raise HTTPException(status_code=429, detail="busy")
    voice = VOICE_BY_GENDER[(payload.gender if payload and payload.gender else "male")]
    call = Call(direction="web", persona="", scenario="sparring", voice=voice, language="el",
                max_duration_seconds=MAX_SECONDS, status=CallStatus.dialing)
    db.add(call)
    await db.commit()
    room = f"spar-{call.id}"
    try:
        async with api.LiveKitAPI(settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret) as lk:
            await lk.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(
                agent_name="prank-caller", room=room, metadata=json.dumps({
                    "call_id": call.id, "prompt": PROMPT, "voice": voice, "language": "el",
                    "language_name": "Greek", "max_duration_seconds": MAX_SECONDS,
                    # The browser joins the room; nothing is dialled or recorded.
                    "test_no_dial": True,
                    # The agent cuts in while the caller talks (agent.heckle_while_talking).
                    "sparring": True,
                }),
            ))
    except Exception:
        call.status = CallStatus.failed
        call.end_reason = "error"
        await db.commit()
        raise HTTPException(status_code=502, detail="Could not start")
    return {"url": settings.livekit_url, "token": receptionist.room_token(room, f"caller-{call.id}", "Caller"),
            "call_id": call.id}
