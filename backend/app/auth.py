"""Founder, Dialer and revocable single-practice API scopes."""
import hashlib
import secrets
from datetime import datetime

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from app.config import get_settings
from app.database import get_db
from app.models import Practice, TenantApiKey


def _matches(value: str, expected: str) -> bool:
    return bool(expected) and secrets.compare_digest(value, expected)


def is_master(value: str) -> bool:
    s = get_settings()
    return s.admin_api_token != s.app_api_token and _matches(value, s.admin_api_token)


def require_master_token(x_api_key: str = Header(default="")) -> None:
    if not is_master(x_api_key):
        raise HTTPException(status_code=403, detail="Founder API key required")


def require_app_token(x_api_key: str = Header(default="")) -> None:
    if not (is_master(x_api_key) or _matches(x_api_key, get_settings().app_api_token)):
        raise HTTPException(status_code=401, detail="Invalid API key")


async def require_admin_token(connection: HTTPConnection, db: AsyncSession = Depends(get_db)) -> None:
    value = connection.headers.get("x-api-key", "")
    if is_master(value):
        db.info["tenant_id"] = None
        return
    key = await db.get(TenantApiKey, hashlib.sha256(value.encode()).hexdigest()) if value else None
    if not key or key.revoked_at or (key.expires_at and key.expires_at <= datetime.utcnow()):
        raise HTTPException(status_code=403, detail="Admin API key required")
    practice = await db.get(Practice, key.practice_id)
    if not practice or practice.offboarded_at:
        raise HTTPException(status_code=403, detail="Inactive practice")
    tenant = connection.path_params.get("practice_id")
    if tenant and tenant != key.practice_id:
        raise HTTPException(status_code=404, detail="Not found")
    # Global creation is reserved to the founder. Lists filter by this scope below.
    if connection.url.path.rstrip("/") == "/practices" and connection.scope.get("method") != "GET":
        raise HTTPException(status_code=403, detail="Founder API key required")
    db.info["tenant_id"] = key.practice_id
