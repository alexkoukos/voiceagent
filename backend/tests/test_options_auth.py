"""The iOS connection test accepts scoped practice keys without granting dialer features."""
import hashlib
from types import SimpleNamespace

import httpx
import pytest

from app.config import get_settings
from app.database import get_db
from app.main import app
from app.models import Practice, TenantApiKey


@pytest.mark.asyncio
async def test_options_reports_practice_scope(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_api_token", "founder-test")
    monkeypatch.setattr(settings, "app_api_token", "dialer-test")
    monkeypatch.setattr(settings, "own_caller_number", "+302100000000")

    class Database:
        def __init__(self):
            self.info = {}

        async def get(self, model, key):
            if model is TenantApiKey and key == hashlib.sha256(b"practice-test").hexdigest():
                return SimpleNamespace(practice_id="practice-1", revoked_at=None, expires_at=None)
            if model is Practice and key == "practice-1":
                return SimpleNamespace(offboarded_at=None)
            return None

    async def database():
        yield Database()

    app.dependency_overrides[get_db] = database
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            tenant = await client.get("/options", headers={"x-api-key": "practice-test"})
            assert tenant.status_code == 200
            assert tenant.json()["account_scope"] == "practice"
            assert tenant.json()["own_number_available"] is False

            founder = await client.get("/options", headers={"x-api-key": "founder-test"})
            assert founder.json()["account_scope"] == "founder"
            assert founder.json()["own_number_available"] is True

            dialer = await client.get("/options", headers={"x-api-key": "dialer-test"})
            assert dialer.json()["account_scope"] == "dialer"
            assert (await client.get("/options", headers={"x-api-key": "wrong"})).status_code == 403
    finally:
        app.dependency_overrides.clear()
