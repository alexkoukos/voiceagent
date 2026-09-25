"""Deterministic booking logic: dates, free slots and writes (PRD B1-B8).

The model only extracts what the caller wants ("την Τρίτη το απόγευμα", a service,
a name). Turning that into a date, listing free slots and writing the appointment is
done here, so a booking is never a model's guess.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import gcal
from app.models import Appointment, AppointmentStatus, Customer, Practice, Staff

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_EL = ("Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή")
WEEKDAY_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS_EL = ("Ιανουαρίου", "Φεβρουαρίου", "Μαρτίου", "Απριλίου", "Μαΐου", "Ιουνίου", "Ιουλίου",
             "Αυγούστου", "Σεπτεμβρίου", "Οκτωβρίου", "Νοεμβρίου", "Δεκεμβρίου")

DEFAULT_RULES = {
    "slot_step_minutes": 15,
    "buffer_minutes": 0,
    "max_days_ahead": 60,
    "min_notice_minutes": 60,
    "holidays": [],
}

# Parts of the day as callers say them -> (from, to) in local time.
PARTS_OF_DAY = {
    "morning": (time(0), time(12)),
    "noon": (time(12), time(15)),
    # Greek "απόγευμα" is after lunch, roughly 15:00 onwards (afternoon shop hours are 17-21).
    "afternoon": (time(15), time(21)),
    "evening": (time(19), time(23, 59)),
}


def _plain(s: str) -> str:
    """Lowercase, no accents, final sigma folded: 'Τρίτης' -> 'τριτησ'."""
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.replace("ς", "σ")


# Stems, matched against the start of each word after _plain().
_WEEKDAY_STEMS = (
    ("δευτερ", "monday", "mon"),
    ("τριτ", "tuesday", "tue"),
    ("τεταρτ", "wednesday", "wed"),
    ("πεμπτ", "thursday", "thu"),
    ("παρασκευ", "friday", "fri"),
    ("σαββατ", "saturday", "sat"),
    ("κυριακ", "sunday", "sun"),
)
_MONTH_STEMS = ("ιανουαρ", "φεβρουαρ", "μαρτ", "απριλ", "μαι", "ιουν", "ιουλ", "αυγουστ",
                "σεπτεμβρ", "οκτωβρ", "νοεμβρ", "δεκεμβρ")
_MONTHS_EN = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_PART_STEMS = (
    ("πρωι", "morning"), ("morning", "morning"),
    ("μεσημερ", "noon"), ("noon", "noon"), ("midday", "noon"),
    ("απογευμ", "afternoon"), ("afternoon", "afternoon"),
    ("βραδ", "evening"), ("evening", "evening"), ("night", "evening"),
)


@dataclass
class ResolvedDate:
    day: date | None
    part_of_day: str | None


def resolve_date(phrase: str, today: date) -> ResolvedDate:
    """'αύριο', 'μεθαύριο', 'την Τρίτη', 'την άλλη Τρίτη', 'την άλλη εβδομάδα', '15/10',
    '15 Οκτωβρίου', 'tomorrow', 'next Friday', '2026-10-15' -> a date (Europe/Athens 'today'
    is passed in). A bare weekday means its next occurrence after today. Returns day=None
    when nothing in the phrase is a date."""
    p = _plain(phrase)
    words = re.findall(r"[\w/.\-]+", p)
    part = next((part for w in words for stem, part in _PART_STEMS if w.startswith(stem)), None)

    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", p)
    if m:
        try:
            return ResolvedDate(date(int(m[1]), int(m[2]), int(m[3])), part)
        except ValueError:
            return ResolvedDate(None, part)
    # "15/10"; "5.30" is a time, not a date, so only valid dates count.
    for m in re.finditer(r"\b(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?\b", p):
        d = _day_month(today, int(m[1]), int(m[2]), m[3])
        if d:
            return ResolvedDate(d, part)
    for m in re.finditer(r"\b(\d{1,2})\s+(\w+)", p):
        month = next((i + 1 for i, stem in enumerate(_MONTH_STEMS) if m[2].startswith(stem)), None)
        month = month or next((i + 1 for i, stem in enumerate(_MONTHS_EN) if m[2].startswith(stem)), None)
        d = _day_month(today, int(m[1]), month, None) if month else None
        if d:
            return ResolvedDate(d, part)

    if any(w.startswith("μεθαυριο") for w in words) or "day after tomorrow" in p:
        return ResolvedDate(today + timedelta(days=2), part)
    if any(w.startswith("αυριο") for w in words) or "tomorrow" in words:
        return ResolvedDate(today + timedelta(days=1), part)
    if any(w.startswith("σημερα") for w in words) or "today" in words or "tonight" in words:
        return ResolvedDate(today, part)

    next_week = any(w.startswith(("αλλη", "επομενη", "ερχομενη")) for w in words) or "next" in words
    weekday = next(
        (i for w in words for i, (el, en, _) in enumerate(_WEEKDAY_STEMS) if w.startswith(el) or w == en),
        None,
    )
    monday_next_week = today + timedelta(days=7 - today.weekday())
    if weekday is not None:
        if next_week:
            return ResolvedDate(monday_next_week + timedelta(days=weekday), part)
        ahead = (weekday - today.weekday()) % 7 or 7
        return ResolvedDate(today + timedelta(days=ahead), part)
    if next_week and any(w.startswith(("εβδομαδ", "βδομαδ", "week")) for w in words):
        return ResolvedDate(monday_next_week, part)
    return ResolvedDate(None, part)


def _day_month(today: date, day: int, month: int, year: str | None) -> date | None:
    try:
        if year:
            y = int(year)
            return date(y + 2000 if y < 100 else y, month, day)
        d = date(today.year, month, day)
        # "15/1" said in December means next January.
        return d if d >= today else date(today.year + 1, month, day)
    except ValueError:
        return None


def say_date(d: date, language: str = "el") -> str:
    """How the agent reads a date back: 'Τρίτη 14 Οκτωβρίου'."""
    if language == "el":
        return f"{WEEKDAY_EL[d.weekday()]} {d.day} {MONTHS_EL[d.month - 1]}"
    return f"{WEEKDAY_EN[d.weekday()]} {d.strftime('%B')} {d.day}"


def rules_for(practice: Practice) -> dict:
    return {**DEFAULT_RULES, **(practice.rules or {})}


def find_service(practice: Practice, service_id: str | None) -> dict | None:
    services = practice.services or []
    if not service_id:
        return services[0] if len(services) == 1 else None
    key = _plain(service_id)
    return next(
        (s for s in services if _plain(s["id"]) == key or _plain(s.get("name", "")) == key), None
    )


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def free_slots(
    practice: Practice,
    day: date,
    duration_minutes: int,
    busy: list[tuple[datetime, datetime]],
    now: datetime,
    hours: dict | None = None,
) -> list[datetime]:
    """Start times on `day` where a `duration_minutes` visit fits inside working hours,
    after the minimum notice, and clear of every busy interval (plus the buffer)."""
    rules = rules_for(practice)
    tz = ZoneInfo(practice.timezone)
    if day.isoformat() in rules["holidays"]:
        return []
    if day > now.astimezone(tz).date() + timedelta(days=rules["max_days_ahead"]):
        return []
    step = timedelta(minutes=rules["slot_step_minutes"])
    length = timedelta(minutes=duration_minutes)
    buffer = timedelta(minutes=rules["buffer_minutes"])
    earliest = now + timedelta(minutes=rules["min_notice_minutes"])
    slots = []
    for start_s, end_s in (hours if hours else practice.hours or {}).get(WEEKDAY_KEYS[day.weekday()], []):
        start = datetime.combine(day, _hhmm(start_s), tz)
        end = datetime.combine(day, _hhmm(end_s), tz)
        t = start
        while t + length <= end:
            if t >= earliest and not any(
                t < b_end + buffer and b_start - buffer < t + length for b_start, b_end in busy
            ):
                slots.append(t)
            t += step
    return slots


def in_part_of_day(slot: datetime, part: str | None) -> bool:
    if not part:
        return True
    lo, hi = PARTS_OF_DAY[part]
    return lo <= slot.timetz().replace(tzinfo=None) < hi


def hours_state(practice: Practice, now: datetime) -> tuple[str, datetime | None]:
    """("open" | "break" | "closed", when it next opens) at `now` (R3, C5)."""
    tz = ZoneInfo(practice.timezone)
    local = now.astimezone(tz)
    holidays = rules_for(practice)["holidays"]

    def spans(d: date) -> list[tuple[datetime, datetime]]:
        if d.isoformat() in holidays:
            return []
        return [
            (datetime.combine(d, _hhmm(a), tz), datetime.combine(d, _hhmm(b), tz))
            for a, b in (practice.hours or {}).get(WEEKDAY_KEYS[d.weekday()], [])
        ]

    today = spans(local.date())
    if any(a <= local < b for a, b in today):
        return "open", None
    next_open = None
    for i in range(0, 15):
        starts = [a for a, _ in spans(local.date() + timedelta(days=i)) if a > local]
        if starts:
            next_open = min(starts)
            break
    state = "break" if any(b <= local for _, b in today) and any(a > local for a, _ in today) else "closed"
    return state, next_open


# --- staff and calendars ---


@dataclass
class Resource:
    """Something bookable with its own calendar: a staff member, or the practice itself
    when it has no bookable staff."""
    staff: Staff | None
    hours: dict
    calendar_id: str | None

    @property
    def staff_id(self) -> str | None:
        return self.staff.id if self.staff else None


async def staff_of(db: AsyncSession, practice_id: str) -> list[Staff]:
    result = await db.execute(
        select(Staff).where(Staff.practice_id == practice_id, Staff.active.is_(True)).order_by(Staff.created_at)
    )
    return list(result.scalars())


def match_staff(staff: list[Staff], said: str) -> Staff | None:
    """'με τον Γιώργο' -> the staff member called Γιώργος (accents, case and endings ignored)."""
    words = [w for w in re.findall(r"\w+", _plain(said)) if len(w) >= 3]
    if not words:
        return None

    def close(a: str, b: str) -> bool:
        # Greek endings change with case: Γιώργος / Γιώργο / Γιώργου.
        stem = min(len(a), len(b)) - 1
        return stem >= 3 and a[:stem] == b[:stem] and abs(len(a) - len(b)) <= 2

    for person in staff:
        names = [person.name, person.role, *(person.aliases or [])]
        tokens = [t for n in names for t in re.findall(r"\w+", _plain(n)) if len(t) >= 3]
        if any(close(w, t) for w in words for t in tokens):
            return person
    return None


def resources_for(
    practice: Practice,
    staff: list[Staff],
    service_id: str,
    staff_id: str | None = None,
    staff_ids: list[str] | None = None,
) -> list[Resource]:
    bookable = [
        p for p in staff
        if p.bookable and (not p.service_ids or service_id in p.service_ids)
        and (staff_ids is None or p.id in staff_ids)
    ]
    if staff_id:
        bookable = [p for p in bookable if p.id == staff_id]
        return [Resource(p, p.hours or practice.hours or {}, p.calendar_id) for p in bookable]
    if not any(p.bookable for p in staff):
        return [Resource(None, practice.hours or {}, practice.calendar_id)]
    return [Resource(p, p.hours or practice.hours or {}, p.calendar_id) for p in bookable]


async def busy_intervals(
    db: AsyncSession,
    practice: Practice,
    start: datetime,
    end: datetime,
    resource: Resource | None = None,
    exclude_id: str | None = None,
) -> list[tuple[datetime, datetime]]:
    """Our own bookings plus whatever else is in the resource's Google Calendar
    (manual entries, doctoranytime sync)."""
    q = select(Appointment.starts_at, Appointment.ends_at).where(
        Appointment.practice_id == practice.id,
        Appointment.status == AppointmentStatus.booked,
        Appointment.starts_at < end,
        Appointment.ends_at > start,
    )
    if resource is not None and resource.staff is not None:
        q = q.where(Appointment.staff_id == resource.staff.id)
    if exclude_id:
        q = q.where(Appointment.id != exclude_id)
    busy = [(s, e) for s, e in (await db.execute(q)).all()]
    calendar_id = resource.calendar_id if resource is not None else practice.calendar_id
    if calendar_id:
        if not gcal.configured():
            raise BookingError("calendar_error")
        excluded = await db.get(Appointment, exclude_id) if exclude_id else None
        if (excluded and excluded.practice_id == practice.id and excluded.gcal_event_id
                and excluded.staff_id == (resource.staff_id if resource else None)):
            busy += await gcal.busy_except(calendar_id, start, end, excluded.gcal_event_id)
        else:
            busy += await gcal.busy(calendar_id, start, end)
    return busy


async def availability(
    db: AsyncSession,
    practice: Practice,
    day: date,
    service: dict,
    now: datetime,
    resources: list[Resource] | None = None,
    exclude_id: str | None = None,
) -> dict[datetime, list[Resource]]:
    """Free start times on `day`, each with the resources free at that time (in order)."""
    if resources is None:
        resources = resources_for(practice, await staff_of(db, practice.id), service["id"])
    tz = ZoneInfo(practice.timezone)
    start = datetime.combine(day, time(0), tz)
    buffer = timedelta(minutes=rules_for(practice)["buffer_minutes"])
    free: dict[datetime, list[Resource]] = {}
    for r in resources:
        # Closed days, holidays and dates outside the booking window need no I/O.
        if not free_slots(practice, day, service["duration_minutes"], [], now, r.hours):
            continue
        busy = await busy_intervals(db, practice, start - buffer, start + timedelta(days=1) + buffer, r, exclude_id)
        for slot in free_slots(practice, day, service["duration_minutes"], busy, now, r.hours):
            free.setdefault(slot, []).append(r)
    return dict(sorted(free.items()))


async def check_availability(
    db: AsyncSession,
    practice: Practice,
    when: str,
    service_id: str | None,
    now: datetime,
    *,
    staff_name: str | None = None,
    staff_ids: list[str] | None = None,
    language: str | None = None,
    exclude_id: str | None = None,
) -> dict:
    """What the agent's check_availability tool returns. `when` is the caller's own words;
    `staff_name` is who they asked for ("με τον Γιώργο"), empty for anyone free."""
    tz = ZoneInfo(practice.timezone)
    language = language or practice.language
    service = find_service(practice, service_id)
    if service is None:
        return {"error": "unknown_service", "services": [s["id"] for s in practice.services or []]}
    staff = await staff_of(db, practice.id)
    person = None
    if staff_name and staff_name.strip() and not _anyone(staff_name):
        person = match_staff(staff, staff_name)
        if person is None or not person.bookable:
            return {"error": "unknown_staff", "staff": [p.name for p in staff if p.bookable]}
    resources = resources_for(practice, staff, service["id"], person.id if person else None, staff_ids)
    if not resources:
        return {"error": "nobody_does_this_service", "service_id": service["id"]}

    today = now.astimezone(tz).date()
    resolved = resolve_date(when, today)
    if resolved.day is None:
        return {"error": "no_date", "hint": "Ask the caller which day they want."}
    if resolved.day < today:
        return {"error": "date_in_past", "date": resolved.day.isoformat()}

    slots = list(await availability(db, practice, resolved.day, service, now, resources, exclude_id))
    matching = [s for s in slots if in_part_of_day(s, resolved.part_of_day)]
    out = {
        "date": resolved.day.isoformat(),
        "date_spoken": say_date(resolved.day, language),
        "service_id": service["id"],
        "duration_minutes": service["duration_minutes"],
        "part_of_day": resolved.part_of_day,
        "free_times": [s.strftime("%H:%M") for s in matching][:12],
    }
    if person:
        out["staff"] = person.name
    if not matching:
        out["other_free_times_that_day"] = [s.strftime("%H:%M") for s in slots][:6]
        out["next_days_with_free_times"] = await _next_free_days(
            db, practice, resolved.day, service, now, resources=resources, language=language, exclude_id=exclude_id
        )
    return out


def _anyone(said: str) -> bool:
    p = _plain(said)
    return any(w in p for w in ("οποιο", "οποια", "ελευθερ", "αδιαφορ", "anyone", "any ", "whoever", "doesn"))


async def _next_free_days(
    db: AsyncSession,
    practice: Practice,
    after: date,
    service: dict,
    now: datetime,
    count: int = 2,
    *,
    resources: list[Resource] | None = None,
    language: str | None = None,
    exclude_id: str | None = None,
) -> list[dict]:
    found = []
    for i in range(1, 15):
        day = after + timedelta(days=i)
        if day > now.astimezone(ZoneInfo(practice.timezone)).date() + timedelta(days=rules_for(practice)["max_days_ahead"]):
            break
        slots = list(await availability(db, practice, day, service, now, resources, exclude_id))
        if slots:
            found.append({
                "date": day.isoformat(),
                "date_spoken": say_date(day, language or practice.language),
                "free_times": [s.strftime("%H:%M") for s in slots][:4],
            })
            if len(found) == count:
                break
    return found


class BookingError(Exception):
    def __init__(self, code: str, **details) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


async def _lock(db: AsyncSession, practice: Practice) -> None:
    # One calendar write at a time per practice; released at commit or rollback.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"book:{practice.id}"})


def _slot_start(practice: Practice, day: date, start_time: str) -> datetime:
    try:
        return datetime.combine(day, _hhmm(start_time), ZoneInfo(practice.timezone))
    except ValueError:
        raise BookingError("bad_time")


async def _slot_taken(
    db: AsyncSession, practice: Practice, day: date, starts_at: datetime, free: list[datetime],
    service: dict, now: datetime, resources: list[Resource], exclude_id: str | None = None,
) -> BookingError:
    await db.commit()  # nothing written; frees the lock
    later = [s for s in free if s > starts_at][:2] or free[:2]
    if not later:
        nxt = await _next_free_days(db, practice, day, service, now, count=1, resources=resources, exclude_id=exclude_id)
        return BookingError("slot_taken", alternatives=nxt)
    return BookingError(
        "slot_taken",
        alternatives=[{"date": day.isoformat(), "date_spoken": say_date(day, practice.language),
                       "free_times": [s.strftime("%H:%M") for s in later]}],
    )


async def upsert_customer(db: AsyncSession, practice: Practice, phone: str | None, name: str) -> Customer | None:
    if not phone:
        return None
    customer = (
        await db.execute(select(Customer).where(Customer.practice_id == practice.id, Customer.phone == phone))
    ).scalar_one_or_none()
    if customer is None:
        customer = Customer(practice_id=practice.id, phone=phone, name=name.strip())
        db.add(customer)
        await db.flush()
    elif name.strip():
        customer.name = name.strip()
    return customer


async def book(
    db: AsyncSession,
    practice: Practice,
    *,
    day: date,
    start_time: str,
    service_id: str,
    customer_name: str,
    customer_phone: str | None,
    call_id: str | None,
    now: datetime,
    staff_name: str | None = None,
    staff_ids: list[str] | None = None,
    source: str = "agent",
) -> Appointment:
    """Books one slot. Serialised per practice with a Postgres advisory lock, re-checks
    the slot inside the lock (B2), and is idempotent per call + slot (B8). With no staff
    named, the first staff member free at that time gets it (R2). Raises
    BookingError("slot_taken", alternatives=[...]) when the slot is gone."""
    service = find_service(practice, service_id)
    if service is None:
        raise BookingError("unknown_service")
    if not customer_name.strip():
        raise BookingError("missing_name")
    starts_at = _slot_start(practice, day, start_time)
    ends_at = starts_at + timedelta(minutes=service["duration_minutes"])
    # Same call, slot, service and person = a retry of the same booking (B8).
    key = f"{call_id}:{starts_at.isoformat()}:{service['id']}:{_plain(staff_name or '')}" if call_id else None

    await _lock(db, practice)
    if key:
        existing = (
            await db.execute(select(Appointment).where(Appointment.idempotency_key == key))
        ).scalar_one_or_none()
        if existing:
            await db.commit()  # nothing written; ends the transaction and frees the lock
            return existing

    staff = await staff_of(db, practice.id)
    person = None
    if staff_name and staff_name.strip() and not _anyone(staff_name):
        person = match_staff(staff, staff_name)
        if person is None:
            await db.commit()
            raise BookingError("unknown_staff", staff=[p.name for p in staff if p.bookable])
    resources = resources_for(practice, staff, service["id"], person.id if person else None, staff_ids)
    free = await availability(db, practice, day, service, now, resources)
    if starts_at not in free:
        raise await _slot_taken(db, practice, day, starts_at, list(free), service, now, resources)
    chosen = free[starts_at][0]

    customer = await upsert_customer(db, practice, customer_phone, customer_name)
    appt = Appointment(
        practice_id=practice.id,
        call_id=call_id,
        customer_name=customer_name.strip(),
        customer_phone=customer_phone,
        customer_id=customer.id if customer else None,
        staff_id=chosen.staff_id,
        service_id=service["id"],
        service_name=service.get("name", service["id"]),
        starts_at=starts_at,
        ends_at=ends_at,
        idempotency_key=key,
        source=source,
    )
    db.add(appt)
    try:
        await db.flush()
        if chosen.calendar_id and gcal.configured():
            appt.gcal_event_id = await gcal.create_event(
                chosen.calendar_id,
                summary=f"{appt.service_name}: {appt.customer_name}",
                description=f"Τηλέφωνο: {customer_phone or '-'}\nΚλείστηκε από τον ψηφιακό βοηθό.",
                start=starts_at,
                end=ends_at,
                timezone=practice.timezone,
            )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise BookingError("duplicate")
    except Exception:
        # Roll back locally and take a message (B7). A timed-out remote write may
        # still have succeeded; full Calendar/Postgres reconciliation is pending.
        await db.rollback()
        raise BookingError("calendar_error")
    return appt


async def _booked(
    db: AsyncSession, practice: Practice, appointment_id: str, *, allow_cancelled: bool = False,
) -> Appointment:
    # Refresh after acquiring the advisory lock; another transaction may have changed it.
    appt = await db.get(Appointment, appointment_id, populate_existing=True)
    statuses = [AppointmentStatus.booked, AppointmentStatus.cancelled] if allow_cancelled else [AppointmentStatus.booked]
    if appt is None or appt.practice_id != practice.id or appt.status not in statuses:
        await db.commit()
        raise BookingError("unknown_appointment")
    return appt


def _calendar_of(practice: Practice, staff: list[Staff], appt: Appointment) -> str | None:
    if appt.staff_id:
        person = next((p for p in staff if p.id == appt.staff_id), None)
        return person.calendar_id if person else None
    return practice.calendar_id


async def reschedule(
    db: AsyncSession, practice: Practice, *, appointment_id: str, day: date, start_time: str, now: datetime,
) -> tuple[Appointment, datetime]:
    """Moves a booked appointment to a new free slot with the same person (B5).
    Returns the appointment and its old start."""
    starts_at = _slot_start(practice, day, start_time)
    await _lock(db, practice)
    appt = await _booked(db, practice, appointment_id)
    if appt.starts_at == starts_at:
        await db.commit()
        return appt, appt.starts_at
    service = find_service(practice, appt.service_id) or {
        "id": appt.service_id, "duration_minutes": int((appt.ends_at - appt.starts_at).total_seconds() // 60)
    }
    staff = await staff_of(db, practice.id)
    resources = resources_for(practice, staff, service["id"], appt.staff_id)
    if not resources:
        await db.commit()
        raise BookingError("unknown_staff")
    free = await availability(db, practice, day, service, now, resources, exclude_id=appt.id)
    if starts_at not in free:
        raise await _slot_taken(db, practice, day, starts_at, list(free), service, now, resources, appt.id)
    old = appt.starts_at
    appt.starts_at = starts_at
    appt.ends_at = starts_at + timedelta(minutes=service["duration_minutes"])
    appt.updated_at = datetime.utcnow()
    try:
        calendar_id = _calendar_of(practice, staff, appt)
        if appt.gcal_event_id and not (calendar_id and gcal.configured()):
            raise BookingError("calendar_error")
        if calendar_id and appt.gcal_event_id and gcal.configured():
            await gcal.move_event(calendar_id, appt.gcal_event_id, appt.starts_at, appt.ends_at, practice.timezone)
        await db.commit()
    except Exception:
        await db.rollback()
        raise BookingError("calendar_error")
    return appt, old


async def cancel(db: AsyncSession, practice: Practice, *, appointment_id: str) -> Appointment:
    await _lock(db, practice)
    appt = await _booked(db, practice, appointment_id, allow_cancelled=True)
    if appt.status == AppointmentStatus.cancelled:
        await db.commit()
        return appt
    staff = await staff_of(db, practice.id)
    appt.status = AppointmentStatus.cancelled
    appt.updated_at = datetime.utcnow()
    try:
        calendar_id = _calendar_of(practice, staff, appt)
        if appt.gcal_event_id and not (calendar_id and gcal.configured()):
            raise BookingError("calendar_error")
        if calendar_id and appt.gcal_event_id and gcal.configured():
            await gcal.delete_event(calendar_id, appt.gcal_event_id)
        await db.commit()
    except Exception:
        await db.rollback()
        raise BookingError("calendar_error")
    return appt


async def upcoming_for(db: AsyncSession, practice: Practice, phone: str | None, now: datetime) -> list[Appointment]:
    if not phone:
        return []
    result = await db.execute(
        select(Appointment).where(
            Appointment.practice_id == practice.id,
            Appointment.customer_phone == phone,
            Appointment.status == AppointmentStatus.booked,
            Appointment.starts_at > now,
        ).order_by(Appointment.starts_at)
    )
    return list(result.scalars())


def describe(practice: Practice, appt: Appointment, staff: list[Staff], language: str) -> dict:
    local = appt.starts_at.astimezone(ZoneInfo(practice.timezone))
    out = {
        "appointment_id": appt.id,
        "date": local.date().isoformat(),
        "date_spoken": say_date(local.date(), language),
        "time": local.strftime("%H:%M"),
        "service_id": appt.service_id,
        "service": appt.service_name,
        "customer_name": appt.customer_name,
    }
    person = next((p for p in staff if p.id == appt.staff_id), None)
    if person:
        out["staff"] = person.name
    return out
