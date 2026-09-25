"""Scripted test calls over WebRTC (PRD W4): no phone minutes, repeatable.

For each scenario a synthetic caller joins the practice's demo room, says each line
(ElevenLabs TTS) after the agent finishes speaking, then hangs up. The script then reads
the call record from the backend and checks the outcome and booking.

    cd agent
    uv run --python 3.12 --with-requirements requirements.txt python scripts/scripted_calls.py \
        --backend https://<backend> --slug <demo slug> --scenarios scripts/scenarios.el.json

Needs APP_API_TOKEN (to read the call log) and ELEVEN_API_KEY (caller voice) in the env.
The backend's AGENT_NAME decides which worker answers (prank-caller-test for a local one).
"""

import argparse
import asyncio
import json
import os
import sys
import time

import httpx
import numpy as np
from dotenv import load_dotenv
from livekit import rtc

SAMPLE_RATE = 16000
CALLER_VOICE = os.environ.get("CALLER_VOICE_ID", "TX3LPaxmHKxFdv7VOQHJ")
# The agent has finished its turn after this much silence.
AGENT_SILENCE_S = 1.4
TURN_TIMEOUT_S = 25


async def tts(client: httpx.AsyncClient, text: str) -> np.ndarray:
    r = await client.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{CALLER_VOICE}",
        params={"output_format": "pcm_16000"},
        headers={"xi-api-key": os.environ["ELEVEN_API_KEY"]},
        json={"text": text, "model_id": "eleven_flash_v2_5"},
        timeout=30,
    )
    r.raise_for_status()
    return np.frombuffer(r.content, dtype=np.int16)


class AgentEar:
    """Tracks when the agent last made sound."""

    def __init__(self) -> None:
        self.last_sound = 0.0
        self.spoke = False

    async def listen(self, track: rtc.Track) -> None:
        async for ev in rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=1):
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


async def say(source: rtc.AudioSource, pcm: np.ndarray) -> None:
    chunk = SAMPLE_RATE // 50  # 20 ms
    for i in range(0, len(pcm), chunk):
        part = pcm[i:i + chunk]
        if len(part) < chunk:
            part = np.pad(part, (0, chunk - len(part)))
        await source.capture_frame(rtc.AudioFrame(part.tobytes(), SAMPLE_RATE, 1, chunk))
    silence = np.zeros(chunk, dtype=np.int16).tobytes()
    for _ in range(25):  # half a second of silence so the agent's turn detection fires
        await source.capture_frame(rtc.AudioFrame(silence, SAMPLE_RATE, 1, chunk))


async def run_scenario(args, sc: dict, http: httpx.AsyncClient) -> dict:
    r = await http.post(f"{args.backend}/demo/{args.slug}/session", timeout=20)
    r.raise_for_status()
    s = r.json()
    room = rtc.Room()
    ear = AgentEar()
    tasks = []

    @room.on("track_subscribed")
    def _on_track(track, *_):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            tasks.append(asyncio.create_task(ear.listen(track)))

    await room.connect(s["url"], s["token"])
    source = rtc.AudioSource(SAMPLE_RATE, 1)
    track = rtc.LocalAudioTrack.create_audio_track("caller", source)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    audio = [await tts(http, line) for line in sc["lines"]]
    await ear.wait_turn_end()  # greeting
    for line, pcm in zip(sc["lines"], audio):
        print(f"    caller: {line}")
        await say(source, pcm)
        await ear.wait_turn_end()
    await asyncio.sleep(1)
    await room.disconnect()
    for t in tasks:
        t.cancel()

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
    if exp.get("time") and exp["time"] not in (appt.get("starts_at") or "") and appt:
        local = appt.get("starts_at", "")
        problems.append(f"time {local} doesn't contain {exp['time']} (UTC)")
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
    args = p.parse_args()
    scenarios = json.load(open(args.scenarios, encoding="utf-8"))
    if args.only:
        scenarios = [s for s in scenarios if args.only in s["name"]]
    results = []
    async with httpx.AsyncClient() as http:
        for sc in scenarios:
            print(f"- {sc['name']}")
            try:
                res = await run_scenario(args, sc, http)
            except Exception as e:
                res = {"name": sc["name"], "ok": False, "problems": [f"{type(e).__name__}: {e}"]}
            results.append(res)
            print("    " + ("OK" if res["ok"] else "FAIL: " + "; ".join(res["problems"])))
            await asyncio.sleep(3)
    passed = sum(r["ok"] for r in results)
    print(f"\n{passed}/{len(results)} passed ({100 * passed / max(1, len(results)):.0f}%, target 95%)")
    json.dump(results, open("scripted_calls_result.json", "w"), ensure_ascii=False, indent=2)
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
