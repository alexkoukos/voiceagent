"""The share link may reveal measurements only for calls explicitly attached to it."""
import hashlib
import json
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from app.config import get_settings
from app.database import get_db
from app.models import Call, CallStatus, CallTelemetryEvent, DemoDashboard, Practice
from app.routers import demo_dashboard as dashboard


@pytest_asyncio.fixture
async def shared_http(sessions, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_api_token", "demo-test-founder")
    app = FastAPI()
    app.include_router(dashboard.router)
    async def database():
        async with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = database
    async with sessions() as db:
        practice = Practice(name="Demo", slug="safe-demo", routing_rules={"demo_only": True}, services=[])
        other = Practice(name="Private", slug="private-clinic", routing_rules={}, services=[])
        db.add_all([practice, other])
        await db.commit()
        ids = practice.id, other.id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, ids


async def make_link(http, practice_id):
    result = await http.post(f"/practices/{practice_id}/demo-dashboard", json={},
                             headers={"x-api-key": "demo-test-founder"})
    assert result.status_code == 200, result.text
    return result.json()


@pytest.mark.asyncio
async def test_issuing_link_requires_founder_and_dedicated_demo(shared_http, sessions):
    http, (pid, private) = shared_http
    for headers in ({}, {"x-api-key": "dialer"}):
        assert (await http.post(f"/practices/{pid}/demo-dashboard", json={}, headers=headers)).status_code == 403
    assert (await http.post(f"/practices/{private}/demo-dashboard", json={},
                            headers={"x-api-key": "demo-test-founder"})).status_code == 409
    link = await make_link(http, pid)
    token = link['path'].split('/')[-1]
    async with sessions() as db:
        saved = await db.get(DemoDashboard, link['id'])
        assert saved.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert saved.token_hash != token
    page = await http.get(link['path'])
    assert page.status_code == 200
    assert page.headers['cache-control'] == 'no-store'
    assert page.headers['referrer-policy'] == 'no-referrer'
    assert 'What I’ve learned' not in page.text and 'Three different prices' not in page.text and 'Founder API key' not in page.text
    assert (await http.get('/demo-dashboard/' + 'a' * 43 + '/data')).status_code == 404


@pytest.mark.asyncio
async def test_metrics_isolate_links_and_omit_private_fields(shared_http, sessions):
    http, (pid, other_pid) = shared_http
    first, second = await make_link(http, pid), await make_link(http, pid)
    async with sessions() as db:
        def call(**kwargs):
            return Call(persona='', scenario='', practice_id=pid, direction='web',
                        status=CallStatus.completed, **kwargs)
        visible = call(demo_dashboard_id=first['id'], summary='PRIVATE NAME', duration_seconds=60,
                       outcome='booked', cost_breakdown={'known_usd': .2, 'complete': True,
                       'usage_reported': True, 'pricing_version': 'test', 'lines': [
                           {'component': 'llm', 'cost_usd': .2}]})
        hidden = [call(demo_dashboard_id=second['id']), call(),
                  call(demo_dashboard_id=first['id'], delete_requested=True)]
        phone = call(demo_dashboard_id=first['id']); phone.direction = 'inbound'
        foreign = call(demo_dashboard_id=first['id']); foreign.practice_id = other_pid
        db.add_all([visible, *hidden, phone, foreign]); await db.flush()
        for c, value in [(visible, 800), (hidden[0], 99999), (phone, 99999)]:
            db.add(CallTelemetryEvent(call_id=c.id, session_id=str(uuid.uuid4()), sequence=1,
                                      payload={'event': 'answer_latency_estimate', 'latency_ms': value,
                                               'extra_private': 'PRIVATE NAME'}))
        await db.commit()
    response = await http.get(first['path'] + '/data')
    assert response.status_code == 200
    data = response.json()
    assert data['count'] == 1 and data['bookings'] == 1
    assert data['answer']['p50_ms'] == 800 and data['answer']['samples'] == 1
    assert data['cost']['known_usd'] == .2 and data['cost']['per_minute_usd'] == .2
    assert 'PRIVATE' not in response.text and 'summary' not in response.text
    assert 'transcript' not in data and 'caller_number' not in response.text
    assert visible.id not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['expired', 'revoked', 'offboarded', 'not_demo'])
async def test_disabled_links_deny_every_surface(shared_http, sessions, reason):
    http, (pid, _) = shared_http
    link = await make_link(http, pid)
    async with sessions() as db:
        saved = await db.get(DemoDashboard, link['id'])
        practice = await db.get(Practice, pid)
        if reason == 'expired': saved.expires_at = datetime.utcnow() - timedelta(seconds=1)
        elif reason == 'revoked': saved.revoked_at = datetime.utcnow()
        elif reason == 'offboarded': practice.offboarded_at = datetime.utcnow()
        else: practice.routing_rules = {}
        await db.commit()
    for suffix in ('', '/data', '/call'):
        assert (await http.get(link['path'] + suffix)).status_code == 404
    assert (await http.post(link['path'] + '/call/session')).status_code == 404


