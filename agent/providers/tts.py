"""Text-to-speech providers: the receptionist's voice."""

import os
from collections.abc import Callable
from dataclasses import dataclass

from livekit.agents import tts as livekit_tts
from livekit.plugins import elevenlabs, google

from speech import ELEVENLABS_TTS_MODEL, elevenlabs_voice_settings, speech_instructions
from voices import elevenlabs_voice, gemini_voice

GEMINI_TTS_MODEL = os.environ.get("GEMINI_TTS_MODEL", "gemini-3.8-flash-lite-tts")
GEMINI_TTS_FALLBACK_MODEL = os.environ.get("GEMINI_TTS_FALLBACK_MODEL", "gemini-3.8-flash-tts")


@dataclass(frozen=True)
class TtsProvider:
    name: str
    build: Callable[[str, str], object]  # (voice key, language) -> tts.TTS
    # Open the connection while the call is set up, not on the first reply.
    prewarm: bool


def eleven_labs(voice: str, language: str):
    return elevenlabs.TTS(
        voice_id=elevenlabs_voice(voice, language), model=ELEVENLABS_TTS_MODEL,
        language=language, encoding="pcm_24000",
        voice_settings=elevenlabs.VoiceSettings(**elevenlabs_voice_settings()),
        apply_text_normalization="auto",
    )


def gemini(voice: str, language: str):
    models = list(dict.fromkeys((GEMINI_TTS_MODEL, GEMINI_TTS_FALLBACK_MODEL)))
    return livekit_tts.FallbackAdapter([
        google.beta.GeminiTTS(
            model=model, voice_name=gemini_voice(voice),
            instructions=speech_instructions(language), api_key=os.environ.get("GEMINI_API_KEY"),
        ) for model in models
    ], max_retry_per_tts=1)


PROVIDERS = {
    "elevenlabs": TtsProvider("elevenlabs", eleven_labs, prewarm=True),
    "gemini": TtsProvider("gemini", gemini, prewarm=False),
}
