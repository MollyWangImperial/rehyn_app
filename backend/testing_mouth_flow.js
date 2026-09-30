// The normal T3 task supplies all steps and targets. Testing adds diagnostics.
function handToMouthTaskEnabled(){return tasks[currentTaskIdx]?.id==='T3';}
function testingMouthEnabled(){return LIBRARY_TEST_MODE && ASSIGNED_TASK_IDS.length===1 && ASSIGNED_TASK_IDS[0]==='T3';}
function mouthScreenDistance(a,b){
  if(!a || !b)return Infinity;
  const aspect=video.videoWidth/video.videoHeight||1;
  return Math.hypot((a.x-b.x)*Math.max(1,aspect),(a.y-b.y)*Math.max(1,1/aspect));
}
const mouthFlow={stopped:false,assisted:false,tail:Promise.resolve(),pending:0,epoch:0,lastDraw:0,cache:new Map(),inflight:new Map(),recovery:null,replaying:false};
const mouthMovement=new RehynTestingMouth.MovementWindow();
const mouthCompensationFlags=new RehynTestingMouth.CompensationFlags();
let mouthMovementScoring=false,mouthInteractionUntil=0;
function mouthMovementFrame(landmarks,step=getCurrentStep()){
  const side=landmarks&&sideLandmarks(landmarks,AFFECTED_SIDE);
  const aspect=video.videoWidth/video.videoHeight||1;
  const screen=p=>p&&({x:p.x*Math.max(1,aspect),y:p.y*Math.max(1,1/aspect)});
  const target=step&&getEffectiveTargetXY(step);
  return {wrist:side&&landmarkIsUsable(side.wrist,.65)?screen(side.wrist):null,
    shoulder:side&&landmarkIsUsable(side.shoulder,.65)?screen(side.shoulder):null,
    target:target&&screen(targetCanvasPoint(step,target))};
}
function beginMouthMovement(step){
  if(!testingMouthEnabled())return;
  const f=mouthMovementFrame(latestPoseLandmarks,step);
  mouthMovement.begin(step.id,f.wrist,f.shoulder,f.target);mouthMovementScoring=false;
}
function testingMouthContact(landmarks,step=getCurrentStep()){
  const target=step && getEffectiveTargetXY(step);
  const rawTarget=target && targetCanvasPoint(step,target);
  const aspect=video.videoWidth/video.videoHeight||1;
  if(!isMouthTarget(step))return RehynReachTarget.wristContact({landmarks,side:AFFECTED_SIDE,target:rawTarget,aspect});
  const points=affectedMouthContactPoints(landmarks);
  const point=rawTarget && points.length ? points.reduce((a,b)=>
    RehynReachTarget.screenDistance(a,rawTarget,aspect)<=RehynReachTarget.screenDistance(b,rawTarget,aspect)?a:b) : null;
  return {point,distance:RehynReachTarget.screenDistance(point,rawTarget,aspect)};
}
function mouthContactFrameValid(landmarks,now){
  return !handToMouthTaskEnabled() || (reachCameraFresh(now) && !!testingMouthContact(landmarks).point);
}
function updateMouthMovement(landmarks,now,frameValid){
  if(!testingMouthEnabled())return true;
  const step=getCurrentStep(),f=mouthMovementFrame(landmarks,step);
  let headApproach=false;
  if(isMouthTarget(step) && frameValid && [0,11,12].every(i=>landmarkIsUsable(landmarks?.[i],.65))) {
    const aspect=video.videoWidth/video.videoHeight||1;
    const head=assessmentQuality.postureReadout(assessmentQuality.raw(landmarks,latestPoseWorldLandmarks,aspect));
    headApproach=RehynTestingMouth.headApproachesRaisedHand({cue:head.head_drop,threshold:assessmentQuality.config.compensations.head_drop.threshold,nose:landmarks[0],
      hand:testingMouthContact(landmarks,step).point,shoulderY:(landmarks[11].y+landmarks[12].y)/2,aspect});
  }
  if(now<mouthInteractionUntil){
    mouthMovement.begin(step?.id,f.wrist,f.shoulder,f.target);
    mouthMovementScoring=false;
  }else mouthMovementScoring=mouthMovement.update({...f,now,headApproach,
    inTarget:frameValid&&checkTarget(landmarks),
    enabled:frameValid&&mouthCanMeasure()&&!stepCompleted&&now-voiceFinishedAt>=350});
  if(!mouthMovementScoring)assessmentQuality.pauseEvidence();
  return mouthMovementScoring;
}
const mouthPanel=document.getElementById('mouthDiagnostics');
const mouthVoice=new RehynVoiceGuide.VoiceGuide({audio:audioEl,fetchAudio:fetchMouthAudio,timeoutMs:60000,progressTimeoutMs:2500,onChange(state){
  document.getElementById('mouthVoiceState').textContent=state.status;
  if(state.caption)document.getElementById('mouthCaption').textContent=state.caption;
  document.getElementById('mouthReplay').disabled=state.busy;
}});
mouthVoice.enabled=VOICE_GUIDANCE_ENABLED;
document.getElementById('mouthVoiceOn').checked=VOICE_GUIDANCE_ENABLED;
async function fetchMouthAudio(text){
  if(mouthFlow.cache.has(text))return mouthFlow.cache.get(text);
  if(mouthFlow.inflight.has(text))return mouthFlow.inflight.get(text);
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),15000);
  const request=fetch(`${API_BASE}/testing/mouth/voice`,{method:'POST',signal:controller.signal,headers:{...ACCOUNT_HEADERS,'Content-Type':'application/json'},body:JSON.stringify({text})})
    .then(async response=>{if(!response.ok)throw new Error(`Molly voice unavailable (${response.status})`);const data=await response.json();if(!data.audio_b64)throw new Error('Empty voice');mouthFlow.cache.set(text,data.audio_b64);return data.audio_b64;})
    .finally(()=>{clearTimeout(timeout);mouthFlow.inflight.delete(text);});
  mouthFlow.inflight.set(text,request);return request;
}
function prefetchMouthVoice(text){if(text && mouthVoice.enabled)void fetchMouthAudio(text).catch(()=>{});}
function mouthSay(text){
  const epoch=mouthFlow.epoch;
  mouthFlow.pending++;
  const pending=mouthFlow.tail.catch(()=>false).then(async()=>{
    let failures=0;
    while(!mouthFlow.stopped && epoch===mouthFlow.epoch){
      const ok=await mouthVoice.speak(text);
      if(ok)return !mouthFlow.stopped && epoch===mouthFlow.epoch;
      if(mouthFlow.stopped || epoch!==mouthFlow.epoch)return false;
      // Turning voice off is an explicit choice to read this same instruction.
      if(!mouthVoice.enabled)continue;
      // An unexpected cancellation is not instruction completion. Replay the
      // same cue; only an explicit exit may discard the queued instruction.
      // Retry a transient failure once. Never silently disable every later cue.
      if(++failures<2)continue;
      document.getElementById('mouthVoiceState').textContent='Voice paused. Tap Resume Molly voice to hear this instruction, or turn voice off for captions.';
      const replay=document.getElementById('mouthReplay');
      replay.textContent='Resume Molly voice';replay.disabled=false;
      const resume=await new Promise(resolve=>{mouthFlow.recovery=resolve;});
      mouthFlow.recovery=null;
      replay.textContent='Replay instruction';
      if(!resume)return false;
      failures=0;
    }
    return false;
  }).finally(()=>{mouthFlow.pending--;});mouthFlow.tail=pending.catch(()=>false);return pending;
}
function mouthStepLines(step){return step.id==='T3-S2'?[step.voice,MOUTH_TEST_VOICE.steps[2]]:step.id==='T3-S3'?[]:[step.voice];}
function stopMouthVoice(){mouthFlow.stopped=true;mouthFlow.epoch++;mouthVoice.cancel();mouthFlow.recovery?.(false);}
function mouthCanMeasure(){return !testingMouthEnabled() || (!calibratingAssessment && voiceFinishedAt>0 && !mouthFlow.stopped && mouthFlow.pending===0 && !mouthFlow.recovery && !mouthFlow.replaying && !mouthVoice.busy && !mouthVoice.failed && !correctionVoicePlaying);}
document.getElementById('mouthVoiceOn').onchange=event=>{
  mouthVoice.enabled=event.target.checked;
  if(!mouthVoice.enabled){mouthVoice.cancel();mouthFlow.recovery?.(true);}
  else void replayMouthVoice();
};
async function replayMouthVoice(){
  if(mouthFlow.stopped || mouthVoice.busy || mouthFlow.replaying)return;
  mouthFlow.replaying=true;
  mouthVoice.enabled=true;document.getElementById('mouthVoiceOn').checked=true;
  // Unlock directly from the click for browsers that block automatic playback.
  audioUnlockPromise=null;
  try{
    await unlockAudioPlayback();
    if(mouthFlow.stopped)return;
    if(mouthFlow.recovery)mouthFlow.recovery(true);
    else if(mouthVoice.caption)await mouthSay(mouthVoice.caption);
  }finally{mouthFlow.replaying=false;}
}
document.getElementById('mouthReplay').onclick=replayMouthVoice;
document.getElementById('mouthAssisted').onchange=event=>{
  // Assistance is diagnostic context; the Testing total uses the 100/15 rule.
  // Do not erase recorded assistance by unticking the control on a later step.
  if(event.target.checked){mouthFlow.assisted=true;event.target.disabled=true;}
};
document.getElementById('mouthSkip').onclick=()=>document.getElementById('skipBtn').click();
if(testingMouthEnabled()){
  document.body.classList.add('testing-mouth');mouthPanel.classList.remove('hidden');
  // Keep navigation outside the camera, where it cannot cover the mouth target.
  mouthPanel.prepend(document.getElementById('top'));
  MOUTH_TEST_VOICE.lines.forEach(prefetchMouthVoice);
  requestAnimationFrame(syncCameraViewport);
  const pauseForControl=event=>{
    if(!event.target.closest?.('button,input,a,select,textarea,[role="button"]'))return;
    mouthInteractionUntil=performance.now()+750;mouthMovementScoring=false;
    assessmentQuality.pauseEvidence();inTargetSince=null;lastInTargetTs=0;
  };
  document.addEventListener('pointerdown',pauseForControl,true);
  document.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key))pauseForControl(event);},true);
}
function drawTestingMouth(landmarks,world){
  if(!testingMouthEnabled())return;
  // Capture every sampled frame before the next step can reset its evidence.
  // Reading the small compensation records does not copy the angle time series.
  const flags=mouthCompensationFlags.update(assessmentQuality,{scoring:mouthMovementScoring&&mouthCanMeasure(),fresh:!!landmarks});
  const now=performance.now(),aspect=video.videoWidth/video.videoHeight||1;
  const readout=RehynReachAngles.assessmentReadout(assessmentQuality,landmarks,world,aspect);
  readout.elbowArc=RehynTestingMouth.flexionArc(landmarks,AFFECTED_SIDE,aspect,RehynReachAngles.jointArc);
  ctx.save();ctx.translate(canvas.width,0);ctx.scale(-1,1);
  RehynReachAngles.drawAngleArcs(ctx,readout,canvas.width,canvas.height,canvas.clientWidth);ctx.restore();
  if(now-mouthFlow.lastDraw<100)return;mouthFlow.lastDraw=now;
  const step=getCurrentStep(),rubric=assessmentQuality.rubric||assessmentQuality.config.tasks.T3.steps[0];
  const live=assessmentQuality.postureReadout(assessmentQuality.raw(landmarks,world,aspect));
  const trunk=assessmentQuality.trunkLeanReadout(landmarks,aspect);
  if(trunk.supported)live.trunk_lean=trunk.degrees;
  const cueState=(value,threshold,run)=>!assessmentQuality.baseline?'Waiting for upright calibration':!Number.isFinite(value)?'Not measurable now':(run?.max_streak_ms||0)>=500?`Confirmed this step · ${(run.max_streak_ms/1000).toFixed(2)} s`:value>threshold?`Above threshold · ${((run?.streak||0)/1000).toFixed(2)} / 0.5 s`:'Below threshold';
  for(const [name,value,id] of [['Shoulder',trunk.trunkGrowth,'trunk_lean'],['Face',trunk.headGrowth,'head_drop']]) {
    document.getElementById('mouth'+name+'Cue').textContent=Number.isFinite(value)?`${value.toFixed(1)}%`:'—';
    document.getElementById('mouth'+name+'State').textContent=cueState(value,assessmentQuality.config.compensations[id].threshold,assessmentQuality.compensations[id]);
  }
  const ratio=value=>Number.isFinite(value)?`${value.toFixed(4)}×`:'—';
  document.getElementById('mouthShoulderScale').textContent=ratio(trunk.shoulderScale);
  document.getElementById('mouthFaceScale').textContent=ratio(trunk.faceScale);
  document.getElementById('mouthRelativeFaceScale').textContent=ratio(trunk.relativeFaceScale);
  document.getElementById('mouthTrunkState').textContent=!mouthMovementScoring?'Live preview · posture is not being scored':'Shoulder growth checks trunk; additional face growth checks head.';
  if(landmarks && live.torso)live.target_control=!calibratingAssessment&&checkTarget(landmarks)?1:0;
  const view=RehynTestingMouth.diagnostic(assessmentQuality,!!landmarks,live,rubric);
  const warning=document.getElementById('mouthCompensationAlert');
  warning.hidden=flags.detected.length===0;
  if(flags.detected.length){
    const title=flags.active.length?`${flags.activeLabels.join(' + ')} detected now`:`Detected in this test: ${flags.labels.join(' + ')}`;
    const detail=`Whole-test score: 15 / 100. Recorded: ${flags.labels.join(', ')}.`;
    if(document.getElementById('mouthCompensationTitle').textContent!==title)document.getElementById('mouthCompensationTitle').textContent=title;
    if(document.getElementById('mouthCompensationScore').textContent!==detail)document.getElementById('mouthCompensationScore').textContent=detail;
  }
  const fmt=(v,unit='deg')=>!Number.isFinite(v)?'Not measurable':`${(unit==='ratio'?v*100:v).toFixed(1)}${unit==='ratio'?'%':unit==='deg'?'°':unit==='percent'?'%':''}`;
  document.getElementById('mouthStep').textContent=calibratingAssessment?'Finding your seated position':`Step ${currentStepIdx+1} of 4 · ${step?.caption||'Complete'}`;
  const hold=step && inTargetSince!==null?Math.min(step.hold_ms,now-inTargetSince):0;
  document.getElementById('mouthHold').textContent=calibratingAssessment?'Calibration in progress':stepCompleted?'Target complete · moving to the next step':mouthVoice.busy?'Listen to the instruction':!landmarks?'Tracking unavailable · hold paused':!mouthMovementScoring?'Waiting for your reach · posture is not being scored':`Scoring this reach and hold · ${(hold/1000).toFixed(1)} / ${((step?.hold_ms||0)/1000).toFixed(1)} s at target`;
  document.getElementById('mouthHandState').textContent=isMouthTarget(step)?latestHandLandmarks && now-latestHandSeenAt<=handLandmarkFreshMs()?'Hand tracking: fingertips and palm':'Hand tracking: using visible pose hand points while fine tracking is unavailable':'Target contact: affected wrist';
  document.getElementById('mouthSkip').disabled=!mouthCanMeasure() || stepCompleted;
  function rows(id,items){
    const container=document.getElementById(id),shape=JSON.stringify(items.map(item=>item.map(([tag])=>tag)));
    if(container.dataset.shape!==shape){
      container.replaceChildren();container.dataset.shape=shape;
      for(const item of items){const div=document.createElement('div');div.className='mouthMetric';for(const [tag] of item)div.append(document.createElement(tag));container.append(div);}
    }
    items.forEach((item,i)=>item.forEach(([,text],j)=>{const node=container.children[i].children[j];if(node.textContent!==text)node.textContent=text;}));
  }
  const completionOnly=rubric.scoring_method==='target_completion';
  rows('mouthMetrics',completionOnly?[[['b','Return to lap'],['strong','Complete the lap target hold'],['small','Posture checks still count toward the whole-test 100/15 score.']]]:view.criteria.map(rule=>[['b',rule.label],['strong',fmt(rule.current,rule.unit)],['small',`Diagnostic reference ${fmt(rule.target,rule.unit)} · no angle point deduction`],['small',`${rule.statistic_source==='sample_proportion'?'All-sample target proportion':rule.statistic_source==='target_median'?'Median at target':'Movement median (fallback)'}: ${fmt(rule.value,rule.unit)} · ${rule.statistic_samples} samples`]]));
  rows('mouthAngles',[[['b','Arm elevation'],['strong',fmt(readout.armScore)],['small',`Model 3D estimate · cyan 2D arc: ${fmt(readout.armElevationArc?.degrees)}`]],
    [['b','Elbow bend'],['strong',fmt(live.elbow_flexion)],['small',`Yellow 2D arc · internal extension: ${fmt(readout.elbowScore)}`]]]);
  rows('mouthCompensations',view.compensations.filter(check=>check.id!=='trunk_lean').map(check=>[['b',check.id==='head_drop'?'Head forward (face growth)':check.id==='shoulder_hike'?'Shoulder hiking (shoulder rise)':check.label],['strong',fmt(check.current,check.unit)],['small',`Above ${fmt(check.threshold,check.unit)} for 0.5 s`],['small',`${check.status} · current ${(check.duration/1000).toFixed(2)} s · longest ${(check.longest/1000).toFixed(2)} s`],...(check.id==='head_drop'?[['small','Face growth beyond shoulder-span growth']]:[]) ]));
}
