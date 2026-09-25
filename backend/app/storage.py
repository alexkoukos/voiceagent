import boto3
import asyncio
import logging
from datetime import datetime, timedelta
from botocore.config import Config
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.database import async_session
from app.models import Call, CallStatus, RecordingDeletion

logger = logging.getLogger(__name__)


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


async def queue_recording_deletion(db, key: str, call_id: str | None = None) -> None:
    """Keep the object key in Postgres until deletion succeeds after egress settles."""
    await db.execute(pg_insert(RecordingDeletion).values(key=key, call_id=call_id)
                     .on_conflict_do_nothing(index_elements=[RecordingDeletion.key]))


async def process_recording_deletions() -> int:
    """Idempotent, durable retry worker. A late egress upload is swept for 15 minutes."""
    processed = 0
    async with async_session() as db:
        due = list((await db.execute(select(RecordingDeletion.key).where(
            RecordingDeletion.next_attempt_at <= datetime.utcnow(),
        ).order_by(RecordingDeletion.next_attempt_at).limit(20))).scalars())
    for key in due:
        async with async_session() as db:
            item = (await db.execute(select(RecordingDeletion).where(RecordingDeletion.key == key)
                                     .with_for_update(skip_locked=True))).scalar_one_or_none()
            if item is None or item.next_attempt_at > datetime.utcnow():
                continue
            now = datetime.utcnow()
            call = await db.get(Call, item.call_id) if item.call_id else None
            if call and call.status in (CallStatus.pending, CallStatus.queued, CallStatus.dialing, CallStatus.active):
                item.next_attempt_at = now + timedelta(minutes=1)
                await db.commit()
                continue
            try:
                await asyncio.to_thread(delete_recording, item.key)
                still_exists = await asyncio.to_thread(recording_exists, item.key)
                if still_exists:
                    raise RuntimeError("object still exists after delete")
                settled_at = (call.ended_at if call and call.ended_at else item.created_at) + timedelta(minutes=15)
                if now >= settled_at:
                    await db.delete(item)
                else:
                    item.next_attempt_at = now + timedelta(minutes=1)
                    item.last_error = None
                processed += 1
            except Exception as exc:
                logger.exception("recording deletion failed for %s", item.key)
                item.attempts += 1
                item.last_error = str(exc)[:500]
                item.next_attempt_at = now + timedelta(seconds=min(3600, 30 * 2 ** min(item.attempts, 7)))
            await db.commit()
    return processed
