"""Language models for the text engines: what the receptionist says and which tools it calls."""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass

from google.genai import types as genai_types
from livekit.plugins import google

# The "brain". Flash-Lite starts answering in ~0.45 s.
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-3.5-flash-lite")


@dataclass(frozen=True)
class LlmProvider:
    name: str
    model: str
    build: Callable[[], object]  # -> llm.LLM


def gemini_auth() -> dict:
    """AI Studio with the key, or Vertex AI in one region (GEMINI_VERTEX_LOCATION, e.g.
    eu) with the service account in GOOGLE_SERVICE_ACCOUNT_JSON: EU processing."""
    location = os.environ.get("GEMINI_VERTEX_LOCATION")
    if not location:
        return {"api_key": os.environ.get("GEMINI_API_KEY")}
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return {"vertexai": True, "project": os.environ["GEMINI_VERTEX_PROJECT"], "location": location,
            "credentials": credentials}


def gemini_flash_lite():
    return google.LLM(
        model=LLM_MODEL,
        **gemini_auth(),
        temperature=0.9,
        thinking_config=genai_types.ThinkingConfig(thinking_level="minimal"),
    )


PROVIDERS = {
    "gemini-flash-lite": LlmProvider("gemini-flash-lite", LLM_MODEL, gemini_flash_lite),
}
