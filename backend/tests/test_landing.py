"""Public landing page and lead request behavior."""

import base64
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app import crypto
from app.config import get_settings
from app.database import get_db
from app.main import app
from app.models import DemoLead, Notification


@pytest_asyncio.fixture
async def http(sessions):
    async def database():
        async with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = database
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_landing_renders_pilot_and_unavailable_demo(monkeypatch):
    monkeypatch.setattr(get_settings(), "landing_demo_slug", "")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")
        assert response.status_code == 200
        assert 'lang="el"' in response.text
        assert "14 ημέρες" in response.text
        assert "Η ζωντανή δοκιμή δεν είναι διαθέσιμη" in response.text
        assert '<iframe' not in response.text
        assert (await client.get("/privacy")).status_code == 200
        assert (await client.get("/terms")).status_code == 200


@pytest.mark.asyncio
async def test_lead_validation_and_unconfigured_destination():
    payload = {
        "name": "Δοκιμή", "business_name": "Ιατρείο Δοκιμής", "business_type": "medical",
        "contact_method": "email", "contact_detail": "test@example.com", "website": "",
        "website_trap": "", "request_id": uuid.uuid4().hex,
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/leads", json=payload, headers={"Origin": "https://outside.example"})).status_code == 403
        assert (await client.post("/leads", json={**payload, "contact_detail": "invalid"})).status_code == 422
        if not all((get_settings().founder_email, get_settings().smtp_host,
                    get_settings().email_from, get_settings().data_encryption_key)):
            assert (await client.post("/leads", json=payload)).status_code == 503


@pytest.mark.asyncio
async def test_lead_is_saved_queued_once_and_rate_limited(http, sessions, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "founder_email", "sales@example.com")
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "email_from", "demo@example.com")
    monkeypatch.setattr(settings, "data_encryption_key", base64.b64encode(b"x" * 32).decode())
    crypto._keys.cache_clear()
    try:
        payload = {
            "name": "Δοκιμή", "business_name": "Ιατρείο Δοκιμής", "business_type": "medical",
            "contact_method": "email", "contact_detail": "test@example.com", "website": "",
            "website_trap": "", "request_id": uuid.uuid4().hex,
        }
        assert (await http.post("/leads", json=payload, headers={"Accept": "application/json"})).status_code == 200
        assert (await http.post("/leads", json=payload, headers={"Accept": "application/json"})).status_code == 200
        async with sessions() as db:
            leads = (await db.execute(select(DemoLead))).scalars().all()
            queued = (await db.execute(select(Notification).where(Notification.kind == "demo_lead"))).scalars().all()
            assert len(leads) == len(queued) == 1
            assert leads[0].contact_detail == "test@example.com"
        for _ in range(4):
            assert (await http.post("/leads", json={**payload, "request_id": uuid.uuid4().hex})).status_code == 200
        assert (await http.post("/leads", json={**payload, "request_id": uuid.uuid4().hex})).status_code == 429
    finally:
        crypto._keys.cache_clear()
