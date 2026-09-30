// Runs inside the assessment module, so targets, holds and scoring use one state.
const reachFlow={support:null,assisted:false,step:null,
  policy:new RehynTestingReach.LoweringPolicy(),raisedLevel:0,lastVideo:null,lastVideoAt:0,waiting:false,
  stopped:false,recovery:null,replaying:false};
const reachPanel=document.getElementById('reachSupportPanel');
const reachCaption=document.getElementById('reachGuidance');
const reachPolicyKey=`rehyn-testing-reach-policy-1:${CURRENT_USER_ID}:${AFFECTED_SIDE}`;
try{reachFlow.policy.restore(JSON.parse(sessionStorage.getItem(reachPolicyKey)||'null'));}catch{}
function saveReachPolicy(){try{sessionStorage.setItem(reachPolicyKey,JSON.stringify(reachFlow.policy.snapshot()));}catch{}}
function testingReachEnabled(){return LIBRARY_TEST_MODE && tasks.length===1 && tasks[0]?.id==='T1';}
function seatedTestingCalibrationEnabled(){return testingReachEnabled() || testingMouthEnabled();}
const reachCameraGate=new RehynReachCameraQuality.Gate();
const reachCameraCanvas=document.createElement('canvas');
const reachCameraContext=reachCameraCanvas.getContext('2d',{willReadFrequently:true});
let reachCameraImageAt=-Infinity,reachCameraVideoTime=null,reachCameraImage=null,reachCameraFrame=null;
let reachCameraMeasuredAt=-Infinity;
function resetReachCameraQuality(){
  reachCameraGate.reset();reachCameraImageAt=-Infinity;reachCameraVideoTime=null;
  reachCameraImage=null;reachCameraFrame=null;reachCameraMeasuredAt=-Infinity;
}
function reachCameraStatus(){
  if(!seatedTestingCalibrationEnabled())return {ready:true};
  const now=performance.now();
  if(calibratingAssessment)return reachCameraGate.status(now);
  return RehynReachCameraQuality.runtimeStatus({now,measuredAt:reachCameraMeasuredAt,image:reachCameraImage,frame:reachCameraFrame});
}
function updateReachCameraQuality(result,lm){
  if(!seatedTestingCalibrationEnabled())return;
  const now=performance.now();
  // A frozen camera frame cannot count repeatedly toward a clear two seconds.
  if(video.currentTime===reachCameraVideoTime)return;
  reachCameraVideoTime=video.currentTime;reachCameraMeasuredAt=now;
  const aspect=video.videoWidth/video.videoHeight;
  const metrics=assessmentQuality.raw(lm,latestPoseWorldLandmarks,aspect);
  reachCameraFrame=RehynReachCameraQuality.gradingFrame(lm,latestPoseWorldLandmarks,metrics,AFFECTED_SIDE,testingMouthEnabled());
  if(now-reachCameraImageAt>=200){
    reachCameraImageAt=now;
    try{
      reachCameraCanvas.width=320;reachCameraCanvas.height=Math.round(320/aspect);
      reachCameraContext.drawImage(video,0,0,reachCameraCanvas.width,reachCameraCanvas.height);
      const mask=result?.segmentationMasks?.[0];
      reachCameraImage=RehynReachCameraQuality.inspectImage(
        reachCameraContext.getImageData(0,0,reachCameraCanvas.width,reachCameraCanvas.height),lm,
        mask?{data:mask.getAsFloat32Array(),width:mask.width,height:mask.height}:null);
    }catch{reachCameraImage={issue:'pixels'};}
  }
  const baseline=assessmentQuality.baseline;
  const baselineReady=(!testingReachEnabled() || !!assessmentQuality.trunkLeanBaseline) && baseline
    && ['width','screenWidth','shoulderLine','neckGap'].every(k=>Number.isFinite(baseline[k]));
  if(calibratingAssessment){
    const status=reachCameraGate.update({now,image:reachCameraImage,
      frame:{...reachCameraFrame,calibration:{pose:lm,world:latestPoseWorldLandmarks,aspect}},baselineReady:!!baselineReady});
    if(!status.clearReady || !lapTargetCalibration.ready){
      assessmentQuality.baselines=[];assessmentQuality.baseline=null;
      assessmentQuality.trunkLeanBaselineFrames=[];assessmentQuality.trunkLeanBaseline=null;
      reachCameraGate.baselineReady=false;
      reachCameraGate.baselineDetail=lapTargetCalibration.ready?'Waiting for the two-second camera-quality window.':'Waiting for the hand to settle at the lap point.';
    }else{
      const lap=lapTargetCalibration.target,wi=AFFECTED_SIDE==='left'?15:16;
      const samples=reachCameraGate.frames.filter(f=>{
        const wrist=f.calibration?.pose?.[wi];
        return wrist && Math.hypot(wrist.x-lap.x,wrist.y-lap.y)<=.04;
      }).map(f=>({now:f.now,...f.calibration}));
      // Same trunk-lean formulas, collected alongside the lap hold rather
      // than requiring another 45 frames followed by a second timer.
      if(!assessmentQuality.calibrateSeatedTestingWindow(samples)){
        assessmentQuality.baseline=null;assessmentQuality.trunkLeanBaseline=null;
        reachCameraGate.ready=false;reachCameraGate.issue='baseline';
        reachCameraGate.baselineReady=false;
        reachCameraGate.baselineDetail=assessmentQuality.testingReachCalibrationIssue;
        reachCameraGate.detail=assessmentQuality.testingReachCalibrationIssue;
      }else{
        reachCameraGate.baselineDetail='';
      }
    }
  }
  if(testingReachEnabled())syncReachCameraQuality();
}
function syncReachCalibrationChecks(status){
  for(const row of calibrationQuality.querySelectorAll('[data-quality-check]')){
    const result=status.checks?.[row.dataset.qualityCheck];
    const state=result?.ready===true?'ready':result?.ready===false?'blocked':'waiting';
    row.dataset.state=state;
    row.classList.toggle('done',state==='ready');
    row.querySelector('.statusDot').textContent=state==='ready'?'✓':row.dataset.checkNumber;
    row.querySelector('.qualityCheckState').textContent=state==='ready'?'Passed':state==='blocked'?'Needs attention':'Waiting';
    row.querySelector('.qualityCheckDetail').textContent=result?.detail||'Waiting for a camera measurement.';
  }
}
function syncReachCameraQuality(){
  const status=reachCameraStatus(),element=document.getElementById('reachCameraQuality');
  const message=status.advisory&&status.ready
    ? 'Your clothing and background look similar, but tracking is clear. The target remains active.'
    : status.ready?'':status.message+(!calibratingAssessment
      ? ' Angle grading is waiting for clear tracking. A clearly tracked wrist still counts anywhere inside the circle.' : '');
  if(element.textContent!==message)element.textContent=message;
  element.classList.toggle('hidden',calibratingAssessment || !message);
}
function reachCameraCalibrationFrameUsable(){
  return !testingReachEnabled() || (performance.now()-reachCameraMeasuredAt<=450
    && reachCameraImage && !reachCameraImage.issue && reachCameraFrame && !reachCameraFrame.issue);
}
function reachStepVoiceLines(step){
  if(step.id==='T1-S2'){
    // Give the hold cue before the raised target unlocks so the patient does
    // not lower their hand between the reach and hold steps. Both lines are
    // already available as private Molly clips.
    const holdVoice=tasks[0]?.steps?.find(item=>item.id==='T1-S3')?.voice;
    return [step.voice,holdVoice].filter(Boolean);
  }
  if(step.id==='T1-S3')return [];
  return [step.voice].filter(Boolean);
}
function syncReachCaption(){
  // The calibration card already contains the instruction. Keep the camera
  // clear when speech works; show text in the controls if speech is off.
  reachCaption.classList.toggle('hidden',(reachVoice.enabled && !reachVoice.failed && !reachFlow.recovery) || calibratingAssessment || !reachCaption.textContent);
}
const reachMollyCache=new Map(),reachMollyInflight=new Map();
async function fetchReachMollyAudio(text){
  if(reachMollyCache.has(text))return reachMollyCache.get(text);
  if(reachMollyInflight.has(text))return reachMollyInflight.get(text);
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),15000);
  const request=fetch(`${API_BASE}/testing/reach/voice`,{
    method:'POST',signal:controller.signal,headers:{'Content-Type':'application/json',...ACCOUNT_HEADERS},
    body:JSON.stringify({text}),
  }).then(async response=>{
    if(!response.ok)throw new Error(`Molly voice unavailable (${response.status})`);
    const data=await response.json();
    if(!data.audio_b64)throw new Error('Molly voice audio is empty');
    reachMollyCache.set(text,data.audio_b64);
    return data.audio_b64;
  }).finally(()=>{clearTimeout(timeout);reachMollyInflight.delete(text);});
  reachMollyInflight.set(text,request);
  return request;
}
function prefetchReachMollyAudio(text){
  if(text && reachVoice.enabled)void fetchReachMollyAudio(text).catch(()=>{});
}
const reachVoice=new RehynVoiceGuide.VoiceGuide({fetchAudio:fetchReachMollyAudio,audio:audioEl,timeoutMs:60000,onChange(v){
  document.getElementById('reachVoiceState').textContent=v.status;
  if(v.caption)reachCaption.textContent=v.caption;
  syncReachCaption();
}});
reachVoice.enabled=VOICE_GUIDANCE_ENABLED;
document.getElementById('reachVoiceOn').checked=VOICE_GUIDANCE_ENABLED;
let reachSpeechTail=Promise.resolve(),reachSpeechEpoch=0;
function reachSay(text){
  const epoch=reachSpeechEpoch;
  // VoiceGuide.speak replaces the current audio. Keep prompts in order so a
  // coaching or support cue cannot cut off an unfinished step instruction.
  const current=reachSpeechTail.catch(()=>false).then(async()=>{
    let failures=0;
    while(!reachFlow.stopped && epoch===reachSpeechEpoch){
      const ok=await reachVoice.speak(text);
      if(ok)return !reachFlow.stopped && epoch===reachSpeechEpoch;
      if(reachFlow.stopped || epoch!==reachSpeechEpoch)return false;
      if(!reachVoice.enabled)continue; // Explicit choice: read this same cue.
      if(!reachVoice.failed)return false;
      // One playback/network failure must not disable all remaining cues.
      if(++failures<2)continue;
      document.getElementById('reachVoiceState').textContent='Voice paused. Tap Resume Molly voice to hear this instruction, or turn voice off for captions.';
      const replay=document.getElementById('reachReplay');
      replay.classList.remove('hidden');replay.disabled=false;
      const resume=await new Promise(resolve=>{reachFlow.recovery=resolve;});
      reachFlow.recovery=null;replay.classList.add('hidden');
      if(!resume)return false;
      failures=0;
    }
    return false;
  });
  reachSpeechTail=current.catch(()=>false);
  return current;
}
document.getElementById('reachVoiceOn').onchange=event=>{
  reachVoice.enabled=event.target.checked;
  if(!reachVoice.enabled){reachVoice.cancel();reachFlow.recovery?.(true);}
  else void resumeReachVoice();
  syncReachCaption();
};
async function resumeReachVoice(){
  if(reachFlow.stopped || reachVoice.busy || reachFlow.replaying)return;
  reachFlow.replaying=true;reachVoice.enabled=true;
  document.getElementById('reachVoiceOn').checked=true;
  document.getElementById('reachReplay').disabled=true;
  // Invoke the audio unlock directly from the click when a browser needs it.
  audioUnlockPromise=null;
  try{
    await unlockAudioPlayback();
    if(reachFlow.stopped)return;
    if(reachFlow.recovery)reachFlow.recovery(true);
    else if(reachVoice.caption)await reachSay(reachVoice.caption);
  }finally{reachFlow.replaying=false;document.getElementById('reachReplay').disabled=false;}
}
document.getElementById('reachReplay').onclick=resumeReachVoice;
function reachChoice(title,copy,choices){
  reachFlow.waiting=true;reachPanel.classList.remove('hidden');
  document.getElementById('reachPause').disabled=true;
  document.getElementById('reachAssistance').disabled=true;
  document.getElementById('reachSupportTitle').textContent=title;
  document.getElementById('reachSupportCopy').textContent=copy;
  const actions=document.getElementById('reachSupportActions');actions.replaceChildren();
  choices.forEach(([label,action])=>{
    const button=document.createElement('button');button.type='button';button.textContent=label;
    button.onclick=()=>{actions.querySelectorAll('button').forEach(b=>b.disabled=true);reachFlow.waiting=false;reachPanel.classList.add('hidden');document.getElementById('reachPause').disabled=false;document.getElementById('reachAssistance').disabled=false;void action();};actions.append(button);
  });
}
function askReachSupport(){
  // Testing has its own caption strip. The generic assessment footer repeats
  // "Single task test / Preparing…" and can cover the low lap target.
  document.getElementById('bottom').classList.add('hidden');
  document.getElementById('reachControls').classList.remove('hidden');
  syncReachCaption();
  prefetchReachMollyAudio(TESTING_REACH_CALIBRATION_INSTRUCTION);
  prefetchReachMollyAudio(CALIBRATION_COMPLETE_INSTRUCTION);
  prefetchReachMollyAudio(tasks[0]?.steps?.[0]?.voice);
  reachChoice('Is someone beside you?',
    'Is a carer or family member here who could help if needed? We will ask before any assisted movement. Your camera will start after you answer.'+(localReview.enabled?' This local test saves a review video on this computer.':''),[
      ['Yes, someone is here',()=>{reachFlow.support=true;void beginAssessmentSetup();}],
      ['No, I am on my own',()=>{reachFlow.support=false;void beginAssessmentSetup();}],
    ]);
}
function reachCameraFresh(now){
  const sourceTime=video.currentTime;
  if(sourceTime!==reachFlow.lastVideo){reachFlow.lastVideo=sourceTime;reachFlow.lastVideoAt=now;}
  return video.readyState>=2 && now-reachFlow.lastVideoAt<=250 && now-lastPoseScanTs<=250;
}
function reachContactFrameValid(lm,now){
  return !testingReachEnabled() || (reachCameraFresh(now) && !!testingReachWristContact(lm).point);
}
function reachObservation(lm,now){
  if(!reachCameraFresh(now))return null;
  return RehynTestingReach.observation(lm,AFFECTED_SIDE,video.videoWidth/video.videoHeight);
}
function showReachHelp(){
  const copy='The target has not been completed after another comfortable attempt. You can rest, finish, or use help if someone is here.';
  const choices=[];
  if(reachFlow.support)choices.push(['Continue with assistance',async()=>{
    reachFlow.assisted=true;if(reachFlow.step)reachFlow.step.assisted=true;
    const ok=await reachSay('Ask the person beside you to help only in the way your therapist has shown you. Do not pull or force the arm. This attempt will be marked as assisted. Stop if it hurts.');
    if(!ok)return;
    if(reachFlow.step)reachFlow.step.retry();
  }]);
  choices.push(['Try again independently',async()=>{
    reachFlow.step.retry();
  }]);
  choices.push(['Finish test for now',()=>endReachTest()]);
  reachChoice(reachFlow.support?'Help is available':'Pause and get support',
    copy+(reachFlow.support?' Assistance must be confirmed; the camera cannot identify it reliably.':' If you need hands-on help, pause until a carer or family member is available.'),choices);
  void reachSay(reachFlow.support
    ? 'I have not detected a completed movement. If needed, ask the person beside you to help in the way your therapist has shown you. Choose assisted practice, try again, or finish for now.'
    : 'Let us pause. I have not detected a completed movement. If you need help, wait until a carer or family member is with you. You can try again or finish for now.');
}
function beginReachStep(step){
  if(!testingReachEnabled())return;
  reachStepVoiceLines(step).forEach(prefetchReachMollyAudio);
  prefetchUpcomingVoice();
  const base=step.id==='T1-S1'?forwardReachPlacement.start:step.id==='T1-S4'?assessmentLapTarget:forwardReachPlacement.raised;
  reachFlow.step=new RehynTestingReach.ReachStep({policy:reachFlow.policy,id:step.id,base,lap:mirrorX(assessmentLapTarget),
    level:step.id==='T1-S3'?reachFlow.raisedLevel:0,assisted:reachFlow.assisted});
}
// Target contact needs a fresh affected wrist, not every landmark required for
// grading. Otherwise an obscured hip/ear silently cancels a valid centre hit.
// Keep the complete quality gate for calibration and measurement collection.
function reachCanAttempt(){return !testingReachEnabled() || (!calibratingAssessment && preAssessmentCalibrationReady && !reachFlow.stopped && !reachFlow.waiting && !reachFlow.recovery && !reachFlow.replaying
  && !reachVoice.busy && !reachVoice.failed && reachFlow.step && ['attempt','retry'].includes(reachFlow.step.phase));}
