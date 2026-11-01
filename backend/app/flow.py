"""The receptionist's conversation, run as a state machine in the backend (FLOW_ENGINE=fsm).

Each caller turn the worker sends the caller's words and one model call's reading of them
(`understood`: intent, service, day, time, name, ...). This module merges that into the call's
state (`calls.flow`), decides the next step with the same booking code the tools use, and
answers with what to say:

    {"say": "<the backend's sentence>"}             facts: offers, readback, booked, questions
    {"reply": "model", "then": "<next question>"}   free conversation: small talk, business questions
    {"end_call": true}                               goodbye: the worker says the closing line and hangs up
    {"handoff": "<who>"}                             the worker starts the transfer

The model never books, moves or cancels: a write needs the backend's readback and the caller's
own plain yes (`_affirmative`, checked against the stored transcript), as on the tool flow.
"""

import json
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app import booking, receptionist, schemas
from app.models import Call

TASKS = ("book", "change", "cancel", "message")
SLOTS = ("service", "staff", "day", "time", "name", "phone", "reason")

LINES = {
    "el": {
        "service": "Για ποια υπηρεσία; Έχουμε {services}.",
        "day": "Ποια μέρα θα σας βόλευε;",
        "name": "Ποιο είναι το ονοματεπώνυμό σας;",
        "phone": "Σε ποιο τηλέφωνο μπορούμε να σας βρούμε;",
        "reason": "Τι θέλετε να τους πω;",
        "message_name": "Με ποιο όνομα να αφήσω το μήνυμα;",
        "message_saved": "Το μήνυμα καταγράφηκε και θα σας καλέσουν πίσω. Θα θέλατε κάτι άλλο;",
        "help": "Πώς μπορώ να βοηθήσω;",
        "change_what": "Εντάξει. Τι θέλετε να αλλάξουμε;",
        "not_free": "Στις {time} δεν είναι ελεύθερα.",
        "nothing_that_day": "{day} δεν έχουμε ελεύθερη ώρα{part}.",
        "past_day": "Αυτή η μέρα έχει περάσει. Ποια άλλη μέρα θα σας βόλευε;",
        "closed": "Είμαστε κλειστά από {from_} έως {to}.",
        "away": "{who} λείπει από {from_} έως {to}.",
        "no_appointment": "Δεν βρίσκω ραντεβού στον αριθμό από τον οποίο καλείτε. Θέλετε να αφήσετε μήνυμα;",
        "found": "Βρήκα το ραντεβού σας: {service}, {day} {time}.",
        "taken": "Δυστυχώς αυτή η ώρα μόλις κλείστηκε.",
        "after_hours": "Αυτή την ώρα δεν κλείνουμε ραντεβού. Θέλετε να αφήσετε μήνυμα;",
        "unavailable": "Δεν μπορεί να έρθει στο τηλέφωνο αυτή τη στιγμή. Θέλετε να αφήσετε μήνυμα;",
        "transferring": "Σας συνδέω τώρα.",
        "waiting": "Προσπαθώ να τον βρω, μείνετε στη γραμμή.",
        "parts": {"morning": " το πρωί", "afternoon": " το απόγευμα", "evening": " το βράδυ"},
        "and": " και ",
    },
    "en": {
        "service": "Which service is it for? We offer {services}.",
        "day": "Which day would suit you?",
        "name": "Could I have your full name?",
        "phone": "Which number can we reach you on?",
        "reason": "What would you like me to tell them?",
        "message_name": "What name should I leave the message under?",
        "message_saved": "Your message is saved and they'll call you back. Is there anything else?",
        "help": "How can I help you?",
        "change_what": "All right. What would you like to change?",
        "not_free": "{time} isn't free.",
        "nothing_that_day": "On {day} we have nothing free{part}.",
        "past_day": "That day has passed. Which other day would suit you?",
        "closed": "We're closed from {from_} to {to}.",
        "away": "{who} is away from {from_} to {to}.",
        "no_appointment": "I can't find an appointment for the number you're calling from. Would you like to leave a message?",
        "found": "I found your appointment: {service}, {day} {time}.",
        "taken": "Sorry, that time has just been taken.",
        "after_hours": "We don't book appointments at this hour. Would you like to leave a message?",
        "unavailable": "They can't come to the phone right now. Would you like to leave a message?",
        "transferring": "I'm putting you through now.",
        "waiting": "I'm trying to reach them, please stay on the line.",
        "parts": {"morning": " in the morning", "afternoon": " in the afternoon", "evening": " in the evening"},
        "and": " and ",
    },
}
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class Turn:
    """One caller turn: the state, what was understood, and the steps taken (for the audit and evals)."""

    def __init__(self, db: AsyncSession, call: Call, practice, text: str, understood: dict):
        self.db, self.call, self.practice = db, call, practice
        self.text = text.strip()
        self.u = understood or {}
        self.st = dict(call.flow or {})
        self.lang = "el" if receptionist._lang(call, practice) == "el" else "en"
        self.actions: list[list] = []

    def line(self, key: str, **values) -> str:
        return LINES[self.lang][key].format(**values)

    async def tool(self, name: str, fn, args=None) -> dict:
        result = await (fn(self.db, self.call, args) if args is not None else fn(self.db, self.call))
        self.actions.append([name, args.model_dump(mode="json") if args is not None else {}, result])
        return result


