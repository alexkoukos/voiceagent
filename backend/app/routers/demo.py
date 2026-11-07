"""Web demo link (PRD W2): /demo/<slug> lets a prospect talk to their own configured
agent from the browser over WebRTC, no phone number needed.

Public on purpose: the slug is a shareable link, not a credential. Calls count against
MAX_CONCURRENT_CALLS and the duration cap.
"""

from html import escape
from pathlib import Path
from string import Template
from typing import Literal
from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import receptionist
from app.booking import WEEKDAY_KEYS, WEEKDAY_EN
from app.config import get_settings
from app.database import get_db
from app.dispatcher import active_count
from app.models import CallStatus, Practice

router = APIRouter(prefix="/demo", tags=["demo"])
PAGE = Template((Path(__file__).resolve().parents[1] / "templates" / "demo.html").read_text(encoding="utf-8"))


async def _practice(db: AsyncSession, slug: str) -> Practice:
    practice = (await db.execute(select(Practice).where(Practice.slug == slug, Practice.offboarded_at.is_(None)))).scalar_one_or_none()
    if practice is None:
        raise HTTPException(status_code=404, detail="Not found")
    return practice


def public_info(practice: Practice) -> dict:
    """Explicit public fields only; never serialize the practice or its knowledge base."""
    return {
        "name": practice.name,
        "timezone": practice.timezone,
        "services": [
            {key: service.get(key) for key in ("name", "duration_minutes", "price")}
            for service in practice.services or []
        ],
        "hours": {day: (practice.hours or {}).get(day, []) for day in WEEKDAY_KEYS},
    }


@router.get("/{slug}/info")
async def demo_info(slug: str, db: AsyncSession = Depends(get_db)):
    return public_info(await _practice(db, slug))


@router.get("/{slug}", response_class=HTMLResponse)
async def demo_page(slug: str, db: AsyncSession = Depends(get_db), embed: bool = False):
    practice = await _practice(db, slug)
    info = public_info(practice)
    # The page is in English; the assistant speaks the practice's language.
    speaks = ("It answers in Greek; say \u201cEnglish mode\u201d to switch to English. "
              if practice.language == "el" else "")
    service_rows = "".join(
        '<div class="info-row"><dt>' + escape(str(service["name"] or "—"))
        + '<small>' + escape(str(service["duration_minutes"] or "—")) + ' min'
        + '</small></dt><dd>' + escape(str(service["price"] or "Ask for pricing"))
        + '</dd></div>' for service in info["services"]
    )
    hours_rows = "".join(
        '<div class="info-row"><dt>' + escape(WEEKDAY_EN[i]) + '</dt><dd>'
        + escape(", ".join(f"{start}–{end}" for start, end in info["hours"][day]) or "Closed") + '</dd></div>'
        for i, day in enumerate(WEEKDAY_KEYS)
    )
    return PAGE.substitute(
        lang="en",
        page_class="embed" if embed else "",
        name=escape(practice.name),
        eyebrow="AI RECEPTIONIST DEMO",
        subtitle=speaks + "Ask about prices and opening hours, or try booking an appointment.",
        call="Start call",
        hang_up="Hang up",
        note="Allow microphone access and speak naturally.",
        s_ready="Ready to call",
        s_connecting="Connecting…",
        s_live="On the call",
        s_ended="Call ended",
        s_busy="All lines are busy. Try again shortly.",
        s_error="Something went wrong. Please try again.",
        s_mic_error="Allow microphone access and try again.",
        info_title="Services, prices & opening hours",
        info_note="The same information the assistant uses during your call.",
        services_title="Services & prices",
        hours_title="Opening hours",
        voice_label="Voice",
        voice_female="Female",
        voice_male="Male",
        timezone=escape(info["timezone"]),
        service_rows=service_rows,
        hours_rows=hours_rows,
    )


class DemoSessionRequest(BaseModel):
    gender: Literal["female", "male"] | None = None
    voice: Literal["Kore", "Zubenelgenubi"] | None = None


@router.post("/{slug}/session")
async def demo_session(slug: str, payload: DemoSessionRequest | None = None, db: AsyncSession = Depends(get_db)):
    practice = await _practice(db, slug)
    return await start_demo_session(practice, db, voice=(payload.voice or ({"male":"Zubenelgenubi", "female":"Kore"}.get(payload.gender))) if payload else None)


async def start_demo_session(practice: Practice, db: AsyncSession, *, dashboard_id: str | None = None, voice: str | None = None):
    settings = get_settings()
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('global-demo-admission'))"))
    if await active_count(db) >= settings.max_concurrent_calls:
        raise HTTPException(status_code=429, detail="busy")
    try:
        call, metadata = await receptionist.start_call(db, practice, direction="web", caller_number=None, voice=voice)
    except (receptionist.Busy, receptionist.OverCap):
        raise HTTPException(status_code=429, detail="busy")
    if voice is not None:
        metadata["voice"] = voice
        call.voice = voice
    if dashboard_id is not None:
        call.demo_dashboard_id = dashboard_id
    if voice is not None or dashboard_id is not None:
        await db.commit()
    room = receptionist.room_of(call)
    try:
        await receptionist.dispatch(room, metadata)
    except Exception:
        call.status = CallStatus.failed
        call.end_reason = "error"
        await db.commit()
        raise HTTPException(status_code=502, detail="Could not start the call")
    return {
        "url": settings.livekit_url,
        "token": receptionist.room_token(room, f"caller-{call.id}", "Caller"),
        "call_id": call.id,
    }


@router.get("/{slug}/widget.js")
async def widget(slug: str, db: AsyncSession = Depends(get_db)):
    """"Call us" button for the business's site (W3): <script src=".../demo/<slug>/widget.js" defer></script>"""
    practice = await _practice(db, slug)
    label = "Καλέστε μας" if practice.language == "el" else "Call us"
    return Response(WIDGET.replace("__SLUG__", slug).replace("__LABEL__", label),
                    media_type="application/javascript", headers={"Cache-Control": "public, max-age=300"})


WIDGET = r"""(function () {
  var src = document.currentScript && document.currentScript.src;
  var base = src ? src.replace(/\/demo\/.*$/, "") : "";
  var url = base + "/demo/__SLUG__?embed=1";
  var btn = document.createElement("button");
  btn.textContent = "\u260E __LABEL__";
  btn.setAttribute("style", "position:fixed;right:16px;bottom:16px;z-index:2147483646;padding:14px 20px;border:0;" +
    "border-radius:999px;background:#1f6f5c;color:#fff;font:600 16px system-ui,sans-serif;cursor:pointer;" +
    "box-shadow:0 6px 24px rgba(0,0,0,.2)");
  var frame = null;
  btn.onclick = function () {
    if (frame) { frame.remove(); frame = null; return; }
    frame = document.createElement("iframe");
    frame.src = url;
    frame.allow = "microphone; autoplay";
    frame.title = "__LABEL__";
    frame.setAttribute("style", "position:fixed;right:16px;bottom:80px;z-index:2147483647;width:min(380px,calc(100vw - 32px));" +
      "height:min(560px,calc(100vh - 110px));border:0;border-radius:20px;box-shadow:0 12px 40px rgba(0,0,0,.25);background:#fff");
    document.body.appendChild(frame);
  };
  document.body.appendChild(btn);
})();
"""
