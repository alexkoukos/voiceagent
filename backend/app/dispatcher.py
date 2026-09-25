from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.livekit_dispatch import dispatch_call
from app.models import Call, CallStatus, Friend
from app.prompts import build_call_prompt

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


async def start_call(db: AsyncSession, call: Call, friend: Friend) -> None:
    merged_prompt = build_call_prompt(
        friend_name=friend.name,
        persona=call.persona,
        scenario=call.scenario,
        context=call.context,
        reveal=call.reveal,
        max_duration_seconds=call.max_duration_seconds,
        language=call.language or "el",
    )
    try:
        await dispatch_call(
            call_id=call.id,
            friend_phone_number=friend.phone_number,
            merged_prompt=merged_prompt,
            voice=call.voice,
            max_duration_seconds=call.max_duration_seconds,
            from_own_number=call.from_own_number,
            language=call.language or "el",
        )
        call.status = CallStatus.dialing
    except Exception:
        call.status = CallStatus.failed
        call.ended_at = datetime.utcnow()
        await db.commit()
        raise
    await db.commit()


async def start_next_queued(db: AsyncSession) -> None:
    """Fill free slots with the oldest queued calls."""
    from app import events

    limit = get_settings().max_concurrent_calls
    while await active_count(db) < limit:
        result = await db.execute(
            select(Call).where(Call.status == CallStatus.queued).order_by(Call.created_at).limit(1)
        )
        call = result.scalar_one_or_none()
        if call is None:
            return
        if call.practice_id:
            # Receptionist reminder / waitlist call.
            from app.receptionist import dispatch_outbound
            await dispatch_outbound(db, call)
            events.publish(call.id)
            continue
        friend = await db.get(Friend, call.friend_id)
        try:
            await start_call(db, call, friend)
        except Exception:
            pass  # marked failed by start_call; try the next one
        events.publish(call.id)
