/* T3 diagnostics consume the very same tracker snapshot that is submitted. */
(function(root) {
  const finite=Number.isFinite;
  function diagnostic(tracker, fresh=true) {
    const snapshot=tracker.snapshot(), raw=fresh ? tracker.currentMetrics : {};
    return {
      criteria:(tracker.rubric?.criteria||[]).map(rule=>{
        const evidence=snapshot.measurements[rule.metric];
        return {...rule,current:finite(raw[rule.metric])?raw[rule.metric]:null,
          value:evidence?.samples>=5 && finite(evidence.value)?evidence.value:null,
          samples:evidence?.samples||0,statistic_samples:evidence?.statistic_samples||0,
          statistic_source:evidence?.statistic_source||null};
      }),
      compensations:(tracker.rubric?.compensations||[]).map(id=>{
        const run=tracker.compensations[id], threshold=tracker.config.compensations[id].threshold;
        const current=finite(raw[id])?raw[id]:null;
        const eligible=run?.eligible_ms||0, longest=run?.max_streak_ms||0;
        return {id,label:tracker.config.compensations[id].label,threshold,current,
          unit:id==='head_drop'?'proxy':'deg',duration:current===null?0:run?.streak||0,
          longest,eligible,status:eligible>=500 && longest>=500 && run.max_value>threshold
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
  root.RehynTestingMouth={diagnostic,flexionArc};
  if(typeof module!=='undefined')module.exports=root.RehynTestingMouth;
})(globalThis);
