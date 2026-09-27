from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import booking, ical_feed, public_fetch
from app.models import CalendarFeed, Practice, Staff

ICS = b'''BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:weekly\r
DTSTART;TZID=Europe/Athens:20300107T100000\r
DTEND;TZID=Europe/Athens:20300107T110000\r
RRULE:FREQ=WEEKLY;COUNT=3\r
EXDATE;TZID=Europe/Athens:20300114T100000\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:holiday\r
DTSTART;VALUE=DATE:20300108\r
DTEND;VALUE=DATE:20300109\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:free\r
DTSTART;TZID=Europe/Athens:20300107T120000\r
DTEND;TZID=Europe/Athens:20300107T130000\r
TRANSP:TRANSPARENT\r
END:VEVENT\r
END:VCALENDAR\r
'''


def test_recurrence_exceptions_all_day_and_transparent():
    tz = ZoneInfo("Europe/Athens")
    start = datetime(2030, 1, 7, tzinfo=tz)
    times = ical_feed.intervals(ICS, start, start + timedelta(days=20), "Europe/Athens")
    assert len(times) == 3
    assert sorted(a.day for a, _ in times) == [7, 8, 21]
    assert next(b-a for a,b in times if a.day == 8) == timedelta(days=1)


@pytest.mark.asyncio
async def test_feed_blocks_only_its_tenant_and_staff_and_fails_closed(sessions, monkeypatch):
    fetch = AsyncMock(return_value=(ICS, "https://example.com/feed"))
    monkeypatch.setattr(public_fetch, "get", fetch)
    ical_feed._cache.clear()
    async with sessions() as db:
        a, b = Practice(name="A"), Practice(name="B")
        db.add_all([a,b]); await db.flush()
        person = Staff(practice_id=a.id, name="A staff")
        db.add(person); await db.flush()
        feed = CalendarFeed(practice_id=a.id, staff_id=person.id, name="Other tool", url="https://example.com/feed")
        db.add(feed); await db.commit()
        start = datetime(2030,1,7,tzinfo=ZoneInfo("Europe/Athens"))
        end = start + timedelta(days=1)
        assert await ical_feed.busy(db,b,None,start,end) == []
        assert await ical_feed.busy(db,a,None,start,end) == []
        assert len(await ical_feed.busy(db,a,person.id,start,end)) == 1
        assert len(await ical_feed.busy(db,a,person.id,start,end)) == 1
        assert fetch.await_count == 1
        ical_feed._cache.clear()
        fetch.side_effect = TimeoutError()
        with pytest.raises(booking.BookingError, match="calendar_error"):
            await booking.busy_intervals(db,a,start,end,booking.Resource(person,{},None))


@pytest.mark.asyncio
async def test_public_fetch_pins_address_and_rejects_private_redirect(monkeypatch):
    import asyncio
    loop = asyncio.get_running_loop()
    dns = AsyncMock(return_value=[(2,1,6,"",("93.184.215.14",443))])
    monkeypatch.setattr(loop,"getaddrinfo",dns)
    real_client = httpx.AsyncClient
    seen=[]
    def respond(request):
        seen.append(request)
        assert request.url.host == "93.184.215.14"
        assert request.headers["host"] == "example.com"
        assert request.extensions["sni_hostname"] == "example.com"
        dns.return_value = [(2,1,6,"",("127.0.0.1",443))]
        return httpx.Response(302,headers={"location":"https://internal.example/admin"})
    monkeypatch.setattr(public_fetch.httpx,"AsyncClient",lambda **kw: real_client(transport=httpx.MockTransport(respond),**kw))
    with pytest.raises(ValueError,match="Private network"):
        await public_fetch.get("https://example.com/feed")
    assert len(seen)==1
    for url in ("http://example.com", "https://user:pass@example.com", "https://example.com:8443"):
        with pytest.raises(ValueError):
            await public_fetch.get(url)
