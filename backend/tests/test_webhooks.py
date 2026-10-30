"""Telnyx webhook: signed requests only; incoming SMS never change settings (audit P0-5)."""

import base64
import json
import time

import httpx
import pytest
from nacl.signing import SigningKey

from app.config import get_settings
from app.main import app


def _signed(key: SigningKey, body: bytes, timestamp: str | None = None) -> dict:
    timestamp = timestamp or str(int(time.time()))
    signature = key.sign(timestamp.encode() + b"|" + body).signature
    return {"telnyx-signature-ed25519": base64.b64encode(signature).decode(), "telnyx-timestamp": timestamp,
            "content-type": "application/json"}


@pytest.mark.asyncio
async def test_signed_sms_is_accepted_but_ignored(monkeypatch):
    key = SigningKey.generate()
    monkeypatch.setattr(get_settings(), "telnyx_public_key", base64.b64encode(bytes(key.verify_key)).decode())
    import app.routers.webhooks as webhooks
    assert not hasattr(webhooks, "_sms_received")
    body = json.dumps({"data": {"event_type": "message.received", "payload": {
        "from": {"phone_number": "+306911111111"}, "to": [{"phone_number": "+302100000000"}],
        "text": "Κλειστά όλη την εβδομάδα"}}}).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        ok = await http.post("/webhooks/telnyx", content=body, headers=_signed(key, body))
        assert ok.status_code == 200
        tampered = await http.post("/webhooks/telnyx", content=body + b" ", headers=_signed(key, body))
        assert tampered.status_code == 401
        stale = await http.post("/webhooks/telnyx", content=body,
                                headers=_signed(key, body, str(int(time.time()) - 3600)))
        assert stale.status_code == 401