async def turn(db: AsyncSession, call: Call, text: str, understood: dict) -> dict:
    call_id = call.id  # a tool's rollback expires ORM attributes
    practice = await receptionist._practice(db, call)
    t = Turn(db, call, practice, text, understood)
    out = await _decide(t)
    call = await db.get(Call, call_id)
    call.flow = t.st
    await db.commit()
    return {**out, "actions": t.actions, "state": _summary(t.st)}


async def _decide(t: Turn) -> dict:
    u, st = t.u, t.st
    intent = u.get("intent") or "none"

    # A readback is waiting: only the caller's own plain yes carries it out.
    if st.get("confirmation_id"):
        if receptionist._affirmative(t.text):
            result = await t.tool("confirm_pending", receptionist.tool_confirm_pending, schemas.ConfirmPendingArgs(
                confirmation_id=st["confirmation_id"], confirmation_text=t.text, execute=True))
            st.pop("confirmation_id")
            if result.get("say") and not result.get("error"):
                st.update(task=None, done=True)
                for slot in ("time", "offer", "appointment_id"):
                    st.pop(slot, None)
                return {"say": result["say"]}
            st.pop("time", None)
            st.pop("offer", None)
            lead = t.line("taken") if result.get("error") == "slot_taken" else ""
            return await _continue(t, lead=lead)
        st.pop("confirmation_id")
        if not any(u.get(slot) for slot in SLOTS) and not u.get("goodbye"):
            return {"say": t.line("change_what")}

    if u.get("goodbye"):
        return {"end_call": True}

    if intent == "abuse":
        result = await t.tool("route_call", receptionist.tool_route, schemas.RouteArgs(intent="off_topic"))
        if result.get("path") == "end_call":
            return {"say": result.get("say", ""), "end_call": True}
        return {"say": result["say"]} if result.get("say") else {"reply": "model"}

    if intent == "human":
        result = await t.tool("route_call", receptionist.tool_route,
                              schemas.RouteArgs(intent="human", staff=u.get("staff") or None))
        if result.get("path") == "handoff":
            return {"handoff": result.get("target") or u.get("staff") or "",
                    "say_transferring": t.line("transferring"), "say_waiting": t.line("waiting")}
        st["task"] = "message"
        return {"say": t.line("unavailable")}

    if intent in TASKS and st.get("task") != intent:
        st.update(task=intent, done=False)
        if intent != "message":
            result = await t.tool("route_call", receptionist.tool_route, schemas.RouteArgs(
                intent=intent, staff=u.get("staff") or None))
            if result.get("path") != "booking":
                st["task"] = "message"
                return {"say": t.line("after_hours")}
        else:
            await t.tool("route_call", receptionist.tool_route, schemas.RouteArgs(intent="message"))

    if intent == "question" and not st.get("asked_question"):
        st["asked_question"] = True
        await t.tool("route_call", receptionist.tool_route, schemas.RouteArgs(intent="question"))

    changed_day = u.get("day") and u.get("day") != st.get("day")
    for slot in SLOTS:
        if u.get(slot):
            st[slot] = u[slot]
    if changed_day:
        st.pop("offer", None)
        if not u.get("time"):
            st.pop("time", None)

    if intent in ("question", "smalltalk", "unclear", "other") and not _new_slots(u):
        # Free conversation: the model's own answer, then back to what the call still needs.
        nxt = await _continue(t, peek=True)
        return {"reply": "model", "then": nxt.get("say") if nxt else None}
    if not st.get("task"):
        return {"reply": "model"}
    return await _continue(t)


