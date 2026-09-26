"""Changes the business asks for in plain words, by SMS or by phone (PRD OP2).

"Κλειστά 10 έως 25 Αυγούστου", "ο Γιώργος λείπει Παρασκευή", "Τρίτη 10 με 2", "ο καθαρισμός
κάνει 55€". A text model only turns the words into a structured change; this code checks
it, reads it back, and applies it after a clear yes. Closures, leave and hours apply at once;
prices and information go to the founder's approval queue, like the magic link.
Only a registered staff mobile can ask, and by phone only after the practice PIN.
"""

import hashlib
import hmac
import logging
import re
import secrets
import unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import booking, config_changes, onboarding
from app.booking import WEEKDAY_EL, WEEKDAY_EN, WEEKDAY_KEYS, say_date
from app.models import AdminRequest, Practice, Staff

logger = logging.getLogger(__name__)

MANAGERS = ("owner", "doctor", "secretary")
PENDING_MINUTES = 30
PIN_TRIES = 3
YES = {"ναι", "ναί", "ne", "nai", "yes", "y", "ok", "οκ", "σωστα", "σωστο", "ν"}
NO = {"οχι", "ochi", "oxi", "no", "n", "ακυρο", "ακυρωση", "cancel", "ο"}

PARSE_PROMPT = """You turn a message from a business owner or employee into one settings change.
Today is {today} ({weekday}). Timezone Europe/Athens. Staff: {staff}. Services: {services}.
Message: "{text}"

Return JSON only, one of:
{{"action": "closure", "date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD", "staff_name": null or a staff name, "reason": null or short text}}
  (the business, or one person, is closed/away on these days; a single day has date_from = date_to)
{{"action": "remove_closure", "date_from": "YYYY-MM-DD", "staff_name": null or a staff name}}
{{"action": "hours", "days": {{"mon": [["09:00", "14:00"], ["17:00", "21:00"]], "sat": []}}}}
  (only the weekdays mentioned; [] means closed that day; keys mon tue wed thu fri sat sun)
{{"action": "price", "service_name": "as said", "price": "e.g. 55€"}}
{{"action": "info", "topic": "short topic", "text": "the information callers should hear"}}
{{"action": "unknown"}}
Dates: a weekday means the next one from today (today itself if it is that weekday).
Never invent dates or times that are not in the message."""


def _plain(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower().strip())
    return re.sub(r"[^\w]", "", "".join(c for c in s if unicodedata.category(c) != "Mn"))


def answer(text: str) -> bool | None:
    """True for yes, False for no, None for anything else."""
    p = _plain(text)
    return True if p in YES else False if p in NO else None


# --- PIN (phone) ---


def hash_pin(practice_id: str, pin: str) -> str:
    salt = secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), f"{practice_id}:{salt}".encode(), 200_000).hex()
    return f"{salt}${digest}"


def pin_ok(practice: Practice, pin: str) -> bool:
    if not practice.admin_pin_hash or not re.fullmatch(r"\d{4,6}", pin or ""):
        return False
    salt, digest = practice.admin_pin_hash.split("$", 1)
    got = hashlib.pbkdf2_hmac("sha256", pin.encode(), f"{practice.id}:{salt}".encode(), 200_000).hex()
    return hmac.compare_digest(got, digest)


# --- who may ask ---


async def sender_staff(db: AsyncSession, practice: Practice, phone: str | None) -> Staff | None:
    if not phone:
        return None
    return (await db.execute(select(Staff).where(
        Staff.practice_id == practice.id, Staff.phone == phone, Staff.active.is_(True)
    ))).scalars().first()


# --- parse, check, read back ---


class Rejected(Exception):
    def __init__(self, reply: str) -> None:
        super().__init__(reply)
        self.reply = reply


def _t(practice: Practice, el: str, en: str) -> str:
    return el if practice.language == "el" else en


