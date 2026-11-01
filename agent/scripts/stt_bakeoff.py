"""Speech-to-text bake-off on Greek phone audio: the same lines through every engine.

Each caller line of the scenarios is voiced once (ElevenLabs, two voices), reduced to phone
quality (8 kHz, G.711 μ-law, as on the SIP trunk) and streamed in real time to each engine.
Scored against the script: word errors (digits and number words count as equal), lines with
no transcript at all, lines in Latin letters, and how long after the audio ends the final
text arrives.

    cd agent
    uv run --python 3.12 --with-requirements requirements.txt python scripts/stt_bakeoff.py \\
        [--engines "scribe;scribe:terms=none,silence=0.7;deepgram/nova-3"] [--limit 40] [--noise -50]

Needs ELEVEN_API_KEY and LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET (LiveKit Inference).
"""

import argparse
import asyncio
import audioop  # noqa: deprecated in 3.13; this script runs on 3.12
import difflib
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import httpx
import numpy as np
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import inference, stt as agents_stt
from livekit.agents.utils import http_context

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CACHE = Path(__file__).resolve().parent / ".stt_bakeoff_audio"
VOICES = {"m": "cuab90umcstNgL8U7orz", "f": "mRTQIE2xdk2oMdoKFGJu"}  # native Greek (voices.py)
DEFAULT_ENGINES = ("scribe", "deepgram/nova-3", "speechmatics/enhanced", "assemblyai/universal-3-6-pro",
                   "cartesia/ink-2", "openai/gpt-live-transcribe", "google/gemini-3.5-transcribe-live")
NUMBERS = {"ενα": "1", "μια": "1", "μια": "1", "δυο": "2", "τρια": "3", "τρεισ": "3", "τεσσερα": "4", "τεσσερισ": "4",
           "πεντε": "5", "εξι": "6", "επτα": "7", "εφτα": "7", "οχτω": "8", "οκτω": "8", "εννεα": "9", "εννια": "9",
           "δεκα": "10", "εντεκα": "11", "δωδεκα": "12", "μηδεν": "0"}


def words(text: str) -> list[str]:
    plain = unicodedata.normalize("NFD", text.lower())
    plain = "".join(c for c in plain if unicodedata.category(c) != "Mn").replace("ς", "σ")
    out = []
    for w in re.findall(r"\w+", plain):
        if w == "00":
            continue  # "10:00" for «δέκα»
        out.append(NUMBERS.get(w, w))
    return out


def errors(ref: list[str], hyp: list[str]) -> int:
    return sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in
               difflib.SequenceMatcher(None, ref, hyp, autojunk=False).get_opcodes() if op != "equal")


async def voice(http: httpx.AsyncClient, text: str, who: str) -> bytes:
    """The line as 8 kHz μ-law decoded back to 16-bit PCM: what the trunk delivers."""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"{who}-{hashlib.sha1(text.encode()).hexdigest()[:16]}.pcm"
    if path.exists():
        return path.read_bytes()
    r = await http.post(f"https://api.elevenlabs.io/v1/text-to-speech/{VOICES[who]}",
                        params={"output_format": "pcm_16000"}, headers={"xi-api-key": os.environ["ELEVEN_API_KEY"]},
                        json={"text": text, "model_id": "eleven_flash_v2_5", "language_code": "el"}, timeout=30)
    r.raise_for_status()
    pcm8, _ = audioop.ratecv(r.content, 2, 1, 16000, 8000, None)
    phone = audioop.ulaw2lin(audioop.lin2ulaw(pcm8, 2), 2)
    path.write_bytes(phone)
    return phone


def engine(name: str):
    """"scribe" is production's Scribe; "scribe:terms=none,silence=0.7,vad=0.6" a variant
    (terms: all | days | none)."""
    import providers.stt as provider_stt
    if name == "scribe":
        return provider_stt.scribe("el")
    if name.startswith("scribe:"):
        from livekit.plugins import elevenlabs
        opts = dict(kv.split("=") for kv in name.split(":", 1)[1].split(","))
        terms = {"all": provider_stt.vocab_terms("el", None),
                 "days": ["Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή"],
                 "none": []}[opts.get("terms", "all")]
        vad = {"vad_silence_threshold_secs": float(opts.get("silence", provider_stt.SCRIBE_EARLY_SECS))}
        if "vad" in opts:
            vad["vad_threshold"] = float(opts["vad"])
        return elevenlabs.STT(keyterms=terms or None, model="scribe_v2_realtime", language_code="el", server_vad=vad)
    return inference.STT(name, language="el")


