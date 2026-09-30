const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { VoiceGuide } = require('../reach_voice.js');

test('the prior device voice speaks a Testing instruction without a server audio request', async () => {
  let spoken;
  const synth={getVoices:()=>[{lang:'en-GB',name:'English device voice'}],cancel(){},
    speak(utterance){spoken=utterance;queueMicrotask(()=>utterance.onend());}};
  const Utterance=class {constructor(text){this.text=text;}};
  const guide=new VoiceGuide({synth,Utterance});
  assert.equal(await guide.speak('Rest your hand on your lap.'),true);
  assert.equal(spoken.text,'Rest your hand on your lap.');
  assert.equal(spoken.voice.name,'English device voice');
  assert.equal(guide.status,'Device voice');
});

test('Testing retries a transient audio failure and keeps later Molly instructions enabled', async () => {
  const source=fs.readFileSync(path.join(__dirname,'..','testing_reach_flow.js'),'utf8');
  assert.match(source,/fetchAudio:fetchReachMollyAudio/);
  assert.match(source,/\/testing\/reach\/voice/);
  assert.match(source,/\.\.\.ACCOUNT_HEADERS/);
  const start=source.indexOf('let reachSpeechTail=Promise.resolve()');
  const end=source.indexOf('\n}',start)+2;
  const calls=[];
  const reachVoice={enabled:true,failed:false,async speak(text){
    calls.push({text,enabled:this.enabled});
    if(calls.length===1){this.failed=true;return false;}
    this.failed=false;return true;
  }};
  const checkbox={checked:true};
  const context={reachFlow:{stopped:false,voiceUnavailable:false},reachVoice,
    document:{getElementById:()=>checkbox}};
  vm.createContext(context);vm.runInContext(source.slice(start,end),context);
  assert.equal(await context.reachSay('Reach forward'),true);
  assert.equal(await context.reachSay('Hold at the target'),true);
  assert.deepEqual(calls,[{text:'Reach forward',enabled:true},{text:'Reach forward',enabled:true},{text:'Hold at the target',enabled:true}]);
  assert.equal(reachVoice.enabled,true);
  assert.equal(checkbox.checked,true);
});

test('Testing queues overlapping prompts so the first instruction finishes', async () => {
  const source=fs.readFileSync(path.join(__dirname,'..','testing_reach_flow.js'),'utf8');
  const start=source.indexOf('let reachSpeechTail=Promise.resolve()');
  const end=source.indexOf('\n}',start)+2;
  const seen=[];
  let finishFirst;
  const context={reachFlow:{stopped:false,voiceUnavailable:false},
    reachVoice:{enabled:true,failed:false,speak(text){
      seen.push(text);
      if(text==='first')return new Promise(resolve=>{finishFirst=resolve;});
      return Promise.resolve(true);
    }},document:{getElementById:()=>({checked:true})}};
  vm.runInNewContext(source.slice(start,end),context);
  const first=context.reachSay('first');
  const second=context.reachSay('second');
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(seen,['first']);
  finishFirst(true);
  assert.equal(await first,true);
  assert.equal(await second,true);
  assert.deepEqual(seen,['first','second']);
});

