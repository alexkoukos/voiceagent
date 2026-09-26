"""After hang-up: outcome, summary, cost, and the business's email (PRD Call log)."""

import asyncio
import logging
from datetime import datetime

import httpx
from sqlalchemy import select

from app import booking, events, notifications, texts
from app.config import get_settings
from app.database import async_session
from app.models import Appointment, Call, CallStatus, Message, Practice, RoutingEvent, TranscriptEntry, TranscriptRole

logger = logging.getLogger("finalize")

SUMMARY_PROMPT = {
    "el": ("Γράψε περίληψη 2 έως 4 προτάσεων στα ελληνικά για την επιχείρηση: ποιος πήρε, τι ήθελε και τι έγινε "
           "(ραντεβού, μήνυμα, πληροφορία). Μην αναφέρεις ιατρικές λεπτομέρειες ή συμπτώματα· μόνο τον λόγο με "
           "δύο γενικές λέξεις (π.χ. «για έλεγχο»). Μόνο το κείμενο της περίληψης.\n\nΣυνομιλία:\n{transcript}"),
    "en": ("Write a 2 to 4 sentence summary in English for the business: who called, what they wanted, what "
           "happened (booking, message, information). No medical details or symptoms; only the reason in a couple "
           "of general words. Only the summary text.\n\nConversation:\n{transcript}"),
}
_tasks: set[asyncio.Task] = set()


