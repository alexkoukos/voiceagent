"""Read-only external busy times. Never write to an imported calendar (O8)."""
import asyncio
import hashlib
import time
from collections import OrderedDict
from datetime import date, datetime, time as daytime, timedelta
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events
from sqlalchemy import select

from app import public_fetch
from app.models import CalendarFeed

# URLs are secret; cache by tenant/feed/url digest and expire after five minutes.
_cache: OrderedDict[tuple, tuple[float, bytes]] = OrderedDict()


def intervals(data: bytes, start: datetime, end: datetime, timezone: str) -> list:
    calendar = icalendar.Calendar.from_ical(data)
    if calendar.name != "VCALENDAR":
        raise ValueError("Expected an iCalendar feed")
    tz = ZoneInfo(timezone)
    def moment(value):
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=tz)
        return datetime.combine(value, daytime.min, tzinfo=tz)
    # Bound pathological recurrence rules before expanding them.
    for event in calendar.walk("VEVENT"):
        freq = (event.get("RRULE") or {}).get("FREQ", [])
        if any(str(f) in ("SECONDLY", "MINUTELY") for f in freq):
            raise ValueError("Sub-hourly calendar recurrence is unsupported")
    result = []
    for event in recurring_ical_events.of(calendar).between(start, end):
        if str(event.get("STATUS", "")).upper() == "CANCELLED" or str(event.get("TRANSP", "")).upper() == "TRANSPARENT":
            continue
        lo = event.decoded("DTSTART")
        hi = event.decoded("DTEND", None)
        if hi is None:
            hi = lo + event.decoded("DURATION", timedelta(days=1) if type(lo) is date else timedelta())
        a, b = moment(lo), moment(hi)
        if a < end and b > start:
            result.append((a, b))
    return result


async def busy(db, practice, staff_id, start, end):
    feeds = (await db.execute(select(CalendarFeed).where(
        CalendarFeed.practice_id == practice.id,
        (CalendarFeed.staff_id.is_(None)) | (CalendarFeed.staff_id == staff_id),
    ))).scalars()
    result = []
    for feed in feeds:
        key = (practice.id, feed.id, hashlib.sha256(feed.url.encode()).digest())
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            data = cached[1]
            _cache.move_to_end(key)
        else:
            data, _ = await public_fetch.get(feed.url)
            # Only cache feeds that parse; never substitute old data after a fetch failure.
            intervals(data, start, end, practice.timezone)
            _cache[key] = (time.monotonic(), data)
            while len(_cache) > 64:
                _cache.popitem(last=False)
        result += await asyncio.to_thread(intervals, data, start, end, practice.timezone)
    return result
