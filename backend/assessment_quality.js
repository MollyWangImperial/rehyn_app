/* Camera screening evidence. Elbow angles use aspect-corrected 2D image space;
 * other joint angles use one model's world space.
 * Missing/occluded landmarks abstain; thresholds are not clinical diagnoses. */
(function(root) {
  const finite = Number.isFinite;
  const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
  const sub = (a, b) => [a.x-b.x, a.y-b.y, a.z-b.z];
  const sub2D = (a, b, aspect) => [(a.x-b.x)*aspect,a.y-b.y];
  const length = a => Math.hypot(...a);
  const midpoint = (a,b) => ({x:(a.x+b.x)/2,y:(a.y+b.y)/2,z:(a.z+b.z)/2});
  const angleV = (a,b) => length(a) > .005 && length(b) > .005 ? Math.acos(clamp(a.reduce((v,x,i)=>v+x*b[i],0)/(length(a)*length(b)),-1,1))*180/Math.PI : NaN;
  const angle = (a,b,c) => a && b && c ? angleV(sub(a,b),sub(c,b)) : NaN;
  const angle2D = (a,b,c,aspect) => a && b && c ? angleV(sub2D(a,b,aspect),sub2D(c,b,aspect)) : NaN;
  const quantile = (a,q=.5) => a.length ? [...a].sort((x,y)=>x-y)[Math.floor((a.length-1)*q)] : NaN;

  class Tracker {
    constructor(config, side, {peakReachAngles=false,testingReachTrunkLean=false,trunkLeanMetrics=root.TrunkLeanMetrics}={}) {
      this.config=config;
      this.peakReachAngles=peakReachAngles;
      this.testingReachTrunkLean=testingReachTrunkLean;
      this.trunkLeanMetrics=trunkLeanMetrics;
      this.trunkLeanBaselineFrames=[];
      this.trunkLeanBaseline=null;
      this.a=side === "left" ? {s:11,e:13,w:15,h:23,k:25,f:27,ear:7,i:19,p:17,t:31} : {s:12,e:14,w:16,h:24,k:26,f:28,ear:8,i:20,p:18,t:32};
      this.o=side === "left" ? {s:12,h:24,k:26,f:28,ear:8} : {s:11,h:23,k:25,f:27,ear:7};
      this.baselines=[];
      this.baseline=null;
      this.reset(null);
    }
    reset(rubric) {
      this.rubric=rubric;
      this.measurements={}; this.observations={}; this.compensations={}; this.lastTime=null;
      this.seriesStart=null;
      this.currentMetrics={};
      this.sawClosed=false; this.openCloseCycle=0;
    }
    raw(p,w,aspectRatio=1) {
      const a=this.a,o=this.o,r={};
      const imageAspect=finite(aspectRatio) && aspectRatio>0 ? aspectRatio : 1;
      const visible = ids => p && ids.every(i=>p[i] && [p[i].x,p[i].y].every(finite) && (p[i].visibility ?? 0)>=.65);
      const usable = ids => w && visible(ids) && ids.every(i=>w[i] && [w[i].x,w[i].y,w[i].z].every(finite));
      if(visible([a.s,a.e,a.w])) {
        r.elbow_extension=angle2D(p[a.s],p[a.e],p[a.w],imageAspect);
        r.elbow_flexion=180-r.elbow_extension;
      }
      if(usable([a.s,a.e,a.w,a.h])) {
        r.arm_elevation=angle(w[a.h],w[a.s],w[a.e]);
        const armLength=length(sub(w[a.s],w[a.e]))+length(sub(w[a.e],w[a.w]));
        if(armLength>.15) r.reach_ratio=length(sub(w[a.s],w[a.w]))/armLength;
      }
      if(usable([11,12,23,24])) {
        const sh=midpoint(w[11],w[12]), hip=midpoint(w[23],w[24]);
        r.torso=sub(sh,hip);
        r.width=length(sub(w[11],w[12]));
        r.screenWidth=Math.hypot(p[11].x-p[12].x,p[11].y-p[12].y);
        r.shoulderLine=(w[o.s].y-w[a.s].y)/Math.max(.05,r.width);
        r.hipLine=(w[o.h].y-w[a.h].y)/Math.max(.05,r.width);
      }
      if(usable([a.s,a.ear])) r.neckGap=length(sub(w[a.s],w[a.ear]));
      if(usable([7,8,0])) {
        const ears=midpoint(w[7],w[8]);
        r.headPitch=(w[0].y-ears.y)/Math.max(.04,length(sub(w[7],w[8])));
      }
      if(usable([a.s,a.h,a.k,a.f])) {
        r.knee_extension=angle(w[a.h],w[a.k],w[a.f]);
        r.hip_extension=angle(w[a.s],w[a.h],w[a.k]);
        r.hip_flexion=180-r.hip_extension;
      }
      if(usable([o.s,o.h,o.k])) r.other_hip_flexion=180-angle(w[o.s],w[o.h],w[o.k]);
      if(usable([a.k,a.f,a.t])) r.ankle=angle(w[a.k],w[a.f],w[a.t]);
      if(usable([a.h,a.k,a.f,o.f])) r.step_distance=length(sub(w[a.f],w[o.f]))/Math.max(.2,length(sub(w[a.h],w[a.k]))+length(sub(w[a.k],w[a.f])));
      // Coarse pose fingers cannot establish inward vs outward bend. Report
      // alignment only, and abstain for foreshortened, closed or occluded hands.
      if(usable([a.e,a.w,a.i,a.p])) {
        const hand=midpoint(w[a.i],w[a.p]);
        const screenHand=midpoint(p[a.i],p[a.p]);
        const fore=length(sub(w[a.e],w[a.w])), palm=length(sub(hand,w[a.w]));
        const projection=Math.hypot(screenHand.x-p[a.w].x,screenHand.y-p[a.w].y);
        if(fore>.12 && palm>.055 && projection>.025 && palm/fore>.22 && palm/fore<.9) r.wrist_bend=180-angle(w[a.e],w[a.w],hand);
      }
      return r;
    }
    calibrate(p,w,aspectRatio=1) {
      if(this.testingReachTrunkLean && !this.trunkLeanBaseline && this.trunkLeanMetrics) {
        const frame=this.trunkLeanMetrics.metricsFromLandmarks(p,aspectRatio);
        // Build the upright reference from 45 clear frames. Briefly occluded
        // frames do not erase the clear frames already collected.
        if(frame.valid)
          this.trunkLeanBaselineFrames=[...this.trunkLeanBaselineFrames,frame].slice(-45);
        if(this.trunkLeanBaselineFrames.length===45)
          this.trunkLeanBaseline=this.trunkLeanMetrics.baselineFromSamples(this.trunkLeanBaselineFrames);
      }
      const r=this.raw(p,w,aspectRatio);
      if(!r.torso || !finite(r.width)) return;
      this.baselines.push(r);
      if(this.baselines.length>45) this.baselines.shift();
      if(this.baselines.length<15) return;
      const keys=["width","screenWidth","shoulderLine","hipLine","neckGap","headPitch","ankle","wrist_bend"];
      const b=Object.fromEntries(keys.map(k=>[k,quantile(this.baselines.map(x=>x[k]).filter(finite))]));
      const directions=this.baselines.map(x=>x.torso);
      b.torso=[0,1,2].map(i=>quantile(directions.map(v=>v[i])));
      if(Math.max(...directions.map(v=>angleV(v,b.torso)))<6) this.baseline=b;
    }
    trunkLeanReadout(p,aspectRatio=1) {
      if(!this.testingReachTrunkLean || !this.trunkLeanMetrics || !this.trunkLeanBaseline)
        return {supported:false,detected:false,degrees:NaN};
      const frame=this.trunkLeanMetrics.metricsFromLandmarks(p,aspectRatio);
      return this.trunkLeanMetrics.newForwardLeanEvidence(frame,this.trunkLeanBaseline);
    }
    sample({pose,world,hand,handOpen,handClosed,pinch,gaitAlternations,inTarget,now,aspectRatio=1}) {
      if(!this.rubric) return;
      if(this.seriesStart===null) this.seriesStart=now;
      const dt=this.lastTime===null ? 0 : clamp(now-this.lastTime,0,100);
      const gap=this.lastTime!==null && now-this.lastTime>200;
      this.lastTime=now;
      const r=this.raw(pose,world,aspectRatio), b=this.baseline;
      const handValid=hand && hand.length===21 && hand.every(p=>p && finite(p.x) && finite(p.y));
      if(handValid) {
        r.hand_open=handOpen; r.hand_closed=handClosed; r.pinch=pinch;
        if(handClosed>=.65) this.sawClosed=true;
        if(this.sawClosed && handOpen>=.7) this.openCloseCycle=1;
        r.open_close_cycle=this.openCloseCycle;
      }
      if(r.torso) r.target_control=inTarget ? 1 : 0;
      if(finite(r.step_distance)) r.gait_alternations=gaitAlternations;
      if(b && r.torso && finite(r.screenWidth) && Math.abs(r.screenWidth/b.screenWidth-1)<.3) {
        if(!this.testingReachTrunkLean || !/^T1-S/.test(this.rubric.id))
          r.trunk_lean=angleV(r.torso,b.torso);
        // Subtract normal elevation-related shoulder rise. Require both line
        // elevation and neck shortening so opposite shoulder drop alone is not a shrug.
        if(finite(r.neckGap) && finite(b.neckGap)) {
          const rise=Math.max(0,r.shoulderLine-b.shoulderLine);
          const shortened=Math.max(0,(b.neckGap-r.neckGap)/b.width);
          r.shoulder_hike=Math.max(0,Math.atan2(Math.min(rise,shortened),.5)*180/Math.PI-(r.arm_elevation||0)*.12);
        }
        r.hip_hike=Math.atan2(Math.max(0,r.hipLine-b.hipLine),.5)*180/Math.PI;
        if(finite(r.headPitch) && finite(b.headPitch)) r.head_drop=Math.max(0,r.headPitch-b.headPitch)*60;
        if(finite(r.ankle) && finite(b.ankle)) r.ankle_change=Math.abs(r.ankle-b.ankle);
        if(finite(r.wrist_bend) && finite(b.wrist_bend)) r.wrist_extension_change=Math.abs(r.wrist_bend-b.wrist_bend);
      }
      const comparisonLean=this.testingReachTrunkLean && /^T1-S/.test(this.rubric.id);
      if(comparisonLean) {
        const evidence=this.trunkLeanReadout(pose,aspectRatio);
        if(evidence.supported && finite(evidence.degrees)) {
          r.trunk_lean=evidence.degrees;
          r.trunk_lean_detected=evidence.detected;
          r.trunk_lean_evidence=evidence;
        }
      }
      this.currentMetrics=r;
      // Keep diagnostic measurements separate from the scoring criteria. These
      // make a test run inspectable without changing the patient scoring rubric.
      const diagnosticKeys=new Set([...this.rubric.criteria.map(rule=>rule.metric),
        ...this.rubric.compensations,"arm_elevation","elbow_extension","elbow_flexion","reach_ratio","wrist_bend","target_control"]);
      for(const key of diagnosticKeys) {
        const v=r[key];
        if(!finite(v)) continue;
        const record=this.observations[key] ||= {samples:0,values:[],endpoints:[],min:v,max:v,targetSamples:0};
        record.samples++; record.min=Math.min(record.min,v); record.max=Math.max(record.max,v);
        record.values.push(v); if(record.values.length>600) record.values.shift();
        if(inTarget) {record.targetSamples++; record.endpoints.push(v); if(record.endpoints.length>120) record.endpoints.shift();}
      }
      for(const rule of this.rubric.criteria) {
        const v=r[rule.metric];
        if(!finite(v)) continue;
        const record=this.measurements[rule.metric] ||= {samples:0,total:0,values:[],endpoints:[],series:[],seriesInterval:100,lastSeriesAt:null};
        record.samples++;
        record.total+=v;
        if(!record.peak || v>record.peak.value) record.peak={elapsed_ms:Math.max(0,Math.round(now-this.seriesStart)),value:v,in_target:!!inTarget};
        record.values.push(v);
        if(record.values.length>600) record.values.shift();
        if(inTarget) {record.endpoints.push(v); if(record.endpoints.length>120) record.endpoints.shift();}
        if(record.lastSeriesAt===null || now-record.lastSeriesAt>=record.seriesInterval) {
          record.series.push({elapsed_ms:Math.max(0,Math.round(now-this.seriesStart)),value:+v.toFixed(3),in_target:!!inTarget});
          record.lastSeriesAt=now;
          if(record.series.length>240) {
            record.series=record.series.filter((_,index)=>index%2===0);
            record.seriesInterval*=2;
          }
        }
      }
      for(const id of this.rubric.compensations) {
        const c=this.compensations[id] ||= {eligible_ms:0,max_value:0,max_streak_ms:0,streak:0,active:false};
        const comparison=id==="trunk_lean" && comparisonLean;
        if(comparison) {
          c.method="pelvis_normalized_shoulder_or_face_v1";
          const cues=r.trunk_lean_evidence?.cues;
          if(finite(cues?.pelvisNormalizedShoulderScale)) c.shoulder_peak=Math.max(c.shoulder_peak||0,cues.pelvisNormalizedShoulderScale);
          if(finite(cues?.pelvisNormalizedFaceScale)) c.face_peak=Math.max(c.face_peak||0,cues.pelvisNormalizedFaceScale);
          c.cue_runs ||= {shoulder:{streak_ms:0,run_peak:null,confirmed_ms:0,confirmed_peak:null},face:{streak_ms:0,run_peak:null,confirmed_ms:0,confirmed_peak:null}};
          for(const [name,key,threshold] of [["shoulder","pelvisNormalizedShoulderScale",12],["face","pelvisNormalizedFaceScale",7]]) {
            const cue=cues?.[key], run=c.cue_runs[name];
            const over=finite(cue) && (name==="shoulder" ? cue>threshold : cue+1e-9>=threshold);
            if(!over || gap) {run.streak_ms=0;run.run_peak=null;}
            if(over) {
              run.streak_ms+=dt;
              run.run_peak=Math.max(run.run_peak??cue,cue);
              if(run.streak_ms>=500 && run.streak_ms>=run.confirmed_ms) {
                run.confirmed_ms=run.streak_ms;
                run.confirmed_peak=run.run_peak;
              }
            }
          }
        }
        const v=r[id];
        if(!finite(v)) {c.streak=0; c.active=false; continue;}
        c.eligible_ms+=dt;
        c.max_value=Math.max(c.max_value,v);
        if(comparison) {
          c.max_streak_ms=Math.max(...Object.values(c.cue_runs).map(run=>run.confirmed_ms));
          c.active=Object.values(c.cue_runs).some(run=>run.streak_ms>=500);
        } else {
          c.streak=v>this.config.compensations[id].threshold ? (gap?0:c.streak)+dt : 0;
          c.max_streak_ms=Math.max(c.max_streak_ms,c.streak);
          c.active=c.streak>=500;
        }
      }
    }
    snapshot() {
      return {version:this.config.version,measurements:Object.fromEntries(Object.entries(this.measurements).map(([k,r])=>{
        const peak=this.peakReachAngles && /^T1-S/.test(this.rubric.id) && ["arm_elevation","elbow_extension"].includes(k);
        // Preserve the exact winning frame even if chart decimation drops it.
        const series=peak && r.peak ? [...r.series.filter(p=>p.elapsed_ms!==r.peak.elapsed_ms).slice(-239),r.peak].sort((a,b)=>a.elapsed_ms-b.elapsed_ms) : r.series;
        return [k,{samples:r.samples,
        value:peak ? r.peak?.value : k==="target_control" ? r.total/r.samples : quantile(r.endpoints.length>=5?r.endpoints:r.values,.5),
        statistic_samples:peak ? r.samples : k==="target_control" ? r.samples : (r.endpoints.length>=5?r.endpoints:r.values).length,
        statistic_source:peak ? "movement_maximum" : k==="target_control" ? "sample_proportion" : r.endpoints.length>=5 ? "target_median" : "movement_median",
        peak_elapsed_ms:peak ? r.peak?.elapsed_ms : null,series}];})),
        compensations:Object.fromEntries(Object.entries(this.compensations).map(([k,c])=>[k,{eligible_ms:Math.round(c.eligible_ms),max_value:c.max_value,max_streak_ms:Math.round(c.max_streak_ms),
          ...(c.method?{method:c.method,shoulder_peak:c.shoulder_peak??null,face_peak:c.face_peak??null,
            cue_evidence:Object.fromEntries(Object.entries(c.cue_runs||{}).map(([name,run])=>[name,{duration_ms:Math.round(run.confirmed_ms),peak:run.confirmed_peak}]))}: {})}])),
        observations:Object.fromEntries(Object.entries(this.observations).map(([k,r])=>[k,{samples:r.samples,
          median:quantile(r.values),endpoint:r.endpoints.length>=5?quantile(r.endpoints):null,
          min:r.min,max:r.max,target_fraction:r.targetSamples/r.samples}]))};
    }
    active() {return Object.keys(this.compensations).filter(id=>this.compensations[id].active);}
    draw(ctx,pose,width,height) {
      if(!pose) return;
      const a=this.a;
      const paths={trunk_lean:[11,23,24,12,11],shoulder_hike:[a.ear,a.s],head_drop:[7,0,8],wrist_bend:[a.e,a.w,a.i],hip_hike:[23,24]};
      ctx.save(); ctx.strokeStyle="#FF5757"; ctx.lineWidth=4; ctx.lineCap="round"; ctx.setLineDash([2,7]);
      for(const id of this.active()) {
        const points=paths[id].map(i=>pose[i]);
        if(points.some(p=>!p || (p.visibility??0)<.65)) continue;
        ctx.beginPath();points.forEach((p,i)=>{if(i)ctx.lineTo(p.x*width,p.y*height);else ctx.moveTo(p.x*width,p.y*height);});ctx.stroke();
        const p=pose[id==="wrist_bend"?a.w:id==="shoulder_hike"?a.s:id==="head_drop"?0:a.h];
        ctx.beginPath();ctx.arc(p.x*width,p.y*height,Math.max(22,width*.035),0,Math.PI*2);ctx.stroke();
      }
      ctx.restore();
    }
  }
  root.RehynAssessmentQuality={Tracker};
  if(typeof module!=="undefined") module.exports={Tracker};
})(globalThis);
