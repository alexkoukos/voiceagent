"""Scripted test calls over WebRTC (PRD W4): no phone minutes, repeatable.

For each scenario a synthetic caller joins the practice's demo room, says each line
(Gemini or ElevenLabs TTS) after the agent finishes speaking, then hangs up. The script then reads
the call record from the backend and checks the outcome and booking.

    cd agent
    uv run --python 3.12 --with-requirements requirements.txt python scripts/scripted_calls.py \
        --backend https://<backend> --slug <demo slug> --scenarios scripts/scenarios.el.json

Needs APP_API_TOKEN (to read the call log) and GEMINI_API_KEY or ELEVEN_API_KEY
for the caller voice. Gemini is preferred when both are configured.
The backend's AGENT_NAME decides which worker answers (prank-caller-test for a local one).
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import numpy as np
from dotenv import load_dotenv
from livekit import rtc
from livekit.plugins import google

# The agent has finished its turn after this much silence.
AGENT_SILENCE_S = 1.4
TURN_TIMEOUT_S = 25


class CallerVoice:
    def __init__(self, provider: str, client: httpx.AsyncClient) -> None:
        self.provider = provider
        self.client = client
        self.sample_rate = 24000 if provider == "gemini" else 16000
        self.gemini = (google.beta.GeminiTTS(
            api_key=os.environ["GEMINI_API_KEY"],
            # A separate model quota keeps test-caller synthesis from starving the
            # receptionist's own 3.8 TTS during long scripted calls.
            model=os.environ.get("CALLER_GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview"),
            voice_name=os.environ.get("CALLER_GEMINI_VOICE", "Puck"),
        ) if provider == "gemini" else None)

    async def synthesize(self, text: str) -> np.ndarray:
        if self.gemini:
            stream = self.gemini.synthesize(text)
            try:
                chunks = [np.frombuffer(chunk.frame.data, dtype=np.int16).copy() async for chunk in stream]
            finally:
                await stream.aclose()
            return np.concatenate(chunks) if chunks else np.empty(0, dtype=np.int16)
        r = await self.client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{os.environ.get('CALLER_VOICE_ID', 'TX3LPaxmHKxFdv7VOQHJ')}",
            params={"output_format": "pcm_16000"},
            headers={"xi-api-key": os.environ["ELEVEN_API_KEY"]},
            json={"text": text, "model_id": "eleven_flash_v2_5"},
            timeout=30,
        )
        r.raise_for_status()
        return np.frombuffer(r.content, dtype=np.int16)

    async def aclose(self) -> None:
        if self.gemini:
            await self.gemini.aclose()


class AgentEar:
    """Tracks when the agent last made sound."""

    def __init__(self) -> None:
        self.last_sound = 0.0
        self.spoke = False

    async def listen(self, track: rtc.Track, sample_rate: int) -> None:
        async for ev in rtc.AudioStream(track, sample_rate=sample_rate, num_channels=1):
            samples = np.frombuffer(ev.frame.data, dtype=np.int16)
            if samples.size and np.sqrt(np.mean(samples.astype(np.float32) ** 2)) > 300:
                self.last_sound = time.monotonic()
                self.spoke = True

    async def wait_turn_end(self) -> None:
        start = time.monotonic()
        while time.monotonic() - start < TURN_TIMEOUT_S:
            if self.spoke and time.monotonic() - self.last_sound > AGENT_SILENCE_S:
                self.spoke = False
                return
            await asyncio.sleep(0.1)


async def say(source: rtc.AudioSource, pcm: np.ndarray, sample_rate: int) -> None:
    chunk = sample_rate // 50  # 20 ms
    for i in range(0, len(pcm), chunk):
        part = pcm[i:i + chunk]
        if len(part) < chunk:
            part = np.pad(part, (0, chunk - len(part)))
        await source.capture_frame(rtc.AudioFrame(part.tobytes(), sample_rate, 1, chunk))
    silence = np.zeros(chunk, dtype=np.int16).tobytes()
    for _ in range(25):  # half a second of silence so the agent's turn detection fires
        await source.capture_frame(rtc.AudioFrame(silence, sample_rate, 1, chunk))


async def run_scenario(args, sc: dict, http: httpx.AsyncClient, caller: CallerVoice) -> dict:
    r = await http.post(f"{args.backend}/demo/{args.slug}/session", timeout=20)
    r.raise_for_status()
    s = r.json()
    room = rtc.Room()
    ear = AgentEar()
    tasks = []

    @room.on("track_subscribed")
    def _on_track(track, *_):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            tasks.append(asyncio.create_task(ear.listen(track, caller.sample_rate)))

    try:
        await room.connect(s["url"], s["token"])
        source = rtc.AudioSource(caller.sample_rate, 1)
        track = rtc.LocalAudioTrack.create_audio_track("caller", source)
        await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        audio = [await caller.synthesize(line) for line in sc["lines"]]
        await ear.wait_turn_end()  # greeting
        for line, pcm in zip(sc["lines"], audio):
            print(f"    caller: {line}")
            await say(source, pcm, caller.sample_rate)
            await ear.wait_turn_end()
        await asyncio.sleep(1)
    finally:
        await room.disconnect()
        for task in tasks:
            task.cancel()

    headers = {"x-api-key": os.environ["APP_API_TOKEN"]}
    call = None
    for _ in range(30):  # wait for the call to be finalized (outcome + summary)
        await asyncio.sleep(2)
        practice_calls = await http.get(f"{args.backend}/practices/{args.practice}/calls/{s['call_id']}", headers=headers)
        call = practice_calls.json()
        if call.get("outcome") and call.get("status") in ("completed", "failed"):
            break
    return check(sc, call)


def check(sc: dict, call: dict) -> dict:
    exp = sc.get("expect", {})
    problems = []
    if exp.get("outcome") and call.get("outcome") != exp["outcome"]:
        problems.append(f"outcome {call.get('outcome')} != {exp['outcome']}")
    appt = call.get("appointment") or {}
    if exp.get("service_id") and appt.get("service_id") != exp["service_id"]:
        problems.append(f"service {appt.get('service_id')} != {exp['service_id']}")
    if exp.get("time") and exp["time"] not in (appt.get("starts_at") or ""):
        local = appt.get("starts_at", "")
        problems.append(f"time {local} doesn't contain {exp['time']} (UTC)")
    if exp.get("time_local"):
        starts_at = appt.get("starts_at")
        local = (datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
                 .astimezone(ZoneInfo(exp.get("timezone", "Europe/Athens"))).strftime("%H:%M")
                 if starts_at else None)
        if local != exp["time_local"]:
            problems.append(f"local time {local} != {exp['time_local']}")
    if exp.get("customer_name") and exp["customer_name"].lower() not in (appt.get("customer_name") or "").lower():
        problems.append(f"name {appt.get('customer_name')!r}")
    for flag in exp.get("flags", []):
        if flag not in call.get("flags", []):
            problems.append(f"missing flag {flag}")
    if exp.get("path"):
        paths = [r["path"] for r in call.get("routing", [])]
        if exp["path"] not in paths:
            problems.append(f"routing path {exp['path']} not in {paths}")
    return {"name": sc["name"], "ok": not problems, "problems": problems, "call_id": call.get("id"),
            "summary": call.get("summary")}


async def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--backend", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--practice", required=True, help="practice id (to read its call log)")
    p.add_argument("--scenarios", default="scripts/scenarios.el.json")
    p.add_argument("--only", help="run scenarios whose name contains this")
    p.add_argument("--caller-tts", choices=("auto", "gemini", "elevenlabs"), default="auto")
    args = p.parse_args()
    provider = ("gemini" if os.environ.get("GEMINI_API_KEY") else "elevenlabs") if args.caller_tts == "auto" else args.caller_tts
    if not os.environ.get("GEMINI_API_KEY" if provider == "gemini" else "ELEVEN_API_KEY"):
        p.error(f"{provider} caller voice needs its API key")
    scenarios = json.load(open(args.scenarios, encoding="utf-8"))
    if args.only:
        scenarios = [s for s in scenarios if args.only in s["name"]]
    results = []
    async with httpx.AsyncClient() as http:
        caller = CallerVoice(provider, http)
        try:
            for sc in scenarios:
                print(f"- {sc['name']}")
                try:
                    res = await run_scenario(args, sc, http, caller)
                except Exception as e:
                    res = {"name": sc["name"], "ok": False, "problems": [f"{type(e).__name__}: {e}"]}
                results.append(res)
                print("    " + ("OK" if res["ok"] else "FAIL: " + "; ".join(res["problems"])))
                await asyncio.sleep(3)
        finally:
            await caller.aclose()
    passed = sum(r["ok"] for r in results)
    print(f"\n{passed}/{len(results)} passed ({100 * passed / max(1, len(results)):.0f}%, target 95%)")
    json.dump(results, open("scripted_calls_result.json", "w"), ensure_ascii=False, indent=2)
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
