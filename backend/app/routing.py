"""Call routing (PRD R1-R9): the backend decides where a call goes, from per-business
rules in `practices.routing_rules`; the model only reports what the caller wants.

Every decision is logged in `routing_events` with the rule that made it (R9).

routing_rules keys (all optional):
    emergency:  {"enabled": bool, "phrases": [...]}      R5, C4 (on by default for healthcare/vets)
    handoff:    {"enabled": bool, "mode": "app"|"sip", "timeout_seconds": 20,
                 "ask_twice": bool, "after_hours": bool}  R6, C6, C7
    after_hours: {"booking": bool, "message": bool}       R3, C5
    language_switch: bool                                 R7
"""

import re
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking import _plain, match_staff, say_date, staff_of
from app.models import Call, Practice, RoutingEvent, Staff

HEALTH_VERTICALS = {"dentist", "doctor", "clinic", "aesthetic", "physio", "vet"}

EMERGENCY_PHRASES = {
    "el": ["δεν αναπνέει", "δεν ανασαίνει", "πόνος στο στήθος", "πονάει το στήθος", "λιποθύμησε",
           "λιποθυμάει", "χάνει τις αισθήσεις", "πολύ αίμα", "αιμορραγεί", "δεν απαντάει", "σπασμούς",
           "εγκεφαλικό", "έμφραγμα", "δεν μπορεί να αναπνεύσει", "πνίγεται", "αυτοκτον"],
    "en": ["not breathing", "can't breathe", "cannot breathe", "chest pain", "fainted", "unconscious",
           "bleeding a lot", "heavy bleeding", "seizure", "stroke", "heart attack", "choking", "suicid"],
}
EMERGENCY_SCRIPT = {
    "el": "Αυτό ακούγεται επείγον. Καλέστε αμέσως το 166 ή το 112. Θα ενημερώσω αμέσως και το ιατρείο.",
    "en": "That sounds like an emergency. Please call 112 right now. I'll let the practice know straight away.",
}

INTENTS = ("book", "change", "cancel", "confirm", "question", "message", "human", "emergency", "unclear", "off_topic")

# Said on the last off-topic / abusive turn before the agent hangs up.
END_LINE = {
    "el": "Αυτή η γραμμή είναι μόνο για ραντεβού και ερωτήσεις για την επιχείρηση. Δεν μπορώ να βοηθήσω σε κάτι άλλο, "
          "οπότε κλείνω την κλήση. Αν χρειαστείτε κάτι, καλέστε ξανά. Καλή συνέχεια.",
    "en": "This line is only for appointments and questions about the business. I can't help with anything else, "
          "so I'll end the call now. If you need something, please call again. Goodbye.",
}
BOOKING_INTENTS = {"book", "change", "cancel", "confirm"}


def rules_for(practice: Practice) -> dict:
    r = dict(practice.routing_rules or {})
    health = practice.vertical in HEALTH_VERTICALS
    emergency = {"enabled": health, "phrases": [], **(r.get("emergency") or {})}
    emergency["phrases"] = list(emergency["phrases"]) + EMERGENCY_PHRASES["el"] + EMERGENCY_PHRASES["en"]
    return {
        "emergency": emergency,
        "handoff": {"enabled": True, "mode": "app", "timeout_seconds": 20, "ask_twice": True,
                    "after_hours": False, **(r.get("handoff") or {})},
        "after_hours": {"booking": True, "message": True, **(r.get("after_hours") or {})},
        # Off by default: the call keeps its language (Greek, or English for foreign numbers).
        "language_switch": r.get("language_switch", False),
        # Off-topic or abusive requests allowed before the agent ends the call.
        "off_topic_limit": int(r.get("off_topic_limit", 3)),
        "end_line": r.get("end_line") or {},
    }


def is_emergency(practice: Practice, text: str) -> bool:
    rules = rules_for(practice)["emergency"]
    if not rules["enabled"]:
        return False
    t = _plain(text)
    return any(_plain(p) in t for p in rules["phrases"])


