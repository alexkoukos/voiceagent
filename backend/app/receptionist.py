"""2.0 receptionist calls (inbound phone, web demo, outbound reminders) and the agent's tools.

Every tool returns a small dict the model reads; every decision and write happens here.
"""

import json
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from livekit import api
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import booking, events, notifications, routing, texts
from app.config import get_settings
from app.languages import english_name, language_for_phone
from app.models import (
    Appointment, Call, CallStatus, Customer, Handoff, Message, Practice, RoutingEvent, WaitlistEntry,
)
from app.prompts import build_receptionist_prompt, default_greeting

OUTCOME_PRIORITY = {
    "info_given": 1, "message_taken": 3, "confirmed": 4, "booked": 4, "rescheduled": 4, "cancelled": 4,
    "transferred": 5,
}
STALE_SECONDS = 180


class Busy(Exception):
    pass


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
    for practice in (await db.execute(select(Practice))).scalars():
        if dialed_number in (practice.phone_numbers or []):
            return practice
    return None


async def active_calls(db: AsyncSession, practice_id: str | None = None) -> int:
    cutoff = datetime.utcnow() - timedelta(seconds=get_settings().max_call_duration_seconds + STALE_SECONDS)
    q = select(func.count()).select_from(Call).where(
        Call.status.in_([CallStatus.dialing, CallStatus.active]), Call.created_at > cutoff
    )
    if practice_id:
        q = q.where(Call.practice_id == practice_id)
    return (await db.execute(q)).scalar_one()


def busy_line(practice: Practice, language: str) -> str:
    if language == "el":
        return f"{practice.name}. Όλες οι γραμμές είναι απασχολημένες. Παρακαλούμε καλέστε ξανά σε λίγα λεπτά."
    return f"{practice.name}. All our lines are busy. Please call again in a few minutes."


# --- starting calls ---


async def start_call(
    db: AsyncSession, practice: Practice, *, direction: str, caller_number: str | None
) -> tuple[Call, dict]:
    """Creates the call record for an inbound or web call and returns the agent's metadata.
    Raises Busy when the practice is at its concurrent-call cap (G8)."""
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

    def prompt_for(lang: str) -> str:
        return build_receptionist_prompt(
            practice, now=now, caller_number=call.caller_number, language=lang, hours_state=state,
            next_open=next_open, customer_name=customer.name if customer else None,
            upcoming=[booking.describe(practice, a, staff, lang) for a in upcoming], staff=staff,
            purpose=purpose.get(lang) if purpose else None,
        )

    record = call.direction != "web"
    greeting = default_greeting(practice, now=now, language=language, recording=record, hours_state=state,
                                after_hours=rules["after_hours"])
    instruction = None
    if purpose:
        instruction = purpose["greeting_" + ("el" if language == "el" else "en")]
    elif customer and customer.name:
        first = upcoming[0] if upcoming else None
        if language == "el":
            instruction = (f"Πες αυτόν τον χαιρετισμό: «{greeting}». Μετά χαιρέτα τον πελάτη με το όνομά του "
                           f"({customer.name}), φυσικά.")
            if first:
                d = booking.describe(practice, first, staff, language)
                instruction += (f" Πες ότι βλέπεις το ραντεβού του {d['date_spoken']} στις {d['time']} "
                                "και ρώτα αν παίρνει γι' αυτό, για αλλαγή ή επιβεβαίωση.")
        else:
            instruction = f"Say this greeting: \"{greeting}\". Then greet the caller by name ({customer.name})."
            if first:
                d = booking.describe(practice, first, staff, language)
                instruction += (f" Say you can see their appointment on {d['date_spoken']} at {d['time']} and ask "
                                "if they're calling about it, to change or confirm it.")
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
                   "κλείσε αμέσως με hang_up χωρίς να πεις τίποτα."),
            "en": (f"This is an OUTBOUND reminder call. The customer ({appt.customer_name}) has an appointment on "
                   f"{en['date_spoken']} at {en['time']} for {en['service']} (appointment_id {appt.id}). Ask whether "
                   "they'll come. Yes: confirm_appointment. Change: check_availability then reschedule_appointment. "
                   "Cancel: cancel_appointment. If voicemail answers, hang_up at once without speaking."),
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
                   "book_appointment. Αν απαντήσει τηλεφωνητής, κλείσε αμέσως με hang_up."),
            "en": (f"This is an OUTBOUND call: the customer ({entry.customer_name}) was on the waitlist for "
                   f"{service['name']}. A slot opened on {en_day} at {hhmm} (date {day}, time {hhmm}, service_id "
                   f"{entry.service_id}). Offer it; if they want it, read back and book_appointment. If voicemail "
                   "answers, hang_up at once."),
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


