"""Short filler words the agent says when a reply is slow, in the language being spoken."""

import random

FILLERS: dict[str, list[str]] = {
    # Polite, for professional calls (plural of politeness, no "Άκου" / "Κοίτα").
    "el": ["Μάλιστα…", "Ένα λεπτό…", "Λοιπόν…", "Βεβαίως…", "Ναι…"],
    "en": ["Sure…", "One moment…", "Right…", "Okay…", "Let me see…"],
    "de": ["Äh…", "Also…", "Hm…", "Schau mal…", "Na ja…"],
    "fr": ["Euh…", "Bon…", "Écoute…", "Hmm…", "Alors…"],
    "es": ["Eh…", "Bueno…", "Mira…", "Pues…", "Hmm…"],
    "it": ["Ehm…", "Allora…", "Senti…", "Beh…", "Mah…"],
    "pt": ["Hum…", "Olha…", "Então…", "Bem…"],
    "nl": ["Eh…", "Nou…", "Kijk…", "Hm…"],
    "tr": ["Şey…", "Bak…", "Hmm…", "Yani…"],
    "ru": ["Э-э…", "Ну…", "Слушай…", "Так…"],
    "pl": ["Yyy…", "No…", "Słuchaj…", "Hmm…"],
    "uk": ["Е-е…", "Ну…", "Слухай…"],
    "ar": ["يعني…", "طيب…", "اسمع…"],
}
NEUTRAL = ["Hmm…", "Mm…"]

# Scribe may report ISO 639-3 codes; the rest of the app uses ISO 639-1.
_THREE_LETTER = {
    "ell": "el", "gre": "el", "eng": "en", "deu": "de", "ger": "de", "fra": "fr", "fre": "fr",
    "spa": "es", "ita": "it", "por": "pt", "nld": "nl", "dut": "nl", "tur": "tr", "rus": "ru",
    "pol": "pl", "ukr": "uk", "ara": "ar",
}


def normalize_language(code: str | None) -> str | None:
    if not code:
        return None
    code = code.lower().split("-")[0].split("_")[0]
    return _THREE_LETTER.get(code, code)


class FillerPicker:
    """Random filler for a language, never the same one twice in a row."""

    def __init__(self) -> None:
        self._last: str | None = None

    def pick(self, language: str) -> str:
        options = [f for f in FILLERS.get(language, NEUTRAL) if f != self._last] or NEUTRAL
        self._last = random.choice(options)
        return self._last
