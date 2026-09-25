import base64
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from app.config import get_settings

logger = logging.getLogger("telnyx")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

MAX_SKEW_SECONDS = 300


def _verify(public_key_b64: str, signature_b64: str, timestamp: str, body: bytes) -> None:
    try:
        if abs(time.time() - int(timestamp)) > MAX_SKEW_SECONDS:
            raise ValueError("stale timestamp")
        VerifyKey(base64.b64decode(public_key_b64)).verify(
            timestamp.encode() + b"|" + body, base64.b64decode(signature_b64)
        )
    except (BadSignatureError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid Telnyx signature")


@router.post("/telnyx", status_code=200)
async def telnyx_webhook(request: Request):
    body = await request.body()
    public_key = get_settings().telnyx_public_key
    if not public_key:
        # Fail closed: without the key we can't tell Telnyx from anyone else.
        raise HTTPException(status_code=503, detail="Telnyx webhook not configured")
    _verify(
        public_key,
        request.headers.get("telnyx-signature-ed25519", ""),
        request.headers.get("telnyx-timestamp", ""),
        body,
    )
    payload = await request.json()
    data = payload.get("data", {})
    logger.info("telnyx event %s: %s", data.get("event_type"), data.get("payload"))
    return {"received": True}
