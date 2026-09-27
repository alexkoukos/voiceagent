from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app import health
from app.config import get_settings
from app.models import Alert, Device, HealthCheck, Notification, Practice


@pytest.mark.asyncio
async def test_probe_acknowledgement_during_dispatch_is_not_lost(sessions, monkeypatch):
    settings=get_settings()
    for key,value in {"livekit_url":"wss://test","livekit_api_key":"test","health_agent_check_minutes":5}.items():
        monkeypatch.setattr(settings,key,value)
    monkeypatch.setattr(health,"_last_agent",None)
    monkeypatch.setattr(health,"_last_livekit",None)
    monkeypatch.setattr(health,"_pending",{})
    monkeypatch.setattr(health,"_livekit_ok",AsyncMock(return_value=None))
    async def dispatch(token):
        assert health.acknowledge(token)
    monkeypatch.setattr(health,"_dispatch_probe",dispatch)
    async with sessions() as db:
        now=datetime.utcnow()
        await health.check(db,now)
        await health.check(db,now+timedelta(seconds=65))
        assert not health._pending
        assert (await db.execute(select(Alert))).scalars().all()==[]


@pytest.mark.asyncio
async def test_stale_monitor_alerts_only_founder_and_deduplicates(sessions, monkeypatch):
    monkeypatch.setattr(get_settings(),"health_synthetic_checks_enabled",True)
    async with sessions() as db:
        practice,other=Practice(name="A"),Practice(name="B")
        db.add_all([practice,other]);await db.flush()
        db.add_all([Device(token="founder",practice_id=None),Device(token="other-tenant",practice_id=other.id)])
        await db.commit()
        now=datetime.utcnow()
        await health.check_synthetic_reports(db,practice,now)
        await health.check_synthetic_reports(db,practice,now)
        await db.commit()
        assert len((await db.execute(select(Alert))).scalars().all())==1
        pushes=(await db.execute(select(Notification).where(Notification.channel=="push"))).scalars().all()
        assert [p.recipient for p in pushes]==["founder"]
        assert pushes[0].practice_id==practice.id
        db.add(HealthCheck(practice_id=practice.id,kind="web",success=True,created_at=now))
        await db.commit()
        await health.check_synthetic_reports(db,practice,now+timedelta(minutes=5))
        assert len((await db.execute(select(Alert))).scalars().all())==1
