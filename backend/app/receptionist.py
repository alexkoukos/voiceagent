"""2.0 receptionist calls (inbound phone, web demo, outbound reminders) and the agent's tools.

Every tool returns a small dict the model reads; every decision and write happens here.
"""

import json
import re
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from livekit import api
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import admin_changes, alerts, booking, events, notifications, routing, texts
from app.config import get_settings
from app.languages import english_name, language_for_phone
from app.models import (
    Appointment, Call, CallStatus, Customer, Handoff, Message, Practice, RoutingEvent, Staff, TranscriptEntry,
    TranscriptRole, WaitlistEntry,
)
from app.prompts import build_receptionist_prompt, default_greeting

OUTCOME_PRIORITY = {
    "info_given": 1, "message_taken": 3, "confirmed": 4, "booked": 4, "rescheduled": 4, "cancelled": 4,
    "transferred": 5,
}
STALE_SECONDS = 180
DIALING_STALE_SECONDS = 120


class Busy(Exception):
    pass


class Blocked(Exception):
    """The caller is on the practice's blocked list (OP10): hang up without a word."""


class OverCap(Exception):
    """This month's cost reached the practice's cap (OP10)."""


# OP10: this many short, silent calls from one number in a day blocks it.
SPAM_CALLS = 3
SPAM_SECONDS = 8


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def set_outcome(call: Call, outcome: str) -> None:
    if OUTCOME_PRIORITY.get(outcome, 0) >= OUTCOME_PRIORITY.get(call.outcome or "", 0):
        call.outcome = outcome


def add_flag(call: Call, flag: str) -> None:
    if flag not in (call.flags or []):
        call.flags = [*(call.flags or []), flag]  # new list so the JSON column is saved


def call_language(practice: Practice, caller_number: str | None) -> str:
    """The practice's language, or English for a caller from a foreign number."""
    if caller_number and caller_number.startswith("+") and language_for_phone(caller_number) != "el":
        return "en"
    return practice.language


async def practice_for_number(db: AsyncSession, dialed_number: str) -> Practice | None:
    for practice in (await db.execute(select(Practice).where(Practice.offboarded_at.is_(None)))).scalars():
        if dialed_number in (practice.phone_numbers or []):
            return practice
    return None


async def active_calls(db: AsyncSession, practice_id: str | None = None) -> int:
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=get_settings().max_call_duration_seconds + STALE_SECONDS)
    q = select(func.count()).select_from(Call).where(or_(
        and_(Call.status == CallStatus.active, Call.created_at > cutoff),
        # Rings or browser joins take well under this; longer means the agent never started.
        and_(Call.status == CallStatus.dialing, Call.created_at > now - timedelta(seconds=DIALING_STALE_SECONDS)),
    ))
    if practice_id:
        q = q.where(Call.practice_id == practice_id)
    return (await db.execute(q)).scalar_one()


async def month_cost(db: AsyncSession, practice: Practice, now: datetime | None = None) -> float:
    now = now or datetime.utcnow()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    return float((await db.execute(select(func.coalesce(func.sum(Call.cost_estimate), 0)).where(
        Call.practice_id == practice.id, Call.created_at >= start))).scalar_one())


async def check_cost_cap(db: AsyncSession, practice: Practice) -> None:
    """Alert at 80% of the cap, refuse new calls at 100% (OP10)."""
    cap = practice.monthly_cost_cap_eur
    if not cap:
        return
    spent = await month_cost(db, practice)
    month = datetime.utcnow().strftime("%Y-%m")
    if spent >= 0.8 * cap:
        full = spent >= cap
        await alerts.raise_alert(
            db, practice, "cost_cap",
            f"{practice.name}: {'cost cap reached' if full else '80% of cost cap'} ({spent:.2f}€ / {cap:.2f}€)",
            "New calls are refused until next month or a higher cap." if full else "",
            dedupe_key=f"cost:{practice.id}:{month}:{'100' if full else '80'}",
        )
        await db.commit()
        if full:
            raise OverCap()


async def block_if_spam(db: AsyncSession, call: Call) -> bool:
    """Repeated short, silent calls from one number get it blocked (OP10). True if blocked now."""
    if not call.caller_number or call.practice_id is None:
        return False
    since = datetime.utcnow() - timedelta(days=1)
    calls = list((await db.execute(select(Call).where(
        Call.practice_id == call.practice_id, Call.caller_number == call.caller_number, Call.created_at >= since,
    ))).scalars())
    spoke = set((await db.execute(select(TranscriptEntry.call_id).where(
        TranscriptEntry.call_id.in_([c.id for c in calls]), TranscriptEntry.role == TranscriptRole.friend,
    ))).scalars())
    silent = [c for c in calls if c.id not in spoke and (c.duration_seconds or 0) <= SPAM_SECONDS
              and c.status in (CallStatus.completed, CallStatus.failed)]
    if len(silent) < SPAM_CALLS:
        return False
    practice = await db.get(Practice, call.practice_id)
    if call.caller_number in (practice.blocked_numbers or []):
        return False
    practice.blocked_numbers = [*(practice.blocked_numbers or []), call.caller_number]
    await alerts.raise_alert(db, practice, "spam_blocked", f"{practice.name}: blocked {call.caller_number}",
                             f"{len(silent)} silent calls under {SPAM_SECONDS}s in 24h. Unblock in the app if wrong.",
                             dedupe_key=f"spam:{practice.id}:{call.caller_number}")
    return True


def cap_line(practice: Practice, language: str) -> str:
    if language == "el":
        return f"{practice.name}. Δεν μπορούμε να απαντήσουμε αυτή τη στιγμή. Παρακαλούμε καλέστε ξανά αργότερα."
    return f"{practice.name}. We can't take your call right now. Please call again later."


