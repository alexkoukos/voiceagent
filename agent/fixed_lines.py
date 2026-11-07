"""Fixed lines (greeting, goodbye, emergency and profanity lines) voiced in advance.

They are known before the call starts, so they are voiced whole while the call is being
set up and cached on disk; a line that isn't ready in time is streamed instead.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path

import httpx
from livekit import rtc
from speech import ELEVENLABS_TTS_MODEL, Pronunciation, elevenlabs_voice_settings

logger = logging.getLogger("receptionist")

SAMPLE_RATE = 24000
_FRAME_SAMPLES = SAMPLE_RATE // 50  # 20 ms


async def _voice_line(text: str, voice_id: str, language: str,
                      model: str = ELEVENLABS_TTS_MODEL) -> list[rtc.AudioFrame]:
    body = {"text": Pronunciation(language).apply(text), "model_id": model,
            "voice_settings": elevenlabs_voice_settings(), "apply_text_normalization": "auto"}
    if "v2_5" in model:  # language enforcement exists only on the v2.5 models
        body["language_code"] = language
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            params={"output_format": f"pcm_{SAMPLE_RATE}"},
            headers={"xi-api-key": os.environ["ELEVEN_API_KEY"]},
            json=body,
        )
        r.raise_for_status()
        pcm = r.content
    step = _FRAME_SAMPLES * 2  # 16-bit mono
    frames = []
    for i in range(0, len(pcm) - len(pcm) % 2, step):
        chunk = pcm[i:i + step]
        frames.append(rtc.AudioFrame(chunk, SAMPLE_RATE, 1, len(chunk) // 2))
    return frames


# Fixed lines (greeting, goodbye, closing lines) are known in advance. Streamed through
# Flash they started rushed and garbled, and the English hint was read by the Greek voice
# (real call, 2026-10-01). Voice them whole, per language, once per worker.
PRERENDER_TTS_MODEL = os.environ.get("PRERENDER_TTS_MODEL", "eleven_multilingual_v2")
_GREEK = re.compile(r"[\u0370-\u03ff\u1f00-\u1fff]")
_fixed: dict[tuple, "asyncio.Task[list[rtc.AudioFrame]]"] = {}


def language_segments(text: str, language: str) -> list[tuple[str, str]]:
    """«… Πώς μπορώ να σας βοηθήσω; For English, say English mode.» -> Greek part, English part."""
    if language != "el":
        return [(text, language)]
    segments: list[tuple[str, str]] = []
    for sentence in re.split(r"(?<=[.!?;])\s+", text.strip()):
        if not sentence:
            continue
        lang = "el" if _GREEK.search(sentence) else "en"
        if segments and segments[-1][1] == lang:
            segments[-1] = (f"{segments[-1][0]} {sentence}", lang)
        else:
            segments.append((sentence, lang))
    return segments


FIXED_CACHE_DIR = Path(tempfile.gettempdir()) / "fixed-lines"


async def _render_fixed(text: str, voice_id: str, language: str) -> list[rtc.AudioFrame]:
    # Each call runs in its own process, so keep the audio on disk for the next call.
    key = hashlib.sha256(json.dumps([text, voice_id, language, PRERENDER_TTS_MODEL,
                                     elevenlabs_voice_settings()]).encode()).hexdigest()[:32]
    path = FIXED_CACHE_DIR / f"{key}.pcm"
    try:
        pcm = path.read_bytes()
    except OSError:
        frames: list[rtc.AudioFrame] = []
        for segment, lang in language_segments(text, language):
            frames += await _voice_line(segment, voice_id, lang, PRERENDER_TTS_MODEL)
        try:
            FIXED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(b"".join(bytes(f.data) for f in frames))
            tmp.replace(path)
        except OSError:
            logger.warning("could not cache a fixed line")
        return frames
    step = _FRAME_SAMPLES * 2
    return [rtc.AudioFrame(pcm[i:i + step], SAMPLE_RATE, 1, len(pcm[i:i + step]) // 2)
            for i in range(0, len(pcm) - len(pcm) % 2, step)]


def prerender(text: str, voice_id: str, language: str) -> "asyncio.Task[list[rtc.AudioFrame]]":
    """Start (or reuse) voicing a fixed line; failed renders are retried on the next call."""
    key = (text, voice_id, language, PRERENDER_TTS_MODEL)
    task = _fixed.get(key)
    if task is None or (task.done() and (task.cancelled() or task.exception())):
        task = _fixed[key] = asyncio.ensure_future(_render_fixed(text, voice_id, language))
    return task


async def fixed_audio(text: str, voice_id: str, language: str, wait: float = 2.0):
    """The pre-voiced line if ready within `wait` seconds, else None (the caller streams it)."""
    try:
        frames = await asyncio.wait_for(asyncio.shield(prerender(text, voice_id, language)), timeout=wait)
    except Exception as e:
        logger.warning("fixed line not pre-voiced, streaming it instead: %s", e)
        return None
    if not frames:
        return None

    async def audio():
        for frame in frames:
            yield frame
    return audio()
