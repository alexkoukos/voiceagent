"""Operator alerts (PRD OP9): things the founder must see, even when away. Each one is
emailed/texted to the founder and listed in the app until acknowledged; unacknowledged after
ALERT_ACK_MINUTES it goes to the backup contact too."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import events, notifications
from app.config import get_settings
from app.models import Alert, Practice


async def _send(db: AsyncSession, alert: Alert, email: str, sms: str, prefix: str = "") -> None:
    subject = prefix + alert.subject
    if email:
        await notifications.queue(db, practice_id=alert.practice_id, kind=f"alert_{alert.kind}", channel="email",
                                  recipient=email, subject=subject, body=alert.body, call_id=alert.call_id)
    if sms:
        await notifications.queue(db, practice_id=alert.practice_id, kind=f"alert_{alert.kind}", channel="sms",
                                  recipient=sms, body=f"{subject}\n{alert.body}"[:600], call_id=alert.call_id)


async def raise_alert(
    db: AsyncSession, practice: Practice | None, kind: str, subject: str, body: str = "", *,
    call_id: str | None = None, dedupe_key: str | None = None,
) -> Alert | None:
    """None when an alert with this dedupe_key already exists."""
    values = dict(practice_id=practice.id if practice else None, kind=kind, subject=subject[:300], body=body,
                  call_id=call_id, dedupe_key=dedupe_key)
    if dedupe_key:
        inserted = (await db.execute(
            pg_insert(Alert).values(id=str(uuid.uuid4()), created_at=datetime.utcnow(), **values)
            .on_conflict_do_nothing(index_elements=[Alert.dedupe_key]).returning(Alert.id)
        )).scalar_one_or_none()
        if inserted is None:
            return None
        alert = await db.get(Alert, inserted)
    else:
        alert = Alert(**values)
        db.add(alert)
        await db.flush()
    s = get_settings()
    await _send(db, alert, s.founder_email, s.founder_sms)
    events.publish("alerts")
    return alert


async def escalate(db: AsyncSession, now: datetime | None = None) -> int:
    """Unacknowledged alerts older than the ack window go to the backup contact, once."""
    s = get_settings()
    if not (s.backup_email or s.backup_sms):
        return 0
    now = now or datetime.utcnow()
    due = list((await db.execute(select(Alert).where(
        Alert.acked_at.is_(None), Alert.escalated_at.is_(None),
        Alert.created_at < now - timedelta(minutes=s.alert_ack_minutes),
    ).with_for_update(skip_locked=True))).scalars())
    for alert in due:
        await _send(db, alert, s.backup_email, s.backup_sms, prefix="[backup] ")
        alert.escalated_at = now
    return len(due)
