"""Changes after go-live (PRD OP2, OP3): every change is a config version with one-step rollback.

The founder's app publishes at once. A doctor's magic link publishes hours and closures at
once (they confirm on the page first), while services, prices and FAQ wait in the founder's
approval queue, since a wrong price is repeated to every caller.
"""

import hashlib
import secrets
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import booking, notifications
from app.models import AdminLink, Appointment, ConfigVersion, Practice, Staff
from app.schemas import PracticeIn, PracticeOut

# What a version snapshots and a rollback restores.
FIELDS = ("hours", "services", "rules", "knowledge_base")
# From a doctor's link these need the founder's approval.
NEEDS_APPROVAL = ("services", "knowledge_base")
LINK_HOURS = 72


class ChangeError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def snapshot(practice: Practice) -> dict:
    return {k: getattr(practice, k) for k in FIELDS}


def validated(practice: Practice, changes: dict) -> dict:
    """`changes` checked by the same rules as a full practice update. Closures are kept:
    only the closure calls change them."""
    current = PracticeOut.model_validate(practice).model_dump(mode="json")
    columns = PracticeIn.model_validate({**current, **changes}).to_columns()
    out = {k: columns[k] for k in changes if k in FIELDS}
    if "rules" in out:
        out["rules"]["closures"] = (practice.rules or {}).get("closures") or []
    return out


LABELS = {
    "el": {"hours": "ωράριο", "services": "υπηρεσίες και τιμές", "rules": "κανόνες", "knowledge_base": "πληροφορίες"},
    "en": {"hours": "hours", "services": "services and prices", "rules": "rules", "knowledge_base": "information"},
}


def describe(changes: dict, language: str = "en") -> str:
    labels = LABELS["el" if language == "el" else "en"]
    return ", ".join(labels[k] for k in FIELDS if k in changes)


async def _ensure_baseline(db: AsyncSession, practice: Practice) -> None:
    """The state before the first change, so even that one can be rolled back."""
    first = (await db.execute(
        select(ConfigVersion.id).where(ConfigVersion.practice_id == practice.id, ConfigVersion.status == "published")
        .limit(1)
    )).first()
    if first is None:
        db.add(ConfigVersion(practice_id=practice.id, source="baseline", summary="baseline",
                             snapshot=snapshot(practice), created_at=datetime.utcnow() - timedelta(microseconds=1)))


async def publish(
    db: AsyncSession, practice: Practice, changes: dict, *, source: str, author: str = "", summary: str = "",
    version: ConfigVersion | None = None,
) -> ConfigVersion | None:
    """Applies `changes` (already valid columns) and records the version. None if nothing changed."""
    changes = {k: v for k, v in changes.items() if k in FIELDS and v != getattr(practice, k)}
    if not changes:
        return None
    await _ensure_baseline(db, practice)
    for k, v in changes.items():
        setattr(practice, k, v)
    if version is None:
        version = ConfigVersion(practice_id=practice.id, source=source, author=author)
        db.add(version)
    version.status = "published"
    version.changes = changes
    version.summary = summary or version.summary or describe(changes, practice.language)
    version.snapshot = snapshot(practice)
    version.decided_at = datetime.utcnow()
    await db.flush()
    return version


async def propose(db: AsyncSession, practice: Practice, changes: dict, *, author: str, summary: str = "") -> ConfigVersion | None:
    changes = {k: v for k, v in changes.items() if k in FIELDS and v != getattr(practice, k)}
    if not changes:
        return None
    version = ConfigVersion(practice_id=practice.id, status="pending", source="link", author=author,
                            changes=changes, summary=summary or describe(changes, practice.language))
    db.add(version)
    await db.flush()
    return version


async def _version(db: AsyncSession, practice: Practice, version_id: str) -> ConfigVersion:
    version = await db.get(ConfigVersion, version_id)
    if version is None or version.practice_id != practice.id:
        raise ChangeError("not_found")
    return version


async def approve(db: AsyncSession, practice: Practice, version_id: str) -> ConfigVersion:
    """Applies a pending change on top of today's config (other changes may have been published since)."""
    version = await _version(db, practice, version_id)
    if version.status != "pending":
        raise ChangeError("not_pending")
    applied = await publish(db, practice, validated(practice, version.changes), source=version.source,
                            version=version)
    if applied is None:
        # Already true today: nothing to apply, but it leaves the queue.
        version.status, version.decided_at = "published", datetime.utcnow()
        version.snapshot = snapshot(practice)
    return version


async def reject(db: AsyncSession, practice: Practice, version_id: str) -> ConfigVersion:
    version = await _version(db, practice, version_id)
    if version.status != "pending":
        raise ChangeError("not_pending")
    version.status, version.decided_at = "rejected", datetime.utcnow()
    return version