function reachCanMeasure(){return !testingReachEnabled() || (reachCanAttempt() && reachCameraStatus().ready);}
async function handleReachEvent(event){
  const step=reachFlow.step;if(!step)return;
  inTargetSince=null;lastInTargetTs=0;
  if(event==='encourage'){
    if(await reachSay('You are doing well to keep trying. If comfortable, try once more toward the centre of the circle. Take your time and keep your shoulder relaxed. You can stop or ask for help.'))step.cueDone();
  }else if(event==='announce_lower'){
    const prompt=step.id==='T1-S1'
      ? 'Thank you for trying. I will bring this low target a little closer to your resting hand. Wait for the circle to stop, then try again comfortably.'
      : 'Thank you for trying. I will lower the target to make the next reach easier. Wait for the circle to stop, then try again comfortably.';
    if(await reachSay(prompt))step.commitLowering();
  }else if(event==='lowered'){
    reachCaption.textContent='The circle is ready. Reach comfortably toward its centre.';
    syncReachCaption();
  }else if(event==='needs_support'){step.finish(false);saveReachPolicy();showReachHelp();}
}
function tickTestingReach(lm,now){
  if(!testingReachEnabled() || reachFlow.stopped || calibratingAssessment)return;
  syncReachCameraQuality();
  const step=reachFlow.step;
  if(!step || stepCompleted || voiceFinishedAt===0)return;
  const obs=reachObservation(lm,now);
  // A successful contact is already progressing through its hold. Do not
  // interrupt it with an unsuccessful-attempt cue or move the target away.
  const holding=['attempt','retry'].includes(step.phase) && checkTarget(lm);
  const paused=reachVoice.busy || reachVoice.failed || reachFlow.waiting || reachFlow.recovery || reachFlow.replaying || correctionVoicePlaying || !reachCameraStatus().ready || holding;
  // Adaptation and target activation must see the same affected-wrist contact,
  // including S4 whose lap anchor is stored in raw camera coordinates.
  const event=step.tick({now,valid:!!obs,paused,distance:obs?testingReachWristContact(lm).distance:Infinity});
  if(event)void handleReachEvent(event);
  document.getElementById('reachDifficulty').textContent=`${step.assisted?'Assisted':'Independent'} · target difficulty ${Math.round(step.difficulty*100)}%`;
}
function finishTestingReachStep(success){
  if(!testingReachEnabled() || !reachFlow.step)return null;
  const step=reachFlow.step;step.finish(success);
  saveReachPolicy();
  if(step.id==='T1-S2')reachFlow.raisedLevel=step.level;
  return {...step.snapshot(),support_available:reachFlow.support};
}
function stopReachVoice(){
  reachFlow.stopped=true;reachSpeechEpoch++;reachVoice.cancel();reachFlow.recovery?.(false);
}
function endReachTest(){
  if(reachFlow.step&&!reachFlow.step.finished)reachFlow.step.finish(false,true);
  stopReachVoice();reachPanel.classList.add('hidden');
  if(!taskResults[0])taskResults[0]={task_id:'T1',completed_steps:0,total_steps:4,duration_ms:0,steps:[],metrics:{}};
  taskResults[0].metrics.testing_reach={support_available:reachFlow.support,stopped:true};
  finishAssessment();
}
document.getElementById('reachPause').onclick=()=>{
  if(reachFlow.stopped)return;
  reachChoice('Take a pause','Rest your arm. Timing and targets are paused. You can continue when comfortable or finish now.',[
    ['Continue',()=>{}],['Finish test for now',endReachTest],
  ]);
};
document.getElementById('reachAssistance').onclick=()=>{
  if(!reachFlow.step || reachFlow.stopped)return;
  reachChoice('Is someone helping your arm?', 'Record hands-on help for this and the remaining steps. Someone simply being beside you does not count as assistance.',[
    ['Yes, record assistance',()=>{reachFlow.support=true;reachFlow.assisted=true;reachFlow.step.assisted=true;}],['No, continue',()=>{}],
  ]);
};
