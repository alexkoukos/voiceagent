import boto3
from botocore.config import Config

from app.config import get_settings


def _client():
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=f"https://{s.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=s.r2_access_key_id,
        aws_secret_access_key=s.r2_secret_access_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def presigned_recording_url(key: str, expires_seconds: int = 600) -> str:
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": get_settings().r2_bucket_name, "Key": key},
        ExpiresIn=expires_seconds,
    )


def delete_recording(key: str) -> None:
    _client().delete_object(Bucket=get_settings().r2_bucket_name, Key=key)


_background: set = set()


def delete_recording_later(key: str) -> None:
    """Egress finishes uploading after the call ends, so retry the delete a few times."""
    import asyncio
    import logging

    async def _run() -> None:
        for delay in (0, 30, 120, 300):
            await asyncio.sleep(delay)
            try:
                await asyncio.to_thread(delete_recording, key)
            except Exception:
                logging.getLogger(__name__).exception("recording delete failed for %s", key)

    task = asyncio.get_running_loop().create_task(_run())
    _background.add(task)
    task.add_done_callback(_background.discard)