@pytest.mark.asyncio
async def test_revocation_is_scoped_and_requires_founder(shared_http):
    http, (pid, other) = shared_http
    link = await make_link(http, pid)
    path = f"/practices/{pid}/demo-dashboard/{link['id']}"
    assert (await http.delete(path)).status_code == 403
    headers = {'x-api-key': 'demo-test-founder'}
    assert (await http.delete(f"/practices/{other}/demo-dashboard/{link['id']}", headers=headers)).status_code == 404
    assert (await http.delete(path, headers=headers)).status_code == 204
    assert (await http.get(link['path'] + '/data')).status_code == 404


@pytest.mark.asyncio
async def test_session_attaches_link_before_dispatch(shared_http, sessions, monkeypatch):
    from app.routers import demo
    http, (pid, _) = shared_http
    link = await make_link(http, pid)
    async def start_call(db, practice, **kwargs):
        call = Call(practice_id=practice.id, persona='', scenario='', direction='web')
        db.add(call); await db.commit()
        return call, {'call_id': call.id}
    async def dispatch(room, metadata):
        async with sessions() as db:
            saved = await db.get(Call, metadata['call_id'])
            assert saved.demo_dashboard_id == link['id']
            assert saved.voice == metadata['voice'] == 'eleven_sarah'
            assert (await db.get(Practice, pid)).voice != 'eleven_sarah'
    monkeypatch.setattr(demo.receptionist, 'start_call', start_call)
    monkeypatch.setattr(demo.receptionist, 'dispatch', dispatch)
    monkeypatch.setattr(demo.receptionist, 'room_token', lambda *args: 'room-token')
    monkeypatch.setattr(demo, 'active_count', AsyncMock(return_value=0))
    assert (await http.post(link['path'] + '/call/session', json={'voice':'arbitrary-provider-id'})).status_code == 422
    response = await http.post(link['path'] + '/call/session', json={'voice':'eleven_sarah'})
    assert response.status_code == 200, response.text
    assert response.json()['token'] == 'room-token'


def test_missing_cost_and_nonfinite_timings_stay_unknown():
    call = SimpleNamespace(id='call', status=CallStatus.active, outcome=None, duration_seconds=None,
                           created_at=datetime.utcnow(), cost_breakdown=None)
    result = dashboard.summarize([call], [('call', {'event': 'answer_latency_estimate', 'latency_ms': float('nan')}),
                                         ('other', {'event': 'answer_latency_estimate', 'latency_ms': 1})])
    assert result['answer']['p50_ms'] is None and result['answer']['samples'] == 0
    assert result['cost']['known_usd'] is None and result['resolved_pct'] is None
    call.cost_breakdown = {'known_usd': 0, 'usage_reported': True, 'complete': False,
                          'lines': [{'component': 'tts', 'cost_usd': None}]}
    result = dashboard.summarize([call], [])
    assert result['cost']['known_usd'] == 0
    assert result['cost']['unpriced_components'] == ['tts']
    assert result['cost']['complete_calls'] == 0
    assert result['cost']['per_minute_usd'] is None


def test_median_averages_the_two_middle_measurements():
    assert dashboard.percentile([1000, 3000], .5) == 2000
    assert dashboard.percentile([], .5) is None

@pytest.mark.asyncio
async def test_permanent_link_is_owner_controlled_and_still_revocable(shared_http, sessions):
    http, (pid, other) = shared_http
    link = await make_link(http, pid)
    path = f"/practices/{pid}/demo-dashboard/{link['id']}"
    body = {'expires_in_days': None}
    assert (await http.patch(path, json=body)).status_code == 403
    headers = {'x-api-key': 'demo-test-founder'}
    assert (await http.patch(f"/practices/{other}/demo-dashboard/{link['id']}", json=body, headers=headers)).status_code == 404
    assert (await http.patch(path, json=body, headers=headers)).json()['expires_at'] is None
    async with sessions() as db:
        assert (await db.get(DemoDashboard, link['id'])).expires_at is None
    page = await http.get(link['path'])
    assert page.status_code == 200 and 'No automatic expiry' in page.text
    assert 'Astra · Interactive demo' not in page.text
    assert (await http.get(link['path'] + '/data')).json()['expires_at'] is None
    assert (await http.get(link['path'] + '/call')).status_code == 200
    assert (await http.delete(path, headers=headers)).status_code == 204
    assert (await http.get(link['path'])).status_code == 404
    assert (await http.patch(path, json=body, headers=headers)).status_code == 404
    permanent = await http.post(f'/practices/{pid}/demo-dashboard', json=body, headers=headers)
    assert permanent.status_code == 200 and permanent.json()['expires_at'] is None
