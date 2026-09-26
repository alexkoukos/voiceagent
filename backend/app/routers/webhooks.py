import base64
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from datetime import datetime, timezone

from sqlalchemy import select

from app import admin_changes, notifications
from app.config import get_settings
from app.database import async_session
from app.models import Staff
from app.schemas import normalize_phone

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
    event = data.get("event_type")
    if event == "message.received":
        await _sms_received(data.get("payload") or {})
    else:
        logger.info("telnyx event %s", event)
    return {"received": True}


async def _sms_received(p: dict) -> None:
    """A business texting a change (OP2). Message text is not logged: it can hold patient names."""
    sender = normalize_phone((p.get("from") or {}).get("phone_number", ""))
    to = normalize_phone(((p.get("to") or [{}])[0]).get("phone_number", ""))
    text = (p.get("text") or "").strip()
    if not sender or not text:
        return
    async with async_session() as db:
        reply = await admin_changes.handle_sms(db, sender, to, text, datetime.now(timezone.utc))
        if reply:
            practice_id = (await db.execute(select(Staff.practice_id).where(Staff.phone == sender))).scalars().first()
            await notifications.queue(db, practice_id=practice_id, kind="admin_sms_reply", channel="sms",
                                      recipient=sender, body=reply)
        await db.commit()
    notifications.kick()
