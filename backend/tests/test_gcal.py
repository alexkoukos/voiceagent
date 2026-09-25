from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import gcal


@pytest.mark.asyncio
async def test_busy_except_paginates_and_preserves_overlapping_events(monkeypatch):
    def event(id, **values):
        return {"id": id, "start": {"dateTime": "2026-09-28T09:00:00+03:00"},
                "end": {"dateTime": "2026-09-28T09:30:00+03:00"}, **values}

    request = AsyncMock(side_effect=[
        {"timeZone": "Europe/Athens", "nextPageToken": "second", "items": [
            event("own"), event("overlapping"), event("free", transparency="transparent"),
            {"id": "deleted", "status": "cancelled"},
        ]},
        {"timeZone": "Europe/Athens", "items": [
            event("all-day", start={"date": "2026-09-28"}, end={"date": "2026-09-29"}),
        ]},
    ])
    monkeypatch.setattr(gcal, "_request", request)
    start = datetime(2026, 9, 28, tzinfo=ZoneInfo("Europe/Athens"))
    intervals = await gcal.busy_except("test@example.invalid", start, start.replace(day=29), "own")
    assert len(intervals) == 2
    assert intervals[0][0].hour == 9
    assert intervals[1] == (start, start.replace(day=29))
    assert request.await_count == 2
    assert request.call_args.kwargs["params"]["pageToken"] == "second"
    assert request.call_args.kwargs["params"]["singleEvents"] == "true"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 410, 403, 500])
async def test_delete_retry_only_accepts_already_gone(monkeypatch, status):
    response = httpx.Response(status, request=httpx.Request("DELETE", "https://example.invalid/event"))
    request = AsyncMock(side_effect=httpx.HTTPStatusError("error", request=response.request, response=response))
    monkeypatch.setattr(gcal, "_request", request)
    if status in (404, 410):
        await gcal.delete_event("calendar", "event")
    else:
        with pytest.raises(httpx.HTTPStatusError):
            await gcal.delete_event("calendar", "event")