function reachRecoveryFixture(failures){
  const source=fs.readFileSync(path.join(__dirname,'../testing_reach_flow.js'),'utf8'),nodes=new Map();
  const node=id=>{if(!nodes.has(id))nodes.set(id,{checked:true,disabled:false,textContent:'',hidden:true,
    classList:{add(){node(id).hidden=true;},remove(){node(id).hidden=false;}}});return nodes.get(id);};
  const guide={enabled:true,failed:false,busy:false,caption:'',calls:[],cancel(){this.failed=false;},async speak(text){
    this.caption=text;this.calls.push({text,enabled:this.enabled});this.failed=this.enabled&&failures-->0;return !this.failed;
  }};
  const context=vm.createContext({reachFlow:{stopped:false,recovery:null,replaying:false,step:{phase:'attempt'}},reachVoice:guide,
    testingReachEnabled:()=>true,calibratingAssessment:false,preAssessmentCalibrationReady:true,reachCameraStatus:()=>({ready:true}),
    document:{getElementById:node},syncReachCaption(){},audioUnlockPromise:null,unlockAudioPlayback:async()=>true});
  const stop=source.indexOf('function stopReachVoice(');
  vm.runInContext(source.slice(source.indexOf('let reachSpeechTail=Promise.resolve()'),source.indexOf('function reachChoice('))
    +source.slice(source.indexOf('function reachCanAttempt('),source.indexOf('async function handleReachEvent('))
    +source.slice(stop,source.indexOf('\n}',stop)+2),context);
  return {context,guide,node};
}
test('T1 repeated failure waits for resume and preserves the raised-reach then hold cue order',async()=>{
  const {context,guide,node}=reachRecoveryFixture(2);
  let resolved=false;const first=context.reachSay('raise').then(ok=>{resolved=true;return ok}),second=context.reachSay('hold');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(resolved,false);assert.equal(context.reachCanAttempt(),false);
  assert.equal(guide.enabled,true);assert.equal(node('reachVoiceOn').checked,true);
  assert.equal(node('reachReplay').hidden,false);
  await node('reachReplay').onclick();assert.equal(await first,true);assert.equal(await second,true);
  assert.deepEqual(guide.calls.map(c=>c.text),['raise','raise','raise','hold']);
  assert.ok(guide.calls.every(c=>c.enabled));assert.equal(node('reachReplay').hidden,true);
  assert.equal(context.reachCanAttempt(),true);
});
test('T1 switches to captions only after an explicit choice and completes the pending cue',async()=>{
  const {context,guide,node}=reachRecoveryFixture(99),first=context.reachSay('raise');
  await new Promise(resolve=>setImmediate(resolve));node('reachVoiceOn').onchange({target:{checked:false}});
  assert.equal(await first,true);assert.deepEqual(guide.calls.at(-1),{text:'raise',enabled:false});
  assert.equal(context.reachFlow.recovery,null);
});
test('T1 stop cancels recovery and all queued speech',async()=>{
  const {context,guide}=reachRecoveryFixture(99),first=context.reachSay('raise'),second=context.reachSay('hold');
  await new Promise(resolve=>setImmediate(resolve));context.stopReachVoice();
  assert.equal(await first,false);assert.equal(await second,false);assert.equal(guide.calls.length,2);
});

test('testing voice waits for Molly audio to finish', async () => {
  const audio = {
    pause() {},
    play() { return Promise.resolve(); },
  };
  const guide = new VoiceGuide({ fetchAudio: async () => 'SUQz', audio, synth: null });
  const speaking = guide.speak('Reach forward');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(audio.src, 'data:audio/mpeg;base64,SUQz');
  assert.equal(guide.busy, true);
  audio.onended();
  assert.equal(await speaking, true);
  assert.equal(guide.busy, false);
  assert.equal(guide.status, 'Molly voice');
});

test('failed Molly audio leaves the test waiting for retry or captions', async () => {
  const audio = { pause() {}, play() { return Promise.reject(new Error('blocked')); } };
  const guide = new VoiceGuide({ fetchAudio: async () => 'SUQz', audio, synth: null });
  assert.equal(await guide.speak('Reach forward'), false);
  assert.equal(guide.failed, true);
  guide.enabled = false;
  guide.captionMs = 0;
  assert.equal(await guide.speak('Reach forward'), true);
  assert.equal(guide.status, 'Captions only');
});

test('a transient media pause resumes the same Molly instruction', async () => {
  let plays=0;
  const audio={paused:false,ended:false,duration:7,currentTime:2,pause(){this.paused=true;},
    play(){plays++;this.paused=false;return Promise.resolve();}};
  const guide=new VoiceGuide({fetchAudio:async()=> 'SUQz',audio,synth:null});
  const speaking=guide.speak('Sit upright and reach forward');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(plays,1);
  audio.paused=true;
  audio.onpause();
  await new Promise(resolve=>setTimeout(resolve,250));
  assert.equal(plays,2);
  assert.equal(guide.busy,true);
  audio.currentTime=audio.duration;
  audio.onended();
  assert.equal(await speaking,true);
});

