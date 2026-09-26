"""Which language a call is in.

Calls default to Greek regardless of the caller's number. English is used only
when explicitly requested (e.g. via the app's language selection / the
``CallCreate.language`` request field). Only Greek and English are supported:
other languages sounded bad, and the model drifted into them on noisy lines.
The agent keeps the call's language for the whole call.
"""

# code -> (name in its own language for the app, English name for the prompt)
LANGUAGES: dict[str, tuple[str, str]] = {
    "el": ("Ελληνικά", "Greek"),
    "en": ("English", "English"),
}

DEFAULT_LANGUAGE = "el"

# International dialling prefix -> language. Longest prefix wins; unmatched numbers fall back to the default (Greek).
_PREFIXES: dict[str, str] = {"30": "el", "357": "el"}


def language_for_phone(phone_number: str) -> str:
    digits = phone_number.lstrip("+")
    for length in (3, 2, 1):
        lang = _PREFIXES.get(digits[:length])
        if lang:
            return lang
    return DEFAULT_LANGUAGE


def english_name(code: str) -> str:
    return LANGUAGES.get(code, LANGUAGES[DEFAULT_LANGUAGE])[1]