def busy_line(practice: Practice, language: str) -> str:
    if language == "el":
        return f"{practice.name}. Όλες οι γραμμές είναι απασχολημένες. Παρακαλούμε καλέστε ξανά σε λίγα λεπτά."
    return f"{practice.name}. All our lines are busy. Please call again in a few minutes."


# --- starting calls ---


async def start_call(
    db: AsyncSession, practice: Practice, *, direction: str, caller_number: str | None
) -> tuple[Call, dict]:
    """Creates the call record for an inbound or web call and returns the agent's metadata.
    Raises Busy when the practice is at its concurrent-call cap (G8), Blocked for a blocked
    caller and OverCap past the monthly cost cap (OP10)."""
    if caller_number and caller_number in (practice.blocked_numbers or []):
        raise Blocked()
    await check_cost_cap(db, practice)
    if await active_calls(db, practice.id) >= practice.max_concurrent_calls:
        raise Busy()
    now = utcnow()
    language = call_language(practice, caller_number)
    state, _ = booking.hours_state(practice, now)
    customer = None
    if caller_number:
        customer = (await db.execute(
            select(Customer).where(Customer.practice_id == practice.id, Customer.phone == caller_number)
        )).scalar_one_or_none()
    call = Call(
        practice_id=practice.id,
        direction=direction,
        caller_number=caller_number,
        customer_id=customer.id if customer else None,
        persona="",
        scenario="",
        voice=practice.voice,
        language=language,
        max_duration_seconds=get_settings().max_call_duration_seconds,
        status=CallStatus.active if direction == "inbound" else CallStatus.dialing,
        started_at=datetime.utcnow() if direction == "inbound" else None,
        hours_state=state,
        flags=[],
    )
    db.add(call)
    await db.flush()
    upcoming = await booking.upcoming_for(db, practice, caller_number, now)
    await routing.log_call_start(db, call, state, known=customer is not None)
    await db.commit()
    await db.refresh(call)
    events.publish(f"practice:{practice.id}")
    return call, await build_metadata(db, practice, call, customer=customer, upcoming=upcoming)


async def build_metadata(
    db: AsyncSession,
    practice: Practice,
    call: Call,
    *,
    customer: Customer | None = None,
    upcoming: list[Appointment] | None = None,
) -> dict:
    now = utcnow()
    language = call.language or practice.language
    state, next_open = booking.hours_state(practice, now)
    staff = await booking.staff_of(db, practice.id)
    upcoming = upcoming or []
    rules = routing.rules_for(practice)
    purpose = await _purpose(db, practice, call, staff, language)
    _, admin = await _admin(db, call)

    def prompt_for(lang: str) -> str:
        prompt = build_receptionist_prompt(
            practice, now=now, caller_number=call.caller_number, language=lang, hours_state=state,
            next_open=next_open, customer_name=customer.name if customer else None,
            upcoming=[booking.describe(practice, a, staff, lang) for a in upcoming], staff=staff,
            purpose=purpose.get(lang) if purpose else None,
        )
        if admin:
            prompt += ("\n## Αλλαγές ρυθμίσεων\nΚαλεί από το κινητό του/της " + admin.name + ". Αν ζητήσει αλλαγή "
                       "(κλειστά, άδεια, ωράριο, τιμή, πληροφορία), κάλεσε πρώτα stop_recording, μετά ζήτα τον κωδικό PIN και κάλεσε admin_login. Μετά "
                       "admin_change με τα λόγια του, διάβασε το say_and_ask και περίμενε καθαρό «ναι» ή «όχι» πριν το "
                       "admin_confirm. Ποτέ μην επαναλάβεις τον κωδικό.\n" if lang == "el" else
                       "\n## Settings changes\nThis is " + admin.name + "'s registered mobile. If they ask for a change "
                       "(closure, leave, hours, a price, information), first call stop_recording, then ask for the PIN and call admin_login. Then "
                       "admin_change with their words, read say_and_ask, and wait for a clear yes or no before "
                       "admin_confirm. Never repeat the PIN.\n")
        if rules["language_switch"]:
            prompt += ("\nΑν ο πελάτης μιλά καθαρά αγγλικά στην πρώτη του απάντηση, κάλεσε route_call με language=en "
                       "και συνέχισε στα αγγλικά. Αν είναι ασαφές ή ακούγεται φωνή στο βάθος, ζήτα επανάληψη στα "
                       "ελληνικά· μην αλλάξεις γλώσσα.\n" if lang == "el" else
                       "\nIf the caller clearly speaks Greek on the first turn, call route_call with language=el "
                       "and continue in Greek. If unclear or a background voice is audible, ask them to repeat "
                       "in English; do not switch languages.\n")
        return prompt

    record = call.direction != "web"
    greeting = default_greeting(practice, language=language)
    instruction = None
    if purpose:
        instruction = purpose["greeting_" + ("el" if language == "el" else "en")]
    meta = {
        "mode": "receptionist",
        "call_id": call.id,
        "practice_id": practice.id,
        "direction": call.direction,
        "prompt": prompt_for(language),
        "prompts": {"el": prompt_for("el"), "en": prompt_for("en")},
        "greeting": greeting,
        "greeting_instruction": instruction,
        "voice": practice.voice,
        "language": language,
        "language_name": english_name(language),
        "max_duration_seconds": call.max_duration_seconds,
        "record": record,
        "emergency": {
            "enabled": rules["emergency"]["enabled"],
            "phrases": rules["emergency"]["phrases"],
            "script": routing.EMERGENCY_SCRIPT,
        },
        "handoff_timeout_seconds": rules["handoff"]["timeout_seconds"],
        # Words the transcriber should expect: staff, services, departments.
        "vocabulary": _vocabulary(practice, staff),
        "waitlist": bool((practice.reminders or {}).get("waitlist")),
    }
    if call.direction == "outbound":
        settings = get_settings()
        meta.update(
            dial_number=call.caller_number,
            sip_trunk_id=settings.sip_trunk_id,
            outbound_number=practice.outbound_number or settings.sip_outbound_number,
        )
    return meta


