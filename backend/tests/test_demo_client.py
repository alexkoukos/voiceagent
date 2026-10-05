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
        name="Κομμωτήριο <Αθηνά>", language="el", timezone="Europe/Athens", services=[], hours={})))
    html = await demo.demo_page("preview", None, embed=True)
    assert '<html lang="el" class="embed">' in html
    assert "Κομμωτήριο &lt;Αθηνά&gt;" in html
    assert "Υπηρεσίες, τιμές και ωράριο" in html
    assert 'id="log"' not in html
    assert "TranscriptionReceived" not in html


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
 const elements=new Map(['btn','status','call-panel','log','empty','jump','voice'].map(id=>[id,new Element(id)]));
 elements.get('voice').value='eleven_sarah';
 elements.get('btn').dataset={call:'Start call',hangUp:'Hang up'};
 elements.get('log').dataset={you:'You',assistant:'Assistant'};
 const media=[];
 let mode=process.argv[1], stopped=0, requested=0;
 class Room {
  constructor(){ Room.last=this; this.handlers={}; this.localParticipant={identity:'caller',publishTrack:async()=>{if(mode==='publish')throw Error('publish failed');}}; }
  on(e,f){this.handlers[e]=f;}
  async connect(){ const el={remove(){media.splice(media.indexOf(el),1);}};this.handlers.track({kind:'audio',attach:()=>el}); }
  async disconnect(){this.handlers.disconnected();}
 }
 const context=vm.createContext({document:{getElementById:id=>elements.get(id),createElement:()=>new Element(),
  documentElement:{lang:'en'},body:{appendChild:el=>media.push(el)}},
  window:{location:{pathname:'/demo/test-demo'},LivekitClient:{Room,createLocalAudioTrack:async()=>{
   if(mode==='microphone'){const error=Error('denied');error.name='NotAllowedError';throw error;}
   return {stop(){stopped++;}};
  },RoomEvent:{TrackSubscribed:'track',TrackUnsubscribed:'untrack',TranscriptionReceived:'text',Disconnected:'disconnected'}},addEventListener(){}},
  console:{error(){}},AbortSignal,fetch:async(url, options)=>{assert.equal(JSON.parse(options.body).voice,"eleven_sarah");requested++;if(mode==='network')throw Error('offline');return {ok:mode!=='busy',status:mode==='busy'?429:200,json:async()=>({url:'wss://test',token:'test'})};}});
 vm.runInContext(input,context);
 await vm.runInContext('start()',context);
 assert.equal(elements.get('btn').disabled,false);
 if(mode==='connected') {
  assert.equal(elements.get('btn').className,'end');assert.equal(media.length,1);
  assert.equal(Room.last.handlers.text,undefined,'no live transcription subscription');
  await elements.get('btn').click();
 } else assert.equal(elements.get('status').textContent,mode==='microphone' ?
  'Allow microphone access and try again.' : mode==='busy' ? 'All lines are busy. Try again shortly.' : 'Something went wrong. Please try again.');
 assert.equal(requested,mode==='microphone'?0:1,'denied microphone must not create a session');
 assert.equal(stopped,mode==='microphone'?0:1,'release microphone on hangup and failure');
 assert.equal(media.length,0);
 assert.equal(elements.get('btn').className,'');
 console.log('PASS '+mode);
}).on('error',e=>{console.error(e);process.exitCode=1;});
'''


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["network","microphone","connected","busy","publish"])
async def test_demo_recovers_and_releases_audio(monkeypatch,mode):
    if not shutil.which("node"):
        pytest.skip("Node is required for client JavaScript checks")
    monkeypatch.setattr(demo,"_practice",AsyncMock(return_value=SimpleNamespace(name="Test",language="en",timezone="Europe/Athens",services=[],hours={})))
    html=await demo.demo_page("test-demo",None)
    js=re.findall(r'<script>(.*?)</script>',html,re.S)[-1]
    result=subprocess.run(["node","-e",NODE_TEST,mode],input=js,text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert "PASS" in result.stdout


@pytest.mark.asyncio
async def test_public_info_is_allowlisted_and_html_escaped(monkeypatch):
    practice = SimpleNamespace(
        name="Dental", language="en", timezone="Europe/Athens",
        services=[{"name": "<script>alert(1)</script>", "price": "40€", "duration_minutes": 30,
                   "internal_note": "private"}],
        hours={"mon": [["09:00", "14:00"]]}, knowledge_base={"PIN": "secret"},
        calendar_id="private-calendar", notifications={"emails": ["private@example.com"]})
    monkeypatch.setattr(demo, "_practice", AsyncMock(return_value=practice))
    info = await demo.demo_info("preview", None)
    assert set(info) == {"name", "timezone", "services", "hours"}
    assert set(info["services"][0]) == {"name", "price", "duration_minutes"}
    html = await demo.demo_page("preview", None)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "40€" in html and "09:00–14:00" in html
    assert "private" not in html and "secret" not in html


@pytest.mark.asyncio
async def test_public_info_preserves_not_found(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(demo, "_practice", AsyncMock(side_effect=HTTPException(404, "Not found")))
    with pytest.raises(HTTPException) as exc:
        await demo.demo_info("missing", None)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_public_demo_http_access_without_credentials(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from app.database import get_db

    practice = SimpleNamespace(name="Dental", language="en", timezone="Europe/Athens",
                               services=[{"name": "Cleaning", "duration_minutes": 45, "price": "60€"}],
                               hours={})
    database = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalar_one_or_none=lambda: practice)))
    async def db_override():
        yield database
    app = FastAPI()
    app.include_router(demo.router)
    app.dependency_overrides[get_db] = db_override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get('/demo/test-dental/info')
        assert response.status_code == 200
        assert response.json()['services'][0]['price'] == '60€'
        page = await client.get('/demo/test-dental')
        assert page.status_code == 200 and '60€' in page.text
        database.execute.return_value.scalar_one_or_none = lambda: None
        assert (await client.get('/demo/offboarded/info')).status_code == 404


def test_dental_demo_fixture_has_no_external_connections():
    import json
    from pathlib import Path
    from app.schemas import PracticeIn
    data = json.loads((Path(__file__).parents[1] / 'config' / 'demo_dentist.json').read_text())
    practice = PracticeIn.model_validate(data)
    assert not practice.phone_numbers and not practice.calendar_id
    assert not practice.notifications.customer_sms and not practice.notifications.emails
    assert not practice.reminders.enabled and not practice.outbound_number
    assert all(service.price for service in practice.services)