async def log(db: AsyncSession, call: Call, kind: str, value: str, rule: str, path: str = "") -> None:
    db.add(RoutingEvent(practice_id=call.practice_id, call_id=call.id, kind=kind, value=value[:200],
                        rule=rule, path=path))


async def _count(db: AsyncSession, call: Call, kind: str, value: str | None = None) -> int:
    q = select(func.count()).select_from(RoutingEvent).where(RoutingEvent.call_id == call.id, RoutingEvent.kind == kind)
    if value is not None:
        q = q.where(RoutingEvent.value == value)
    return (await db.execute(q)).scalar_one()


async def department_of(db: AsyncSession, practice: Practice, call: Call) -> dict | None:
    """The department this call was routed to, if any (R8)."""
    last = (await db.execute(
        select(RoutingEvent.value).where(RoutingEvent.call_id == call.id, RoutingEvent.kind == "department")
        .order_by(RoutingEvent.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    return next((d for d in practice.departments or [] if d["id"] == last), None) if last else None


def match_department(practice: Practice, said: str) -> dict | None:
    words = set(re.findall(r"\w+", _plain(said)))
    for d in practice.departments or []:
        names = {d["id"], d.get("name", ""), *d.get("aliases", [])}
        tokens = {t for n in names for t in re.findall(r"\w+", _plain(n)) if len(t) >= 3}
        if any(w[:5] == t[:5] for w in words for t in tokens if len(w) >= 3):
            return d
    return None


def _role_or_staff(staff: list[Staff], said: str) -> Staff | None:
    return match_staff(staff, said) if said.strip() else None


async def route(
    db: AsyncSession,
    practice: Practice,
    call: Call,
    *,
    intent: str,
    staff_name: str = "",
    department: str = "",
    language: str = "",
    now: datetime,
) -> dict:
    """What the agent's route_call tool returns: the path to take and what to do next."""
    rules = rules_for(practice)
    intent = intent if intent in INTENTS else "unclear"
    out: dict = {}

    # R7: language switch, decided here so it's logged and bounded to el/en.
    if language in ("el", "en") and language != (call.language or practice.language):
        if rules["language_switch"]:
            call.language = language
            out["switch_language"] = language
            await log(db, call, "language", language, "R7 caller language")
        else:
            await log(db, call, "language", language, "R7 switch disabled")

    # R8: department.
    if department.strip():
        dep = match_department(practice, department)
        if dep:
            out["department"] = dep.get("name", dep["id"])
            out["services"] = [s["id"] for s in practice.services or [] if s["id"] in dep.get("service_ids", [])] or None
            await log(db, call, "department", dep["id"], "R8 department match")
        else:
            out["department_error"] = "unknown"
            out["departments"] = [d.get("name", d["id"]) for d in practice.departments or []]

    from app.booking import hours_state
    state, next_open = hours_state(practice, now)
    lang = call.language or practice.language

    if intent == "emergency":
        await log(db, call, "intent", intent, "R5 emergency", "emergency")
        out.update(path="emergency", say=EMERGENCY_SCRIPT["el" if lang == "el" else "en"],
                   next="Say the emergency line, then take an urgent message (take_message with urgent=true).")
        return out

    if intent == "off_topic":
        n = await _count(db, call, "intent", "off_topic") + 1
        if n >= rules["off_topic_limit"]:
            await log(db, call, "intent", intent, f"off-topic {n}/{rules['off_topic_limit']} -> end call", "end_call")
            key = "el" if lang == "el" else "en"
            out.update(path="end_call", say=rules["end_line"].get(key) or END_LINE[key])
            return out
        await log(db, call, "intent", intent, f"off-topic {n}/{rules['off_topic_limit']}", "refuse")
        out.update(path="refuse", next=(
            "Say briefly and politely that you can only help with this business (appointments, questions about it, "
            "messages), and ask what they need. Don't do what they asked, don't joke along, don't explain further."))
        return out

    if intent == "unclear":
        n = await _count(db, call, "intent", "unclear")
        if n == 0:
            await log(db, call, "intent", intent, "R1 clarify once", "clarify")
            out.update(path="clarify", next="Ask ONE short clarifying question: booking, a question, or a message?")
        else:
            await log(db, call, "intent", intent, "R1 unclear twice -> message", "message")
            out.update(path="message", next="Offer to take a message for the business (take_message).")
        return out

    if intent == "human":
        return out | await _route_human(db, practice, call, rules, state, staff_name, lang)

    if intent in BOOKING_INTENTS:
        path = "booking"
        if state != "open" and not rules["after_hours"]["booking"]:
            await log(db, call, "hours", state, "R3 after-hours booking off", "message")
            out.update(path="message", next="Say the business is closed now and offer to take a message.")
            return out
        await log(db, call, "intent", intent, "R1 booking intent", path)
        if staff_name.strip():
            staff = await staff_of(db, practice.id)
            person = match_staff(staff, staff_name)
            if person and person.bookable:
                out["staff"] = person.name
                await log(db, call, "staff", person.name, "R2 staff alias", path)
            elif any(w in _plain(staff_name) for w in ("οποιο", "ελευθερ", "anyone", "whoever")):
                out["staff"] = "anyone"
                await log(db, call, "staff", "anyone", "R2 first free", path)
            else:
                out["staff_error"] = "unknown"
                out["staff_list"] = [p.name for p in staff if p.bookable]
        next_steps = {
            "book": "Find the service and day, then check_availability.",
            "change": "Call find_appointments, confirm which one, then check_availability and reschedule_appointment.",
            "cancel": "Call find_appointments, confirm which one, then cancel_appointment.",
            "confirm": "Call find_appointments and confirm the appointment with confirm_appointment.",
        }
        out.update(path=path, next=next_steps[intent])
        return out

    if intent == "message":
        await log(db, call, "intent", intent, "R1 message", "message")
        out.update(path="message", next="Take the message with take_message.")
        return out

    # question
    await log(db, call, "intent", intent, "R1 question", "call_center")
    out.update(path="call_center",
               next="Answer only from the business details. If it isn't there, say so and offer a message.")
    if state != "open":
        out["hours_state"] = state
        if next_open:
            out["opens"] = f"{say_date(next_open.date(), lang)} {next_open.strftime('%H:%M')}"
    return out


async def _route_human(
    db: AsyncSession, practice: Practice, call: Call, rules: dict, state: str, staff_name: str, lang: str
) -> dict:
    h = rules["handoff"]
    staff = await staff_of(db, practice.id)
    person = _role_or_staff(staff, staff_name)
    target = person.name if person else (staff_name.strip() or "")
    if not h["enabled"]:
        await log(db, call, "human", target, "R6 handoff disabled", "message")
        return {"path": "message", "next": "Say they can't come to the phone; take a message (take_message)."}
    if state != "open" and not h["after_hours"]:
        await log(db, call, "human", target, "R6 closed -> message", "message")
        return {"path": "message", "hours_state": state,
                "next": "Say the business is closed now; take a message (take_message) so they call back."}
    asked = await _count(db, call, "human")
    failed = "tool_error" in (call.flags or [])
    if h["ask_twice"] and asked == 0 and not failed:
        await log(db, call, "human", target, "R6 first ask -> offer help", "offer_help")
        who = target or ("ο γιατρός" if lang == "el" else "they")
        return {"path": "offer_help", "target": target,
                "next": f"Say {who} can't come to the phone right now, and offer to help yourself or take a "
                        "message. If they ask again, call route_call with intent human again."}
    await log(db, call, "human", target, "R6 handoff", "handoff")
    return {"path": "handoff", "target": target,
            "next": "Tell them you'll try to connect them, then call transfer_to_human."}


async def log_call_start(db: AsyncSession, call: Call, state: str, known: bool) -> None:
    await log(db, call, "hours", state, "R3 hours at call start")
    if known:
        await log(db, call, "known_caller", call.caller_number or "", "R4 known caller")