def line_noise(n_bytes: int, db: float | None) -> bytes:
    """Faint noise, as on a real line, instead of digital silence."""
    if db is None:
        return b"\0" * n_bytes
    noise = np.random.default_rng().normal(0, 32768 * 10 ** (db / 20), n_bytes // 2)
    return noise.clip(-32768, 32767).astype(np.int16).tobytes()


NOISE_DB: float | None = None


async def transcribe(stt, pcm: bytes) -> tuple[str, float | None]:
    """Stream in real time, then 1.5 s of silence; returns the final text and its delay after the audio."""
    stream = stt.stream()
    finals, last_final = [], None
    ended = 0.0

    async def read():
        nonlocal last_final
        async for ev in stream:
            if ev.type == agents_stt.SpeechEventType.FINAL_TRANSCRIPT and ev.alternatives and ev.alternatives[0].text.strip():
                finals.append(ev.alternatives[0].text.strip())
                last_final = time.monotonic()

    reader = asyncio.create_task(read())
    chunk = 8000 // 50 * 2  # 20 ms
    if NOISE_DB is not None:
        under = np.frombuffer(line_noise(len(pcm) + len(pcm) % 2, NOISE_DB), dtype=np.int16)[: len(pcm) // 2]
        pcm = (np.frombuffer(pcm[: len(under) * 2], dtype=np.int16).astype(np.int32) + under).clip(-32768, 32767).astype(np.int16).tobytes()
    for i in range(0, len(pcm), chunk):
        part = pcm[i:i + chunk].ljust(chunk, b"\0")
        stream.push_frame(rtc.AudioFrame(part, 8000, 1, chunk // 2))
        await asyncio.sleep(0.02)
    ended = time.monotonic()
    for _ in range(75):
        stream.push_frame(rtc.AudioFrame(line_noise(chunk, NOISE_DB), 8000, 1, chunk // 2))
        await asyncio.sleep(0.02)
    stream.end_input()
    try:
        await asyncio.wait_for(reader, timeout=8)
    except asyncio.TimeoutError:
        reader.cancel()
    await stream.aclose()
    return " ".join(finals), (last_final - ended) * 1000 if last_final else None


async def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    p = argparse.ArgumentParser()
    p.add_argument("--engines", default=";".join(DEFAULT_ENGINES), help="separated by ;")
    p.add_argument("--scenarios", default="scripts/scenarios.el.json")
    p.add_argument("--limit", type=int, help="first N distinct lines")
    p.add_argument("--parallel", type=int, default=4, help="lines streamed at the same time per engine")
    p.add_argument("--out", default="stt_bakeoff_result.json")
    p.add_argument("--noise", type=float, help="line noise in dBFS (e.g. -55) instead of digital silence")
    args = p.parse_args()
    global NOISE_DB
    NOISE_DB = args.noise

    lines = list(dict.fromkeys(l for s in json.load(open(args.scenarios, encoding="utf-8")) for l in s["lines"]))
    lines = lines[:args.limit] if args.limit else lines
    async with httpx.AsyncClient() as http:
        clips = [(line, who, await voice(http, line, who)) for line in lines for who in VOICES]
    print(f"{len(clips)} clips ({len(lines)} lines x {len(VOICES)} voices), phone quality")

    http_context._new_session_ctx()
    results = {}

    async def run(name: str) -> None:
        try:
            stt = engine(name)
        except Exception as e:
            results[name] = {"error": f"{type(e).__name__}: {e}"}
            return
        slots = asyncio.Semaphore(args.parallel)

        async def one(line, who, pcm):
            async with slots:
                try:
                    return line, who, *await transcribe(stt, pcm)
                except Exception as e:
                    return line, who, f"<error {type(e).__name__}>", None

        rows = await asyncio.gather(*(one(*c) for c in clips))
        failed = sum(text.startswith("<error") for _, _, text, _ in rows)
        rows = [r for r in rows if not r[2].startswith("<error")]
        ref_words = err = dropped = latin = 0
        delays = []
        for line, who, text, delay in rows:
            ref, hyp = words(line), words(text)
            ref_words += len(ref)
            err += errors(ref, hyp)
            dropped += not hyp
            latin += bool(re.search(r"[a-zA-Z]{3,}", text)) and not re.search(r"[a-zA-Z]{3,}", line)
            if delay is not None:
                delays.append(delay)
        delays.sort()
        results[name] = {
            "word_error_pct": round(100 * err / max(1, ref_words), 1), "dropped_lines": dropped, "latin_lines": latin,
            "failed_requests": failed, "scored_clips": len(rows),
            "final_ms_p50": round(delays[len(delays) // 2]) if delays else None,
            "final_ms_p95": round(delays[int(len(delays) * 0.95)]) if delays else None,
            "lines": [{"said": l, "voice": w, "heard": t, "ms": d and round(d)} for l, w, t, d in rows],
        }

    for name in args.engines.split(";"):  # one engine at a time: providers rate-limit parallel streams
        await run(name)
    print(f"\n{'engine':38} {'words wrong':>11} {'dropped':>8} {'latin':>6} {'final p50':>10} {'p95':>7} {'failed':>7}")
    for name, r in sorted(results.items(), key=lambda kv: kv[1].get("word_error_pct", 999)):
        if "error" in r:
            print(f"{name:38} {r['error'][:60]}")
            continue
        print(f"{name:38} {r['word_error_pct']:>10}% {r['dropped_lines']:>8} {r['latin_lines']:>6} "
              f"{r['final_ms_p50'] or '-':>9}ms {r['final_ms_p95'] or '-':>6}ms {r['failed_requests']:>7}")
    json.dump(results, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
