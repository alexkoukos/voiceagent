from dataclasses import replace
from types import SimpleNamespace

import agent as worker
import pytest
from livekit import rtc


def test_english_request_is_understood_in_greek_transcription():
    assert worker.wants_language("English") == "en"
    assert worker.wants_language("Ίνγκλις") == "en"
    assert worker.wants_language("Ένγκλις") == "en"
    assert worker.wants_language("Για αγγλικά") == "en"


def test_other_words_do_not_change_language():
    assert worker.wants_language("Θέλω ραντεβού την Τετάρτη") is None
    assert worker.wants_language("una cerveza") is None
    assert worker.wants_language("Ελληνικά") == "el"


def test_web_call_uses_final_transcript_instead_of_raw_model_events(monkeypatch):
    monkeypatch.setattr(worker, "NOISE_CANCELLATION", False)
    assert worker.room_options(web=True, caller_identity="caller").text_output is False
    assert worker.room_options(web=False, caller_identity="caller").text_output is not False


@pytest.mark.asyncio
async def test_receptionist_uses_text_pipeline_when_elevenlabs_has_no_credits(monkeypatch):
    async def unusable():
        return False

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("ELEVEN_API_KEY", "test-key")
    monkeypatch.setattr(worker, "elevenlabs_usable", unusable)
    assert await worker.pick_engine("test-call") == "text_pipeline"


def test_text_pipeline_feeds_deepgram_transcript_to_text_model(monkeypatch):
    stt = object()
    llm = object()
    tts = object()
    vad = object()
    models = []
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    stt_providers = worker.providers.stt.PROVIDERS
    monkeypatch.setitem(stt_providers, "deepgram", replace(stt_providers["deepgram"], build=lambda *args: stt))
    monkeypatch.setattr(worker.google, "LLM", lambda **kwargs: llm)
    monkeypatch.setattr(worker.google.beta, "GeminiTTS", lambda **kwargs: models.append(kwargs["model"]) or object())
    monkeypatch.setattr(worker.providers.tts.livekit_tts, "FallbackAdapter", lambda providers, **kwargs: tts)
    monkeypatch.setattr(worker, "AgentSession", lambda **kwargs: kwargs)
    ctx = SimpleNamespace(proc=SimpleNamespace(userdata={"vad": vad}))

    session = worker.build_session(ctx, "text_pipeline", "default", "el")

    assert session["stt"] is stt
    assert session["llm"] is llm
    assert session["tts"] is tts
    assert models == [worker.providers.tts.GEMINI_TTS_MODEL, worker.providers.tts.GEMINI_TTS_FALLBACK_MODEL]
    assert session["vad"] is vad
    assert session["turn_handling"]["turn_detection"] == "stt"


@pytest.mark.asyncio
async def test_web_transcript_hides_latin_garble_in_greek_mode():
    published = []

    async def publish(transcription):
        published.append(transcription)

    mic = SimpleNamespace(sid="mic", source=rtc.TrackSource.SOURCE_MICROPHONE)
    local = SimpleNamespace(identity="agent", track_publications={"mic": mic}, publish_transcription=publish)
    caller = SimpleNamespace(track_publications={"mic": mic})
    room = SimpleNamespace(
        isconnected=lambda: True, local_participant=local, remote_participants={"caller": caller},
    )

    await worker.publish_web_transcript(room, "friend", "Edilcat di Calabria", "caller", "el")
    assert published == []

    await worker.publish_web_transcript(room, "friend", "Με πονάει το δόντι", "caller", "el")
    assert published[-1].participant_identity == "caller"
    assert published[-1].segments[0].text == "Με πονάει το δόντι"
    assert published[-1].segments[0].final is True

    await worker.publish_web_transcript(room, "friend", "English", "caller", "el")
    assert published[-1].segments[0].text == "English"

    await worker.publish_web_transcript(room, "friend", "I'd like an appointment", "caller", "en")
    assert published[-1].segments[0].text == "I'd like an appointment"


def test_mentioning_a_language_is_not_a_request():
    # Real call, 2026-10-01: an English call switched back to Greek on this sentence.
    assert worker.wants_language("Yeah. Did you hear my conversation in Greek?") is None
    assert worker.wants_language("Did you hear me speaking Greek before?") is None
    assert worker.wants_language("Can we switch to Greek, please?") == "el"
    assert worker.wants_language("Η γυναίκα μου είναι Αγγλίδα αλλά μιλάει καλά") is None
    assert worker.wants_language("English mode. Where are you located?") == "en"
    assert worker.wants_language("Hi, do you speak English? I'd like to book a check-up.") == "en"
    assert worker.wants_language("Μπορούμε να μιλήσουμε στα αγγλικά;") == "en"
    assert worker.wants_language("Ελληνικά παρακαλώ") == "el"
    assert worker.wants_language("Greek mode") == "el"


def stub_parts(monkeypatch):
    """Every provider builds a placeholder; returns what each one was asked to build."""
    built = []
    for registry in (worker.providers.stt.PROVIDERS, worker.providers.tts.PROVIDERS, worker.providers.llm.PROVIDERS):
        for name, provider in list(registry.items()):
            monkeypatch.setitem(registry, name, replace(
                provider, build=lambda *args, name=name: built.append((name, *args)) or (name, *args)))
    return built


@pytest.mark.parametrize("language,semantic", [("el", False), ("en", True)])
def test_scribe_uses_one_greek_end_of_turn_decision(monkeypatch, language, semantic):
    from unittest.mock import Mock
    detector = Mock(return_value="semantic-detector")
    monkeypatch.setattr(worker.inference, "TurnDetector", detector)
    stub_parts(monkeypatch)
    monkeypatch.setattr(worker, "AgentSession", lambda **kwargs: kwargs)
    ctx = SimpleNamespace(proc=SimpleNamespace(userdata={"vad": object()}))
    session = worker.build_session(ctx, "pipeline", "default", language)
    options = session["turn_handling"]
    assert options["turn_detection"] == ("semantic-detector" if semantic else "stt")
    assert options["endpointing"]["min_delay"] == (worker.ENDPOINT_MIN_DELAY if semantic else 0)
    assert detector.call_count == int(semantic)
    assert options["interruption"]["resume_false_interruption"] is True


def test_stt_provider_can_be_swapped_on_the_main_engine_only(monkeypatch):
    stub_parts(monkeypatch)
    monkeypatch.setattr(worker, "AgentSession", lambda **kwargs: kwargs)
    ctx = SimpleNamespace(proc=SimpleNamespace(userdata={"vad": object()}))
    monkeypatch.setenv("STT_PROVIDER", "deepgram")
    session = worker.build_session(ctx, "pipeline", "Kore", "el", ["Γιώργος"])
    assert session["stt"] == ("deepgram", "el", ["Γιώργος"])
    assert session["tts"] == ("elevenlabs", "Kore", "el")
    assert session["turn_handling"]["endpointing"]["min_delay"] == worker.ENDPOINT_MIN_DELAY
    # The fallback engine keeps its own parts.
    assert worker.build_session(ctx, "text_pipeline", "Kore", "el")["tts"] == ("gemini", "Kore", "el")
    monkeypatch.delenv("STT_PROVIDER")
    assert worker.build_session(ctx, "pipeline", "Kore", "el")["stt"] == ("scribe", "el", None)