async def parse(practice: Practice, staff: list[Staff], text: str, now: datetime) -> dict:
    local = now.astimezone(ZoneInfo(practice.timezone))
    prompt = PARSE_PROMPT.format(
        today=local.date().isoformat(), weekday=WEEKDAY_EN[local.weekday()], text=text.replace('"', "'")[:500],
        staff=", ".join(p.name for p in staff) or "none",
        services=", ".join(s.get("name", s["id"]) for s in practice.services or []) or "none",
    )
    try:
        return await onboarding._gemini_json([{"text": prompt}])
    except onboarding.ImportError_:
        return {"action": "unknown"}


def _day(value, today: date) -> date:
    try:
        d = date.fromisoformat(str(value))
    except ValueError:
        raise Rejected("")
    if d < today or d > today + timedelta(days=400):
        raise Rejected("")
    return d


async def plan(db: AsyncSession, practice: Practice, sender: Staff, text: str, now: datetime) -> tuple[dict, str]:
    """(checked change, readback). Raises Rejected with the reply when it can't be done."""
    staff = await booking.staff_of(db, practice.id)
    parsed = await parse(practice, staff, text, now)
    action = parsed.get("action")
    today = now.astimezone(ZoneInfo(practice.timezone)).date()
    manager = sender.role in MANAGERS
    lang = practice.language
    help_text = _t(practice,
                   "Δεν κατάλαβα. Παραδείγματα: «Κλειστά 10 έως 25 Αυγούστου», «Ο Γιώργος λείπει Παρασκευή», "
                   "«Τρίτη 10:00 με 14:00», «Ο καθαρισμός κάνει 55€».",
                   "I didn't understand. Examples: \"Closed 10 to 25 August\", \"George is off Friday\", "
                   "\"Tuesday 10:00 to 14:00\", \"Cleaning costs 55€\".")
    not_allowed = _t(practice, "Από αυτό το κινητό μπορείτε να δηλώσετε μόνο τη δική σας άδεια.",
                     "From this phone you can only set your own leave.")
    try:
        if action in ("closure", "remove_closure"):
            person = None
            if parsed.get("staff_name"):
                person = booking.match_staff(staff, str(parsed["staff_name"]))
                if person is None:
                    raise Rejected(_t(practice, f"Δεν βρήκα τον/την «{parsed['staff_name']}».",
                                      f"I couldn't find \"{parsed['staff_name']}\"."))
            if not manager:
                # Staff can only set their own leave; "I'm off Friday" means them.
                if person is None:
                    person = sender
                elif person.id != sender.id:
                    raise Rejected(not_allowed)
            who = person.name if person else _t(practice, "Όλη η επιχείρηση", "The whole business")
            d_from = _day(parsed.get("date_from"), today)
            if action == "remove_closure":
                match = next((c for c in booking.rules_for(practice)["closures"] or []
                              if c["from"] <= d_from.isoformat() <= c["to"]
                              and c.get("staff_id") == (person.id if person else None)), None)
                if match is None:
                    raise Rejected(_t(practice, "Δεν βρήκα κλειστές μέρες εκεί.", "No closure found on that day."))
                change = {"action": "remove_closure", "closure_id": match["id"]}
                span = f"{say_date(date.fromisoformat(match['from']), lang)} – {say_date(date.fromisoformat(match['to']), lang)}"
                readback = _t(practice, f"Αφαίρεση κλειστών ({who}): {span}.", f"Remove closure ({who}): {span}.")
            else:
                d_to = _day(parsed.get("date_to") or d_from, today)
                if d_to < d_from:
                    raise Rejected("")
                change = {"action": "closure", "date_from": d_from.isoformat(), "date_to": d_to.isoformat(),
                          "staff_id": person.id if person else None, "reason": (parsed.get("reason") or None)}
                span = say_date(d_from, lang) + ("" if d_to == d_from else f" – {say_date(d_to, lang)}")
                readback = _t(practice, f"Κλειστά ({who}): {span}.", f"Closed ({who}): {span}.")
        elif action == "hours" and manager:
            days = parsed.get("days") or {}
            if not days or any(k not in WEEKDAY_KEYS for k in days):
                raise Rejected(help_text)
            hours = {**(practice.hours or {}), **{k: [list(x) for x in v] for k, v in days.items()}}
            config_changes.validated(practice, {"hours": hours})
            names = WEEKDAY_EL if lang == "el" else WEEKDAY_EN
            parts = [f"{names[WEEKDAY_KEYS.index(k)]} "
                     + (", ".join(f"{a}-{b}" for a, b in v) or _t(practice, "κλειστά", "closed")) for k, v in days.items()]
            change = {"action": "hours", "hours": hours}
            readback = _t(practice, "Νέο ωράριο: ", "New hours: ") + "; ".join(parts) + "."
        elif action == "price" and manager:
            svc = booking.find_service(practice, str(parsed.get("service_name") or ""))
            if svc is None:
                wanted = booking._plain(str(parsed.get("service_name") or ""))
                svc = next((s for s in practice.services or [] if wanted and wanted in booking._plain(s.get("name", ""))), None)
            if svc is None or not parsed.get("price"):
                raise Rejected(_t(practice, "Δεν βρήκα αυτή την υπηρεσία.", "I couldn't find that service."))
            change = {"action": "price", "service_id": svc["id"], "price": str(parsed["price"])[:40]}
            readback = _t(practice, f"Τιμή «{svc['name']}»: {change['price']} (θα ελεγχθεί πριν ισχύσει).",
                          f"Price \"{svc['name']}\": {change['price']} (checked before it applies).")
        elif action == "info" and manager and parsed.get("topic") and parsed.get("text"):
            change = {"action": "info", "topic": str(parsed["topic"])[:80], "text": str(parsed["text"])[:1000]}
            readback = _t(practice, f"Πληροφορία «{change['topic']}»: {change['text']} (θα ελεγχθεί πριν ισχύσει).",
                          f"Information \"{change['topic']}\": {change['text']} (checked before it applies).")
        elif action in ("hours", "price", "info"):
            raise Rejected(not_allowed)
        else:
            raise Rejected(help_text)
    except Rejected as e:
        raise Rejected(e.reply or help_text)
    except Exception as e:  # validation of hours
        logger.info("admin change rejected: %s", e)
        raise Rejected(help_text)
    return change, readback


