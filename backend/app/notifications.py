"""Notification outbox (PRD Notifications).

Everything is queued in the `notifications` table first and sent by a background worker
with retries. Unconfigured channels remain pending until credentials are available.
"""

import asyncio
import logging
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import push
from app.config import get_settings
from app.database import async_session
from app.models import Device, Notification, Practice

logger = logging.getLogger("notifications")

MAX_ATTEMPTS = 8
_wake = asyncio.Event()


class NotConfigured(Exception):
    pass


async def queue(
    db: AsyncSession,
    *,
    practice_id: str | None,
    kind: str,
    channel: str,
    recipient: str,
    subject: str = "",
    body: str = "",
    call_id: str | None = None,
    data: dict | None = None,
    dedupe_key: str | None = None,
    status: str = "pending",
) -> Notification | None:
    """Insert in the caller's transaction, ignoring only an identical event's key.

    A collision must never roll back the call outcome or other queued recipients.
    The caller commits and calls kick(); None means the event was already queued.
    """
    statement = insert(Notification).values(
        practice_id=practice_id, kind=kind, channel=channel, recipient=recipient, subject=subject,
        body=body, call_id=call_id, data=data or {}, dedupe_key=dedupe_key, status=status,
    )
    statement = statement.on_conflict_do_nothing(index_elements=[Notification.dedupe_key])
    return (await db.execute(statement.returning(Notification))).scalar_one_or_none()


async def exists(db: AsyncSession, dedupe_key: str) -> bool:
    return (await db.execute(
        select(Notification.id).where(Notification.dedupe_key == dedupe_key).limit(1)
    )).first() is not None


def kick() -> None:
    _wake.set()


def business_emails(practice: Practice) -> list[str]:
    return [e for e in (practice.notifications or {}).get("emails", []) if e]


async def queue_business(
    db: AsyncSession,
    practice: Practice,
    *,
    kind: str,
    subject: str,
    body: str,
    call_id: str | None = None,
    urgent: bool = False,
    dedupe_key: str | None = None,
    data: dict | None = None,
) -> None:
    """Email to the business; urgent ones also go as push and SMS to the urgent numbers.
    With a dedupe_key, an event already queued is not queued again."""
    if dedupe_key and await exists(db, f"{dedupe_key}:sent"):
        return
    if dedupe_key:
        # Marker row so the check above works even when the business has no email.
        marker = await queue(db, practice_id=practice.id, kind=kind, channel="none", recipient="-", call_id=call_id,
                             dedupe_key=f"{dedupe_key}:sent", status="skipped")
        if marker is None:
            return
    for i, email in enumerate(business_emails(practice)):
        await queue(db, practice_id=practice.id, kind=kind, channel="email", recipient=email, subject=subject,
              body=body, call_id=call_id, data=data, dedupe_key=f"{dedupe_key}:email:{i}" if dedupe_key else None)
    if urgent:
        for number in (practice.notifications or {}).get("urgent_sms", []):
            await queue(db, practice_id=practice.id, kind=kind, channel="sms", recipient=number,
                  body=f"{subject}\n{body}"[:600], call_id=call_id)
        await queue_push(db, practice, kind=kind, title=subject, body=body[:180], call_id=call_id, data=data)


async def queue_push(
    db: AsyncSession, practice: Practice, *, kind: str, title: str, body: str,
    call_id: str | None = None, data: dict | None = None, staff_id: str | None = None,
) -> None:
    q = select(Device).where(Device.practice_id == practice.id)
    devices = list((await db.execute(q)).scalars())
    if staff_id and any(d.staff_id == staff_id for d in devices):
        devices = [d for d in devices if d.staff_id == staff_id]
    for d in devices:
        await queue(db, practice_id=practice.id, kind=kind, channel="push", recipient=d.token, subject=title,
              body=body, call_id=call_id, data={**(data or {}), "environment": d.environment})


# --- senders ---


def _send_email_sync(to: str, subject: str, body: str) -> None:
    s = get_settings()
    if not (s.smtp_host and s.email_from):
        raise NotConfigured("SMTP_HOST / EMAIL_FROM not set")
    msg = EmailMessage()
    msg["From"] = s.email_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
        smtp.starttls()
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(msg)


async def _send_sms(to: str, body: str) -> None:
    s = get_settings()
    if not (s.telnyx_api_key and s.sms_from):
        raise NotConfigured("TELNYX_API_KEY / SMS_FROM not set")
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            "https://api.telnyx.com/v2/messages",
            headers={"Authorization": f"Bearer {s.telnyx_api_key}"},
            json={"from": s.sms_from, "to": to, "text": body},
        )
        r.raise_for_status()


async def send(n: Notification) -> None:
    if n.channel == "email":
        await asyncio.to_thread(_send_email_sync, n.recipient, n.subject, n.body)
    elif n.channel == "sms":
        await _send_sms(n.recipient, n.body)
    elif n.channel == "push":
        if not push.configured():
            raise NotConfigured("APNs not configured")
        data = dict(n.data or {})
        env = data.pop("environment", "sandbox")
        await push.send(n.recipient, title=n.subject, body=n.body, data={"kind": n.kind, **data}, environment=env)
    else:
        raise NotConfigured(f"unknown channel {n.channel}")


async def _process_one() -> int:
    """Own one row until its send result is committed; other workers skip it."""
    async with async_session() as db:
        n = (await db.execute(
            select(Notification)
            .where(Notification.status == "pending", Notification.next_attempt_at <= datetime.utcnow())
            .order_by(Notification.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )).scalar_one_or_none()
        if n is None:
            return 0
        try:
            await send(n)
            n.attempts += 1
            n.status, n.sent_at, n.last_error = "sent", datetime.utcnow(), None
        except NotConfigured as e:
            # Missing credentials are recoverable and do not consume send attempts.
            n.last_error = str(e)
            n.next_attempt_at = datetime.utcnow() + timedelta(seconds=60)
        except Exception as e:
            n.attempts += 1
            n.last_error = f"{type(e).__name__}: {e}"[:1000]
            if n.attempts >= MAX_ATTEMPTS:
                n.status = "failed"
                logger.error("notification %s gave up: %s", n.id, n.last_error)
            else:
                n.next_attempt_at = datetime.utcnow() + timedelta(seconds=min(3600, 10 * 2 ** n.attempts))
        await db.commit()
        return 1


async def process_due(limit: int = 20) -> int:
    """Bounded parallel delivery so a slow SMTP send doesn't block SMS or push."""
    slots = asyncio.Semaphore(5)

    async def process():
        async with slots:
            return await _process_one()

    results = await asyncio.gather(*(process() for _ in range(limit)), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return sum(results)


async def run_worker() -> None:
    while True:
        try:
            while await process_due():
                pass
        except Exception:
            logger.exception("notification worker")
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
