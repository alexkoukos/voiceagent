"""Success metrics (PRD) and routing analytics (R9), computed from the call log."""

import statistics
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Appointment, Call, CallStatus, Notification, Practice, RoutingEvent
from app.finalize import wants_business_summary

RESOLVED = {"booked", "rescheduled", "cancelled", "confirmed", "info_given", "message_taken"}


def _pct(n: int, d: int) -> float | None:
    return round(100 * n / d, 1) if d else None


async def compute(db: AsyncSession, practice: Practice, days: int = 30, include_web: bool = False) -> dict:
    since = datetime.utcnow() - timedelta(days=days)
    q = select(Call).where(Call.practice_id == practice.id, Call.created_at >= since,
                           Call.status.in_([CallStatus.completed, CallStatus.failed]))
    if not include_web:
        q = q.where(Call.direction != "web")
    calls = list((await db.execute(q)).scalars())
    answered = [c for c in calls if c.direction != "outbound" and c.status == CallStatus.completed]
    outcomes = Counter(c.outcome or "unknown" for c in calls)
    n = len(answered)

    reviewed_routing = [c for c in calls if c.review and c.review.get("routing_correct") is not None]
    booking_calls = [c for c in calls if c.review and c.review.get("booking_correct") is not None]
    latencies = [c.latency_ms_median for c in calls if c.latency_ms_median]
    minutes = sum((c.duration_seconds or 0) for c in calls) / 60
    cost = sum(c.cost_estimate or 0 for c in calls)

    # Business notified within 60 s of hang-up (the per-call summary email).
    notes = list((await db.execute(select(Notification).where(
        Notification.practice_id == practice.id, Notification.kind == "call_summary",
        Notification.channel == "email", Notification.created_at >= since,
    ))).scalars())
    # Count calls, not recipient rows. Pending, failed and missing notifications
    # must stay in the denominator or an outage misleadingly reports 100% success.
    ended = {c.id: c.ended_at for c in calls if c.ended_at and wants_business_summary(practice, c)}
    on_time = {n_.call_id for n_ in notes
               if n_.status == "sent" and n_.sent_at and n_.call_id in ended
               and 0 <= (n_.sent_at - ended[n_.call_id]).total_seconds() <= 60}

    bookings = (await db.execute(select(func.count()).select_from(Appointment).where(
        Appointment.practice_id == practice.id, Appointment.source.in_(["agent", "waitlist"]),
        Appointment.created_at >= since,
    ))).scalar_one()
    return {
        "days": days,
        "calls": len(calls),
        "answered": n,
        "outcomes": dict(outcomes),
        "routing_accuracy_pct": _pct(sum(c.review["routing_correct"] for c in reviewed_routing), len(reviewed_routing)),
        "routing_reviewed": len(reviewed_routing),
        "booking_accuracy_pct": _pct(sum(c.review["booking_correct"] for c in booking_calls), len(booking_calls)),
        "booking_reviewed": len(booking_calls),
        "resolved_without_human_pct": _pct(sum(c.outcome in RESOLVED for c in answered), n),
        "handoff_pct": _pct(sum(c.outcome == "transferred" for c in answered), n),
        "abandoned_pct": _pct(sum(c.outcome == "abandoned" for c in answered), n),
        "latency_ms_median": int(statistics.median(latencies)) if latencies else None,
        "notified_within_60s_pct": _pct(len(on_time), len(ended)),
        "cost_per_minute_eur": round(cost / minutes, 4) if minutes else None,
        "cost_total_eur": round(cost, 2),
        "bookings": bookings,
        "value_estimate_eur": round(bookings * (practice.avg_booking_value or 0), 2),
        "guarantee_threshold": practice.guarantee_threshold,
        "targets": {
            "routing_accuracy_pct": 95, "booking_accuracy_pct": 95, "resolved_without_human_pct": 80,
            "handoff_pct_max": 15, "abandoned_pct_max": 10, "latency_ms_median_max": 1200,
            "notified_within_60s_pct": 99, "cost_per_minute_eur_max": 0.05,
        },
    }


async def routing_report(db: AsyncSession, practice: Practice, days: int = 30) -> dict:
    since = datetime.utcnow() - timedelta(days=days)
    rows = list((await db.execute(select(RoutingEvent).where(
        RoutingEvent.practice_id == practice.id, RoutingEvent.created_at >= since,
        RoutingEvent.kind.notin_(["found", "action"]),
    ))).scalars())
    by_rule = Counter(r.rule for r in rows)
    by_path = Counter(r.path for r in rows if r.path)
    misrouted_ids = [c.id for c in (await db.execute(select(Call).where(
        Call.practice_id == practice.id, Call.created_at >= since,
    ))).scalars() if c.review and c.review.get("routing_correct") is False]
    misroutes = [
        {"call_id": cid, "decisions": [{"kind": r.kind, "value": r.value, "rule": r.rule, "path": r.path}
                                       for r in rows if r.call_id == cid]}
        for cid in misrouted_ids
    ]
    return {"days": days, "decisions": len(rows), "by_rule": dict(by_rule), "by_path": dict(by_path),
            "misroutes": misroutes}