def _new_slots(u: dict) -> bool:
    return any(u.get(slot) for slot in SLOTS)


async def _continue(t: Turn, *, lead: str = "", peek: bool = False) -> dict | None:
    """The next step of the current task. With peek, only the next question (no side effects)."""
    task = t.st.get("task")
    if task in ("book", "change"):
        out = await _booking(t, peek=peek)
    elif task == "cancel":
        out = await _cancel(t, peek=peek)
    elif task == "message":
        out = await _message(t, peek=peek)
    else:
        return None if peek else {"reply": "model"}
    if out and lead and out.get("say"):
        out["say"] = f"{lead} {out['say']}"
    return out


async def _found(t: Turn, *, peek: bool) -> tuple[str | None, str]:
    """The caller's own upcoming appointment (by calling number): (id, a line saying which)."""
    if t.st.get("appointment_id") or peek:
        return t.st.get("appointment_id"), ""
    result = await t.tool("find_appointments", receptionist.tool_find_appointments, schemas.FindArgs())
    appts = result.get("appointments") or []
    if not appts:
        return None, ""
    a = appts[0]
    t.st.update(appointment_id=a["appointment_id"], service=a["service_id"])
    return a["appointment_id"], t.line("found", service=a["service"].lower() if t.lang == "el" else a["service"],
                                       day=booking.on_day(a["date_spoken"], t.lang), time=a["time_spoken"])


async def _booking(t: Turn, *, peek: bool) -> dict | None:
    st, lead = t.st, ""
    if st["task"] == "change":
        appointment_id, lead = await _found(t, peek=peek)
        if not appointment_id and not peek:
            st["task"] = "message"
            return {"say": t.line("no_appointment")}

    def ask(key: str, **values) -> dict:
        return {"say": " ".join(filter(None, [lead, t.line(key, **values)]))}

    services = t.practice.services or []
    service = booking.find_service(t.practice, st.get("service"))
    if service is None:
        if not services:
            return ask("day")
        names = [s.get("name", s["id"]).lower() if t.lang == "el" else s.get("name", s["id"]) for s in services]
        joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + LINES[t.lang]["and"] + names[-1]
        return ask("service", services=joined)
    st["service"] = service["id"]
    if not st.get("day"):
        return ask("day")
    if peek:
        return None

    offer = st.get("offer")
    time = _hhmm(st.get("time"))
    if time and not (offer and time in offer.get("free_times", [])):
        exact = await _availability(t, service, after=time, before=time)
        if time in (exact.get("free_times") or []):
            st["offer"] = {"date": exact["date"], "free_times": exact["free_times"]}
            offer = st["offer"]
        else:
            st.pop("time", None)
            spoken = booking.say_time(time, t.lang).removeprefix("στις ").removeprefix("στη ").removeprefix("at ")
            not_free = t.line("not_free", time=spoken)
            out = await _offer(t, service)
            out["say"] = " ".join(filter(None, [lead, not_free[:1].upper() + not_free[1:], out["say"]]))
            return out
    if not offer or not time:
        out = await _offer(t, service)
        if lead:
            out["say"] = f"{lead} {out['say']}"
        return out

    if st["task"] == "book":
        if not st.get("name"):
            return ask("name")
        if not t.call.caller_number and not st.get("phone"):
            return ask("phone")
    return await _readback(t, "book" if st["task"] == "book" else "reschedule", service, offer["date"], time)


async def _availability(t: Turn, service: dict, *, after: str | None = None, before: str | None = None) -> dict:
    return await t.tool("check_availability", receptionist.tool_check_availability, schemas.CheckAvailability(
        when=t.st["day"], service_id=service["id"], staff=t.st.get("staff") or None,
        appointment_id=t.st.get("appointment_id"), after=after, before=before))


