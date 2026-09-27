"""OP1: actual WebRTC conversation every 5 min; opt-in SIP test per DID daily.

Run in an independent service with the agent requirements, ADMIN_API_TOKEN, caller TTS
credentials and (only for --phone) LIVEKIT_* / SIP_TRUNK_ID / SIP_OUTBOUND_NUMBER.
--phone must only be enabled for numbers whose owner agreed to daily synthetic calls.
This script is never started by the backend or by a normal test run.

python scripts/monitor_calls.py --backend https://backend --practice ID [--phone] [--once]
"""
import argparse
import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
from dotenv import load_dotenv
from google.protobuf.duration_pb2 import Duration
from livekit import api, rtc

from scripted_calls import AgentEar, CallerVoice, run_scenario, say

SCENARIO = {"name": "health-information", "lines": ["Τι ώρες είστε ανοιχτά;", "Ευχαριστώ, γεια σας."],
            "expect": {"outcome": "info_given"}}


async def phone_probe(number, caller):
    """Route a real SIP call back through the inbound system and exchange audio both ways."""
    frames = [await caller.synthesize(text) for text in SCENARIO["lines"]]
    room_name = "phone-health-" + uuid.uuid4().hex
    token = (api.AccessToken(os.environ["LIVEKIT_API_KEY"],os.environ["LIVEKIT_API_SECRET"])
             .with_identity("synthetic-monitor").with_grants(api.VideoGrants(room_join=True,room=room_name)).to_jwt())
    room, ear, tasks = rtc.Room(), AgentEar(), []
    @room.on("track_subscribed")
    def track(track, *_):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            tasks.append(asyncio.create_task(ear.listen(track,caller.sample_rate)))
    async with api.LiveKitAPI() as lk:
        try:
            await room.connect(os.environ["LIVEKIT_URL"], token)
            source=rtc.AudioSource(caller.sample_rate,1)
            await room.local_participant.publish_track(rtc.LocalAudioTrack.create_audio_track("monitor",source))
            await lk.sip.create_sip_participant(api.CreateSIPParticipantRequest(
                sip_trunk_id=os.environ["SIP_TRUNK_ID"],sip_call_to=number,
                sip_number=os.environ["SIP_OUTBOUND_NUMBER"],room_name=room_name,
                participant_identity="practice-line",wait_until_answered=True,
                ringing_timeout=Duration(seconds=5)))
            await ear.wait_turn_end()
            for pcm in frames:
                await say(source,pcm,caller.sample_rate)
                await ear.wait_turn_end()
        finally:
            await room.disconnect()
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            try: await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
            except Exception: pass


async def main():
    os.umask(0o077)
    load_dotenv()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend",required=True)
    parser.add_argument("--practice",required=True)
    parser.add_argument("--phone",action="store_true",help="Enable real calls to this practice's configured DIDs")
    parser.add_argument("--once",action="store_true")
    parser.add_argument("--state",type=Path,default=Path("monitor-state.json"))
    args=parser.parse_args()
    headers={"x-api-key":os.environ["ADMIN_API_TOKEN"]}
    state=json.loads(args.state.read_text()) if args.state.exists() else {}
    async with httpx.AsyncClient(timeout=15) as http:
        caller=CallerVoice("gemini" if os.environ.get("GEMINI_API_KEY") else "elevenlabs",http)
        try:
            while True:
                cycle=time.monotonic()
                response=await http.get(f"{args.backend}/practices/{args.practice}", headers=headers)
                response.raise_for_status();practice=response.json()
                targets=[("web",None)]
                today=datetime.now(timezone.utc).date().isoformat()
                if args.phone:
                    targets += [("phone",n) for n in practice["phone_numbers"] if state.get(args.practice+":"+n)!=today]
                for kind,number in targets:
                    ok=False;detail=""
                    try:
                        if kind=="web":
                            if not practice.get("slug"): raise ValueError("Demo is disabled")
                            result=await run_scenario(SimpleNamespace(backend=args.backend,practice=args.practice,
                                                      slug=practice["slug"],isolated=False),SCENARIO,http,caller)
                            ok=result["ok"];detail="; ".join(result["problems"])
                        else:
                            # Persist the attempt before dialing: restarting never causes a second call that day.
                            state[args.practice+":"+number]=today
                            args.state.write_text(json.dumps(state))
                            started=datetime.now(timezone.utc).replace(tzinfo=None)
                            await phone_probe(number,caller)
                            for _ in range(20):
                                response=await http.get(f"{args.backend}/practices/{args.practice}/calls",headers=headers)
                                response.raise_for_status()
                                matches=[c for c in response.json() if c.get("direction")=="inbound"
                                         and c.get("caller_number")==os.environ["SIP_OUTBOUND_NUMBER"]
                                         and datetime.fromisoformat(c["created_at"].replace("Z","+00:00")).replace(tzinfo=None)>=started]
                                if any(c.get("status")=="completed" and c.get("outcome")=="info_given" for c in matches):
                                    ok=True;break
                                await asyncio.sleep(2)
                            if not ok: detail="No completed inbound assistant call found for this practice"
                    except Exception as exc:
                        detail=type(exc).__name__  # never log signed URLs or credentials
                    report=await http.post(f"{args.backend}/practices/{args.practice}/health-checks",
                                           headers=headers, json={"kind":kind,"number":number,"success":ok,"detail":detail[:300]})
                    report.raise_for_status()
                    print(json.dumps({"practice_id":args.practice,"kind":kind,"ok":ok,"detail":detail}),flush=True)
                    if kind=="phone":
                        state[args.practice+":"+number]=today
                        args.state.write_text(json.dumps(state));args.state.chmod(0o600)
                if args.once: break
                await asyncio.sleep(max(1,300-(time.monotonic()-cycle)))
        finally:
            await caller.aclose()


if __name__=="__main__":
    asyncio.run(main())
