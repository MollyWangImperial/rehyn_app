"""Run the served assessment JavaScript with one opening, including the voice transition."""
import json
import os
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "rehyn_hand_first_open_test")

from backend import server


HARNESS = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const elements = new Map(), timers = [], messages = [];
const ctx = new Proxy({measureText:()=>({width:50})}, {get:(t,k)=>t[k] || (()=>{})});
function element(id){
  if(!elements.has(id)){
    const classes = new Set();
    elements.set(id, {style:{},dataset:{},width:640,height:480,clientWidth:640,clientHeight:480,
      readyState:0,videoWidth:0,videoHeight:0,
      classList:{add:(...ks)=>ks.forEach(k=>classes.add(k)),remove:(...ks)=>ks.forEach(k=>classes.delete(k)),contains:k=>classes.has(k),toggle(k,on){on?classes.add(k):classes.delete(k);}},
      addEventListener(){},appendChild(){},append(){},after(){},setAttribute(){},getContext:()=>ctx,querySelector:()=>null,
      querySelectorAll:()=>[],removeAttribute(){},getBoundingClientRect:()=>({width:640,height:480}),
    });
  }
  return elements.get(id);
}
let now=10000;
const sandbox={URLSearchParams,console,performance:{now:()=>now},
  navigator:{userAgent:'test',maxTouchPoints:0},screen:{width:1440,height:900},
  document:{getElementById:element,querySelector:element,createElement:()=>element(Symbol()),body:element('body')},
  Audio:class{pause(){}},requestAnimationFrame:()=>{},setTimeout:(fn,delay)=>{timers.push(fn);return timers.length;},clearTimeout(){},
  location:{origin:'http://localhost',hostname:'localhost',search:`?package=initial&local_preview=1&affected_side=${input.side}`},
  addEventListener(){},ReactNativeWebView:{postMessage:v=>messages.push(JSON.parse(v))},
  PoseLandmarker:{POSE_CONNECTIONS:[]},HandLandmarker:{HAND_CONNECTIONS:[]},
};
sandbox.window=sandbox;
const context=vm.createContext(sandbox), run=code=>vm.runInContext(code,context);
for(const script of input.support)run(script);
run(input.script);
sandbox.taskData=input.tasks;
run(`tasks=taskData; currentTaskIdx=3; running=true; handLandmarker={};
  drawingUtils={drawConnectors(){},drawLandmarks(){}};
  playVoice=()=>new Promise(resolve=>window.finishVoice=resolve);
  prefetchUpcomingVoice=()=>{};
`);
// A broadside, fully extended hand centered on the displayed target.
const open=Array.from({length:21},()=>({x:.5,y:.45,z:0}));
open[0]={x:.5,y:.53,z:0};
for(const [base,x,y] of [[5,.455,.41],[9,.485,.395],[13,.52,.405],[17,.545,.41]]){
  for(let joint=0;joint<4;joint++) open[base+joint]={x,y:y-joint*.048,z:0};
}
for(let i=1;i<=4;i++) open[i]={x:.49-i*.027,y:.49-i*.025,z:0};
if(input.side==='right') open.forEach(p=>p.x=1-p.x);
if(input.projection==='perspective') open.forEach(p=>{p.z=(p.x-.5)*.16;p.x=.5+(p.x-.5)*.8;});
sandbox.handFrame=open;
function frame(ms=100){
  now+=ms;
  run('latestHandLandmarks=handFrame; latestHandSeenAt=performance.now(); loop();');
}
async function main(){
  const firstVoice=run('startStep()');
  for(let i=0;i<15;i++) frame(); // Open once while the instruction is still playing.
  assert.ok(run('handOpenScore')>.72,'Fixture reproduces the previously rejected fully open hand');
  assert.equal(run('checkTarget(null)'),false,'Instruction must finish before activation');
  sandbox.finishVoice();await firstVoice;
  frame(200);
  assert.equal(run('checkTarget(null)'),false,'Keep the normal settling window');
  frame(200);
  assert.equal(run('checkTarget(null)'),true,'First open hand must activate the ready target');
  for(let i=0;i<14;i++) frame();
  assert.equal(run('stepCompleted'),true,'Steady first opening completes the ready hold');
  timers.shift()(); // Actual nextStep advances to H1-S2 and starts its instruction.
  assert.equal(run('getCurrentStep().id'),'H1-S2');
  for(let i=0;i<12;i++) frame(); // Keep the same open hand; never close/reopen.
  assert.equal(run('checkTarget(null)'),false);
  sandbox.finishVoice();await Promise.resolve();await Promise.resolve();
  frame(400);
  assert.equal(run('checkTarget(null)'),true,'An already-open hand activates the measurement target');
  for(let i=0;i<14;i++) frame();
  assert.equal(run('stepCompleted'),true,'The same opening completes the measurement hold');
  timers.shift()();
  assert.equal(run('getCurrentStep().id'),'H1-S3');
  assert.equal(run('taskResults[3].completed_steps'),2);
  assert.ok(run('taskResults[3].steps[1].metrics.hand_open_score')>.72);
  // Negative cases still require a visible open palm in the target.
  run('currentStepIdx=1; voiceFinishedAt=1;');
  sandbox.handFrame=open.map(p=>({...p,x:p.x+.3}));frame();
  assert.equal(run('checkTarget(null)'),false,'Hand outside target cannot pass');
  sandbox.handFrame=null;frame();
  assert.equal(run('checkTarget(null)'),false,'Missing hand cannot pass');
  sandbox.handFrame=open.map(p=>({...p,x:.5+(p.x-.5)*.08,z:(p.x-.5)*1.2}));
  for(let i=0;i<12;i++) frame();
  assert.equal(run('checkTarget(null)'),false,'Edge-on hand cannot pass');
  sandbox.handFrame=open;
  for(let i=0;i<12;i++) frame();
  run('handOpenScore=.1;');
  assert.equal(run('checkTarget(null)'),false,'Closed hand cannot complete opening');
  // A relaxed hand can still pass preparation, but an open hand never blocks it.
  run('currentStepIdx=0;');
  assert.equal(run('checkTarget(null)'),true);
  run('latestPoseLandmarks=null; affectedHandTrackWrist=null;');
  sandbox.wrongHand={landmarks:[open],handednesses:[[{categoryName:input.side==='left'?'Left':'Right',score:.99}]]};
  assert.equal(run('selectAffectedHandDetection(wrongHand)'),null,'Unaffected hand alone cannot trigger the task');
  console.log('First opening completes readiness and measurement; visibility, palm and voice gates retained');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
"""


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("projection", ["front", "perspective"])
def test_initial_task_four_accepts_the_first_opening(side, projection):
    client = TestClient(server.app)
    response = client.get("/api/pose/runner")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    script = html[html.index("const API_BASE ="):html.index("</script>", html.index("const API_BASE ="))]
    tasks = server.INITIAL_ASSESSMENT_TASKS
    assert tasks[3]["id"] == "H1"
    node = shutil.which("node")
    assert node, "Node.js is required for assessment runner regression tests"
    result = subprocess.run(
        [node, "-e", HARNESS], input=json.dumps({
            "script": script, "tasks": tasks, "side": side, "projection": projection,
            "support": re.findall(r"<script>(.*?)</script>", html[:html.index("const API_BASE =")], re.S),
        }), text=True, capture_output=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
