"""Speech-to-text providers: the caller's words, pinned to the call's language."""

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from livekit.agents import inference
from livekit.plugins import elevenlabs

from speech import env_number

# Pause that ends a transcribed segment. In "own" mode it is also the whole wait before the
# reply; lower split normal-speed speech into fragments (0.3 s, 2026-09-25).
SCRIBE_SILENCE_SECS = env_number("SCRIBE_SILENCE_SECS", 0.7, 0.3, 2.0)
# "early" mode: Scribe finalises the text after this short pause so the reply can be prepared
# (model and voice) while the agent still waits for the turn to end.
SCRIBE_EARLY_SECS = env_number("SCRIBE_EARLY_SECS", 0.35, 0.2, 1.0)
# Greek turns: "early" (default) or "stt" (Scribe's end-of-speech ends the turn, as before).
TURN_MODE_EL = os.environ.get("TURN_MODE_EL", "early")

# How a provider's transcripts end the caller's turn:
# "early": voice activity plus a fixed silence ends it; Scribe's quick final text starts the
#   reply before that, so the model and the voice overlap the wait;
# "own": its end-of-speech event is the turn end, nothing waits after it;
# "pause": its end-of-speech event plus the fixed ENDPOINT_MIN_DELAY;
# "semantic": LiveKit's turn detector decides.
TurnMode = Literal["early", "own", "pause", "semantic"]


@dataclass(frozen=True)
class SttProvider:
    name: str
    build: Callable[[str, list[str] | None], object]  # (language, business vocabulary) -> stt.STT
    turns: Callable[[str], TurnMode]  # by language


# Everyday Greek the transcriber should expect; the business adds its own names and services.
GREEK_VOCABULARY = [
    "ρε", "μωρέ", "κομπλέ", "γαμώτο", "άσ' το", "θα 'ρθω", "κάνα", "τίποτα", "εντάξει", "μπορείς",
    "απογευματάκι", "πρωινό", "ραντεβουδάκι", "ρε φίλε", "έλα", "λέγε", "άντε", "οκ", "ναι ρε",
    "English", "ίνγκλις", "ένγκλις", "αγγλικά",
    # Scribe heard "Τετάρτη" as "Δευτέρα" twice on a real call (2026-10-01).
    "Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή",
]


def vocab_terms(language: str, vocabulary: list[str] | None) -> list[str]:
    """Everyday Greek plus the business's names, split into words: Scribe realtime rejects
    the whole session if any keyterm is over 20 characters."""
    terms = list(GREEK_VOCABULARY if language == "el" else [])
    for phrase in vocabulary or []:
        terms += [phrase] if len(phrase) <= 20 else phrase.split()
    return [t for t in dict.fromkeys(terms) if 2 <= len(t) <= 20][:100]


def scribe_turns(language: str) -> TurnMode:
    # The turn model has no Greek: it waited for max_delay every turn. English: the semantic detector.
    if language != "el":
        return "semantic"
    return "own" if TURN_MODE_EL == "stt" else "early"


def scribe(language: str, vocabulary: list[str] | None = None):
    keyterms = vocab_terms(language, vocabulary)
    silence = SCRIBE_EARLY_SECS if scribe_turns(language) == "early" else SCRIBE_SILENCE_SECS
    return elevenlabs.STT(
        keyterms=keyterms or None,
        model="scribe_v2_realtime",
        # Without a language hint Scribe hears Greek phone audio as Ukrainian (Cyrillic text).
        language_code=language,
        # ElevenLabs decides when a sentence is finished. The plugin's default ("manual")
        # waits for a commit the session never sends, so no final transcript ever arrived
        # and the agent stayed silent for the whole call.
        # 0.3 s cut normal-speed sentences into fragments at every short pause.
        server_vad={"vad_silence_threshold_secs": silence},
    )


def deepgram_terms(vocabulary: list[str] | None) -> list[str]:
    """Favor distinctive business names without over-biasing ordinary speech.

    Deepgram recommends a focused glossary and rejects requests over 500 tokens.
    Keep this well below that limit even when a practice has many services.
    """
    terms: list[str] = []
    seen: set[str] = set()
    words_left = 120
    for value in vocabulary or []:
        term = " ".join(value.split())
        word_count = len(term.split())
        if (not term or len(term) > 80 or word_count > 8 or word_count > words_left
                or term.casefold() in seen):
            continue
        terms.append(term)
        seen.add(term.casefold())
        words_left -= word_count
        if len(terms) == 40:
            break
    return terms


def deepgram(language: str, vocabulary: list[str] | None = None):
    """Deepgram nova-3 via LiveKit Inference: the best streaming option on a real Greek
    phone recording (2026-09-25); Speechmatics, Cartesia and Gemini Transcribe Live garbled
    more, AssemblyAI has no Greek."""
    terms = deepgram_terms(vocabulary)
    options = {"extra_kwargs": {"keyterm": terms}} if terms else {}
    return inference.STT("deepgram/nova-3", language=language, **options)


def openai_live(language: str, vocabulary: list[str] | None = None):
    """OpenAI's live transcription via LiveKit Inference (no separate account). On 120 Greek
    phone-quality clips it got 11% of words wrong against Scribe realtime's 35%, which invents
    continuations («…θα 'ρθω εγώ. Τι θα κάνουμε;», 2026-11-01)."""
    return inference.STT("openai/gpt-live-transcribe", language=language)


def openai_turns(language: str) -> TurnMode:
    # Like Scribe's: Greek turns end on 0.7 s of silence with the reply prepared meanwhile.
    return "early" if language == "el" else "semantic"


PROVIDERS = {
    "scribe": SttProvider("scribe", scribe, scribe_turns),
    "deepgram": SttProvider("deepgram", deepgram, lambda language: "pause"),
    "openai": SttProvider("openai", openai_live, openai_turns),
}