def _vocabulary(practice: Practice, staff) -> list[str]:
    words = [practice.name]
    for p in staff:
        words += [p.name, *(p.aliases or [])]
    words += [s.get("name", "") for s in practice.services or []]
    words += [d.get("name", "") for d in practice.departments or []]
    return [w for w in dict.fromkeys(words) if w][:60]


async def _purpose(db: AsyncSession, practice: Practice, call: Call, staff, language: str) -> dict | None:
    """Outbound calls: what the call is for, in both languages, plus the opening instruction."""
    if call.direction != "outbound" or not call.purpose:
        return None
    if call.purpose == "reminder":
        appt = await db.get(Appointment, call.appointment_id)
        if appt is None:
            return None
        el, en = booking.describe(practice, appt, staff, "el"), booking.describe(practice, appt, staff, "en")
        return {
            "el": (f"Αυτή είναι ΕΞΕΡΧΟΜΕΝΗ κλήση υπενθύμισης. Ο πελάτης ({appt.customer_name}) έχει ραντεβού "
                   f"{el['date_spoken']} στις {el['time']} για {el['service']} (appointment_id {appt.id}). "
                   "Ρώτα αν θα έρθει. Αν ναι, confirm_appointment. Αν θέλει αλλαγή, check_availability και "
                   "reschedule_appointment. Αν θέλει ακύρωση, cancel_appointment. Αν απαντήσει τηλεφωνητής, "
                   "κλείσε αμέσως με hang_up(silent=true) χωρίς να πεις τίποτα."),
            "en": (f"This is an OUTBOUND reminder call. The customer ({appt.customer_name}) has an appointment on "
                   f"{en['date_spoken']} at {en['time']} for {en['service']} (appointment_id {appt.id}). Ask whether "
                   "they'll come. Yes: confirm_appointment. Change: check_availability then reschedule_appointment. "
                   "Cancel: cancel_appointment. If voicemail answers, hang_up(silent=true) at once without speaking."),
            "greeting_el": (f"Ο πελάτης σήκωσε. Χαιρέτα, πες ότι είσαι ο ψηφιακός βοηθός από «{practice.name}» και "
                            f"ότι παίρνεις για να θυμίσεις το ραντεβού του {el['date_spoken']} στις {el['time']}. "
                            "Ρώτα αν θα έρθει."),
            "greeting_en": (f"They picked up. Say hello, that you're the digital assistant of \"{practice.name}\" "
                            f"calling about their appointment on {en['date_spoken']} at {en['time']}, and ask if "
                            "they'll make it."),
        }
    if call.purpose.startswith("waitlist:"):
        _, entry_id, slot = call.purpose.split(":", 2)
        entry = await db.get(WaitlistEntry, entry_id)
        if entry is None:
            return None
        day, hhmm = slot.split("T")
        d = date_cls.fromisoformat(day)
        service = booking.find_service(practice, entry.service_id) or {"name": entry.service_id}
        el_day, en_day = booking.say_date(d, "el"), booking.say_date(d, "en")
        return {
            "el": (f"Αυτή είναι ΕΞΕΡΧΟΜΕΝΗ κλήση: ο πελάτης ({entry.customer_name}) ήταν σε λίστα αναμονής για "
                   f"{service['name']}. Ελευθερώθηκε θέση {el_day} στις {hhmm} (date {day}, time {hhmm}, "
                   f"service_id {entry.service_id}). Πρότεινέ τη. Αν τη θέλει, επιβεβαίωσε και κάλεσε "
                   "check_availability, prepare_action, έπειτα book_appointment. Αν απαντήσει τηλεφωνητής, κλείσε αμέσως με hang_up(silent=true)."),
            "en": (f"This is an OUTBOUND call: the customer ({entry.customer_name}) was on the waitlist for "
                   f"{service['name']}. A slot opened on {en_day} at {hhmm} (date {day}, time {hhmm}, service_id "
                   f"{entry.service_id}). Offer it; if they want it, read back and book_appointment. If voicemail "
                   "answers, hang_up(silent=true) at once."),
            "greeting_el": (f"Ο πελάτης σήκωσε. Χαιρέτα, πες ότι είσαι ο ψηφιακός βοηθός από «{practice.name}» και "
                            f"ότι ελευθερώθηκε θέση {el_day} στις {hhmm}. Ρώτα αν τη θέλει."),
            "greeting_en": (f"They picked up. Say hello, that you're the digital assistant of \"{practice.name}\", "
                            f"and that a slot opened on {en_day} at {hhmm}. Ask if they want it."),
        }
    return None


async def dispatch(room_name: str, metadata: dict) -> None:
    settings = get_settings()
    async with api.LiveKitAPI(settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret) as lk:
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(agent_name=settings.agent_name, room=room_name, metadata=json.dumps(metadata))
        )


def room_token(room_name: str, identity: str, name: str, *, can_publish: bool = True) -> str:
    settings = get_settings()
    return (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_name(name)
        .with_grants(api.VideoGrants(room_join=True, room=room_name, can_publish=can_publish, can_subscribe=True))
        .to_jwt()
    )


