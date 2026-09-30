// Camera measurements stay live; local preview and Testing do not automatically
// record or save review videos. Existing saved recordings are left untouched.
const localReview={enabled:false,active:null,results:{},error:null};
const localReviewStatus=document.createElement('div');
localReviewStatus.id='localRecordingStatus';localReviewStatus.setAttribute('role','status');
localReviewStatus.style.cssText='position:absolute;top:42px;left:16px;z-index:9;background:#102e27e8;color:#fff;padding:6px 10px;border-radius:8px;font:13px sans-serif;max-width:75%;pointer-events:none';
localReviewStatus.hidden=true;
if(localReview.enabled)document.getElementById('stage').append(localReviewStatus);
if(localReview.enabled && testingMouthEnabled()){
  localReviewStatus.style.cssText='position:static;margin:8px 0;color:#c4d8d0;font:13px sans-serif';
  document.getElementById('top').after(localReviewStatus);
}
function reviewStatus(text){if(!localReview.enabled)return;localReviewStatus.hidden=false;localReviewStatus.textContent=text;}
function reviewPhase(){
  if(calibratingAssessment)return 'Camera / lap calibration';
  if(testingReachEnabled() && !reachCanMeasure())return 'Paused / instructions / target moving - not scoring';
  return getCurrentStep()?.caption || getCurrentStep()?.id || 'Task finished';
}
function drawLocalReview(recording){
  if(recording.stopping)return;
  const c=recording.context,w=recording.canvas.width,h=recording.imageHeight,now=performance.now();
  c.fillStyle='#10251f';c.fillRect(0,0,w,recording.canvas.height);
  if(video.readyState>=2){
    c.save();c.translate(w,0);c.scale(-1,1);c.drawImage(video,0,0,w,h);
    if(canvas.width&&canvas.height)c.drawImage(canvas,0,0,w,h);c.restore();
  }
  const raw=video.readyState>=2 && now-lastPoseScanTs<=250 && now-lastAngleVideoAt<=250 ? assessmentQuality.raw(latestPoseLandmarks,latestPoseWorldLandmarks,video.videoWidth/video.videoHeight) : {};
  const rubric=assessmentQuality.rubric;
  const fmt=v=>Number.isFinite(v)?`${v.toFixed(1)}°`:'not tracked';
  c.font='18px sans-serif';c.fillStyle='#fff';
  c.fillText(`${recording.taskId}  |  video ${((now-recording.startedAt)/1000).toFixed(1)} s  |  ${reviewPhase()}`,16,h+27,w-32);
  const mouth=recording.taskId==='T3';
  const keys=mouth && rubric ? rubric.criteria.map(rule=>rule.metric) : ['arm_elevation','elbow_extension'];
  const evidence=mouth?assessmentQuality.snapshot():null;
  keys.forEach((key,i)=>{
    const rule=rubric?.criteria.find(r=>r.metric===key),measurement=assessmentQuality.measurements[key];
    const label=key==='arm_elevation'?'Arm elevation (model 3D)':key==='elbow_flexion'?'Elbow bend (2D)':key==='target_control'?'Target control':'Elbow extension (2D)';
    const unit=v=>key==='target_control'?(Number.isFinite(v)?`${(v*100).toFixed(1)}%`:'not tracked'):fmt(v);
    c.fillStyle=i?'#facc15':'#67e8f9';
    const statistic=evidence?.measurements[key];
    c.fillText(mouth
      ? `${label} | reference: ${rule?unit(rule.target):'not used'} | ${statistic?.statistic_source||'collecting'}: ${statistic?.samples>=5?unit(statistic.value):'not measured'}`
      : `${label}: ${fmt(raw[key])}   |   reference: ${rule?fmt(rule.target):'not used in this step'}   |   valid peak: ${fmt(measurement?.peak?.value)}`,16,h+57+i*28,w-32);
  });
  c.fillStyle='#fff';c.font='16px sans-serif';
  const caption=((mouth?document.getElementById('mouthCaption')?.textContent:document.getElementById('reachGuidance')?.textContent) || voiceText.textContent || '').trim();
  const words=caption.split(' ');let line='',row=0;
  for(const word of words){if(c.measureText(line+word).width>w-32){c.fillText(line,16,h+113+row*21);line='';if(++row>1)break;}line+=word+' ';}
  if(row<2)c.fillText(line,16,h+113+row*21);
  recording.raf=requestAnimationFrame(()=>drawLocalReview(recording));
}
function startLocalReview(taskId){
  if(!localReview.enabled || localReview.active || !video.srcObject)return;
  try{
    if(!window.MediaRecorder || !HTMLCanvasElement.prototype.captureStream)throw new Error('This browser cannot record the camera overlay.');
    const surface=document.createElement('canvas');surface.width=960;
    const imageHeight=Math.round(960*video.videoHeight/video.videoWidth);surface.height=imageHeight+155;
    const mime=['video/webm;codecs=vp8','video/webm','video/mp4'].find(x=>MediaRecorder.isTypeSupported(x));
    if(!mime)throw new Error('No supported video recording format.');
    const recording={taskId,canvas:surface,context:surface.getContext('2d'),imageHeight,startedAt:performance.now(),chunks:[],timeline:[],stopping:false,calibrationIncluded:calibratingAssessment};
    const stream=surface.captureStream(20);recording.stream=stream;
    const recorder=new MediaRecorder(stream,{mimeType:mime,videoBitsPerSecond:1200000});recording.recorder=recorder;
    recorder.ondataavailable=e=>{if(e.data.size)recording.chunks.push(e.data);};
    recorder.onerror=()=>{recording.error='Video recording was interrupted; the saved file may be incomplete.';reviewStatus(recording.error);};
    localReview.active=recording;drawLocalReview(recording);recorder.start(1000);
    reviewStatus('Recording locally · camera + angles · no microphone');
  }catch(error){localReview.error=String(error);reviewStatus(`Recording unavailable: ${localReview.error}`);}
}
function markLocalReviewStep(stepId){
  const r=localReview.active;if(r)r.timeline.push({step_id:stepId,instruction_start_video_ms:Math.round(performance.now()-r.startedAt)});
}
function localSaveChoice(error,blob,filename){
  return new Promise(resolve=>{
    const panel=document.createElement('div');panel.setAttribute('role','alert');panel.id='localRecordingSaveError';
    panel.style.cssText='position:absolute;inset:10%;z-index:60;background:#f3f8f5;color:#123a35;padding:25px;overflow:auto;border-radius:16px';
    const text=document.createElement('p');text.textContent=`The recording has not been saved to a local file yet. ${String(error)} Keep this page open and retry, or download the video to your browser’s Downloads folder.`;panel.append(text);
    for(const [label,result] of [['Retry saving','retry'],['Download video instead','download']]){
      const b=document.createElement('button');b.textContent=label;b.style.cssText='padding:15px;margin:10px;font-size:18px';
      b.onclick=()=>{if(result==='download'){const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=filename;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),60000);}panel.remove();resolve(result);};panel.append(b);
    }
    document.getElementById('stage').append(panel);
  });
}
async function saveLocalReview(recording,taskResult){
  recording.stopping=true;cancelAnimationFrame(recording.raf);
  const duration=Math.round(performance.now()-recording.startedAt);
  if(recording.recorder.state!=='inactive')await new Promise(resolve=>{recording.recorder.onstop=resolve;recording.recorder.stop();});
  recording.stream.getTracks().forEach(t=>t.stop());
  const blob=new Blob(recording.chunks,{type:recording.recorder.mimeType});recording.chunks=[];
  const evidence={task_result:taskResult,side:AFFECTED_SIDE,video_duration_ms:duration,
    calibration_included:recording.calibrationIncluded,
    timeline:recording.timeline,notes:'Mirrored full camera view with aligned overlay. Model-3D arm elevation and 2D elbow extension; microphone not recorded. Each quality.video_start_ms aligns its chart time zero with the video.'};
  let saved=null;
  for(;;){
    try{
      reviewStatus('Saving video and angle evidence to this computer…');
      if(!saved){
        const q=new URLSearchParams({task_id:recording.taskId,duration_ms:String(duration)});
        const response=await fetch(`${API_BASE}/local-assessment-recordings?${q}`,{method:'POST',headers:{...ACCOUNT_HEADERS,'Content-Type':blob.type},body:blob});
        if(!response.ok)throw new Error(`Local video save failed (${response.status}).`);saved=await response.json();
      }
      const response=await fetch(`${API_BASE}/local-assessment-recordings/${saved.id}/evidence`,{method:'POST',headers:{...ACCOUNT_HEADERS,'Content-Type':'application/json'},body:JSON.stringify(evidence)});
      if(!response.ok)throw new Error(`Angle evidence save failed (${response.status}).`);
      saved=await response.json();
      const url=new URL(`${API_BASE}/local-assessment-recordings/${saved.id}/video`,location.href);url.searchParams.set('uid',CURRENT_USER_ID);
      saved.playback_url=url.href;saved.warning=recording.error || null;
      reviewStatus(`Saved: ${saved.path}`);return saved;
    }catch(error){
      const filename=`${new Date().toISOString().replace(/[:.]/g,'-')}_${recording.taskId}.${blob.type.includes('mp4')?'mp4':'webm'}`;
      if(await localSaveChoice(error,blob,filename)==='download')return {status:'download_requested',filename,error:String(error)};
    }
  }
}
async function finishLocalReview(taskResult){
  if(!localReview.enabled)return null;
  const r=localReview.active;
  if(r){
    if(!r.saving)r.saving=saveLocalReview(r,taskResult);
    const result=await r.saving;localReview.results[r.taskId]=result;localReview.active=null;return result;
  }
  return localReview.results[taskResult?.task_id] || (localReview.error?{status:'error',error:localReview.error}:null);
}