test('an early media end does not count as a spoken instruction', async () => {
  const audio={duration:7,currentTime:2,pause(){},play(){return Promise.resolve();}};
  const guide=new VoiceGuide({fetchAudio:async()=> 'SUQz',audio,synth:null});
  const speaking=guide.speak('Sit upright and reach forward');
  await new Promise(resolve=>setImmediate(resolve));
  audio.onended();
  assert.equal(await speaking,false);
  assert.equal(guide.failed,true);
});
test('a silent decoder stall reloads the cached clip and resumes near the last word without unlocking',async()=>{
  let loads=0,plays=0;
  const listeners=new Map();
  const audio={paused:false,ended:false,duration:12,currentTime:0,
    pause(){},play(){plays++;return Promise.resolve();},
    addEventListener(name,callback){listeners.set(name,callback);},removeEventListener(name){listeners.delete(name);},
    load(){loads++;this.currentTime=0;listeners.get('loadedmetadata')?.();}};
  const states=[];
  const guide=new VoiceGuide({audio,fetchAudio:async()=> 'SUQz',synth:null,progressTimeoutMs:40,onChange:s=>states.push(s.status)});
  const speaking=guide.speak('Sit comfortably upright with your affected hand resting on your lap.');
  await new Promise(resolve=>setImmediate(resolve));audio.currentTime=2.8;
  await new Promise(resolve=>setTimeout(resolve,90));
  assert.equal(loads,1);assert.equal(plays,2);assert.equal(guide.busy,true);
  assert.ok(Math.abs(audio.currentTime-2.65)<.01);assert.ok(states.includes('Restoring Molly voice…'));
  audio.currentTime=12;audio.onended();assert.equal(await speaking,true);
  await new Promise(resolve=>setTimeout(resolve,70));assert.equal(loads,1,'No recovery continues after completion');
});
test('persistent silent stalls fail visibly and cancellation stops the progress monitor',async()=>{
  let loads=0;const listeners=new Map();
  const audio={duration:12,currentTime:0,paused:false,ended:false,pause(){},play(){return Promise.resolve();},
    addEventListener(k,fn){listeners.set(k,fn);},removeEventListener(k){listeners.delete(k);},load(){loads++;}};
  const guide=new VoiceGuide({audio,fetchAudio:async()=> 'SUQz',synth:null,progressTimeoutMs:20});
  assert.equal(await guide.speak('Full instruction'),false);assert.equal(guide.failed,true);assert.equal(loads,2);
  const next=guide.speak('Second instruction');await new Promise(resolve=>setImmediate(resolve));guide.cancel();
  assert.equal(await next,false);await new Promise(resolve=>setTimeout(resolve,50));assert.equal(loads,2);
});

test('Testing gives the hold instruction before the raised target unlocks', () => {
  const flow=fs.readFileSync(path.join(__dirname,'..','testing_reach_flow.js'),'utf8');
  const start=flow.indexOf('function reachStepVoiceLines(step){');
  const end=flow.indexOf('\n}',start)+2;
  assert.ok(start>=0 && end>start);
  const reach='Reach toward the bright circle.';
  const hold='Hold your hand steadily at the forward target for a moment.';
  const tasks=[{steps:[{id:'T1-S2',voice:reach},{id:'T1-S3',voice:hold}]}];
  const context=vm.createContext({tasks});
  vm.runInContext(flow.slice(start,end),context);
  assert.deepEqual(Array.from(context.reachStepVoiceLines(tasks[0].steps[0])),[reach,hold]);
  assert.deepEqual(Array.from(context.reachStepVoiceLines(tasks[0].steps[1])),[]);

  const server=fs.readFileSync(path.join(__dirname,'..','server.py'),'utf8');
  const runner=server.slice(server.indexOf('async function startStep(){'),server.indexOf('function distance(a,b){'));
  assert.ok(runner.indexOf('for(const line of reachStepVoiceLines(step))await playVoice(line);')
    < runner.indexOf('voiceFinishedAt = performance.now();'));
  assert.match(server,/Reach the target, then keep your hand there\. Do not lower it until asked\./);
});
