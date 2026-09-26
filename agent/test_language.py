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
