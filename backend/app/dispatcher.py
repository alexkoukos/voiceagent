from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Call, CallStatus

STALE_GRACE_SECONDS = 180
DIALING_STALE_SECONDS = 120


async def active_count(db: AsyncSession) -> int:
    # Calls whose agent died never report back; don't let them hold a slot forever.
    cutoff = datetime.utcnow() - timedelta(
        seconds=get_settings().max_call_duration_seconds + STALE_GRACE_SECONDS
    )
    result = await db.execute(
        select(func.count())
        .select_from(Call)
        .where(or_(
            and_(Call.status == CallStatus.active, Call.created_at > cutoff),
            # A call still "dialing" after the ring window never started (e.g. its agent died).
            and_(Call.status == CallStatus.dialing,
                 Call.created_at > datetime.utcnow() - timedelta(seconds=DIALING_STALE_SECONDS)),
        ))
    )
    return result.scalar_one()


async def start_next_queued(db: AsyncSession) -> None:
    """Fill free slots with the oldest queued reminder / waitlist calls."""
    from app import events

    limit = get_settings().max_concurrent_calls
    while await active_count(db) < limit:
        result = await db.execute(
            select(Call).where(Call.status == CallStatus.queued, Call.practice_id.is_not(None))
            .order_by(Call.created_at).limit(1)
        )
        call = result.scalar_one_or_none()
        if call is None:
            return
        from app.receptionist import dispatch_outbound
        await dispatch_outbound(db, call)
        events.publish(call.id)