def room_of(call: Call) -> str:
    return {"web": f"web-{call.id}", "outbound": f"call-{call.id}"}.get(call.direction, f"inbound-{call.id}")


# --- outbound: reminders and waitlist offers (use case C) ---


def _outbound_call(practice: Practice, phone: str, purpose: str, appointment_id: str | None = None) -> Call:
    return Call(
        practice_id=practice.id, direction="outbound", caller_number=phone, persona="", scenario="",
        voice=practice.voice, language=call_language(practice, phone),
        max_duration_seconds=min(180, get_settings().max_call_duration_seconds),
        status=CallStatus.queued, purpose=purpose, appointment_id=appointment_id, use_case="outbound", flags=[],
    )


async def outbound_allowed(db: AsyncSession, practice: Practice) -> bool:
    """No reminder or waitlist calls after offboarding or past the cost cap (OP7, OP10)."""
    if practice.offboarded_at:
        return False
    cap = practice.monthly_cost_cap_eur
    return not cap or await month_cost(db, practice) < cap


async def queue_reminder(db: AsyncSession, practice: Practice, appt: Appointment) -> Call | None:
    """Only customers with an existing appointment are ever called (G9)."""
    if not appt.customer_phone or appt.status != "booked" or not await outbound_allowed(db, practice):
        return None
    appt.reminder_status = "calling"
    call = _outbound_call(practice, appt.customer_phone, "reminder", appt.id)
    db.add(call)
    return call


async def offer_freed_slot(db: AsyncSession, practice: Practice, appt: Appointment) -> Call | None:
    """A cancellation freed a slot: call the oldest matching waitlist entry."""
    if not (practice.reminders or {}).get("waitlist") or not await outbound_allowed(db, practice):
        return None
    local = appt.starts_at.astimezone(ZoneInfo(practice.timezone))
    # A repeated cancellation must not call another person for the same freed slot.
    # Keep this lock through the caller's commit so simultaneous retries serialize.
    await booking._lock(db, practice)
    slot = f"{local.date().isoformat()}T{local.strftime('%H:%M')}"
    offered = await db.execute(select(RoutingEvent.id).where(
        RoutingEvent.practice_id == practice.id, RoutingEvent.kind == "waitlist_offer",
        RoutingEvent.value == appt.id,
    ).limit(1))
    if offered.first():
        return None
    entries = (await db.execute(
        select(WaitlistEntry).where(
            WaitlistEntry.practice_id == practice.id, WaitlistEntry.status == "waiting",
            WaitlistEntry.service_id == appt.service_id,
            WaitlistEntry.date_from <= local.date(), WaitlistEntry.date_to >= local.date(),
        ).order_by(WaitlistEntry.created_at)
    )).scalars()
    for entry in entries:
        if entry.phone == appt.customer_phone or not booking.in_part_of_day(local, entry.part_of_day):
            continue
        # G9: a waitlist entry alone does not authorize an outbound call.
        existing = (await db.execute(select(Appointment.id).where(
            Appointment.practice_id == practice.id, Appointment.customer_phone == entry.phone,
            Appointment.status == "booked", Appointment.starts_at > utcnow(),
        ).limit(1))).scalar_one_or_none()
        if existing is None:
            continue
        entry.status = "offered"
        call = _outbound_call(practice, entry.phone, f"waitlist:{entry.id}:{slot}")
        db.add(call)
        await db.flush()
        await routing.log(db, call, "waitlist_offer", appt.id, "B8 cancellation retry")
        return call
    return None


async def dispatch_outbound(db: AsyncSession, call: Call) -> None:
    practice = await db.get(Practice, call.practice_id)
    customer = (await db.execute(
        select(Customer).where(Customer.practice_id == practice.id, Customer.phone == call.caller_number)
    )).scalar_one_or_none()
    metadata = await build_metadata(db, practice, call, customer=customer)
    try:
        await dispatch(room_of(call), metadata)
        call.status = CallStatus.dialing
    except Exception:
        call.status = CallStatus.failed
        call.end_reason = "error"
        call.ended_at = datetime.utcnow()
    await db.commit()
    if call.status == CallStatus.failed:
        from app import finalize
        finalize.schedule(call.id)


# --- agent tools ---


async def _practice(db: AsyncSession, call: Call) -> Practice:
    return await db.get(Practice, call.practice_id)


def _lang(call: Call, practice: Practice) -> str:
    return call.language or practice.language


async def _department_staff(db: AsyncSession, practice: Practice, call: Call) -> list[str] | None:
    dep = await routing.department_of(db, practice, call)
    return dep.get("staff_ids") if dep and dep.get("staff_ids") else None


async def tool_route(db: AsyncSession, call: Call, args) -> dict:
    practice = await _practice(db, call)
    result = await routing.route(
        db, practice, call, intent=args.intent, staff_name=args.staff or "", department=args.department or "",
        language=args.language or "", now=utcnow(),
    )
    if result.get("path") == "emergency":
        await _emergency(db, practice, call)
    if result.get("path") == "end_call":
        add_flag(call, "off_topic")
    if args.intent in routing.BOOKING_INTENTS:
        call.use_case = "booking"
    elif call.use_case is None and call.direction != "outbound":
        call.use_case = "call_center"
    await db.commit()
    notifications.kick()
    return result


async def _emergency(db: AsyncSession, practice: Practice, call: Call) -> None:
    add_flag(call, "emergency")
    add_flag(call, "urgent")
    subject, body = texts.emergency_email(practice, call)
    await notifications.queue_business(db, practice, kind="urgent", subject=subject, body=body, call_id=call.id,
                                       urgent=True, dedupe_key=f"emergency:{call.id}")
    await alerts.raise_alert(db, practice, "emergency", subject, body, call_id=call.id,
                             dedupe_key=f"emergency:{call.id}")


