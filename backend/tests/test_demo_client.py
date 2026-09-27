"""Execute the shipped demo JS in Node with mocked DOM/RTC boundaries."""
import re
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.routers import demo

NODE_TEST = r'''
const vm = require('node:vm'), assert = require('node:assert/strict');
let input=''; process.stdin.on('data', x => input+=x);
process.stdin.on('end', async () => {
 const elements=new Map(['btn','status','log'].map(id=>[id,{disabled:false,textContent:'',className:'',innerHTML:'',appendChild(){}}]));
 const media=[];
 let mode=process.argv[1];
 class Room {
  constructor(){ this.handlers={}; this.localParticipant={identity:'caller',setMicrophoneEnabled:async()=>{if(mode==='microphone')throw Error('denied');}}; }
  on(e,f){this.handlers[e]=f;}
  async connect(){ const el={remove(){media.splice(media.indexOf(el),1);}};this.handlers.track({kind:'audio',attach:()=>el}); }
  async disconnect(){this.handlers.disconnected();}
 }
 const context=vm.createContext({document:{getElementById:id=>elements.get(id),body:{appendChild:el=>media.push(el)}},
  window:{LivekitClient:{Room,RoomEvent:{TrackSubscribed:'track',TrackUnsubscribed:'untrack',TranscriptionReceived:'text',Disconnected:'disconnected'}},addEventListener(){}},
  console:{error(){}},AbortSignal,fetch:async()=>{if(mode==='network')throw Error('offline');return {ok:true,json:async()=>({url:'wss://test',token:'test'})};}});
 vm.runInContext(input,context);
 await vm.runInContext('start()',context);
 assert.equal(elements.get('btn').disabled,false);
 if(mode==='connected') {assert.equal(elements.get('btn').className,'end');assert.equal(media.length,1);await elements.get('btn').onclick();}
 else assert.equal(elements.get('status').textContent,'Something went wrong. Please try again.');
 assert.equal(media.length,0);
 assert.equal(elements.get('btn').className,'');
 console.log('PASS '+mode);
}).on('error',e=>{console.error(e);process.exitCode=1;});
'''


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["network","microphone","connected"])
async def test_demo_recovers_and_releases_audio(monkeypatch,mode):
    if not shutil.which("node"):
        pytest.skip("Node is required for client JavaScript checks")
    monkeypatch.setattr(demo,"_practice",AsyncMock(return_value=SimpleNamespace(name="Test",language="en")))
    html=await demo.demo_page("test-demo",None)
    js=re.findall(r'<script>(.*?)</script>',html,re.S)[-1]
    result=subprocess.run(["node","-e",NODE_TEST,mode],input=js,text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert "PASS" in result.stdout
