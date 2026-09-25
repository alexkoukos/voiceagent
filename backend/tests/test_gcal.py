import hashlib
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


@pytest.mark.asyncio
async def test_create_reuses_event_after_ambiguous_timeout(monkeypatch):
    start = datetime(2026, 9, 28, 9, tzinfo=ZoneInfo("Europe/Athens"))
    end = start.replace(minute=30)
    key = "practice:call:slot"
    event_id = "a" + hashlib.sha256(key.encode()).hexdigest()[:48]
    existing = {"id": event_id, "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": end.isoformat()},
                "extendedProperties": {"private": {"voiceagent_key": key}}}
    request = AsyncMock(side_effect=[httpx.ReadTimeout("ambiguous"), existing])
    monkeypatch.setattr(gcal, "_request", request)
    result = await gcal.create_event("calendar", summary="Test", description="Test",
                                     start=start, end=end, timezone="Europe/Athens", event_key=key)
    assert result == event_id
    assert request.call_args_list[0].kwargs["json"]["id"] == event_id
    assert request.call_args_list[1].args[0] == "GET"
