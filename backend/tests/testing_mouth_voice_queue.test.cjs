const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../testing_mouth_flow.js'),'utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function setup(guide){
  const nodes=new Map();
  const node=id=>{if(!nodes.has(id))nodes.set(id,{checked:true,disabled:false,textContent:''});return nodes.get(id)};
  const context=vm.createContext({LIBRARY_TEST_MODE:true,ASSIGNED_TASK_IDS:['T3'],VOICE_GUIDANCE_ENABLED:true,
    calibratingAssessment:false,voiceFinishedAt:100,RehynTestingMouth:require('../testing_mouth.js'),
    document:{getElementById:node},audioEl:{},correctionVoicePlaying:false,audioUnlockPromise:null,
    unlockAudioPlayback:async()=>true,RehynVoiceGuide:{VoiceGuide:function(){return guide;}}});
  vm.runInContext(source.slice(0,source.indexOf("document.getElementById('mouthAssisted')"))+'\nglobalThis.flow=mouthFlow;',context);
  return {context,node};
}
function guide(failCount){return {enabled:true,failed:false,busy:false,caption:'',calls:[],cancel(){this.failed=false;},async speak(text){
  this.caption=text;this.calls.push({text,enabled:this.enabled});
  this.failed=this.enabled&&failCount-->0;return !this.failed;
}};}
test('one failed cue retries the same text and keeps all later Molly instructions enabled',async()=>{
  const g=guide(1),{context,node}=setup(g);
  assert.equal(await context.mouthSay('first'),true);
  assert.equal(await context.mouthSay('second'),true);
  assert.deepEqual(g.calls.map(c=>c.text),['first','first','second']);
  assert.ok(g.calls.every(c=>c.enabled));assert.equal(node('mouthVoiceOn').checked,true);
});
test('repeated failure waits for a gesture, blocks movement and preserves queued order',async()=>{
  const g=guide(2),{context,node}=setup(g);
  let finished=false;const first=context.mouthSay('first').then(ok=>{finished=true;return ok});
  const second=context.mouthSay('second');await tick();
  assert.equal(finished,false);assert.equal(context.mouthCanMeasure(),false);
  assert.equal(node('mouthReplay').textContent,'Resume Molly voice');assert.equal(g.enabled,true);
  assert.deepEqual(g.calls.map(c=>c.text),['first','first']);
  await node('mouthReplay').onclick();assert.equal(await first,true);assert.equal(await second,true);
  assert.deepEqual(g.calls.map(c=>c.text),['first','first','first','second']);
  assert.equal(context.mouthCanMeasure(),true);
});
test('captions require an explicit choice and finish the interrupted instruction',async()=>{
  const g=guide(10),{context,node}=setup(g),first=context.mouthSay('first');await tick();
  node('mouthVoiceOn').onchange({target:{checked:false}});assert.equal(await first,true);
  assert.deepEqual(g.calls.at(-1),{text:'first',enabled:false});
});
test('exiting while recovery waits cancels this cue and all queued instructions',async()=>{
  const g=guide(10),{context}=setup(g),first=context.mouthSay('first'),second=context.mouthSay('second');
  await tick();context.stopMouthVoice();assert.equal(await first,false);assert.equal(await second,false);
  assert.equal(g.calls.length,2);
});
test('a queued cue blocks target timing before playback starts; only finished step instructions unlock it',async()=>{
  const g=guide(0),{context}=setup(g);
  const cue=context.mouthSay('first');assert.equal(context.mouthCanMeasure(),false);
  await cue;assert.equal(context.mouthCanMeasure(),true);
  context.voiceFinishedAt=0;assert.equal(context.mouthCanMeasure(),false);
  context.voiceFinishedAt=100;context.calibratingAssessment=true;assert.equal(context.mouthCanMeasure(),false);
});
test('an unexpected cancellation retries instead of unlocking the target with an unfinished instruction',async()=>{
  const g=guide(0),normal=g.speak.bind(g);let first=true;
  g.speak=async text=>{if(first){first=false;g.calls.push({text,enabled:true});return false;}return normal(text);};
  const {context}=setup(g);assert.equal(await context.mouthSay('complete sentence'),true);
  assert.equal(g.calls.length,2);assert.ok(g.calls.every(c=>c.text==='complete sentence'));
});
