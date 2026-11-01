"""Gemini calls go to Vertex AI in the configured EU region, or to AI Studio with the key."""

import httpx
import pytest

from app import gemini
from app.config import get_settings


@pytest.mark.asyncio
async def test_endpoint_follows_settings(monkeypatch):
    sent = []

    async def post(self, url, **kwargs):
        sent.append((url, kwargs))
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    s = get_settings()
    monkeypatch.setattr(s, "gemini_api_key", "key")
    monkeypatch.setattr(s, "gemini_vertex_location", "")
    await gemini.generate("gemini-3.5-flash-lite", {"contents": []}, timeout=1)
    assert sent[-1][0].startswith("https://generativelanguage.googleapis.com/")
    assert sent[-1][1]["params"] == {"key": "key"}

    monkeypatch.setattr(s, "gemini_vertex_location", "europe-west3")
    monkeypatch.setattr(s, "gemini_vertex_project", "proj")
    monkeypatch.setattr(s, "google_service_account_json", "{}")

    async def token():
        return "tok"

    monkeypatch.setattr(gemini, "_token", token)
    await gemini.generate("gemini-3.5-flash-lite", {"contents": []}, timeout=1)
    url, kwargs = sent[-1]
    assert url == ("https://europe-west3-aiplatform.googleapis.com/v1/projects/proj/locations/europe-west3"
                   "/publishers/google/models/gemini-3.5-flash-lite:generateContent")
    assert kwargs["headers"] == {"Authorization": "Bearer tok"} and "params" not in kwargs

    monkeypatch.setattr(s, "gemini_vertex_location", "eu")  # multi-region: own host, else Google answers 400
    await gemini.generate("gemini-3.5-flash-lite", {"contents": []}, timeout=1)
    assert sent[-1][0].startswith("https://aiplatform.eu.rep.googleapis.com/v1/projects/proj/locations/eu/")

    monkeypatch.setattr(s, "gemini_vertex_project", "")
    with pytest.raises(gemini.NotConfigured):
        await gemini.generate("m", {}, timeout=1)