async def queue_reminder(db: AsyncSession, practice: Practice, appt: Appointment) -> Call | None:
    """Only customers with an existing appointment are ever called (G9)."""
    if not appt.customer_phone or appt.status != "booked":
        return None
    appt.reminder_status = "calling"
    call = _outbound_call(practice, appt.customer_phone, "reminder", appt.id)
    db.add(call)
    return call


async def offer_freed_slot(db: AsyncSession, practice: Practice, appt: Appointment) -> Call | None:
    """A cancellation freed a slot: call the oldest matching waitlist entry."""
    if not (practice.reminders or {}).get("waitlist"):
        return None
    local = appt.starts_at.astimezone(ZoneInfo(practice.timezone))
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
        entry.status = "offered"
        call = _outbound_call(practice, entry.phone, f"waitlist:{entry.id}:{local.date().isoformat()}T{local.strftime('%H:%M')}")
        db.add(call)
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


async def tool_emergency(db: AsyncSession, call: Call) -> dict:
    """The agent heard an emergency phrase (deterministic match, R5)."""
    practice = await _practice(db, call)
    if "emergency" not in (call.flags or []):
        await routing.log(db, call, "emergency", "phrase", "R5 emergency phrase", "emergency")
        await _emergency(db, practice, call)
        await notifications.commit_ignoring_duplicates(db)
        notifications.kick()
    return {"say": routing.EMERGENCY_SCRIPT["el" if _lang(call, practice) == "el" else "en"]}


async def tool_check_availability(db: AsyncSession, call: Call, args) -> dict:
    practice = await _practice(db, call)
    call.use_case = call.use_case if call.use_case == "outbound" else "booking"
    exclude = args.appointment_id if getattr(args, "appointment_id", None) else None
    result = await booking.check_availability(
        db, practice, args.when, args.service_id, utcnow(), staff_name=args.staff,
        staff_ids=await _department_staff(db, practice, call), language=_lang(call, practice), exclude_id=exclude,
    )
    await db.commit()
    return result


async def _customer_sms(db: AsyncSession, practice: Practice, call: Call, kind: str, appt: Appointment) -> None:
    if not appt.customer_phone or (practice.notifications or {}).get("customer_sms") is False:
        return
    key = f"sms:{kind}:{appt.id}:{appt.starts_at.isoformat()}"
    if await notifications.exists(db, key):
        return
    staff = await booking.staff_of(db, practice.id)
    language = _lang(call, practice) if call else practice.language
    notifications.queue(
        db, practice_id=practice.id, kind=f"{kind}_customer", channel="sms", recipient=appt.customer_phone,
        body=texts.customer_sms(practice, kind, booking.describe(practice, appt, staff, language), language),
        call_id=call.id if call else None, dedupe_key=key,
    )


async def tool_book(db: AsyncSession, call: Call, args) -> dict:
    practice = await _practice(db, call)
    language = _lang(call, practice)
    source = "waitlist" if (call.purpose or "").startswith("waitlist:") else "agent"
    try:
        appt = await booking.book(
            db, practice, day=args.date, start_time=args.time, service_id=args.service_id,
            customer_name=args.customer_name, customer_phone=args.customer_phone or call.caller_number,
            call_id=call.id, now=utcnow(), staff_name=args.staff,
            staff_ids=await _department_staff(db, practice, call), source=source,
        )
    except booking.BookingError as e:
        if e.code == "calendar_error":
            call = await db.get(Call, call.id)
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
    await notifications.commit_ignoring_duplicates(db)
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
    practice = await _practice(db, call)
    if args.appointment_id not in await _found_ids(db, call):
        return {"error": "call find_appointments first"}
    try:
        appt, old = await booking.reschedule(
            db, practice, appointment_id=args.appointment_id, day=args.date, start_time=args.time, now=utcnow()
        )
    except booking.BookingError as e:
        if e.code == "calendar_error":
            call = await db.get(Call, call.id)
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
    await notifications.commit_ignoring_duplicates(db)
    notifications.kick()
    staff = await booking.staff_of(db, practice.id)
    return {"rescheduled": True, **booking.describe(practice, appt, staff, _lang(call, practice))}


async def tool_cancel(db: AsyncSession, call: Call, args) -> dict:
    practice = await _practice(db, call)
    if args.appointment_id not in await _found_ids(db, call):
        return {"error": "call find_appointments first"}
    try:
        appt = await booking.cancel(db, practice, appointment_id=args.appointment_id)
    except booking.BookingError as e:
        if e.code == "calendar_error":
            call = await db.get(Call, call.id)
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
    await notifications.commit_ignoring_duplicates(db)
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
    await notifications.commit_ignoring_duplicates(db)
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
