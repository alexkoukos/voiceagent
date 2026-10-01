"""Speech delivery settings and literal pronunciation aliases for audio only."""

import json
import logging
import math
import os
import re
from collections.abc import AsyncIterable

logger = logging.getLogger("prank-caller")


def env_number(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
        if math.isfinite(value) and minimum <= value <= maximum:
            return value
    except ValueError:
        pass
    logger.warning("Invalid %s; using %s", name, default)
    return default


def noise_cancellation_enabled() -> bool:
    value = os.environ.get("NOISE_CANCELLATION", "on").strip().lower()
    if value in {"off", "false", "0", "no"}:
        return False
    if value not in {"on", "true", "1", "yes"}:
        logger.warning("Invalid NOISE_CANCELLATION; enabling the noise filter")
    return True


ELEVENLABS_TTS_MODEL = os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5")
TTS_SPEED = env_number("TTS_SPEED", 0.93, 0.7, 1.2)


def elevenlabs_voice_settings() -> dict:
    return {"stability": 0.6, "similarity_boost": 0.75, "style": 0.0, "speed": TTS_SPEED}


def speech_instructions(language: str) -> str:
    if language == "el":
        return (
            "Μίλα με φυσική ελληνική προφορά και καθαρή άρθρωση, λίγο πιο αργά από το συνηθισμένο. "
            "Ολοκλήρωνε τις λέξεις και άφηνε μικρές παύσεις ανάμεσα στις προτάσεις. "
            "Πες τις ώρες, τις ημερομηνίες και τα ποσά ολογράφως, όπως στην καθημερινή ομιλία. "
            "Διάβαζε τους αριθμούς τηλεφώνου ψηφίο ψηφίο. Μην αλλάζεις ονόματα ή στοιχεία."
        )
    return (
        "Speak with clear, natural English pronunciation, slightly slower than usual. "
        "Finish each word and pause briefly between sentences. "
        "Say times, dates and amounts in words, as in everyday conversation. "
        "Read phone numbers digit by digit. Do not change names or details."
    )


class Pronunciation:
    """Replace whole words/phrases without changing the transcript or booking data.

    Keep enough original text across streaming chunks to match a split name. A
    left-context character prevents replacing a suffix at the next chunk's start.
    No buffering is added when no aliases are configured.
    """

    def __init__(self, language: str):
        # Greek TTS broke up "Παρακαλώ πολύ! Καλό σας απόγευμα." on a real call (2026-10-01):
        # a receptionist doesn't exclaim, so "!" is spoken as a full stop.
        self.calm = language == "el"
        self.aliases: dict[str, str] = {}
        raw = os.environ.get("TTS_PRONUNCIATION_ALIASES", "")
        if raw:
            try:
                config = json.loads(raw)
                aliases = config.get(language, {})
                if not isinstance(aliases, dict) or len(aliases) > 100:
                    raise ValueError
                for key, value in aliases.items():
                    if (not isinstance(key, str) or not isinstance(value, str)
                            or not key.strip() or not value.strip()
                            or len(key) > 80 or len(value) > 160):
                        raise ValueError
                self.aliases = {key.strip().casefold(): value.strip() for key, value in aliases.items()}
            except (ValueError, AttributeError):
                logger.warning("Invalid TTS_PRONUNCIATION_ALIASES; ignoring pronunciation overrides")
        keys = sorted(self.aliases, key=len, reverse=True)
        self.pattern = (re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, keys)) + r")(?!\w)", re.I)
                        if keys else None)
        self.lookahead = max(map(len, keys), default=0) + 1

    def apply(self, text: str) -> str:
        if self.calm:
            text = text.replace("!", ".")
        if self.pattern is None:
            return text
        return self.pattern.sub(lambda match: self.aliases.get(match.group().casefold(), match.group()), text)

    async def stream(self, chunks: AsyncIterable[str]) -> AsyncIterable[str]:
        if self.calm:
            chunks = _calm(chunks)
        if self.pattern is None:
            async for chunk in chunks:
                yield chunk
            return
        pending = ""
        previous = ""
        async for chunk in chunks:
            pending += chunk
            cutoff = len(pending) - self.lookahead
            if cutoff <= 0:
                continue
            source = previous + pending
            offset = len(previous)
            pieces = []
            start = offset
            for match in self.pattern.finditer(source, offset):
                if match.start() >= cutoff + offset:
                    break
                pieces.extend((source[start:match.start()], self.aliases.get(match.group().casefold(), match.group())))
                start = match.end()
            end = max(cutoff + offset, start)
            pieces.append(source[start:end])
            yield "".join(pieces)
            previous = source[end - 1:end]
            pending = source[end:]
        if pending:
            source = previous + pending
            result = self.pattern.sub(
                lambda match: (self.aliases.get(match.group().casefold(), match.group())
                               if match.start() >= len(previous) else match.group()), source,
            )
            yield result[len(previous):]


async def _calm(chunks: AsyncIterable[str]) -> AsyncIterable[str]:
    async for chunk in chunks:
        yield chunk.replace("!", ".")
