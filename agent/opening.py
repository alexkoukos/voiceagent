"""The opening line, written and voiced while the phone is still ringing.

The first thing the agent says is known before the friend answers, so it's
generated during the ring with the same speech model and settings as live replies,
and the agent plays it the moment
the friend says hello. Any failure just means the normal live reply is used.
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass

import httpx
from google import genai
from google.genai import types
from livekit import rtc
from speech import ELEVENLABS_TTS_MODEL, Pronunciation, elevenlabs_voice_settings, speech_instructions

logger = logging.getLogger("prank-caller")

SAMPLE_RATE = 24000
_FRAME_SAMPLES = SAMPLE_RATE // 50  # 20 ms
_TAG = re.compile(r"\[[^\]]{1,30}\]\s*")

_INSTRUCTION = {
    "el": (
        "Ο άλλος μόλις σήκωσε το τηλέφωνο και είπε «Εμπρός;». Γράψε ΜΟΝΟ την πρώτη σου ατάκα: "
        "Πρώτα δήλωσε ότι είσαι βοηθός τεχνητής νοημοσύνης και ότι η κλήση καταγράφεται. Μετά μία σύντομη φράση στον ρόλο σου."
    ),
    "other": (
        "They just picked up and said hello. Write ONLY your very first line, in {language}: "
        "First disclose that you are an AI assistant and this call is recorded, then one short phrase in your role."
    ),
}


@dataclass
class Opening:
    text: str  # what was said, without emotion tags (for the transcript and chat history)
    frames: list[rtc.AudioFrame]

    async def audio(self):
        for frame in self.frames:
            yield frame


async def _write_line(prompt: str, language: str, language_name: str, model: str) -> str:
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    instruction = _INSTRUCTION["el"] if language == "el" else _INSTRUCTION["other"].format(language=language_name)
    resp = await client.aio.models.generate_content(
        model=model,
        contents=instruction,
        config=types.GenerateContentConfig(
            system_instruction=f"{prompt}\n\n{speech_instructions(language)}", max_output_tokens=120, temperature=0.9,
        ),
    )
    return (resp.text or "").strip().strip('"«»')


async def _voice_line(text: str, voice_id: str, language: str) -> list[rtc.AudioFrame]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            params={"output_format": f"pcm_{SAMPLE_RATE}"},
            headers={"xi-api-key": os.environ["ELEVEN_API_KEY"]},
            json={"text": Pronunciation(language).apply(text), "model_id": ELEVENLABS_TTS_MODEL,
                  "language_code": language, "voice_settings": elevenlabs_voice_settings(),
                  "apply_text_normalization": "auto"},
        )
        r.raise_for_status()
        pcm = r.content
    step = _FRAME_SAMPLES * 2  # 16-bit mono
    frames = []
    for i in range(0, len(pcm) - len(pcm) % 2, step):
        chunk = pcm[i:i + step]
        frames.append(rtc.AudioFrame(chunk, SAMPLE_RATE, 1, len(chunk) // 2))
    return frames


async def prepare_opening(
    *, prompt: str, language: str, language_name: str, voice_id: str, llm_model: str
) -> Opening | None:
    try:
        line = await _write_line(prompt, language, language_name, llm_model)
        if not line:
            return None
        spoken = _TAG.sub("", line).strip()
        frames = await _voice_line(spoken, voice_id, language)
        logger.info("opening line ready (%d frames): %s", len(frames), spoken)
        return Opening(text=spoken, frames=frames) if frames and spoken else None
    except Exception as e:
        logger.warning("opening line not prepared, using a live reply instead: %s", e)
        return None


async def ready_opening(task: "asyncio.Task[Opening | None] | None", wait: float = 0.5) -> Opening | None:
    """The prepared opening if it's ready within `wait` seconds, else None."""
    if task is None:
        return None
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=wait)
    except (asyncio.TimeoutError, Exception):
        return None
