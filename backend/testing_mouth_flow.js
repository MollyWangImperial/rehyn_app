// Injected into the assessment module. No separate target or scoring state.
function testingMouthEnabled(){return LIBRARY_TEST_MODE && ASSIGNED_TASK_IDS.length===1 && ASSIGNED_TASK_IDS[0]==='T3';}
function mouthScreenDistance(a,b){
  if(!a || !b)return Infinity;
  const aspect=video.videoWidth/video.videoHeight||1;
  return Math.hypot((a.x-b.x)*Math.max(1,aspect),(a.y-b.y)*Math.max(1,1/aspect));
}
const mouthFlow={stopped:false,assisted:false,tail:Promise.resolve(),epoch:0,lastDraw:0,cache:new Map(),inflight:new Map()};
const mouthPanel=document.getElementById('mouthDiagnostics');
const mouthVoice=new RehynVoiceGuide.VoiceGuide({audio:audioEl,fetchAudio:fetchMouthAudio,timeoutMs:60000,onChange(state){
  document.getElementById('mouthVoiceState').textContent=state.status;
  if(state.caption)document.getElementById('mouthCaption').textContent=state.caption;
}});
mouthVoice.enabled=VOICE_GUIDANCE_ENABLED;
document.getElementById('mouthVoiceOn').checked=VOICE_GUIDANCE_ENABLED;
async function fetchMouthAudio(text){
  if(mouthFlow.cache.has(text))return mouthFlow.cache.get(text);
  if(mouthFlow.inflight.has(text))return mouthFlow.inflight.get(text);
  const request=fetch(`${API_BASE}/testing/mouth/voice`,{method:'POST',headers:{...ACCOUNT_HEADERS,'Content-Type':'application/json'},body:JSON.stringify({text})})
    .then(async response=>{if(!response.ok)throw new Error(`Molly voice unavailable (${response.status})`);const data=await response.json();if(!data.audio_b64)throw new Error('Empty voice');mouthFlow.cache.set(text,data.audio_b64);return data.audio_b64;})
    .finally(()=>mouthFlow.inflight.delete(text));
  mouthFlow.inflight.set(text,request);return request;
}
function prefetchMouthVoice(text){if(text && mouthVoice.enabled)void fetchMouthAudio(text).catch(()=>{});}
function mouthSay(text){
  const epoch=mouthFlow.epoch;
  const pending=mouthFlow.tail.catch(()=>false).then(async()=>{
    if(mouthFlow.stopped || epoch!==mouthFlow.epoch)return false;
    const ok=await mouthVoice.speak(text);
    if(ok)return !mouthFlow.stopped && epoch===mouthFlow.epoch;
    if(mouthFlow.stopped || epoch!==mouthFlow.epoch)return false;
    if(mouthVoice.failed){mouthVoice.enabled=false;document.getElementById('mouthVoiceOn').checked=false;
      document.getElementById('mouthVoiceState').textContent='Molly voice unavailable · follow captions';
      return mouthVoice.speak(text);}
    return false;
  });mouthFlow.tail=pending.catch(()=>false);return pending;
}
function mouthStepLines(step){return step.id==='T3-S2'?[step.voice,MOUTH_TEST_VOICE.steps[2]]:step.id==='T3-S3'?[]:[step.voice];}
function stopMouthVoice(){mouthFlow.stopped=true;mouthFlow.epoch++;mouthVoice.cancel();}
function mouthCanMeasure(){return !testingMouthEnabled() || (!mouthFlow.stopped && !mouthVoice.busy && !mouthVoice.failed && !correctionVoicePlaying);}
document.getElementById('mouthVoiceOn').onchange=event=>{mouthVoice.enabled=event.target.checked;};
document.getElementById('mouthAssisted').onchange=event=>{
  // Assistance applies to the entire T3 task in the existing rubric. Once
  // recorded it cannot be erased by unticking the control on a later step.
  if(event.target.checked){mouthFlow.assisted=true;event.target.disabled=true;}
};
document.getElementById('mouthSkip').onclick=()=>document.getElementById('skipBtn').click();
if(testingMouthEnabled()){
  document.body.classList.add('testing-mouth');mouthPanel.classList.remove('hidden');
  // Keep navigation outside the camera, where it cannot cover the mouth target.
  mouthPanel.prepend(document.getElementById('top'));
  MOUTH_TEST_VOICE.lines.forEach(prefetchMouthVoice);
  requestAnimationFrame(syncCameraViewport);
}
function drawTestingMouth(landmarks,world){
  if(!testingMouthEnabled())return;
  const now=performance.now(),aspect=video.videoWidth/video.videoHeight||1;
  const readout=RehynReachAngles.assessmentReadout(assessmentQuality,landmarks,world,aspect);
  readout.elbowArc=RehynTestingMouth.flexionArc(landmarks,AFFECTED_SIDE,aspect,RehynReachAngles.jointArc);
  ctx.save();ctx.translate(canvas.width,0);ctx.scale(-1,1);
  RehynReachAngles.drawAngleArcs(ctx,readout,canvas.width,canvas.height,canvas.clientWidth);ctx.restore();
  if(now-mouthFlow.lastDraw<100)return;mouthFlow.lastDraw=now;
  const view=RehynTestingMouth.diagnostic(assessmentQuality,!!landmarks && mouthCanMeasure()),step=getCurrentStep();
  const fmt=(v,unit='deg')=>!Number.isFinite(v)?'Not measurable':`${(unit==='ratio'?v*100:v).toFixed(1)}${unit==='ratio'?'%':unit==='deg'?'°':' proxy units'}`;
  document.getElementById('mouthStep').textContent=calibratingAssessment?'Finding your seated position':`Step ${currentStepIdx+1} of 4 · ${step?.caption||'Complete'}`;
  const hold=step && inTargetSince!==null?Math.min(step.hold_ms,now-inTargetSince):0;
  document.getElementById('mouthHold').textContent=calibratingAssessment?'Calibration in progress':mouthVoice.busy?'Listen to the instruction':!landmarks?'Tracking unavailable · hold paused':`Target hold: ${(hold/1000).toFixed(1)} / ${((step?.hold_ms||0)/1000).toFixed(1)} s`;
  function rows(id,items){const container=document.getElementById(id);container.replaceChildren();for(const item of items){const div=document.createElement('div');div.className='mouthMetric';for(const [tag,text] of item){const node=document.createElement(tag);node.textContent=text;div.append(node);}container.append(div);}}
  rows('mouthMetrics',view.criteria.map(rule=>[['b',rule.label],['strong',fmt(rule.current,rule.unit)],['small',`Reference ${fmt(rule.target,rule.unit)}`],['small',`${rule.statistic_source==='sample_proportion'?'All-sample target proportion':rule.statistic_source==='target_median'?'Median at target':'Movement median (fallback)'}: ${fmt(rule.value,rule.unit)} · ${rule.statistic_samples} samples`]]));
  rows('mouthAngles',[[['b','Angle geometry'],['small',`Elbow bend: ${fmt(readout.elbowArc?.degrees)} · internal extension: ${fmt(readout.elbowScore)}`],['small',`Arm: ${fmt(readout.armScore)} model 3D · cyan arc: ${fmt(readout.armElevationArc?.degrees)} image 2D`]]]);
  rows('mouthCompensations',view.compensations.map(check=>[['b',check.label],['strong',fmt(check.current,check.unit)],['small',`Above ${fmt(check.threshold,check.unit)} for 0.5 s`],['small',`${check.status} · current ${(check.duration/1000).toFixed(2)} s · longest ${(check.longest/1000).toFixed(2)} s`]]));
}