async def tool_emergency(db: AsyncSession, call: Call) -> dict:
    """The agent heard an emergency phrase (deterministic match, R5)."""
    practice = await _practice(db, call)
    if "emergency" not in (call.flags or []):
        await routing.log(db, call, "emergency", "phrase", "R5 emergency phrase", "emergency")
        await _emergency(db, practice, call)
        await db.commit()
        notifications.kick()
    return {"say": routing.EMERGENCY_SCRIPT["el" if _lang(call, practice) == "el" else "en"]}


async def tool_check_availability(db: AsyncSession, call: Call, args) -> dict:
    practice = await _practice(db, call)
    call.use_case = call.use_case if call.use_case == "outbound" else "booking"
    exclude = args.appointment_id if getattr(args, "appointment_id", None) else None
    if exclude and exclude not in await _found_ids(db, call):
        return {"error": "call find_appointments first"}
    # The previous offer in this call: "νωρίτερα", "την επόμενη μέρα" are relative to it.
    last = (await db.execute(select(RoutingEvent).where(
        RoutingEvent.call_id == call.id, RoutingEvent.kind == "offer",
    ).order_by(RoutingEvent.created_at.desc()).limit(1))).scalar_one_or_none()
    last_offer = json.loads(last.value)["result"] if last else None
    result = await booking.check_availability(
        db, practice, args.when, args.service_id, utcnow(), staff_name=args.staff,
        staff_ids=await _department_staff(db, practice, call), language=_lang(call, practice), exclude_id=exclude,
        after=booking._hhmm(args.after) if args.after else None,
        before=booking._hhmm(args.before) if args.before else None,
        last_offer=last_offer,
    )
    if not result.get("error"):
        # Persist exactly what the backend offered so a later write cannot use an
        # invented date or time, even if the model misheard a second voice.
        db.add(RoutingEvent(practice_id=practice.id, call_id=call.id, kind="offer", value=json.dumps({
            "result": result, "appointment_id": exclude, "requested_staff": args.staff or "",
        }, ensure_ascii=False), rule="B1 availability offer"))
    await db.commit()
    return result


def _affirmative(text: str | None) -> bool:
    """Require an unambiguous yes in the caller's transcribed reply."""
    plain = booking._plain(text or "")
    words = set(re.findall(r"[\w]+", plain))
    if words & {"οχι", "μη", "δεν", "no", "not", "wait", "αλλα", "but"}:
        return False
    return bool(words & {"ναι", "σωστα", "βεβαια", "επιβεβαιωνω", "ενταξει", "οκ",
                         "yes", "correct", "confirm", "okay", "ok"})


async def _offered(db: AsyncSession, call: Call, *, day: str, time: str, service_id: str,
                   appointment_id: str | None, staff: str | None) -> bool:
    cutoff = datetime.utcnow() - timedelta(minutes=5)
    events = (await db.execute(select(RoutingEvent).where(
        RoutingEvent.call_id == call.id, RoutingEvent.kind == "offer", RoutingEvent.created_at >= cutoff,
    ).order_by(RoutingEvent.created_at.desc()))).scalars()
    for event in events:
        offer = json.loads(event.value)
        result = offer["result"]
        if (result.get("service_id") != service_id or offer.get("appointment_id") != appointment_id
                or booking._plain(offer.get("requested_staff") or "") != booking._plain(staff or "")):
            continue
        days = [result, *(result.get("next_days_with_free_times") or [])]
        if any(d.get("date") == day and time in (d.get("free_times") or []) for d in days):
            return True
    return False


async def tool_prepare_action(db: AsyncSession, call: Call, args) -> dict:
    """Produce the readback from trusted appointment data and arm one confirmation."""
    practice = await _practice(db, call)
    action = args.action
    appointment_id = args.appointment_id if action != "book" else None
    appt = None
    if action != "book":
        if not appointment_id or appointment_id not in await _found_ids(db, call):
            return {"error": "call find_appointments first"}
        appt = await db.get(Appointment, appointment_id)
        if appt is None or appt.practice_id != practice.id or appt.status != "booked":
            return {"error": "unknown_appointment"}
    if action == "cancel":
        day = appt.starts_at.astimezone(ZoneInfo(practice.timezone)).date().isoformat()
        time = appt.starts_at.astimezone(ZoneInfo(practice.timezone)).strftime("%H:%M")
        service_id, name, staff_name = appt.service_id, appt.customer_name, ""
    else:
        if not (args.date and args.time and args.service_id and (args.customer_name or appt)):
            return {"error": "missing_details"}
        day, time = args.date.isoformat(), args.time
        service_id = args.service_id
        name = (args.customer_name or appt.customer_name).strip()
        staff_name = args.staff or ""
        if not await _offered(db, call, day=day, time=time, service_id=service_id,
                              appointment_id=appointment_id, staff=staff_name):
            return {"error": "check_availability_first"}
        if appt and appt.service_id != service_id:
            return {"error": "service_mismatch"}
    service = booking.find_service(practice, service_id)
    if service is None:
        return {"error": "unknown_service"}
    staff = await booking.staff_of(db, practice.id)
    person = booking.match_staff(staff, staff_name) if staff_name else None
    staff_spoken = f", με {person.name}" if person else ""
    spoken_day = booking.say_date(date_cls.fromisoformat(day), _lang(call, practice))
    if _lang(call, practice) == "el":
        verb = "Να ακυρώσω" if action == "cancel" else "Να επιβεβαιώσω"
        line = f"{verb}: {name}, {spoken_day} στις {time}, για {service['name']}{staff_spoken}. Σωστά;"
    else:
        verb = "Shall I cancel" if action == "cancel" else "Please confirm"
        line = f"{verb}: {name}, {spoken_day} at {time}, for {service['name']}{staff_spoken}. Is that correct?"
    data = {"action": action, "date": day, "time": time, "service_id": service_id,
            "customer_name": name, "staff": staff_name, "appointment_id": appointment_id}
    event = RoutingEvent(practice_id=practice.id, call_id=call.id, kind="confirmation",
                         value=json.dumps(data, ensure_ascii=False), rule="B3 trusted readback", path="pending")
    db.add(event)
    await db.commit()
    return {"confirmation_id": event.id, "say": line}


