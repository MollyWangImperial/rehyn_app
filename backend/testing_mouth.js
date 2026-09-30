/* T3 diagnostics consume the very same tracker snapshot that is submitted. */
(function(root) {
  const finite=Number.isFinite;
  const distance=(a,b)=>a&&b?Math.hypot(a.x-b.x,a.y-b.y):Infinity;
  // Coordinates are in camera short-side units. Subtract shoulder motion so
  // leaning toward a mouse with the arm at rest does not start a reach.
  class MovementWindow {
    begin(stepId, wrist, shoulder, target) {
      this.stepId=stepId;this.active=false;
      this.lastMotionAt=null;this.lastRelative=null;
      this.anchor=wrist&&shoulder?{x:wrist.x-shoulder.x,y:wrist.y-shoulder.y}:null;
      this.direction=wrist&&target?{x:target.x-wrist.x,y:target.y-wrist.y}:null;
    }
    update({wrist,shoulder,target,inTarget,headApproach=false,enabled,now=0}) {
      if(!enabled || !wrist || !shoulder || !target)return false;
      // Contact is itself a reach endpoint, even if the approach happened
      // during narration. Outside the target, retain the intentional-motion gate.
      if(inTarget || headApproach){this.active=true;this.lastMotionAt=now;}
      const relative={x:wrist.x-shoulder.x,y:wrist.y-shoulder.y};
      if(!this.lastRelative || distance(relative,this.lastRelative)>=.003){this.lastRelative=relative;this.lastMotionAt=now;}
      if(this.active){
        if(!inTarget && now-this.lastMotionAt>=1500){this.begin(this.stepId,wrist,shoulder,target);return false;}
        return true;
      }
      if(this.stepId==='T3-S3')return this.active=!!inTarget; // continuing hold
      if(!this.anchor){this.begin(this.stepId,wrist,shoulder,target);return false;}
      const length=Math.hypot(this.direction.x,this.direction.y);
      if(length<.005)return this.active=!!inTarget;
      const delta={x:wrist.x-shoulder.x-this.anchor.x,y:wrist.y-shoulder.y-this.anchor.y};
      const progress=(delta.x*this.direction.x+delta.y*this.direction.y)/length;
      this.active=progress>=Math.min(.025,Math.max(.012,length*.2));
      return this.active;
    }
  }
  function headApproachesRaisedHand({cue,nose,hand,shoulderY,aspect=1,threshold=15}) {
    if(!finite(cue) || cue<=threshold || !nose || !hand || !finite(shoulderY))return false;
    const sx=Math.max(1,aspect),sy=Math.max(1,1/aspect);
    // A head-to-raised-hand attempt counts even if the wrist itself is still.
    // A resting hand at the lap/mouse must not open the scoring window.
    return hand.y<=shoulderY+.08/sy && Math.hypot((nose.x-hand.x)*sx,(nose.y-hand.y)*sy)<.20;
  }
  function diagnostic(tracker, fresh=true, liveMetrics=null, rubric=tracker.rubric) {
    const snapshot=tracker.snapshot(), raw=fresh ? liveMetrics||tracker.currentMetrics : {};
    return {
      criteria:(rubric?.criteria||[]).map(rule=>{
        const evidence=snapshot.measurements[rule.metric];
        return {...rule,current:finite(raw[rule.metric])?raw[rule.metric]:null,
          value:evidence?.samples>=5 && finite(evidence.value)?evidence.value:null,
          samples:evidence?.samples||0,statistic_samples:evidence?.statistic_samples||0,
          statistic_source:evidence?.statistic_source||null};
      }),
      compensations:(rubric?.compensations||[]).map(id=>{
        const run=tracker.compensations[id], threshold=tracker.config.compensations[id].threshold;
        const current=finite(raw[id])?raw[id]:null;
        const eligible=run?.eligible_ms||0, longest=run?.max_streak_ms||0;
        return {id,label:tracker.config.compensations[id].label,threshold,current,
          unit:tracker.config.compensations[id].unit||(id==='head_drop'?'proxy':'deg'),duration:current===null?0:run?.streak||0,
          longest,eligible,status:eligible>=500 && longest>=500 && (['pelvis_normalized_shoulder_or_face_v1','pelvis_axis_shoulder_or_face_v2'].includes(run.method) || run.max_value>threshold)
            ?'Detected this step':current===null?'Not measurable now':eligible<500?'Collecting evidence':'Not detected'};
      })
    };
  }
  function flexionArc(landmarks, side, aspect, jointArc) {
    const ids=side==='left'?[11,13,15]:[12,14,16];
    if(!landmarks || !ids.every(i=>landmarks[i] && (landmarks[i].visibility??0)>=.65))return null;
    const [s,e,w]=ids.map(i=>({x:1-landmarks[i].x,y:landmarks[i].y}));
    // The continuation of the upper arm makes the exterior bend arc equal to
    // 180° minus the shoulder-elbow-wrist internal (extension) angle.
    return jointArc({x:2*e.x-s.x,y:2*e.y-s.y},e,w,aspect);
  }
  // Remember confirmed evidence across step resets. Live estimates alone must
  // not flag a compensation or imply a score penalty.
  class CompensationFlags {
    constructor(){this.detected=new Set();}
    update(tracker,{scoring=false,fresh=false}={}){
      for(const id of tracker.rubric?.compensations||[]){
        const check=tracker.compensations[id];
        if(!check || !(check.eligible_ms>=500))continue;
        let confirmed=check.max_streak_ms>=500 && check.max_value>tracker.config.compensations[id].threshold;
        if(id==='trunk_lean' && ['pelvis_normalized_shoulder_or_face_v1','pelvis_axis_shoulder_or_face_v2'].includes(check.method)){
          const cues=check.cue_runs||{};
          confirmed=(cues.shoulder?.confirmed_ms>=500 && cues.shoulder.confirmed_peak>12)
            || (cues.face?.confirmed_ms>=500 && cues.face.confirmed_peak>=7);
        }
        if(confirmed)this.detected.add(id);
      }
      const labels={trunk_lean:'Trunk lean',shoulder_hike:'Shoulder hiking',head_drop:'Head moving forward'};
      const detected=[...this.detected];
      const active=scoring && fresh ? tracker.active().filter(id=>this.detected.has(id)) : [];
      return {detected,active,labels:detected.map(id=>labels[id]||id),
        activeLabels:active.map(id=>labels[id]||id),score:detected.length?15:null};
    }
  }
  root.RehynTestingMouth={diagnostic,flexionArc,MovementWindow,headApproachesRaisedHand,CompensationFlags};
  if(typeof module!=='undefined')module.exports=root.RehynTestingMouth;
})(globalThis);