async def request(db: AsyncSession, practice: Practice, sender: Staff, text: str, *, channel: str,
                  now: datetime, call_id: str | None = None) -> AdminRequest:
    """Plans the change and stores it waiting for a yes. Raises Rejected with the reply."""
    change, readback = await plan(db, practice, sender, text, now)
    # A new request replaces anything still waiting from the same person.
    for old in (await db.execute(select(AdminRequest).where(
        AdminRequest.practice_id == practice.id, AdminRequest.sender == sender.phone,
        AdminRequest.status == "pending"))).scalars():
        old.status, old.decided_at = "cancelled", datetime.utcnow()
    req = AdminRequest(practice_id=practice.id, channel=channel, sender=sender.phone, staff_id=sender.id,
                       call_id=call_id, text=text[:1000], parsed=change, readback=readback)
    db.add(req)
    await db.flush()
    return req


async def pending_for(db: AsyncSession, practice: Practice, sender: str) -> AdminRequest | None:
    req = (await db.execute(select(AdminRequest).where(
        AdminRequest.practice_id == practice.id, AdminRequest.sender == sender, AdminRequest.status == "pending",
    ).order_by(AdminRequest.created_at.desc()))).scalars().first()
    if req and req.created_at < datetime.utcnow() - timedelta(minutes=PENDING_MINUTES):
        req.status, req.decided_at = "expired", datetime.utcnow()
        return None
    return req