async def _offer(t: Turn, service: dict) -> dict:
    """Free times for the caller's day, worded by the backend; the nearest days if it is full."""
    st = t.st
    result = await _availability(t, service)
    error = result.get("error")
    if error == "no_date":
        st.pop("day", None)
        return {"say": t.line("day")}
    if error == "date_in_past":
        st.pop("day", None)
        return {"say": t.line("past_day")}
    if error:
        return {"reply": "model"}
    if result.get("say"):
        st["offer"] = {"date": result["date"], "free_times": result["free_times"]}
        return {"say": result["say"]}
    lead = []
    if result.get("business_closed"):
        c = result["business_closed"]
        lead.append(t.line("closed", from_=booking.on_day(c["from"], t.lang), to=booking.on_day(c["to"], t.lang)))
    elif result.get("staff_away"):
        c = result["staff_away"]
        lead.append(t.line("away", who=result.get("staff", ""), from_=booking.on_day(c["from"], t.lang),
                           to=booking.on_day(c["to"], t.lang)))
    elif not result.get("free_times"):
        day = booking.on_day(result["date_spoken"], t.lang)
        part = LINES[t.lang]["parts"].get(result.get("part_of_day") or "", "")
        lead.append(t.line("nothing_that_day", day=day[:1].upper() + day[1:], part=part))
    candidates = []
    if result.get("other_free_times_that_day"):
        candidates.append({"date": result["date"], "date_spoken": result["date_spoken"],
                           "free_times": result["other_free_times_that_day"]})
    candidates += result.get("next_days_with_free_times") or []
    for c in candidates:
        line = booking.offer_line({"date_spoken": c["date_spoken"], "free_times": c["free_times"]}, t.lang)
        if line:
            st["offer"] = {"date": c["date"], "free_times": c["free_times"]}
            return {"say": " ".join(lead + [line])}
    return {"say": " ".join(lead + [t.line("day")])}


async def _readback(t: Turn, action: str, service: dict, day: str, time: str) -> dict:
    st = t.st
    result = await t.tool("prepare_action", receptionist.tool_prepare_action, schemas.PrepareAction(
        action=action, date=day, time=time, service_id=service["id"],
        customer_name=st.get("name") if action == "book" else None, staff=st.get("staff") or None,
        appointment_id=st.get("appointment_id") if action != "book" else None,
        customer_phone=st.get("phone") if action == "book" else None))
    if result.get("confirmation_id"):
        st["confirmation_id"] = result["confirmation_id"]
        return {"say": result["say"], "readback": True}
    error = result.get("error")
    if error == "name_is_staff":
        st.pop("name", None)
        return {"say": t.line("name")}
    if error == "phone_required":
        return {"say": t.line("phone")}
    if error == "check_availability_first":
        st.pop("offer", None)
        return await _offer(t, service)
    return {"reply": "model"}


async def _cancel(t: Turn, *, peek: bool) -> dict | None:
    appointment_id, lead = await _found(t, peek=peek)
    if peek:
        return None
    if not appointment_id:
        t.st["task"] = "message"
        return {"say": t.line("no_appointment")}
    result = await t.tool("prepare_action", receptionist.tool_prepare_action, schemas.PrepareAction(
        action="cancel", appointment_id=appointment_id))
    if result.get("confirmation_id"):
        t.st["confirmation_id"] = result["confirmation_id"]
        return {"say": " ".join(filter(None, [lead, result["say"]])), "readback": True}
    return {"reply": "model"}


async def _message(t: Turn, *, peek: bool) -> dict | None:
    st = t.st
    if not st.get("reason"):
        return {"say": t.line("reason")}
    if not st.get("name"):
        return {"say": t.line("message_name")}
    if peek:
        return None
    await t.tool("take_message", receptionist.tool_take_message, schemas.MessageArgs(
        caller_name=st["name"], reason=st["reason"], callback_number=st.get("phone") or None,
        urgent=bool(t.u.get("urgent")), for_whom=st.get("staff") or None))
    st.update(task=None, done=True)
    return {"say": t.line("message_saved")}


def _hhmm(value) -> str | None:
    m = HHMM.match(str(value or "").strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def _summary(st: dict) -> str:
    """What the model is told about the call so far, before the next turn."""
    keep = {k: st[k] for k in ("task", "service", "day", "time", "name", "done") if st.get(k)}
    if st.get("offer"):
        keep["offered"] = st["offer"]
    if st.get("confirmation_id"):
        keep["waiting_for"] = "the caller's yes or no to the readback"
    return json.dumps(keep, ensure_ascii=False)
