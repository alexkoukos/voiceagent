import secrets

from fastapi import Header, HTTPException

from app.config import get_settings


def require_app_token(x_api_key: str = Header(default="")) -> None:
    expected = get_settings().app_api_token
    if not expected or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid API key")
