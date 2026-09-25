import boto3
from botocore.config import Config

from app.config import get_settings


def storage_configured() -> bool:
    s = get_settings()
    return bool(s.aws_endpoint_url and s.aws_access_key_id and s.aws_secret_access_key and s.aws_s3_bucket_name)


def _client():
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.aws_endpoint_url,
        aws_access_key_id=s.aws_access_key_id,
        aws_secret_access_key=s.aws_secret_access_key,
        region_name=s.aws_default_region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if s.aws_s3_url_style == "path" else "virtual"},
        ),
    )


def recording_exists(key: str) -> bool:
    """Egress uploads the file a few seconds after the call ends; until then it isn't there."""
    from botocore.exceptions import ClientError

    try:
        _client().head_object(Bucket=get_settings().aws_s3_bucket_name, Key=key)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def presigned_recording_url(key: str, expires_seconds: int = 600) -> str:
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": get_settings().aws_s3_bucket_name, "Key": key},
        ExpiresIn=expires_seconds,
    )


def delete_recording(key: str) -> None:
    _client().delete_object(Bucket=get_settings().aws_s3_bucket_name, Key=key)


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
