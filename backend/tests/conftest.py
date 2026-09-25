"""Opt-in Postgres tests, isolated from application tables and external providers."""

import os
import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import finalize, gcal, notifications
from app.database import Base


@pytest_asyncio.fixture
async def sessions(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run isolated Postgres integration tests")
    schema = "test_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(gcal, "configured", lambda: False)
        monkeypatch.setattr(notifications, "async_session", factory)
        monkeypatch.setattr(finalize, "async_session", factory)
        monkeypatch.setattr(finalize, "summarize", AsyncMock(return_value="Test summary."))
        monkeypatch.setattr(notifications, "send", AsyncMock())
        yield factory
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
