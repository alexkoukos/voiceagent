"""Execute shipped polling code with a mocked network and DOM."""
import json
import re
import shutil
import subprocess

import pytest
from app.routers.demo_dashboard import PAGE, summarize

NODE = r'''
const vm = require('node:vm'), assert = require('node:assert/strict');
let input=''; process.stdin.on('data', x=>input+=x);
process.stdin.on('end', async()=> {
 const {script, data} = JSON.parse(input), mode = process.argv[1];
 class Element {
  constructor(){ this.textContent=''; this.hidden=false; this.children=[]; this.listeners={}; }
  append(...children){ this.children.push(...children); }
  appendChild(child){ this.append(child); }
  replaceChildren(...children){ this.children=children; }
  addEventListener(type, fn){this.listeners[type]=fn;}
  remove(){this.removed=true;}
 }
 const elements=new Map(), listeners={}, timers=new Map(); let sequence=0, requests=0;
 const element=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
 const document={hidden:mode==='hidden', getElementById:element,createElement:()=>new Element(),
  addEventListener:(type,fn)=>listeners[type]=fn};
 const context=vm.createContext({document,window:{addEventListener(){}},location:{pathname:'/demo-dashboard/share'},
  AbortController,Intl,Date,console,
  setTimeout:(fn,delay)=>{const id=++sequence;timers.set(id,{fn,delay});return id;},
  clearTimeout:id=>timers.delete(id),fetch:async(path,options)=>{
   requests++;assert.equal(path,'/demo-dashboard/share/data');assert.equal(options.cache,'no-store');
   if(mode==='error')throw Error('offline');
   return {status:mode==='expired'?404:200,ok:true,json:async()=>data};
  }});
 vm.runInContext(script,context);
 await new Promise(resolve=>setImmediate(resolve));
 if(mode==='hidden') {
   assert.equal(requests,0);document.hidden=false;listeners.visibilitychange();
   await new Promise(resolve=>setImmediate(resolve));assert.equal(requests,1);
 } else assert.equal(requests,1);
 if(mode==='expired') {
   assert.equal(element('expired').hidden,false);assert.equal(element('dashboard').hidden,true);
   assert.equal(element('call-frame').removed,true);assert.equal(timers.size,0);
   await vm.runInContext('refresh()',context);assert.equal(requests,1);
 } else if(mode==='error') {
   assert.match(element('connection').textContent,/stale/);
   assert.equal([...timers.values()][0].delay,8000);
 } else {
   if(mode==='permanent')assert.equal(element('expires').textContent,'No automatic expiry');
   assert.equal(element('count').textContent,0);
   assert.equal(element('cost').textContent,'—');
   assert.equal(element('call-table').hidden,true);
   assert.equal([...timers.values()][0].delay,8000);
 }
 console.log('PASS');
}).on('error',e=>{console.error(e);process.exitCode=1;});
'''


@pytest.mark.parametrize('mode', ['ready', 'permanent', 'hidden', 'expired', 'error'])
def test_polling_handles_empty_hidden_expired_and_failed_requests(mode):
    if not shutil.which('node'):
        pytest.skip('Node is required for client checks')
    page = PAGE.substitute(name='Demo', expires='2026-10-12T12:00:00Z', call_path='/demo-dashboard/token/call')
    data = summarize([], [])
    data.update(updated_at='2026-10-05T12:00:00Z', expires_at='2026-10-12T12:00:00Z',
                older_calls_omitted=False, timings_limited=False)
    if mode == 'permanent':
        data['expires_at'] = None
    result = subprocess.run(['node', '-e', NODE, mode], input=json.dumps({
        'script': re.findall(r'<script>(.*?)</script>', page, re.S | re.I)[0], 'data': data}),
        text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'PASS' in result.stdout
