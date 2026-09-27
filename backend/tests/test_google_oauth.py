"""O3: a doctor connects their own calendar; gcal then uses their token for it."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app import gcal, google_oauth
from app.config import get_settings
from app.models import Practice, Staff
from app.routers import oauth


@pytest.mark.asyncio
async def test_exchange_uses_verified_userinfo_not_id_token(monkeypatch):
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url == google_oauth.TOKEN_URL:
            return httpx.Response(200, json={"refresh_token": "refresh-1", "access_token": "access-1",
                                             "id_token": "untrusted"})
        if request.url == google_oauth.USERINFO_URL:
            return httpx.Response(200, json={"email": "owner@example.com", "email_verified": True})
        return httpx.Response(404)

    client = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(google_oauth.httpx, "AsyncClient",
                        lambda **kwargs: client(transport=transport, **kwargs))
    assert await google_oauth.exchange("code") == ("refresh-1", "owner@example.com")
    assert requests[1].headers["authorization"] == "Bearer access-1"


@pytest.mark.asyncio
async def test_exchange_rejects_unverified_email(monkeypatch):
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url == google_oauth.TOKEN_URL:
            return httpx.Response(200, json={"refresh_token": "refresh-1", "access_token": "access-1"})
        return httpx.Response(200, json={"email": "unverified@example.com", "email_verified": False})

    client = httpx.AsyncClient
    monkeypatch.setattr(google_oauth.httpx, "AsyncClient",
                        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    with pytest.raises(google_oauth.OAuthError, match="unverified_email"):
        await google_oauth.exchange("code")


@pytest.mark.asyncio
async def test_callback_clears_replaced_calendar_token(monkeypatch):
    old = SimpleNamespace(calendar_id="old@example.com")
    staff = SimpleNamespace(calendar_id="old@example.com", practice_id="practice", offboarded_at=None)

    class Session:
        async def execute(self, _query):
            return SimpleNamespace(scalars=lambda: [old])

        async def get(self, _model, _id):
            return staff

        async def delete(self, _item):
            pass

        def add(self, _item):
            pass

        async def commit(self):
            pass

    monkeypatch.setattr(oauth, "validate_assignments", AsyncMock())
    monkeypatch.setattr(google_oauth, "read_state", lambda _state: ("practice", "staff"))
    monkeypatch.setattr(google_oauth, "exchange", AsyncMock(return_value=("refresh", "new@example.com")))
    gcal._oauth_tokens["old@example.com"] = ("old-access", 9999999999)
    try:
        result = await oauth.callback(state="state", code="code", db=Session())
        assert result.headers["location"] == "aicaller://calendar?result=ok"
        assert "old@example.com" not in gcal._oauth_tokens
        assert staff.calendar_id == "new@example.com"
    finally:
        gcal._oauth_tokens.clear()


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
        monkeypatch.setattr(google_oauth, "exchange", AsyncMock(return_value=("refresh-2", "new@example.com")))
        ok = await oauth.callback(state=state, code="new-code", db=db)
        assert ok.headers["location"] == "aicaller://calendar?result=ok"
        assert "giorgos@example.com" not in gcal._oauth_tokens
        assert (await db.get(Staff, doc.id)).calendar_id == "new@example.com"
        conns = await oauth.connections(practice.id, db)
        assert len(conns) == 1 and conns[0].refresh_token == "refresh-2"
        monkeypatch.setattr(s, "google_service_account_json", "")
        with pytest.raises(RuntimeError):
            await gcal._token("someone-else@example.com")

        revoke = AsyncMock()
        monkeypatch.setattr(google_oauth, "revoke", revoke)
        await oauth.disconnect(practice.id, conns[0].id, db)
        revoke.assert_awaited_once_with("refresh-2")
        assert (await db.get(Staff, doc.id)).calendar_id is None
        assert await oauth.connections(practice.id, db) == []
