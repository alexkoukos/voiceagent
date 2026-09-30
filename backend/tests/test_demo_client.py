"""Execute the shipped demo JS in Node with mocked DOM/RTC boundaries."""
import re
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.routers import demo


@pytest.mark.asyncio
async def test_demo_embedded_page_is_localized_and_escapes_practice_name(monkeypatch):
    monkeypatch.setattr(demo, "_practice", AsyncMock(return_value=SimpleNamespace(
        name="Κομμωτήριο <Αθηνά>", language="el")))
    html = await demo.demo_page("preview", None, embed=True)
    assert '<html lang="el" class="embed">' in html
    assert "Κομμωτήριο &lt;Αθηνά&gt;" in html
    assert 'data-you="Εσείς"' in html
    assert 'data-assistant="Βοηθός"' in html


NODE_TEST = r'''
const vm = require('node:vm'), assert = require('node:assert/strict');
let input=''; process.stdin.on('data', x => input+=x);
process.stdin.on('end', async () => {
 class Element {
  constructor(id='') { this.id=id; this.children=[]; this.dataset={}; this.listeners={}; this.hidden=false;
    this.textContent=''; this.className=''; this.disabled=false; this.scrollTop=0; this.scrollHeight=0; this.clientHeight=200; }
  append(...children) { this.children.push(...children); this.scrollHeight += children.length * 80; }
  appendChild(child) { this.append(child); return child; }
  replaceChildren() { this.children=[]; this.scrollHeight=0; this.scrollTop=0; }
  addEventListener(type, listener) { this.listeners[type]=listener; }
  click() { return this.listeners.click(); }
 }
 const elements=new Map(['btn','status','call-panel','log','empty','jump'].map(id=>[id,new Element(id)]));
 elements.get('btn').dataset={call:'Start call',hangUp:'Hang up'};
 elements.get('log').dataset={you:'You',assistant:'Assistant'};
 const media=[];
 let mode=process.argv[1];
 class Room {
  constructor(){ Room.last=this; this.handlers={}; this.localParticipant={identity:'caller',setMicrophoneEnabled:async()=>{
    if(mode==='microphone'){const error=Error('denied');error.name='NotAllowedError';throw error;}}}; }
  on(e,f){this.handlers[e]=f;}
  async connect(){ const el={remove(){media.splice(media.indexOf(el),1);}};this.handlers.track({kind:'audio',attach:()=>el}); }
  async disconnect(){this.handlers.disconnected();}
 }
 const context=vm.createContext({document:{getElementById:id=>elements.get(id),createElement:()=>new Element(),
  documentElement:{lang:'en'},body:{appendChild:el=>media.push(el)}},
  window:{location:{pathname:'/demo/test-demo'},LivekitClient:{Room,RoomEvent:{TrackSubscribed:'track',TrackUnsubscribed:'untrack',TranscriptionReceived:'text',Disconnected:'disconnected'}},addEventListener(){}},
  console:{error(){}},AbortSignal,fetch:async()=>{if(mode==='network')throw Error('offline');return {ok:true,json:async()=>({url:'wss://test',token:'test'})};}});
 vm.runInContext(input,context);
 await vm.runInContext('start()',context);
 assert.equal(elements.get('btn').disabled,false);
 if(mode==='connected') {
  assert.equal(elements.get('btn').className,'end');assert.equal(media.length,1);
  Room.last.handlers.text([{id:'caller-1',text:' Hello there ',final:true}],{identity:'caller'});
  Room.last.handlers.text([{id:'agent-1',text:'Hello! How can I help?',final:true}],{identity:'agent'});
  Room.last.handlers.text([{id:'caller-1',text:'Hello there!',final:true}],{identity:'caller'});
  const log=elements.get('log');
  assert.equal(log.children.length,2,'an updated segment must not create a duplicate turn');
  assert.equal(log.children[0].children[0].children[0].textContent,'You');
  assert.equal(log.children[0].children[1].textContent,'Hello there!');
  assert.equal(log.children[1].children[0].children[0].textContent,'Assistant');
  assert.equal(elements.get('empty').hidden,true);
  log.scrollHeight=1000;log.scrollTop=0;log.clientHeight=100;
  Room.last.handlers.text([{id:'agent-2',text:'Another turn',final:true}],{identity:'agent'});
  assert.equal(elements.get('jump').hidden,false,'new turns should not force a scrolled-up reader to the bottom');
  elements.get('jump').click();assert.equal(log.scrollTop,log.scrollHeight);
  await elements.get('btn').click();
 } else assert.equal(elements.get('status').textContent,mode==='microphone' ?
  'Allow microphone access and try again.' : 'Something went wrong. Please try again.');
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
