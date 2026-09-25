"""Web demo link (PRD W2): /demo/<slug> lets a prospect talk to their own configured
agent from the browser over WebRTC, no phone number needed.

Public on purpose, so the slug is the only secret; calls still count against
MAX_CONCURRENT_CALLS and the duration cap.
"""

from html import escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import receptionist
from app.config import get_settings
from app.database import get_db
from app.dispatcher import active_count
from app.models import CallStatus, Practice

router = APIRouter(prefix="/demo", tags=["demo"])


async def _practice(db: AsyncSession, slug: str) -> Practice:
    practice = (await db.execute(select(Practice).where(Practice.slug == slug))).scalar_one_or_none()
    if practice is None:
        raise HTTPException(status_code=404, detail="Not found")
    return practice


@router.get("/{slug}", response_class=HTMLResponse)
async def demo_page(slug: str, db: AsyncSession = Depends(get_db)):
    practice = await _practice(db, slug)
    greek = practice.language == "el"
    return PAGE.format(
        lang=practice.language,
        name=escape(practice.name),
        slug=escape(slug),
        subtitle="Μιλήστε με τον ψηφιακό βοηθό" if greek else "Talk to the digital assistant",
        call="Κλήση" if greek else "Call",
        hang_up="Τερματισμός" if greek else "Hang up",
        note=("Η κλήση γίνεται από τον browser· θα σας ζητηθεί το μικρόφωνο."
              if greek else "The call runs in your browser; it will ask for your microphone."),
        s_connecting="Σύνδεση…" if greek else "Connecting…",
        s_live="Σε κλήση" if greek else "On the call",
        s_ended="Η κλήση τελείωσε" if greek else "Call ended",
        s_busy="Όλες οι γραμμές είναι απασχολημένες. Δοκιμάστε σε λίγο." if greek else "All lines are busy. Try again shortly.",
        s_error="Κάτι πήγε στραβά. Δοκιμάστε ξανά." if greek else "Something went wrong. Please try again.",
    )


@router.post("/{slug}/session")
async def demo_session(slug: str, db: AsyncSession = Depends(get_db)):
    practice = await _practice(db, slug)
    settings = get_settings()
    if await active_count(db) >= settings.max_concurrent_calls:
        raise HTTPException(status_code=429, detail="busy")
    try:
        call, metadata = await receptionist.start_call(db, practice, direction="web", caller_number=None)
    except receptionist.Busy:
        raise HTTPException(status_code=429, detail="busy")
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


PAGE = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<style>
  :root {{ --bg: #f6f5f2; --card: #fff; --ink: #1c1c1a; --muted: #6b6a66; --accent: #1f6f5c; --danger: #b3261e; --line: #e4e2dc; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #141413; --card: #1e1e1c; --ink: #f1efe9; --muted: #a3a19b; --accent: #4fb89b; --danger: #f2716a; --line: #2e2e2b; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--ink);
         font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 16px; }}
  main {{ width: 100%; max-width: 420px; background: var(--card); border: 1px solid var(--line); border-radius: 20px;
         padding: 32px 24px; text-align: center; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 4px; }}
  p {{ margin: 0; color: var(--muted); }}
  button {{ margin-top: 28px; width: 100%; padding: 16px; border: 0; border-radius: 999px; font: inherit; font-weight: 600;
           color: #fff; background: var(--accent); cursor: pointer; }}
  button.end {{ background: var(--danger); }}
  button:disabled {{ opacity: .6; cursor: default; }}
  #status {{ margin-top: 16px; min-height: 1.5em; }}
  #log {{ margin-top: 20px; text-align: left; font-size: .95rem; max-height: 40vh; overflow-y: auto; }}
  #log div {{ padding: 6px 0; border-top: 1px solid var(--line); }}
  #log .agent {{ color: var(--accent); }}
  small {{ display: block; margin-top: 20px; color: var(--muted); }}
</style>
</head>
<body>
<main>
  <h1>{name}</h1>
  <p>{subtitle}</p>
  <button id="btn">{call}</button>
  <div id="status" aria-live="polite"></div>
  <div id="log"></div>
  <small>{note}</small>
</main>
<script src="https://cdn.jsdelivr.net/npm/livekit-client@2/dist/livekit-client.umd.min.js"></script>
<script>
const LK = window.LivekitClient;
const btn = document.getElementById("btn"), statusEl = document.getElementById("status"), log = document.getElementById("log");
let room = null;
const lines = new Map();
function setStatus(t) {{ statusEl.textContent = t; }}
function idle() {{ room = null; btn.textContent = "{call}"; btn.className = ""; btn.disabled = false; }}
function show(segments, participant) {{
  const agent = !participant || participant.identity !== room.localParticipant.identity;
  for (const s of segments) {{
    let el = lines.get(s.id);
    if (!el) {{ el = document.createElement("div"); el.className = agent ? "agent" : ""; log.appendChild(el); lines.set(s.id, el); }}
    el.textContent = s.text;
  }}
  log.scrollTop = log.scrollHeight;
}}
async function start() {{
  btn.disabled = true; setStatus("{s_connecting}"); log.innerHTML = ""; lines.clear();
  const res = await fetch("/demo/{slug}/session", {{ method: "POST" }});
  if (!res.ok) {{ setStatus(res.status === 429 ? "{s_busy}" : "{s_error}"); idle(); return; }}
  const {{ url, token }} = await res.json();
  room = new LK.Room();
  room.on(LK.RoomEvent.TrackSubscribed, (track) => {{
    if (track.kind === "audio") document.body.appendChild(track.attach());
  }});
  room.on(LK.RoomEvent.TranscriptionReceived, show);
  room.on(LK.RoomEvent.Disconnected, () => {{ setStatus("{s_ended}"); idle(); }});
  try {{
    await room.connect(url, token);
    await room.localParticipant.setMicrophoneEnabled(true);
    setStatus("{s_live}"); btn.textContent = "{hang_up}"; btn.className = "end"; btn.disabled = false;
  }} catch (e) {{
    console.error(e); setStatus("{s_error}"); if (room) room.disconnect(); idle();
  }}
}}
btn.onclick = () => room ? room.disconnect() : start();
</script>
</body>
</html>
"""
