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
    assert await worker.pick_engine("test-call", receptionist=True) == "text_pipeline"
    assert await worker.pick_engine("test-call", receptionist=False) == "realtime"


def test_text_pipeline_feeds_deepgram_transcript_to_text_model(monkeypatch):
    stt = object()
    llm = object()
    tts = object()
    vad = object()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(worker, "caller_stt", lambda language: stt)
    monkeypatch.setattr(worker.google, "LLM", lambda **kwargs: llm)
    monkeypatch.setattr(worker.google.beta, "GeminiTTS", lambda **kwargs: tts)
    monkeypatch.setattr(worker, "AgentSession", lambda **kwargs: kwargs)
    ctx = SimpleNamespace(proc=SimpleNamespace(userdata={"vad": vad}))

    session = worker.build_session(ctx, "text_pipeline", "default", "el")

    assert session["stt"] is stt
    assert session["llm"] is llm
    assert session["tts"] is tts
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
