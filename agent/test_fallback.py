"""Mid-call fallback (ElevenLabs -> Gemini TTS, Scribe -> Deepgram STT), the ElevenLabs credit
check and engine choice, with fake providers: nothing here reaches the network."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from livekit.agents import APIConnectionError, APIConnectOptions, tts as livekit_tts
from livekit.agents import utils

import agent as worker


# --- fake voices ---


class FakeEleven(livekit_tts.TTS):
    """Streaming, 22050 Hz like eleven_flash_v2_5; fails like an account out of credits."""

    def __init__(self, fail: bool = True):
        super().__init__(capabilities=livekit_tts.TTSCapabilities(streaming=True), sample_rate=22050, num_channels=1)
        self.fail = fail
        self.attempts = 0

    def synthesize(self, text, *, conn_options=APIConnectOptions()):
        return _Chunked(tts=self, input_text=text, conn_options=conn_options)

    def stream(self, *, conn_options=APIConnectOptions()):
        return _Stream(tts=self, conn_options=conn_options)


class FakeGemini(livekit_tts.TTS):
    """Non-streaming, 24000 Hz like Gemini TTS."""

    def __init__(self, **_):
        super().__init__(capabilities=livekit_tts.TTSCapabilities(streaming=False), sample_rate=24000, num_channels=1)
        self.texts = []

    def synthesize(self, text, *, conn_options=APIConnectOptions()):
        self.texts.append(text)
        return _Chunked(tts=self, input_text=text, conn_options=conn_options)


def _pcm(rate: int, seconds: float = 0.2) -> bytes:
    return b"\x01\x00" * int(rate * seconds)


class _Chunked(livekit_tts.ChunkedStream):
    async def _run(self, output_emitter):
        t = self._tts
        if isinstance(t, FakeEleven):
            t.attempts += 1
            if t.fail:
                raise APIConnectionError("quota_exceeded: This request exceeds your quota")
        output_emitter.initialize(request_id=utils.shortuuid(), sample_rate=t.sample_rate, num_channels=1,
                                  mime_type="audio/pcm")
        output_emitter.push(_pcm(t.sample_rate))
        output_emitter.flush()


class _Stream(livekit_tts.SynthesizeStream):
    async def _run(self, output_emitter):
        t = self._tts
        t.attempts += 1
        async for _ in self._input_ch:
            pass
        if t.fail:
            raise APIConnectionError("quota_exceeded: This request exceeds your quota")
        output_emitter.initialize(request_id=utils.shortuuid(), sample_rate=t.sample_rate, num_channels=1,
                                  mime_type="audio/pcm", stream=True)
        output_emitter.start_segment(segment_id="s")
        output_emitter.push(_pcm(t.sample_rate))
        output_emitter.end_segment()


@pytest.fixture
def fake_voices(monkeypatch):
    made = {"eleven": [], "gemini": []}

    def eleven(**_):
        made["eleven"].append(FakeEleven())
        return made["eleven"][-1]

    def gemini(**kw):
        made["gemini"].append(FakeGemini(**kw))
        return made["gemini"][-1]

    monkeypatch.setattr(worker.elevenlabs, "TTS", eleven)
    monkeypatch.setattr(worker.google.beta, "GeminiTTS", gemini)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    return made


async def _stream_audio(tts, text: str):
    frames = []
    async with tts.stream() as stream:
        stream.push_text(text)
        stream.end_input()
        async for ev in stream:
            frames.append(ev.frame)
    return frames


@pytest.mark.asyncio
async def test_eleven_out_of_credits_mid_call_speaks_with_gemini(fake_voices):
    tts = worker.pipeline_tts("default")
    # The call keeps ElevenLabs' rate; Gemini's 24 kHz is resampled to it.
    assert tts.sample_rate == 22050
    frames = await _stream_audio(tts, "Καλησπέρα, πώς μπορώ να βοηθήσω;")
    assert frames and all(f.sample_rate == 22050 for f in frames)
    assert sum(f.samples_per_channel for f in frames) > 0.1 * 22050
    eleven = fake_voices["eleven"][0]
    # No retry on ElevenLabs: the caller waits one failed attempt, not three (the second
    # attempt is the background recovery probe, after the reply).
    assert tts._max_retry_per_tts == 0
    assert eleven.attempts <= 2
    assert any("Καλησπέρα" in t for g in fake_voices["gemini"] for t in g.texts)

    # The next reply goes straight to Gemini (ElevenLabs is marked unavailable).
    before = eleven.attempts
    frames = await _stream_audio(tts, "Ναι, βεβαίως.")
    assert frames and all(f.sample_rate == 22050 for f in frames)
    # Only a background recovery probe may touch ElevenLabs, never the reply itself.
    assert eleven.attempts - before <= 1
    assert not tts._status[0].available
    await tts.aclose()


@pytest.mark.asyncio
async def test_eleven_working_is_not_resampled(fake_voices, monkeypatch):
    monkeypatch.setattr(worker.elevenlabs, "TTS", lambda **_: FakeEleven(fail=False))
    tts = worker.pipeline_tts("default")
    frames = await _stream_audio(tts, "Γεια σας.")
    assert frames and all(f.sample_rate == 22050 for f in frames)
    assert not any(g.texts for g in fake_voices["gemini"])
    await tts.aclose()


@pytest.mark.asyncio
async def test_say_with_text_falls_back_too(fake_voices):
    tts = worker.pipeline_tts("default")
    frames = []
    async with tts.synthesize("Η κλήση ηχογραφείται.") as stream:
        async for ev in stream:
            frames.append(ev.frame)
    assert frames and all(f.sample_rate == 22050 for f in frames)
    await tts.aclose()


def test_pipeline_tts_without_gemini_key_is_plain_elevenlabs(fake_voices, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    assert isinstance(worker.pipeline_tts("default"), FakeEleven)


def test_pipeline_stt_is_scribe_then_deepgram(monkeypatch):
    made = []
    monkeypatch.setattr(worker, "scribe_stt", lambda lang, keys=None: made.append(("scribe", lang, keys)) or "S")
    monkeypatch.setattr(worker, "caller_stt", lambda lang: made.append(("deepgram", lang)) or "D")
    captured = {}

    def adapter(stts, **kw):
        captured.update(stts=stts, **kw)
        return "adapter"

    monkeypatch.setattr(worker.livekit_stt, "FallbackAdapter", adapter)
    assert worker.pipeline_stt("el", ["ρε"]) == "adapter"
    assert captured["stts"] == ["S", "D"] and made == [("scribe", "el", ["ρε"]), ("deepgram", "el")]
    # language switch (R7) builds the same pair for the new language
    made.clear()
    assert worker.language_parts("pipeline", "default", "en")["stt"] == "adapter"
    assert made[0][:2] == ("scribe", "en") and made[1] == ("deepgram", "en")


# --- ElevenLabs credits ---


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _sub(**body):
    return lambda request: httpx.Response(200, json=body)


@pytest.fixture
def eleven_key(monkeypatch):
    monkeypatch.setenv("ELEVEN_API_KEY", "test-key")


@pytest.mark.asyncio
async def test_credits_parsing(eleven_key):
    async with _client(_sub(character_limit=100000, character_count=99000)) as c:
        assert await worker.elevenlabs_credits(c) == (1000, False)
    async with _client(_sub(character_limit="100000", character_count="10", can_extend_character_limit=True,
                            allowed_to_extend_character_limit=True)) as c:
        assert await worker.elevenlabs_credits(c) == (99990, True)
    # Can extend in principle but not allowed on this account: not overage.
    async with _client(_sub(character_limit=10, character_count=10, can_extend_character_limit=True,
                            allowed_to_extend_character_limit=False)) as c:
        assert await worker.elevenlabs_credits(c) == (0, False)
    # Missing or null numbers, a non-JSON body, no user_read (401), a 500: all "unknown".
    for handler in (_sub(tier="free"), _sub(character_limit=None, character_count=0),
                    lambda r: httpx.Response(200, text="<html>"),
                    lambda r: httpx.Response(401, json={"detail": {"status": "missing_permissions"}}),
                    lambda r: httpx.Response(500)):
        async with _client(handler) as c:
            assert await worker.elevenlabs_credits(c) is None

    def boom(request):
        raise httpx.ConnectError("down")

    async with _client(boom) as c:
        assert await worker.elevenlabs_credits(c) is None


_REAL_CLIENT = httpx.AsyncClient


def _usable_with(monkeypatch, handler):
    monkeypatch.setattr(worker.httpx, "AsyncClient",
                        lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))


@pytest.mark.asyncio
async def test_usable_reads_credits_before_the_tts_probe(eleven_key, monkeypatch):
    seen = []

    def handler(credits_left, overage=False, probe_status=200):
        def h(request):
            seen.append(request.url.path)
            if request.url.path == "/v1/user/subscription":
                if credits_left is None:
                    return httpx.Response(401)
                return httpx.Response(200, json={"character_limit": 100000, "character_count": 100000 - credits_left,
                                                  "can_extend_character_limit": overage,
                                                  "allowed_to_extend_character_limit": overage})
            return httpx.Response(probe_status, text="quota_exceeded" if probe_status != 200 else "")
        return h

    for credits_left, overage, probe, expected, probed in (
        (100000, False, 200, True, False),   # plenty: no paid probe
        (3000, False, 200, True, False),     # low: warning only
        (1000, False, 200, False, False),    # below the minimum: fallback engine
        (0, True, 200, True, False),         # usage-based billing goes past the limit
        (None, False, 200, True, True),      # no user_read: the TTS probe decides
        (None, False, 401, False, True),     # ... and it says out of credits
    ):
        seen.clear()
        _usable_with(monkeypatch, handler(credits_left, overage, probe))
        assert await worker.elevenlabs_usable() is expected, credits_left
        assert any("text-to-speech" in p for p in seen) is probed, credits_left


@pytest.mark.asyncio
async def test_usable_is_timeboxed(eleven_key, monkeypatch):
    monkeypatch.setattr(worker, "ELEVEN_CHECK_SECONDS", 0.2)

    async def slow(*a, **k):
        await asyncio.sleep(5)

    monkeypatch.setattr(worker, "_elevenlabs_usable", slow)
    loop = asyncio.get_running_loop()
    start = loop.time()
    assert await worker.elevenlabs_usable() is False
    assert loop.time() - start < 1


@pytest.mark.asyncio
async def test_pick_engine(monkeypatch):
    usable = {"v": True}

    async def fake_usable():
        return usable["v"]

    monkeypatch.setattr(worker, "elevenlabs_usable", fake_usable)
    monkeypatch.setattr(worker, "RECEPTIONIST_ENGINE", "pipeline")
    monkeypatch.setattr(worker, "ENGINE", "pipeline")
    monkeypatch.setenv("ELEVEN_API_KEY", "k")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert await worker.pick_engine("c", receptionist=True) == "pipeline"
    assert await worker.pick_engine("c") == "pipeline"
    usable["v"] = False
    assert await worker.pick_engine("c", receptionist=True) == "text_pipeline"
    assert await worker.pick_engine("c") == "realtime"
    # No Gemini key: the receptionist can't use the text pipeline either.
    monkeypatch.delenv("GEMINI_API_KEY")
    assert await worker.pick_engine("c", receptionist=True) == "realtime"
    # No ElevenLabs key: never probes.
    usable["v"] = True
    monkeypatch.delenv("ELEVEN_API_KEY")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setattr(worker, "elevenlabs_usable", lambda: pytest.fail("probed without a key"))
    assert await worker.pick_engine("c", receptionist=True) == "text_pipeline"
    # Realtime is left alone.
    monkeypatch.setattr(worker, "RECEPTIONIST_ENGINE", "realtime")
    assert await worker.pick_engine("c", receptionist=True) == "realtime"
    monkeypatch.setattr(worker, "ENGINE", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert await worker.pick_engine("c") == "realtime"


@pytest.mark.asyncio
async def test_stop_recording_when_recorded_calls_through():
    stopped = []

    async def stop():
        stopped.append(True)

    receiver = SimpleNamespace(_rc=SimpleNamespace(metadata={"record": True}, stop_recording=stop))
    assert await worker.ReceptionistAgent.stop_recording.__wrapped__(receiver) == "recording stopped"
    assert stopped == [True]