async def _confirmed(db: AsyncSession, call: Call, args, action: str) -> bool:
    if not args.confirmation_id or not _affirmative(args.confirmation_text):
        return False
    event = await db.get(RoutingEvent, args.confirmation_id)
    if (event is None or event.call_id != call.id or event.kind != "confirmation"
            or event.created_at < datetime.utcnow() - timedelta(minutes=4)):
        return False
    data = json.loads(event.value)
    if data["action"] != action:
        return False
    if action == "book":
        matches = (data["date"] == args.date.isoformat() and data["time"] == args.time
                   and data["service_id"] == args.service_id
                   and data["customer_name"] == args.customer_name.strip()
                   and booking._plain(data["staff"]) == booking._plain(args.staff or ""))
    elif action == "reschedule":
        matches = (data["appointment_id"] == args.appointment_id and data["date"] == args.date.isoformat()
                   and data["time"] == args.time)
    else:
        matches = data["appointment_id"] == args.appointment_id
    if matches:
        event.path = "accepted"
        await routing.log(db, call, "confirmation_answer", args.confirmation_text.strip(), "B3 caller yes")
    return matches


async def _customer_sms(db: AsyncSession, practice: Practice, call: Call, kind: str, appt: Appointment) -> None:
    if not appt.customer_phone or (practice.notifications or {}).get("customer_sms") is False:
        return
    key = f"sms:{kind}:{appt.id}:{appt.starts_at.isoformat()}"
    if await notifications.exists(db, key):
        return
    staff = await booking.staff_of(db, practice.id)
    language = _lang(call, practice) if call else practice.language
    await notifications.queue(
        db, practice_id=practice.id, kind=f"{kind}_customer", channel="sms", recipient=appt.customer_phone,
        body=texts.customer_sms(practice, kind, booking.describe(practice, appt, staff, language), language),
        call_id=call.id if call else None, dedupe_key=key,
    )


async def tool_book(db: AsyncSession, call: Call, args) -> dict:
    call_id = call.id  # Rollback expires ORM attributes, including call.id.
    practice = await _practice(db, call)
    language = _lang(call, practice)
    source = "waitlist" if (call.purpose or "").startswith("waitlist:") else "agent"
    if not await _confirmed(db, call, args, "book"):
        return {"booked": False, "error": "confirmation_required"}
    try:
        appt = await booking.book(
            db, practice, day=args.date, start_time=args.time, service_id=args.service_id,
            customer_name=args.customer_name, customer_phone=args.customer_phone or call.caller_number,
            call_id=call.id, now=utcnow(), staff_name=args.staff,
            staff_ids=await _department_staff(db, practice, call), source=source,
        )
    except booking.BookingError as e:
        if e.code == "calendar_error":
            call = await db.get(Call, call_id)
            add_flag(call, "tool_error")
            await db.commit()
        return {"booked": False, "error": e.code, **e.details}
    call = await db.get(Call, call.id)
    practice = await _practice(db, call)
    set_outcome(call, "booked")
    call.appointment_id = appt.id
    if call.use_case != "outbound":
        call.use_case = "booking"
    if args.name_uncertain:
        add_flag(call, "name_uncertain")
    if source == "waitlist":
        entry = await db.get(WaitlistEntry, call.purpose.split(":")[1])
        if entry:
            entry.status = "booked"
    await _customer_sms(db, practice, call, "booked", appt)
    await db.commit()
    notifications.kick()
    staff = await booking.staff_of(db, practice.id)
    return {"booked": True, **booking.describe(practice, appt, staff, language)}


async def _found_ids(db: AsyncSession, call: Call) -> set[str]:
    rows = await db.execute(
        select(RoutingEvent.value).where(RoutingEvent.call_id == call.id, RoutingEvent.kind == "found")
    )
    return {v for (v,) in rows} | ({call.appointment_id} if call.appointment_id else set())


async def tool_find_appointments(db: AsyncSession, call: Call, args) -> dict:
    """Upcoming appointments for the caller's number (or the number they gave)."""
    practice = await _practice(db, call)
    from app.schemas import normalize_phone
    phone = normalize_phone(args.phone) if args.phone else call.caller_number
    if phone and not phone.startswith("+") and phone.startswith(("69", "2")) and len(phone) == 10:
        phone = "+30" + phone
    appts = await booking.upcoming_for(db, practice, phone, utcnow())
    staff = await booking.staff_of(db, practice.id)
    for a in appts:
        await routing.log(db, call, "found", a.id, "B5 lookup by phone")
    await db.commit()
    if not appts:
        return {"appointments": [], "hint": "None found for this number. Ask for the number they booked with."}
    return {"appointments": [booking.describe(practice, a, staff, _lang(call, practice)) for a in appts]}