def schedule(call_id: str) -> None:
    t = asyncio.create_task(finalize(call_id))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def summarize(transcript: str, language: str) -> str | None:
    s = get_settings()
    if not s.gemini_api_key or not transcript.strip():
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{s.summary_model}:generateContent"
    prompt = SUMMARY_PROMPT["el" if language == "el" else "en"].format(transcript=transcript[:12000])
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, params={"key": s.gemini_api_key},
                                  json={"contents": [{"parts": [{"text": prompt}]}],
                                        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 300}})
            r.raise_for_status()
            parts = r.json()["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts).strip() or None
    except Exception:
        logger.exception("summary failed")
        return None


def decide_outcome(call: Call, caller_turns: int) -> str:
    if call.outcome:
        return call.outcome
    if call.status == CallStatus.failed:
        return "failed"
    if "tool_error" in (call.flags or []):
        return "failed"
    return "info_given" if caller_turns >= 2 else "abandoned"


def cost(call: Call) -> float | None:
    if call.duration_seconds is None:
        return None
    s = get_settings()
    per_min = s.cost_model_eur_per_min + (s.cost_telephony_eur_per_min if call.direction != "web" else 0)
    return round(call.duration_seconds / 60 * per_min, 4)


def wants_business_summary(practice: Practice, call: Call) -> bool:
    return {
        "inbound": True,
        "web": bool((practice.notifications or {}).get("web_summaries")),
        "outbound": True,
    }.get(call.direction, False)


def fallback_summary(call: Call, language: str) -> str:
    """Safe, non-medical summary while the text model is unavailable."""
    outcome = {
        "booked": ("κλείστηκε ραντεβού", "an appointment was booked"),
        "rescheduled": ("αλλάχθηκε ραντεβού", "an appointment was moved"),
        "cancelled": ("ακυρώθηκε ραντεβού", "an appointment was cancelled"),
        "message_taken": ("κρατήθηκε μήνυμα", "a message was taken"),
        "transferred": ("η κλήση συνδέθηκε με άνθρωπο", "the call was transferred"),
        "abandoned": ("η κλήση έληξε πριν ολοκληρωθεί", "the call ended before resolution"),
        "failed": ("η κλήση απέτυχε", "the call failed"),
    }.get(call.outcome or "", ("δόθηκαν πληροφορίες", "information was provided"))
    if language == "el":
        return f"Η κλήση ολοκληρώθηκε και {outcome[0]}. Δεν ήταν διαθέσιμη αναλυτική περίληψη."
    return f"The call ended and {outcome[1]}. A detailed summary was unavailable."


async def finalize(call_id: str) -> None:
    try:
        async with async_session() as db:
            # Agent retries and scheduler recovery can arrive together. Only one
            # transaction may summarize and queue this call's outcome.
            call = (await db.execute(select(Call).where(
                Call.id == call_id, Call.practice_id.is_not(None), Call.finalized.is_(False),
                Call.status.in_([CallStatus.completed, CallStatus.failed]),
            ).with_for_update(skip_locked=True))).scalar_one_or_none()
            if call is None:
                return
            practice = await db.get(Practice, call.practice_id)
            entries = list((await db.execute(
                select(TranscriptEntry).where(TranscriptEntry.call_id == call.id).order_by(TranscriptEntry.created_at)
            )).scalars())
            caller_turns = sum(1 for e in entries if e.role == TranscriptRole.friend)
            call.outcome = decide_outcome(call, caller_turns)
            if call.use_case is None:
                call.use_case = "booking" if call.outcome in ("booked", "rescheduled", "cancelled", "confirmed") else "call_center"
            if call.max_duration_seconds and (call.duration_seconds or 0) >= call.max_duration_seconds:
                call.flags = [*{*(call.flags or []), "over_duration"}]
            call.cost_estimate = cost(call)
            language = practice.language
            lines = [f"{'Πελάτης' if language == 'el' else 'Caller'}: {e.text}" if e.role == TranscriptRole.friend
                     else f"{'Βοηθός' if language == 'el' else 'Assistant'}: {e.text}" for e in entries]
            call.summary = await summarize("\n".join(lines), language)
            if not call.summary:
                call.summary = fallback_summary(call, language)
                call.flags = [*{*(call.flags or []), "summary_fallback"}]
            call.finalized = True
            if caller_turns == 0 and call.direction == "inbound":
                await db.flush()
                from app import receptionist
                await receptionist.block_if_spam(db, call)

            # One email per call to the business, within 60 s of hang-up (web demos only if asked).
            if wants_business_summary(practice, call):
                appt = await db.get(Appointment, call.appointment_id) if call.appointment_id else None
                staff = await booking.staff_of(db, practice.id)
                described = booking.describe(practice, appt, staff, language) if appt else None
                message = (await db.execute(
                    select(Message).where(Message.call_id == call.id).order_by(Message.created_at.desc()).limit(1)
                )).scalar_one_or_none()
                old = (await db.execute(
                    select(RoutingEvent.value).where(RoutingEvent.call_id == call.id, RoutingEvent.kind == "action")
                    .order_by(RoutingEvent.created_at.desc()).limit(1)
                )).scalar_one_or_none()
                old_when = None
                if old and old.startswith("rescheduled_from:"):
                    dt = datetime.fromisoformat(old.split(":", 1)[1])
                    old_when = f"{booking.say_date(dt.date(), language)} {dt.strftime('%H:%M')}"
                subject, body = texts.call_email(practice, call, described, message, old_when)
                await notifications.queue_business(db, practice, kind="call_summary", subject=subject, body=body,
                                                   call_id=call.id, dedupe_key=f"summary:{call.id}")
            if call.purpose == "reminder" and call.appointment_id:
                appt = await db.get(Appointment, call.appointment_id)
                if appt and appt.reminder_status == "calling":
                    appt.reminder_status = "no_answer" if call.status == CallStatus.failed else "called"
            if (call.purpose or "").startswith("waitlist:") and call.outcome != "booked":
                from app.models import WaitlistEntry
                entry = await db.get(WaitlistEntry, call.purpose.split(":")[1])
                if entry and entry.status == "offered":
                    entry.status = "waiting"
            await db.commit()
            notifications.kick()
            events.publish(call.id)
            events.publish(f"practice:{practice.id}")
    except Exception:
        logger.exception("finalize %s failed", call_id)
