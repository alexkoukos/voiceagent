"""Playback of encrypted recordings. The app gets a 10-minute link from
GET /calls/<id>/recording; this route checks it, decrypts the file and serves byte ranges,
which AVPlayer needs for seeking. The link is the only credential, so it is short-lived and
bound to one call."""

import re
import time
from collections import OrderedDict

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app import crypto
from app.config import get_settings
from app.database import get_db
from app.models import Call
from app.storage import SEALED, open_recording

router = APIRouter(prefix="/recordings", tags=["recordings"])
LINK_SECONDS = 600
# A few decrypted files, so AVPlayer's range requests don't decrypt the same file each time.
_cache: "OrderedDict[str, bytes]" = OrderedDict()
CACHE_FILES = 4


def playback_url(call_id: str) -> str:
    token = jwt.encode({"c": call_id, "exp": int(time.time()) + LINK_SECONDS}, crypto.token_secret(), "HS256")
    return f"{get_settings().backend_public_url.rstrip('/')}/recordings/{token}/audio.mp4"


async def _audio(key: str) -> bytes:
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    data = await run_in_threadpool(open_recording, key)
    _cache[key] = data
    while len(_cache) > CACHE_FILES:
        _cache.popitem(last=False)
    return data


@router.get("/{token}/audio.mp4")
async def play(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        call_id = jwt.decode(token, crypto.token_secret(), algorithms=["HS256"])["c"]
    except (jwt.PyJWTError, KeyError, crypto.KeyError_, ValueError):
        raise HTTPException(status_code=404, detail="expired")
    call = await db.get(Call, call_id)
    if call is None or not (call.recording_url or "").endswith(SEALED):
        raise HTTPException(status_code=404, detail="No recording")
    data = await _audio(call.recording_url)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", request.headers.get("range", ""))
    if m and (m.group(1) or m.group(2)):
        size = len(data)
        if m.group(1):
            start = int(m.group(1))
            end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
        else:
            start, end = max(0, size - int(m.group(2))), size - 1
        if start >= size or start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(data[start:end + 1], status_code=206, media_type="audio/mp4", headers=headers)
    return Response(data, media_type="audio/mp4", headers=headers)
