"""APNs push over HTTP/2 with a token (.p8) key. Needs a paid Apple developer account
and the Push Notifications capability; without APNS_* settings push is skipped."""

import time

import httpx
import jwt

from app.config import get_settings

_token: tuple[float, str] | None = None


def configured() -> bool:
    s = get_settings()
    return bool(s.apns_key_p8 and s.apns_key_id and s.apns_team_id)


def _auth() -> str:
    global _token
    if _token and time.time() - _token[0] < 50 * 60:
        return _token[1]
    s = get_settings()
    key = s.apns_key_p8.replace("\\n", "\n")
    token = jwt.encode({"iss": s.apns_team_id, "iat": int(time.time())}, key, algorithm="ES256",
                       headers={"kid": s.apns_key_id})
    _token = (time.time(), token)
    return token


async def send(device_token: str, *, title: str, body: str, data: dict, environment: str = "sandbox") -> None:
    host = "api.sandbox.push.apple.com" if environment == "sandbox" else "api.push.apple.com"
    payload = {"aps": {"alert": {"title": title, "body": body}, "sound": "default",
                       "interruption-level": "time-sensitive"}, **data}
    async with httpx.AsyncClient(http2=True, timeout=10) as client:
        r = await client.post(
            f"https://{host}/3/device/{device_token}",
            json=payload,
            headers={"authorization": f"bearer {_auth()}", "apns-topic": get_settings().apns_topic,
                     "apns-push-type": "alert", "apns-priority": "10"},
        )
        r.raise_for_status()
