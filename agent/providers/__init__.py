"""Which speech-to-text, language model and voice each text engine uses.

The engine (RECEPTIONIST_ENGINE, or the fallback pick_engine chose) sets the defaults.
On the main "pipeline" engine STT_PROVIDER and LLM_PROVIDER replace one part, to try
another provider without touching the rest. The fallback engine keeps its own parts:
it exists for when the main providers fail.
"""

import os

from providers import llm, stt, tts

ENGINES = {
    # OpenAI live transcription: Scribe realtime invented text and dropped short answers
    # («Ναι.») on Greek phone audio (stt_bakeoff.py and scripted calls, 2026-11-01).
    "pipeline": ("openai", "gemini-flash-lite", "elevenlabs"),
    "text_pipeline": ("deepgram", "gemini-flash-lite", "gemini"),
}


def for_engine(engine: str) -> tuple[stt.SttProvider, llm.LlmProvider, tts.TtsProvider]:
    stt_name, llm_name, tts_name = ENGINES[engine]
    if engine == "pipeline":
        stt_name = os.environ.get("STT_PROVIDER") or stt_name
        llm_name = os.environ.get("LLM_PROVIDER") or llm_name
    return stt.PROVIDERS[stt_name], llm.PROVIDERS[llm_name], tts.PROVIDERS[tts_name]