async def decide(db: AsyncSession, practice: Practice, req: AdminRequest, yes: bool) -> str:
    """Applies or cancels; returns the reply to send."""
    req.decided_at = datetime.utcnow()
    if not yes:
        req.status = "cancelled"
        return _t(practice, "Εντάξει, δεν άλλαξε τίποτα.", "OK, nothing changed.")
    sender = await db.get(Staff, req.staff_id) if req.staff_id else None
    author, source = (sender.name if sender else req.sender), req.channel
    c = req.parsed
    if c["action"] == "closure":
        closure = await config_changes.add_closure(
            db, practice, date_from=date.fromisoformat(c["date_from"]), date_to=date.fromisoformat(c["date_to"]),
            staff_id=c.get("staff_id"), reason=c.get("reason"), source=source, author=author)
        req.status = "applied"
        n = len(await config_changes.to_rebook(db, practice, closure))
        extra = _t(practice, f" {n} ραντεβού θέλουν αλλαγή· θα σας έρθει email.",
                   f" {n} appointments need rebooking; you'll get an email.") if n else ""
        return _t(practice, "Έγινε. Ο βοηθός δεν κλείνει ραντεβού αυτές τις μέρες.",
                  "Done. The assistant books nothing on those days.") + extra
    if c["action"] == "remove_closure":
        try:
            await config_changes.remove_closure(db, practice, c["closure_id"], source=source, author=author)
        except config_changes.ChangeError:
            req.status = "cancelled"
            return _t(practice, "Είχε ήδη αφαιρεθεί.", "It was already removed.")
        req.status = "applied"
        return _t(practice, "Έγινε.", "Done.")
    if c["action"] == "hours":
        await config_changes.publish(db, practice, config_changes.validated(practice, {"hours": c["hours"]}),
                                     source=source, author=author, summary=req.readback[:200])
        req.status = "applied"
        return _t(practice, "Έγινε. Το νέο ωράριο ισχύει από τώρα.", "Done. The new hours apply now.")
    if c["action"] == "price":
        services = [dict(s, price=c["price"]) if s["id"] == c["service_id"] else s for s in practice.services or []]
        await config_changes.propose(db, practice, config_changes.validated(practice, {"services": services}),
                                     author=author, summary=req.readback[:200], source=source)
    else:
        kb = {**(practice.knowledge_base or {}), c["topic"]: c["text"]}
        await config_changes.propose(db, practice, config_changes.validated(practice, {"knowledge_base": kb}),
                                     author=author, summary=req.readback[:200], source=source)
    req.status = "queued"
    return _t(practice, "Στάλθηκε για έλεγχο· θα ισχύσει μόλις εγκριθεί.",
              "Sent for review; it applies once approved.")


def confirm_prompt(practice: Practice, readback: str, channel: str) -> str:
    if channel == "sms":
        return readback + _t(practice, " Απαντήστε ΝΑΙ ή ΟΧΙ.", " Reply YES or NO.")
    return readback + _t(practice, " Σωστά;", " Is that right?")


# --- SMS ---


async def handle_sms(db: AsyncSession, sender: str, to: str, text: str, now: datetime) -> str | None:
    """The reply to an SMS from a registered staff mobile; None ignores the sender (unknown
    numbers get no reply, so nobody can run up SMS costs)."""
    rows = (await db.execute(select(Staff, Practice).join(Practice, Practice.id == Staff.practice_id).where(
        Staff.phone == sender, Staff.active.is_(True), Practice.offboarded_at.is_(None)))).all()
    if not rows:
        return None
    staff, practice = next(((s, p) for s, p in rows if to in (p.phone_numbers or []) or to == p.outbound_number),
                           rows[0])
    yes = answer(text)
    if yes is not None:
        req = await pending_for(db, practice, sender)
        if req is None:
            return _t(practice, "Δεν υπάρχει αλλαγή σε αναμονή.", "There's no change waiting.")
        return await decide(db, practice, req, yes)
    try:
        req = await request(db, practice, staff, text, channel="sms", now=now)
    except Rejected as e:
        return e.reply
    return confirm_prompt(practice, req.readback, "sms")
