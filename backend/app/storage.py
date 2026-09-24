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
