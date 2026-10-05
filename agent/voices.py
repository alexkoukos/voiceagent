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
    "Zubenelgenubi": "nPczCjzI2devNBz1zQrb",  # Brian: the receptionist default
}


def _overrides(variable: str = "ELEVENLABS_VOICE_MAP") -> dict[str, str]:
    raw = os.environ.get(variable, "")
    if not raw:
        return {}
    try:
        mapping = json.loads(raw)
        if not isinstance(mapping, dict) or any(not isinstance(v, str) or not v.strip() for v in mapping.values()):
            raise ValueError
        return mapping
    except (ValueError, AttributeError):
        logger.warning("%s is not a valid voice mapping; ignoring it", variable)
        return {}


DEMO_VOICES = {"eleven_sarah": ELEVENLABS_VOICES["default"], "eleven_jessica": ELEVENLABS_VOICES["Aoede"],
               "eleven_george": ELEVENLABS_VOICES["Charon"], "eleven_brian": ELEVENLABS_VOICES["Algieba"]}


def elevenlabs_voice(key: str, language: str | None = None) -> str:
    if key in DEMO_VOICES:
        return DEMO_VOICES[key]
    mapping = {**ELEVENLABS_VOICES, **_overrides()}
    localized = _overrides(f"ELEVENLABS_VOICE_MAP_{language.upper()}") if language in {"el", "en"} else {}
    return localized.get(key) or localized.get("default") or mapping.get(key) or mapping["default"]


# OpenAI Realtime voices, matched to each key's label in the app.
OPENAI_VOICES: dict[str, str] = {
    "default": "marin",  # female, calm
    "Aoede": "coral",  # female, light
    "Puck": "ash",  # male, upbeat
    "Charon": "cedar",  # male, calm
    "Fenrir": "verse",  # male, energetic
    "Algenib": "echo",  # male, rougher
    "Algieba": "ballad",  # male, soft
    "Zubenelgenubi": "ballad",  # male, casual
}


def openai_voice(key: str) -> str:
    return OPENAI_VOICES.get(key, OPENAI_VOICES["default"])


def gemini_voice(key: str) -> str:
    return GEMINI_DEFAULT_VOICE if key == "default" or key in DEMO_VOICES else key
