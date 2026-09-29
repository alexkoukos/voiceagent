import json
from types import SimpleNamespace

import pytest

import agent as worker
import opening
from speech import Pronunciation, env_number, noise_cancellation_enabled
from voices import elevenlabs_voice


async def chunks(values):
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_pronunciation_survives_every_chunk_boundary(monkeypatch):
    monkeypatch.setenv("TTS_PRONUNCIATION_ALIASES", json.dumps({
        "el": {"OpenAI": "Όπεν έι άι", "Άννα Μαρία": "Άννα-Μαρία", "Άννα": "Άνα", "a": "έι"},
    }))
    pronunciation = Pronunciation("el")
    text = "Η Άννα Μαρία μίλησε στην Άννα, στην OpenAI και στην SuperOpenAI. a cat."
    expected = "Η Άννα-Μαρία μίλησε στην Άνα, στην Όπεν έι άι και στην SuperOpenAI. έι cat."
    assert pronunciation.apply(text) == expected
    for split in range(len(text) + 1):
        actual = "".join([part async for part in pronunciation.stream(chunks([text[:split], text[split:]]))])
        assert actual == expected, split
    assert "".join([part async for part in pronunciation.stream(chunks(text))]) == expected


@pytest.mark.asyncio
async def test_pronunciation_keeps_original_word_boundary_and_does_not_cascade(monkeypatch):
    monkeypatch.setenv("TTS_PRONUNCIATION_ALIASES", '{"en":{"Ann":"Anne","Anne":"Annie"}}')
    pronunciation = Pronunciation("en")
    text = "Ann, Anne, JoanneAnn and Annex. " * 20
    expected = "Anne, Annie, JoanneAnn and Annex. " * 20
    assert "".join([part async for part in pronunciation.stream(chunks(text))]) == expected


@pytest.mark.asyncio
async def test_no_aliases_add_no_streaming_delay(monkeypatch):
    monkeypatch.delenv("TTS_PRONUNCIATION_ALIASES", raising=False)
    original = ["Hello", " ", "there", "."]
    assert [part async for part in Pronunciation("en").stream(chunks(original))] == original


@pytest.mark.parametrize("raw", ['[]', '{"el":[]}', '{"el":{"name":null}}', '{"el":{"":"x"}}', 'invalid'])
def test_bad_pronunciation_config_does_not_break_calls_or_log_content(monkeypatch, caplog, raw):
    monkeypatch.setenv("TTS_PRONUNCIATION_ALIASES", raw)
    assert Pronunciation("el").apply("name") == "name"
    assert "Invalid TTS_PRONUNCIATION_ALIASES" in caplog.text
    assert raw not in caplog.text


@pytest.mark.asyncio
async def test_only_tts_receives_spoken_aliases(monkeypatch):
    monkeypatch.setenv("TTS_PRONUNCIATION_ALIASES", '{"el":{"OpenAI":"Όπεν έι άι"}}')
    monkeypatch.setattr(worker.Agent.default, "tts_node", lambda self, text, settings: text)
    agent = worker.PrankCallerAgent(
        instructions="Call OpenAI.", call_id="test", language="el", language_name="Greek",
    )
    original = "Call OpenAI."
    spoken = "".join([part async for part in agent.tts_node(chunks([original]), None)])
    assert spoken == "Call Όπεν έι άι."
    assert agent.use_tts_aligned_transcript is False
    assert agent.instructions.startswith(original)
    assert original == "Call OpenAI."


