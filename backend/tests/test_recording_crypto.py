"""Recordings are encrypted after upload and played back through a short-lived link with ranges."""

import base64
import io
import os
import time
from datetime import datetime, timedelta

import jwt
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import crypto, storage
from app.config import get_settings
from app.models import Call, CallStatus
from app.routers import calls as calls_router
from app.routers import recordings


class FakeS3:
    def __init__(self):
        self.objects = {}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


@pytest.fixture
def setup(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "data_encryption_key", base64.b64encode(os.urandom(32)).decode())
    for k, v in (("aws_endpoint_url", "http://x"), ("aws_access_key_id", "a"), ("aws_secret_access_key", "b"),
                 ("aws_s3_bucket_name", "bucket")):
        monkeypatch.setattr(s, k, v)
    crypto._keys.cache_clear()
    crypto.token_secret.cache_clear()
    recordings._cache.clear()
    fake = FakeS3()
    monkeypatch.setattr(storage, "_client", lambda: fake)
    yield fake
    crypto._keys.cache_clear()
    crypto.token_secret.cache_clear()


def _request(range_header=None):
    headers = [(b"range", range_header.encode())] if range_header else []
    return Request({"type": "http", "headers": headers, "method": "GET", "path": "/"})


@pytest.mark.asyncio
async def test_seal_and_play(sessions, setup):
    audio = bytes(range(256)) * 40
    setup.objects["recordings/c1.mp4"] = audio
    async with sessions() as db:
        call = Call(persona="", scenario="", status=CallStatus.completed, recording_url="recordings/c1.mp4",
                    ended_at=datetime.utcnow() - timedelta(minutes=5))
        db.add(call)
        await db.commit()
        assert await storage.seal_recordings(db) == 1
        call = await db.get(Call, call.id)
        assert call.recording_url == "recordings/c1.mp4.enc"
        assert "recordings/c1.mp4" not in setup.objects
        assert audio not in setup.objects["recordings/c1.mp4.enc"]

        link = (await calls_router.get_recording(call.id, db))["url"]
        token = link.split("/recordings/")[1].split("/")[0]
        full = await recordings.play(token, _request(), db)
        assert full.body == audio and full.status_code == 200
        part = await recordings.play(token, _request("bytes=100-199"), db)
        assert part.status_code == 206 and part.body == audio[100:200]
        assert part.headers["content-range"] == f"bytes 100-199/{len(audio)}"
        tail = await recordings.play(token, _request("bytes=-10"), db)
        assert tail.body == audio[-10:]

        expired = jwt.encode({"c": call.id, "exp": int(time.time()) - 1}, crypto.token_secret(), "HS256")
        with pytest.raises(HTTPException):
            await recordings.play(expired, _request(), db)
        forged = jwt.encode({"c": call.id, "exp": int(time.time()) + 60}, b"x" * 32, "HS256")
        with pytest.raises(HTTPException):
            await recordings.play(forged, _request(), db)