async def tool_reschedule(db: AsyncSession, call: Call, args) -> dict:
    call_id = call.id
    practice = await _practice(db, call)
    if args.appointment_id not in await _found_ids(db, call):
        return {"error": "call find_appointments first"}
    if not await _confirmed(db, call, args, "reschedule"):
        return {"rescheduled": False, "error": "confirmation_required"}
    try:
        appt, old = await booking.reschedule(
            db, practice, appointment_id=args.appointment_id, day=args.date, start_time=args.time, now=utcnow()
        )
    except booking.BookingError as e:
        if e.code == "calendar_error":
            call = await db.get(Call, call_id)
            add_flag(call, "tool_error")
            await db.commit()
        return {"rescheduled": False, "error": e.code, **e.details}
    call = await db.get(Call, call.id)
    practice = await _practice(db, call)
    set_outcome(call, "rescheduled")
    call.appointment_id = appt.id
    if call.use_case != "outbound":
        call.use_case = "booking"
    if appt.reminder_status == "calling":
        appt.reminder_status = "confirmed"
    old_local = old.astimezone(ZoneInfo(practice.timezone))
    await routing.log(db, call, "action", f"rescheduled_from:{old_local.isoformat()}", "B5 reschedule")
    await _customer_sms(db, practice, call, "rescheduled", appt)
    await db.commit()
    notifications.kick()
    staff = await booking.staff_of(db, practice.id)
    return {"rescheduled": True, **booking.describe(practice, appt, staff, _lang(call, practice))}


async def tool_cancel(db: AsyncSession, call: Call, args) -> dict:
    call_id = call.id
    practice = await _practice(db, call)
    if args.appointment_id not in await _found_ids(db, call):
        return {"error": "call find_appointments first"}
    if not await _confirmed(db, call, args, "cancel"):
        return {"cancelled": False, "error": "confirmation_required"}
    try:
        appt = await booking.cancel(db, practice, appointment_id=args.appointment_id)
    except booking.BookingError as e:
        if e.code == "calendar_error":
            call = await db.get(Call, call_id)
            add_flag(call, "tool_error")
            await db.commit()
        return {"cancelled": False, "error": e.code}
    call = await db.get(Call, call.id)
    practice = await _practice(db, call)
    set_outcome(call, "cancelled")
    call.appointment_id = appt.id
    if call.use_case != "outbound":
        call.use_case = "booking"
    if appt.reminder_status == "calling":
        appt.reminder_status = "cancelled"
    await _customer_sms(db, practice, call, "cancelled", appt)
    await offer_freed_slot(db, practice, appt)
    await db.commit()
    notifications.kick()
    from app.dispatcher import start_next_queued
    await start_next_queued(db)
    return {"cancelled": True}


async def tool_confirm(db: AsyncSession, call: Call, args) -> dict:
    if args.appointment_id not in await _found_ids(db, call):
        return {"error": "call find_appointments first"}
    appt = await db.get(Appointment, args.appointment_id)
    if appt is None or appt.status != "booked":
        return {"confirmed": False, "error": "unknown_appointment"}
    appt.reminder_status = "confirmed"
    set_outcome(call, "confirmed")
    call.appointment_id = appt.id
    await db.commit()
    return {"confirmed": True}


async def tool_take_message(db: AsyncSession, call: Call, args) -> dict:
    practice = await _practice(db, call)
    staff = await booking.staff_of(db, practice.id)
    person = booking.match_staff(staff, args.for_whom) if args.for_whom else None
    from app.schemas import normalize_phone
    m = Message(
        practice_id=practice.id, call_id=call.id, staff_id=person.id if person else None,
        caller_name=args.caller_name.strip(), callback_number=normalize_phone(args.callback_number) if args.callback_number else call.caller_number,
        reason=args.reason.strip(), best_time=args.best_time.strip(), urgent=args.urgent,
    )
    db.add(m)
    set_outcome(call, "message_taken")
    if call.use_case is None:
        call.use_case = "call_center"
    if args.urgent:
        add_flag(call, "urgent")
        await db.flush()
        subject, body = texts.urgent_message_email(practice, m)
        await notifications.queue_business(db, practice, kind="message", subject=subject, body=body, call_id=call.id,
                                           urgent=True, dedupe_key=f"urgent_message:{m.id}")
    await db.commit()
    notifications.kick()
    events.publish(f"practice:{practice.id}")
    return {"saved": True, "callback_number": m.callback_number}


async def tool_transfer(db: AsyncSession, call: Call, args) -> dict:
    """Hand the caller to a person: in-app join (W1) or SIP transfer to their mobile (C6)."""
    practice = await _practice(db, call)
    rules = routing.rules_for(practice)["handoff"]
    staff = await booking.staff_of(db, practice.id)
    person = booking.match_staff(staff, args.target) if args.target else None
    if person is None:
        person = next((p for p in staff if p.role in ("secretary", "owner", "doctor") and p.phone), None) if rules["mode"] == "sip" else None
    target = person.name if person else (args.target or "")
    mode = rules["mode"]
    if mode == "sip" and (call.direction != "inbound" or not (person and person.phone)):
        mode = "app"
    handoff = Handoff(practice_id=practice.id, call_id=call.id, staff_id=person.id if person else None,
                      room_name=room_of(call), mode=mode)
    db.add(handoff)
    await db.flush()
    if mode == "app":
        title, body = texts.handoff_push(practice, call, target)
        await notifications.queue_push(db, practice, kind="handoff", title=title, body=body, call_id=call.id,
                                       data={"handoff_id": handoff.id, "call_id": call.id},
                                       staff_id=person.id if person else None)
    await db.commit()
    notifications.kick()
    events.publish(f"practice:{practice.id}")
    out = {"handoff_id": handoff.id, "mode": mode, "target": target,
           "timeout_seconds": rules["timeout_seconds"]}
    if mode == "sip":
        out["transfer_to"] = f"tel:{person.phone}"
    return out


