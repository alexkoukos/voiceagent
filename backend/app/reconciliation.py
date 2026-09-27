"""OP5: detect Calendar drift and collisions without guessing which booking to keep."""

import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app import alerts, booking, gcal, notifications
from app.models import Appointment, AppointmentStatus, Practice


async def check(db, practice: Practice, now: datetime) -> int:
    await booking._lock(db, practice)
    staff = await booking.staff_of(db, practice.id)
    appointments = list((await db.execute(select(Appointment).where(
        Appointment.practice_id == practice.id, Appointment.status == AppointmentStatus.booked,
        Appointment.ends_at > now,
    ).execution_options(populate_existing=True))).scalars())
    by_calendar: dict[str, list[Appointment]] = {}
    for appointment in appointments:
        calendar = booking._calendar_of(practice, staff, appointment)
        if calendar:
            by_calendar.setdefault(calendar, []).append(appointment)
    count = 0
    for calendar, booked in by_calendar.items():
        end = max(a.ends_at for a in booked) + timedelta(days=1)
        remote = await gcal.calendar_events(calendar, now, end)
        by_id = {e["id"]: e for e in remote}
        busy = [(e["id"], *gcal.event_interval(e, ZoneInfo(e.get("calendar_timezone", practice.timezone))))
                for e in remote if e.get("status") != "cancelled" and e.get("transparency") != "transparent"]
        for appointment in booked:
            reasons = []
            own = by_id.get(appointment.gcal_event_id)
            if appointment.gcal_event_id and own is None:
                own = await gcal.get_event(calendar, appointment.gcal_event_id)
            if not own or own.get("status") == "cancelled":
                reasons.append("Calendar event is missing or cancelled")
            else:
                interval = gcal.event_interval(own, ZoneInfo(own.get("calendar_timezone", practice.timezone)))
                if interval != (appointment.starts_at, appointment.ends_at):
                    reasons.append(f"Calendar time changed to {interval[0].isoformat()} – {interval[1].isoformat()}")
            for event_id, start, finish in busy:
                if event_id != appointment.gcal_event_id and start < appointment.ends_at and finish > appointment.starts_at:
                    reasons.append(f"Overlaps Calendar event {event_id}")
            if not reasons:
                continue
            fingerprint = hashlib.sha256("\n".join(sorted(reasons)).encode()).hexdigest()[:16]
            key = f"calendar-conflict:{appointment.id}:{now.date()}:{fingerprint}"
            subject = f"{practice.name}: calendar conflict"
            body = (f"{appointment.service_name}, {appointment.starts_at.astimezone(ZoneInfo(practice.timezone)):%d/%m/%Y %H:%M}.\n"
                    + "\n".join(reasons) + "\nReview both calendars before contacting the customer.")
            alert = await alerts.raise_alert(db, practice, "calendar_conflict", subject, body,
                                            call_id=appointment.call_id, dedupe_key=key)
            if alert:
                count += 1
                await notifications.queue_business(db, practice, kind="calendar_conflict", subject=subject,
                                                   body=body, urgent=True, call_id=appointment.call_id,
                                                   dedupe_key=key)
    return count
