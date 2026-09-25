"""The app's voice keys, mapped to a voice for each engine.

The app and backend only know stable keys ("default", "Puck", ...). The realtime
engine uses them as Gemini voice names; the pipeline and openai engines map them to
ElevenLabs and OpenAI voices. ELEVENLABS_VOICE_MAP (JSON: {"key": "voice_id"}) overrides the
mapping, e.g. to use your own ElevenLabs voices.
"""

import json
import logging
import os

logger = logging.getLogger("prank-caller")

GEMINI_DEFAULT_VOICE = "Kore"

# ElevenLabs premade voices, chosen to match each key's label in the app.
ELEVENLABS_VOICES: dict[str, str] = {
    "default": "EXAVITQu4vr4xnSDxMaL",  # Sarah: female, calm
    "Aoede": "cgSgspJ2msm6clMCkdW9",  # Jessica: female, light and expressive
    "Puck": "iP95p4xoKVk53GoZ742B",  # Chris: male, casual and upbeat
    "Charon": "JBFqnCBsd6RMkjVDRZzb",  # George: male, warm and calm
    "Fenrir": "TX3LPaxmHKxFdv7VOQHJ",  # Liam: male, energetic
    "Algenib": "N2lVS1w4EtoT3dr4eOWO",  # Callum: male, husky and intense (the grumpy caller)
    "Algieba": "nPczCjzI2devNBz1zQrb",  # Brian: male, deep and soothing
}


def _overrides() -> dict[str, str]:
    raw = os.environ.get("ELEVENLABS_VOICE_MAP", "")
    if not raw:
        return {}
    try:
        return {str(k): str(v) for k, v in json.loads(raw).items()}
    except (ValueError, AttributeError):
        logger.warning("ELEVENLABS_VOICE_MAP is not valid JSON; ignoring it")
        return {}


def elevenlabs_voice(key: str) -> str:
    mapping = {**ELEVENLABS_VOICES, **_overrides()}
    return mapping.get(key) or mapping["default"]


# OpenAI Realtime voices, matched to each key's label in the app.
OPENAI_VOICES: dict[str, str] = {
    "default": "marin",  # female, calm
    "Aoede": "coral",  # female, light
    "Puck": "ash",  # male, upbeat
    "Charon": "cedar",  # male, calm
    "Fenrir": "verse",  # male, energetic
    "Algenib": "echo",  # male, rougher
    "Algieba": "ballad",  # male, soft
}


def openai_voice(key: str) -> str:
    return OPENAI_VOICES.get(key, OPENAI_VOICES["default"])


def gemini_voice(key: str) -> str:
    return GEMINI_DEFAULT_VOICE if key == "default" else key
