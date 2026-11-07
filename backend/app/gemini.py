"""One place that calls Gemini's generateContent for the backend (call summaries, imports,
settings changes). With GEMINI_VERTEX_LOCATION set (e.g. eu) requests go to Vertex AI
in that region, so they are processed in the EU; otherwise to the AI Studio API with the key."""

import asyncio
import json
from functools import lru_cache

import httpx

from app.config import get_settings


class NotConfigured(Exception):
    pass


def _vertex_host(location: str) -> str:
    """Same rule as the google-genai SDK the agent uses: "eu"/"us" are multi-regions with their own host."""
    if location == "global":
        return "aiplatform.googleapis.com"
    if location in {"eu", "us"}:
        return f"aiplatform.{location}.rep.googleapis.com"
    return f"{location}-aiplatform.googleapis.com"


def configured() -> bool:
    s = get_settings()
    return bool(s.gemini_vertex_location or s.gemini_api_key)


@lru_cache
def _credentials():
    from google.oauth2 import service_account

    info = json.loads(get_settings().google_service_account_json)
    return service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/cloud-platform"])


async def _token() -> str:
    creds = _credentials()
    if not creds.valid:
        from google.auth.transport.requests import Request

        await asyncio.to_thread(creds.refresh, Request())
    return creds.token


async def generate(model: str, body: dict, *, timeout: float) -> httpx.Response:
    s = get_settings()
    if s.gemini_vertex_location:
        if not (s.gemini_vertex_project and s.google_service_account_json):
            raise NotConfigured("GEMINI_VERTEX_PROJECT and GOOGLE_SERVICE_ACCOUNT_JSON are needed for Vertex AI")
        loc = s.gemini_vertex_location
        url = (f"https://{_vertex_host(loc)}/v1/projects/{s.gemini_vertex_project}"
               f"/locations/{loc}/publishers/google/models/{model}:generateContent")
        kwargs = {"headers": {"Authorization": f"Bearer {await _token()}"}}
    elif s.gemini_api_key:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        kwargs = {"params": {"key": s.gemini_api_key}}
    else:
        raise NotConfigured("no Gemini credentials")
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(url, json=body, **kwargs)
