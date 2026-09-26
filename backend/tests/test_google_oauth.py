"""O3: a doctor connects their own calendar; gcal then uses their token for it."""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app import gcal, google_oauth
from app.config import get_settings
from app.models import CalendarConnection, Practice, Staff
from app.routers import oauth


@pytest.mark.asyncio
async def test_connect_callback_and_token_choice(sessions, monkeypatch):
    s = get_settings()
    async with sessions() as db:
        practice = Practice(name="Test", timezone="Europe/Athens", hours={},
                            services=[{"id": "c", "name": "C", "duration_minutes": 30}])
        db.add(practice)
        await db.flush()
        doc = Staff(practice_id=practice.id, name="Γιώργος", role="doctor")
        db.add(doc)
        await db.commit()

        with pytest.raises(HTTPException) as missing:
            await oauth.connect(practice.id, oauth.ConnectIn(staff_id=doc.id), db)
        assert missing.value.status_code == 503

        monkeypatch.setattr(s, "google_oauth_client_id", "client")
        monkeypatch.setattr(s, "google_oauth_client_secret", "secret")
        url = (await oauth.connect(practice.id, oauth.ConnectIn(staff_id=doc.id), db))["url"]
        state = url.split("state=")[1].split("&")[0]
        assert "calendar.freebusy" in url and "access_type=offline" in url

        monkeypatch.setattr(google_oauth, "exchange", AsyncMock(return_value=("refresh-1", "giorgos@example.com")))
        bad = await oauth.callback(state="forged", code="x", db=db)
        assert bad.headers["location"].endswith("result=bad_state")
        ok = await oauth.callback(state=state, code="x", db=db)
        assert ok.headers["location"] == "aicaller://calendar?result=ok"
        assert (await db.get(Staff, doc.id)).calendar_id == "giorgos@example.com"
        conns = await oauth.connections(practice.id, db)
        assert conns[0].refresh_token == "refresh-1"

        monkeypatch.setattr("app.database.async_session", sessions)
        monkeypatch.setattr(google_oauth, "refresh", AsyncMock(return_value=("access-1", 3600)))
        gcal._oauth_tokens.clear()
        assert await gcal._token("giorgos@example.com") == "access-1"
        monkeypatch.setattr(s, "google_service_account_json", "")
        with pytest.raises(RuntimeError):
            await gcal._token("someone-else@example.com")

        revoke = AsyncMock()
        monkeypatch.setattr(google_oauth, "revoke", revoke)
        await oauth.disconnect(practice.id, conns[0].id, db)
        revoke.assert_awaited_once_with("refresh-1")
        assert (await db.get(Staff, doc.id)).calendar_id is None
        assert await oauth.connections(practice.id, db) == []
