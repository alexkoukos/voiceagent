"""Which language a call starts in, from the friend's phone prefix.

Only languages that both ElevenLabs Flash v2.5 (voice) and Scribe (transcription)
handle are offered. The call's language is just the starting point: the agent
follows the friend if they switch language mid-call.
"""

# code -> (name in its own language for the app, English name for the prompt)
LANGUAGES: dict[str, tuple[str, str]] = {
    "el": ("Ελληνικά", "Greek"),
    "en": ("English", "English"),
    "de": ("Deutsch", "German"),
    "fr": ("Français", "French"),
    "es": ("Español", "Spanish"),
    "it": ("Italiano", "Italian"),
    "pt": ("Português", "Portuguese"),
    "nl": ("Nederlands", "Dutch"),
    "tr": ("Türkçe", "Turkish"),
    "pl": ("Polski", "Polish"),
    "sv": ("Svenska", "Swedish"),
    "no": ("Norsk", "Norwegian"),
    "da": ("Dansk", "Danish"),
    "fi": ("Suomi", "Finnish"),
    "cs": ("Čeština", "Czech"),
    "sk": ("Slovenčina", "Slovak"),
    "hu": ("Magyar", "Hungarian"),
    "ro": ("Română", "Romanian"),
    "bg": ("Български", "Bulgarian"),
    "hr": ("Hrvatski", "Croatian"),
    "ru": ("Русский", "Russian"),
    "uk": ("Українська", "Ukrainian"),
    "ar": ("العربية", "Arabic"),
    "hi": ("हिन्दी", "Hindi"),
    "ja": ("日本語", "Japanese"),
    "ko": ("한국어", "Korean"),
    "zh": ("中文", "Chinese"),
    "id": ("Bahasa Indonesia", "Indonesian"),
    "ms": ("Bahasa Melayu", "Malay"),
    "fil": ("Filipino", "Filipino"),
    "vi": ("Tiếng Việt", "Vietnamese"),
}

DEFAULT_LANGUAGE = "en"

# International dialling prefix -> language. Longest prefix wins.
_PREFIXES: dict[str, str] = {
    "30": "el", "357": "el",
    "1": "en", "44": "en", "353": "en", "61": "en", "64": "en", "27": "en",
    "49": "de", "43": "de", "41": "de",
    "33": "fr", "352": "fr", "32": "fr",
    "34": "es", "52": "es", "54": "es", "56": "es", "57": "es", "51": "es",
    "39": "it",
    "351": "pt", "55": "pt",
    "31": "nl",
    "90": "tr",
    "48": "pl",
    "46": "sv",
    "47": "no",
    "45": "da",
    "358": "fi",
    "420": "cs",
    "421": "sk",
    "36": "hu",
    "40": "ro",
    "359": "bg",
    "385": "hr",
    "7": "ru",
    "380": "uk",
    "20": "ar", "212": "ar", "961": "ar", "962": "ar", "966": "ar", "971": "ar",
    "91": "hi",
    "81": "ja",
    "82": "ko",
    "86": "zh", "852": "zh", "886": "zh",
    "62": "id",
    "60": "ms",
    "63": "fil",
    "84": "vi",
}


def language_for_phone(phone_number: str) -> str:
    digits = phone_number.lstrip("+")
    for length in (3, 2, 1):
        lang = _PREFIXES.get(digits[:length])
        if lang:
            return lang
    return DEFAULT_LANGUAGE


def english_name(code: str) -> str:
    return LANGUAGES.get(code, LANGUAGES[DEFAULT_LANGUAGE])[1]