async def rollback(db: AsyncSession, practice: Practice, version_id: str, *, author: str = "") -> ConfigVersion | None:
    """Back to how things were right after `version_id`, recorded as a new version."""
    version = await _version(db, practice, version_id)
    if version.status != "published" or not version.snapshot:
        raise ChangeError("not_published")
    when = version.created_at.strftime("%Y-%m-%d %H:%M")
    return await publish(db, practice, dict(version.snapshot), source="rollback", author=author,
                         summary=f"rollback to {when}")


# --- closures and leave (OP3) ---


async def to_rebook(db: AsyncSession, practice: Practice, closure: dict) -> list[Appointment]:
    """Booked appointments inside a closure."""
    tz = ZoneInfo(practice.timezone)
    start = datetime.combine(date.fromisoformat(closure["from"]), time(0), tz)
    end = datetime.combine(date.fromisoformat(closure["to"]) + timedelta(days=1), time(0), tz)
    q = select(Appointment).where(
        Appointment.practice_id == practice.id, Appointment.status == "booked",
        Appointment.starts_at >= start, Appointment.starts_at < end,
    )
    if closure.get("staff_id"):
        q = q.where(Appointment.staff_id == closure["staff_id"])
    return list((await db.execute(q.order_by(Appointment.starts_at))).scalars())


async def add_closure(
    db: AsyncSession, practice: Practice, *, date_from: date, date_to: date, staff_id: str | None,
    reason: str | None, source: str, author: str = "",
) -> dict:
    """The agent stops offering these days at once; appointments already in the range are
    emailed to the business for rebooking."""
    if date_to < date_from:
        raise ChangeError("bad_range")
    person = None
    if staff_id:
        person = await db.get(Staff, staff_id)
        if person is None or person.practice_id != practice.id:
            raise ChangeError("unknown_staff")
    closure = {"id": uuid.uuid4().hex, "from": date_from.isoformat(), "to": date_to.isoformat()}
    if person:
        closure["staff_id"] = person.id
    if reason:
        closure["reason"] = reason
    rules = dict(practice.rules or {})
    rules["closures"] = [*(rules.get("closures") or []), closure]
    lang = practice.language
    who = person.name if person else practice.name
    span = f"{booking.say_date(date_from, lang)} – {booking.say_date(date_to, lang)}"
    await publish(db, practice, {"rules": rules}, source=source, author=author,
                  summary=(f"κλειστά: {who}, {span}" if lang == "el" else f"closed: {who}, {span}"))
    affected = await to_rebook(db, practice, closure)
    if affected:
        staff = await booking.staff_of(db, practice.id)
        lines = []
        for a in affected:
            d = booking.describe(practice, a, staff, lang)
            lines.append(f"- {d['date_spoken']} {d['time']}, {a.customer_name} {a.customer_phone or ''}, {d['service']}")
        subject = (f"{len(lines)} ραντεβού για αλλαγή: {who} κλειστά {span}" if lang == "el"
                   else f"{len(lines)} appointments to rebook: {who} closed {span}")
        await notifications.queue_business(db, practice, kind="closure_rebook", subject=subject,
                                           body="\n".join(lines), dedupe_key=f"closure:{closure['id']}")
    return closure


async def remove_closure(db: AsyncSession, practice: Practice, closure_id: str, *, source: str, author: str = "") -> None:
    rules = dict(practice.rules or {})
    closures = rules.get("closures") or []
    gone = [c for c in closures if c["id"] == closure_id]
    if not gone:
        raise ChangeError("not_found")
    rules["closures"] = [c for c in closures if c["id"] != closure_id]
    c = gone[0]
    await publish(db, practice, {"rules": rules}, source=source, author=author,
                  summary=f"closure removed: {c['from']} – {c['to']}")


# --- magic links ---


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_link(db: AsyncSession, practice: Practice, *, staff_id: str | None = None,
                      hours: int = LINK_HOURS) -> tuple[str, AdminLink]:
    if staff_id:
        person = await db.get(Staff, staff_id)
        if person is None or person.practice_id != practice.id:
            raise ChangeError("unknown_staff")
    token = secrets.token_urlsafe(32)
    link = AdminLink(token_hash=_hash(token), practice_id=practice.id, staff_id=staff_id,
                     expires_at=datetime.utcnow() + timedelta(hours=hours))
    db.add(link)
    await db.flush()
    return token, link


async def resolve_link(db: AsyncSession, token: str) -> tuple[AdminLink, Practice] | None:
    link = await db.get(AdminLink, _hash(token)) if token else None
    if link is None or link.revoked or link.expires_at <= datetime.utcnow():
        return None
    link.last_used_at = datetime.utcnow()
    return link, await db.get(Practice, link.practice_id)