async def tool_handoff_result(db: AsyncSession, call: Call, args) -> dict:
    handoff = await db.get(Handoff, args.handoff_id)
    if handoff is None or handoff.call_id != call.id:
        return {"error": "unknown_handoff"}
    practice = await _practice(db, call)
    handoff.status = args.status
    handoff.resolved_at = datetime.utcnow()
    if args.status in ("joined", "transferred"):
        set_outcome(call, "transferred")
    else:
        staff = await booking.staff_of(db, practice.id)
        person = next((p for p in staff if p.id == handoff.staff_id), None)
        subject, body = texts.handoff_unanswered_email(practice, call, person.name if person else "")
        await notifications.queue_business(db, practice, kind="handoff_unanswered", subject=subject, body=body,
                                           call_id=call.id, urgent=True, dedupe_key=f"handoff:{handoff.id}")
        add_flag(call, "urgent")
    await db.commit()
    notifications.kick()
    events.publish(f"practice:{practice.id}")
    return {"ok": True}


async def tool_add_to_waitlist(db: AsyncSession, call: Call, args) -> dict:
    practice = await _practice(db, call)
    if not (practice.reminders or {}).get("waitlist"):
        return {"error": "waitlist_off"}
    service = booking.find_service(practice, args.service_id)
    if service is None:
        return {"error": "unknown_service"}
    today = utcnow().astimezone(ZoneInfo(practice.timezone)).date()
    resolved = booking.resolve_date(args.when, today)
    start = resolved.day or today
    phone = call.caller_number
    if not phone:
        return {"error": "no_phone"}
    db.add(WaitlistEntry(
        practice_id=practice.id, customer_name=args.customer_name.strip(), phone=phone, service_id=service["id"],
        date_from=start, date_to=start + timedelta(days=max(0, min(args.days, 30))), part_of_day=resolved.part_of_day,
    ))
    await db.commit()
    return {"added": True, "from": start.isoformat()}


async def tool_flag(db: AsyncSession, call: Call, flag: str) -> dict:
    if flag in ("name_uncertain", "recording_refused", "over_duration", "tool_error"):
        add_flag(call, flag)
        await db.commit()
    return {"ok": True}


# --- changes by phone (OP2): registered staff mobile + practice PIN ---


async def _admin(db: AsyncSession, call: Call) -> tuple[Practice, Staff | None]:
    practice = await db.get(Practice, call.practice_id)
    if call.direction != "inbound" or not practice.admin_pin_hash:
        return practice, None
    return practice, await admin_changes.sender_staff(db, practice, call.caller_number)


async def tool_admin_login(db: AsyncSession, call: Call, args) -> dict:
    practice, staff = await _admin(db, call)
    if staff is None:
        return {"error": "not_available"}
    if await routing._count(db, call, "admin_login", "fail") >= admin_changes.PIN_TRIES:
        return {"error": "locked"}
    await scrub_pins(db, call)
    if not admin_changes.pin_ok(practice, re.sub(r"\D", "", args.pin)):
        await routing.log(db, call, "admin_login", "fail", "OP2")
        failures = await routing._count(db, call, "admin_login", "fail") + 1
        if failures >= admin_changes.PIN_TRIES:
            await alerts.raise_alert(db, practice, "admin_pin", f"{practice.name}: {failures} wrong PINs",
                                     f"From {call.caller_number}.", call_id=call.id, dedupe_key=f"pin:{call.id}")
        await db.commit()
        return {"error": "wrong_pin", "tries_left": max(0, admin_changes.PIN_TRIES - failures)}
    await routing.log(db, call, "admin_login", "ok", "OP2")
    call.use_case = "admin"
    await db.commit()
    return {"ok": True, "staff": staff.name,
            "hint": "Ask what they want to change: closures, leave, hours, a price or information."}


async def scrub_pins(db: AsyncSession, call: Call) -> None:
    """A spoken PIN must not stay in the transcript: mask 4-6 digit runs in what the caller said."""
    for e in (await db.execute(select(TranscriptEntry).where(
        TranscriptEntry.call_id == call.id, TranscriptEntry.role == TranscriptRole.friend))).scalars():
        masked = re.sub(r"(?<!\d)(\d[\s-]?){3,5}\d(?!\d)", "••••", e.text)
        if masked != e.text:
            e.text = masked


async def _logged_in(db: AsyncSession, call: Call) -> bool:
    return await routing._count(db, call, "admin_login", "ok") > 0


async def tool_admin_change(db: AsyncSession, call: Call, args) -> dict:
    practice, staff = await _admin(db, call)
    if staff is None or not await _logged_in(db, call):
        return {"error": "login_first"}
    try:
        req = await admin_changes.request(db, practice, staff, args.request, channel="phone",
                                          now=utcnow(), call_id=call.id)
    except admin_changes.Rejected as e:
        return {"error": "not_understood", "say": e.reply}
    await db.commit()
    return {"say_and_ask": admin_changes.confirm_prompt(practice, req.readback, "phone")}


async def tool_admin_confirm(db: AsyncSession, call: Call, args) -> dict:
    practice, staff = await _admin(db, call)
    if staff is None or not await _logged_in(db, call):
        return {"error": "login_first"}
    req = await admin_changes.pending_for(db, practice, staff.phone)
    if req is None or req.call_id != call.id:
        await db.commit()
        return {"error": "nothing_pending"}
    reply = await admin_changes.decide(db, practice, req, args.yes)
    await routing.log(db, call, "admin_change", req.status, "OP2")
    await db.commit()
    notifications.kick()
    events.publish(f"practice:{practice.id}")
    return {"say": reply}
