"""A doctor connects their own Google Calendar (PRD O3): sign in once, in a private browser
session, and their primary calendar is used for their bookings. Only a refresh token is kept,
encrypted at rest; disconnecting revokes it at Google.

Needs a Google OAuth "Web application" client (GOOGLE_OAUTH_CLIENT_ID / _SECRET) whose
redirect URI is <BACKEND_PUBLIC_URL>/oauth/google/callback. Calendar scopes are "sensitive":
until Google verifies the app, up to 100 test users can connect.
"""

import time
from urllib.parse import urlencode

import httpx
import jwt

from app import crypto
from app.config import get_settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPES = " ".join([
    "openid", "email",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
])
STATE_SECONDS = 600
# ASWebAuthenticationSession finishes when the browser reaches this scheme.
APP_RETURN = "aicaller://calendar"


class OAuthError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def configured() -> bool:
    s = get_settings()
    return bool(s.google_oauth_client_id and s.google_oauth_client_secret)


def redirect_uri() -> str:
    return get_settings().backend_public_url.rstrip("/") + "/oauth/google/callback"


def _state_key() -> bytes:
    s = get_settings()
    return crypto.token_secret() if crypto.enabled() else (s.app_api_token or "dev").encode()


def authorize_url(practice_id: str, staff_id: str | None) -> str:
    if not configured():
        raise OAuthError("google_oauth_not_configured")
    state = jwt.encode({"p": practice_id, "s": staff_id, "exp": int(time.time()) + STATE_SECONDS},
                       _state_key(), "HS256")
    return AUTH_URL + "?" + urlencode({
        "client_id": get_settings().google_oauth_client_id, "redirect_uri": redirect_uri(),
        "response_type": "code", "scope": SCOPES, "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "true", "state": state,
    })


def read_state(state: str) -> tuple[str, str | None]:
    try:
        data = jwt.decode(state, _state_key(), algorithms=["HS256"])
    except jwt.PyJWTError:
        raise OAuthError("bad_state")
    return data["p"], data.get("s")


async def exchange(code: str) -> tuple[str, str]:
    """(refresh token, the Google account's email)."""
    s = get_settings()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(TOKEN_URL, data={
            "code": code, "client_id": s.google_oauth_client_id, "client_secret": s.google_oauth_client_secret,
            "redirect_uri": redirect_uri(), "grant_type": "authorization_code",
        })
    if r.status_code != 200:
        raise OAuthError("exchange_failed")
    data = r.json()
    if not data.get("refresh_token"):
        raise OAuthError("no_refresh_token")
    email = jwt.decode(data["id_token"], options={"verify_signature": False}).get("email", "") if data.get("id_token") else ""
    if not email:
        raise OAuthError("no_email")
    return data["refresh_token"], email


async def refresh(refresh_token: str) -> tuple[str, int]:
    s = get_settings()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(TOKEN_URL, data={
            "refresh_token": refresh_token, "client_id": s.google_oauth_client_id,
            "client_secret": s.google_oauth_client_secret, "grant_type": "refresh_token",
        })
    if r.status_code != 200:
        # Revoked or expired: calendar calls fail and booking falls back to messages (B7).
        raise RuntimeError(f"calendar token refresh failed: {r.status_code}")
    data = r.json()
    return data["access_token"], int(data.get("expires_in", 3600))


async def revoke(refresh_token: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(REVOKE_URL, data={"token": refresh_token})
