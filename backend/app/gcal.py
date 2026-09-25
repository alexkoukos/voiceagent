"""Google Calendar through a service account (one calendar per practice).

The practice shares its calendar with the service account's email ("Make changes to
events"). Without GOOGLE_SERVICE_ACCOUNT_JSON, or for a practice with no calendar_id,
appointments live only in our database when no calendar_id is assigned. Assigned
calendars must be reachable; missing credentials must never imply an empty calendar.
"""

import asyncio
import hashlib
import json
from datetime import datetime
from functools import lru_cache
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings

API = "https://www.googleapis.com/calendar/v3"
SCOPES = ["https://www.googleapis.com/auth/calendar"]


def configured() -> bool:
    return bool(get_settings().google_service_account_json)


@lru_cache
def _credentials():
    from google.oauth2 import service_account

    info = json.loads(get_settings().google_service_account_json)
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


async def _token() -> str:
    creds = _credentials()
    if not creds.valid:
        from google.auth.transport.requests import Request

        await asyncio.to_thread(creds.refresh, Request())
    return creds.token


async def _request(method: str, path: str, **kwargs) -> dict:
    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.request(
            method, f"{API}{path}", headers={"Authorization": f"Bearer {await _token()}"}, **kwargs
        )
        r.raise_for_status()
        return r.json() if r.content else {}


async def busy(calendar_id: str, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    data = await _request("POST", "/freeBusy", json={
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "items": [{"id": calendar_id}],
    })
    cal = data["calendars"][calendar_id]
    if cal.get("errors"):
        raise RuntimeError(f"freeBusy: {cal['errors']}")
    return [
        (datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])) for b in cal["busy"]
    ]


async def busy_except(
    calendar_id: str, start: datetime, end: datetime, event_id: str,
) -> list[tuple[datetime, datetime]]:
    """Exclude only the appointment being moved, preserving overlapping other events.

    FreeBusy merges intervals and has no event IDs, so subtracting the appointment's
    time from it could hide a separate booking. Expand recurring events and paginate.
    """
    params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(),
              "singleEvents": "true", "showDeleted": "false", "maxResults": 2500}
    intervals = []
    while True:
        data = await _request("GET", f"/calendars/{quote(calendar_id, safe='')}/events", params=params)
        tz = ZoneInfo(data.get("timeZone", "UTC"))
        for event in data.get("items", []):
            if (event["id"] == event_id or event.get("status") == "cancelled"
                    or event.get("transparency") == "transparent"):
                continue
            def when(value):
                if "dateTime" in value:
                    return datetime.fromisoformat(value["dateTime"])
                return datetime.fromisoformat(value["date"]).replace(tzinfo=tz)
            intervals.append((when(event["start"]), when(event["end"])))
        if not data.get("nextPageToken"):
            return intervals
        params["pageToken"] = data["nextPageToken"]


async def create_event(
    calendar_id: str, *, summary: str, description: str, start: datetime, end: datetime, timezone: str,
    event_key: str,
) -> str:
    # Google permits caller-supplied base32hex IDs. A stable key makes a timed-out
    # insert safe to retry with the same event instead of creating a second one.
    event_id = "a" + hashlib.sha256(event_key.encode()).hexdigest()[:48]
    path = f"/calendars/{quote(calendar_id, safe='')}/events"
    body = {
        "id": event_id,
        "summary": summary,
        "description": description,
        "start": {"dateTime": start.isoformat(), "timeZone": timezone},
        "end": {"dateTime": end.isoformat(), "timeZone": timezone},
        "extendedProperties": {"private": {"voiceagent": "1", "voiceagent_key": event_key}},
    }
    try:
        data = await _request("POST", path, json=body)
        return data["id"]
    except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code != 409:
            raise
        try:
            existing = await _request("GET", f"{path}/{event_id}")
        except httpx.HTTPStatusError as missing:
            if missing.response.status_code == 404:
                raise exc
            raise
        if (existing.get("id") == event_id
                and existing.get("extendedProperties", {}).get("private", {}).get("voiceagent_key") == event_key
                and existing.get("start", {}).get("dateTime") == start.isoformat()
                and existing.get("end", {}).get("dateTime") == end.isoformat()):
            return event_id
        raise RuntimeError("Calendar event ID already exists with different booking details")


async def owned_events(calendar_id: str, start: datetime, end: datetime) -> list[dict]:
    """Our events in a bounded window, for crash recovery after ambiguous writes."""
    path = f"/calendars/{quote(calendar_id, safe='')}/events"
    params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(),
              "privateExtendedProperty": "voiceagent=1", "showDeleted": "false", "maxResults": 2500}
    events = []
    while True:
        data = await _request("GET", path, params=params)
        events.extend(data.get("items", []))
        if not data.get("nextPageToken"):
            return events
        params["pageToken"] = data["nextPageToken"]


async def delete_event(calendar_id: str, event_id: str) -> None:
    try:
        await _request("DELETE", f"/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}")
    except httpx.HTTPStatusError as exc:
        # A retry after a successful remote delete is already complete.
        if exc.response.status_code not in (404, 410):
            raise


async def move_event(calendar_id: str, event_id: str, start: datetime, end: datetime, timezone: str) -> None:
    await _request("PATCH", f"/calendars/{calendar_id}/events/{event_id}", json={
        "start": {"dateTime": start.isoformat(), "timeZone": timezone},
        "end": {"dateTime": end.isoformat(), "timeZone": timezone},
    })
