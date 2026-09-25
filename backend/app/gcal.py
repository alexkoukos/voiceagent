"""Google Calendar through a service account (one calendar per practice).

The practice shares its calendar with the service account's email ("Make changes to
events"). Without GOOGLE_SERVICE_ACCOUNT_JSON, or for a practice with no calendar_id,
appointments live only in our database.
"""

import asyncio
import json
from datetime import datetime
from functools import lru_cache

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


async def create_event(
    calendar_id: str, *, summary: str, description: str, start: datetime, end: datetime, timezone: str
) -> str:
    data = await _request("POST", f"/calendars/{calendar_id}/events", json={
        "summary": summary,
        "description": description,
        "start": {"dateTime": start.isoformat(), "timeZone": timezone},
        "end": {"dateTime": end.isoformat(), "timeZone": timezone},
    })
    return data["id"]


async def delete_event(calendar_id: str, event_id: str) -> None:
    await _request("DELETE", f"/calendars/{calendar_id}/events/{event_id}")


async def move_event(calendar_id: str, event_id: str, start: datetime, end: datetime, timezone: str) -> None:
    await _request("PATCH", f"/calendars/{calendar_id}/events/{event_id}", json={
        "start": {"dateTime": start.isoformat(), "timeZone": timezone},
        "end": {"dateTime": end.isoformat(), "timeZone": timezone},
    })