def test_language_voice_overrides_preserve_existing_mapping(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_VOICE_MAP", '{"default":"generic","Puck":"legacy"}')
    monkeypatch.setenv("ELEVENLABS_VOICE_MAP_EL", '{"default":"greek","Puck":"greek-male"}')
    monkeypatch.setenv("ELEVENLABS_VOICE_MAP_EN", '{"default":"english"}')
    assert elevenlabs_voice("Puck", "el") == "greek-male"
    assert elevenlabs_voice("Kore", "el") == "greek"
    assert elevenlabs_voice("Puck", "en") == "english"
    assert elevenlabs_voice("Puck") == "legacy"
    monkeypatch.setenv("ELEVENLABS_VOICE_MAP_EL", '{"default":null}')
    assert elevenlabs_voice("Puck", "el") == "legacy"


@pytest.mark.parametrize("engine", ["pipeline", "text_pipeline"])
def test_language_switch_updates_both_recognition_and_speech(monkeypatch, engine):
    monkeypatch.setattr(worker, "scribe_stt", lambda language, words: ("stt", language))
    monkeypatch.setattr(worker, "caller_stt", lambda language: ("stt", language))
    monkeypatch.setattr(worker, "build_tts", lambda engine, voice, language: ("tts", language, voice))
    for language in ("en", "el"):
        parts = worker.language_parts(engine, "Kore", language)
        assert parts == {"stt": ("stt", language), "tts": ("tts", language, "Kore")}


def test_elevenlabs_uses_clear_audio_and_the_selected_language(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_VOICE_MAP_EL", '{"default":"greek-voice"}')
    monkeypatch.setattr(worker.elevenlabs, "TTS", lambda **kwargs: kwargs)
    tts = worker.build_tts("pipeline", "Kore", "el")
    assert tts["voice_id"] == "greek-voice"
    assert tts["language"] == "el"
    assert tts["encoding"] == "pcm_24000"
    assert 0.7 <= tts["voice_settings"].speed < 1


@pytest.mark.parametrize("web,expected", [(True, "web-filter"), (False, "phone-filter")])
def test_noise_filter_routes_by_audio_source(monkeypatch, web, expected):
    monkeypatch.setattr(worker.noise_cancellation, "BVC", lambda: "web-filter")
    monkeypatch.setattr(worker.noise_cancellation, "BVCTelephony", lambda: "phone-filter")
    monkeypatch.setattr(worker, "NOISE_CANCELLATION", True)
    options = worker.room_options(web=web, caller_identity="caller")
    assert options.audio_input.noise_cancellation == expected
    assert options.participant_identity == "caller"
    monkeypatch.setattr(worker, "NOISE_CANCELLATION", False)
    assert worker.room_options(web=web).audio_input.noise_cancellation is None


@pytest.mark.parametrize("value", ["off", " OFF ", "false", "0", "no"])
def test_noise_cancellation_can_be_disabled_explicitly(monkeypatch, value):
    monkeypatch.setenv("NOISE_CANCELLATION", value)
    assert noise_cancellation_enabled() is False


def test_noise_cancellation_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("NOISE_CANCELLATION", raising=False)
    assert noise_cancellation_enabled() is True


@pytest.mark.parametrize("value", ["garbled", "nan", "inf", "-1", "5000"])
def test_invalid_timing_settings_fall_back_to_working_values(monkeypatch, value):
    monkeypatch.setenv("REALTIME_SILENCE_MS", value)
    assert env_number("REALTIME_SILENCE_MS", 700, 200, 3000) == 700


def test_realtime_uses_less_sensitive_speech_detection(monkeypatch):
    monkeypatch.setattr(worker.google.beta.realtime, "RealtimeModel", lambda **kwargs: kwargs)
    monkeypatch.setattr(worker, "caller_stt", lambda *args: object())
    settings = worker.language_parts("realtime", "Kore", "el")["llm"]
    vad = settings["realtime_input_config"].automatic_activity_detection
    assert vad.start_of_speech_sensitivity == worker.genai_types.StartSensitivity.START_SENSITIVITY_LOW
    assert vad.end_of_speech_sensitivity == worker.genai_types.EndSensitivity.END_SENSITIVITY_LOW
    assert vad.silence_duration_ms == worker.REALTIME_SILENCE_MS


@pytest.mark.asyncio
async def test_prepared_opening_uses_same_pronunciation_and_delivery_as_replies(monkeypatch):
    sent = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(raise_for_status=lambda: None, content=b"\0\0" * 480)

    monkeypatch.setenv("ELEVEN_API_KEY", "test-key")
    monkeypatch.setenv("TTS_PRONUNCIATION_ALIASES", '{"el":{"OpenAI":"Όπεν έι άι"}}')
    monkeypatch.setattr(opening.httpx, "AsyncClient", Client)
    frames = await opening._voice_line("OpenAI", "greek-voice", "el")
    assert len(frames) == 1
    assert sent[0]["params"]["output_format"] == "pcm_24000"
    body = sent[0]["json"]
    assert body["text"] == "Όπεν έι άι"
    assert body["model_id"] == worker.ELEVENLABS_TTS_MODEL
    assert body["language_code"] == "el"
    assert body["voice_settings"] == worker.elevenlabs_voice_settings()
