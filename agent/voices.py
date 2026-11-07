"""The receptionist's two voices, used on every call in every language.

The app and backend store a voice key: "Zubenelgenubi" is the male voice, anything else
(normally "Kore") the female one. ElevenLabs speaks them as native Greek voices: the
English premade voices sounded foreign, almost Cypriot, in Greek (2026-10-01). Gemini
(the realtime engine and Gemini TTS) has no such voices and uses its own of the same name.
"""

FEMALE = "Kore"
MALE = "Zubenelgenubi"

ELEVENLABS_VOICES: dict[str, str] = {
    FEMALE: "mRTQIE2xdk2oMdoKFGJu",  # Aria: female, warm
    MALE: "cuab90umcstNgL8U7orz",  # Fatsis: male, Athenian, warm
}


def voice_key(key: str | None) -> str:
    return MALE if key == MALE else FEMALE


def elevenlabs_voice(key: str | None) -> str:
    return ELEVENLABS_VOICES[voice_key(key)]


def gemini_voice(key: str | None) -> str:
    return voice_key(key)
