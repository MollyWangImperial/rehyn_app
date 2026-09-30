const {test}=require('node:test');
const assert=require('node:assert/strict');
const {Tracker}=require('../assessment_quality.js');
const trunkLeanMetrics=require('../../testing/trunk-lean-comparison/trunk_lean_metrics.js');
const config={version:'rehyn-task-quality-1',compensations:{trunk_lean:{threshold:12},wrist_bend:{threshold:25}}};
const rule={criteria:[{metric:'elbow_extension',target:150}],compensations:['wrist_bend']};

test('Testing mouth observes compensation on return without altering normal lap scoring',()=>{
  const returnRule={id:'T3-S4',criteria:[],compensations:[],scoring_method:'target_completion'};
  const mouthConfig={...config,compensations:{...config.compensations,shoulder_hike:{threshold:12},head_drop:{threshold:15}}};
  const t=new Tracker(mouthConfig,'right',{testingMouthHeadDrop:true});
  t.reset(returnRule);t.raw=()=>({head_drop:20,shoulder_hike:0,trunk_lean:0});
  for(let now=0;now<=1000;now+=50)t.sample({now,inTarget:true});
  assert.ok(t.snapshot().compensations.head_drop.max_streak_ms>=500);
  assert.equal(t.rubric.scoring_method,'target_completion');
  assert.deepEqual(returnRule.compensations,[]);
  const normal=new Tracker(mouthConfig,'right');normal.reset(returnRule);
  assert.deepEqual(normal.rubric.compensations,[]);
});

test('Testing reach observes return-step compensation without changing its completion rubric',()=>{
  const returnRule={id:'T1-S4',criteria:[],compensations:[],scoring_method:'target_completion'};
  const t=new Tracker({...config,compensations:{...config.compensations,shoulder_hike:{threshold:12}}},'right',{testingReachTrunkLean:true});
  t.reset(returnRule);t.raw=()=>({shoulder_hike:20});
  for(let now=0;now<=1000;now+=50)t.sample({now,inTarget:true});
  assert.ok(t.snapshot().compensations.shoulder_hike.max_streak_ms>=500);
  assert.equal(t.rubric.scoring_method,'target_completion');
  assert.deepEqual(returnRule.compensations,[]);
  const standard=new Tracker(config,'right');standard.reset(returnRule);
  assert.deepEqual(standard.rubric.compensations,[]);
});

test('one noisy frame, missing data and long gaps never confirm compensation',()=>{
  const t=new Tracker(config,'right');t.reset(rule);
  let bend=0;
  t.raw=()=>({elbow_extension:150,wrist_bend:bend});
  for(let now=0;now<=1000;now+=50) t.sample({now,inTarget:true});
  bend=55;t.sample({now:1050,inTarget:true});
  assert.deepEqual(t.active(),[]);
  bend=NaN;t.sample({now:1100,inTarget:true});
  bend=55;t.sample({now:5000,inTarget:true});
  assert.deepEqual(t.active(),[]);
  for(let now=5050;now<=5650;now+=50)t.sample({now,inTarget:true});
  assert.deepEqual(t.active(),['wrist_bend']);
  assert.equal(t.snapshot().measurements.elbow_extension.value,150);
  bend=NaN;t.sample({now:5700});assert.deepEqual(t.active(),[]);
});

test('full elbow at rest cannot hide bent elbow at target; steps reset evidence',()=>{
  const t=new Tracker(config,'left');t.reset(rule);
  let extension=180;t.raw=()=>({elbow_extension:extension,wrist_bend:0});
  for(let now=0;now<1000;now+=50)t.sample({now,inTarget:false});
  extension=90;
  for(let now=1000;now<2000;now+=50)t.sample({now,inTarget:true});
  assert.equal(t.snapshot().measurements.elbow_extension.value,90);
  t.reset(rule);
  assert.deepEqual(t.snapshot().measurements,{});
});

test('elbow extension uses the aspect-corrected 2D image angle and is visibility-gated',()=>{
  const t=new Tracker(config,'right');
  const p=Array.from({length:33},()=>({x:.5,y:.5,z:0,visibility:.99}));
  const w=structuredClone(p);
  p[12]={x:.3,y:.5,z:0,visibility:.99};p[14]={x:.5,y:.5,z:0,visibility:.99};p[16]={x:.7,y:.5,z:0,visibility:.99};
  w[12]={x:0,y:0,z:0};w[14]={x:.2,y:0,z:0};w[16]={x:.2,y:.2,z:0};w[24]={x:0,y:.4,z:0};
  assert.equal(t.raw(p,w,16/9).elbow_extension,180);
  assert.equal(t.raw(p,null,16/9).elbow_extension,180);
  p[12]={x:.4,y:.4,z:0,visibility:.99};p[14]={x:.5,y:.5,z:0,visibility:.99};p[16]={x:.6,y:.5,z:0,visibility:.99};
  assert.ok(Math.abs(t.raw(p,w,2).elbow_extension-153.4349488)<.0001);
  p[16].visibility=.2;
  assert.equal(t.raw(p,w,2).elbow_extension,undefined);
});

test('baseline is a stable median, and a bent trunk is detected after calibration',()=>{
  const t=new Tracker(config,'right');
  let torso=[0,-.5,0];t.raw=()=>({torso,width:.4,screenWidth:.25,shoulderLine:0,hipLine:0,elbow_extension:150});
  for(let i=0;i<20;i++)t.calibrate([],[]);
  assert.ok(t.baseline);
  t.reset({criteria:rule.criteria,compensations:['trunk_lean']});
  torso=[.25,-.433,0];
  for(let now=0;now<1000;now+=50)t.sample({now,inTarget:true});
  assert.deepEqual(t.active(),['trunk_lean']);
});

test('normal shoulder rise and opposite shoulder drop do not become shoulder hiking',()=>{
  const t=new Tracker({...config,compensations:{shoulder_hike:{threshold:12}}},'right');
  let r={torso:[0,-.5,0],width:.4,screenWidth:.25,shoulderLine:0,hipLine:0,neckGap:.2,arm_elevation:0};
  t.raw=()=>r;
  for(let i=0;i<20;i++)t.calibrate([],[]);
  t.reset({criteria:[],compensations:['shoulder_hike']});
  r={...r,shoulderLine:.08,neckGap:.17,arm_elevation:90};
  for(let now=0;now<1000;now+=50)t.sample({now});
  assert.deepEqual(t.active(),[]);
  r={...r,shoulderLine:.35,neckGap:.2};
  for(let now=1000;now<2000;now+=50)t.sample({now});
  assert.deepEqual(t.active(),[]);
  r={...r,neckGap:.1,arm_elevation:40};
  for(let now=2000;now<3000;now+=50)t.sample({now});
  assert.deepEqual(t.active(),['shoulder_hike']);
});


test('T3 shoulder rise detects a hike without ear-gap shortening, while brief or absent rises do not count',()=>{
  for(const side of ['left','right'])for(const neckGap of [.2,.3,NaN]){
    const t=new Tracker({...config,compensations:{shoulder_hike:{threshold:12}}},side,{testingMouthHeadDrop:true});
    let r={torso:[0,-.5,0],width:.4,screenWidth:.25,shoulderLine:0,hipLine:0,neckGap:.2,arm_elevation:40};
    t.raw=()=>r;
    for(let i=0;i<20;i++)t.calibrate([],[]);
    t.reset({id:'T3-S2',criteria:[],compensations:['shoulder_hike']});
    r={...r,shoulderLine:.35,neckGap};
    for(let now=0;now<=400;now+=50)t.sample({now});
    assert.deepEqual(t.active(),[],'A short rise must not trigger a warning');
    for(let now=450;now<=650;now+=50)t.sample({now});
    assert.deepEqual(t.active(),['shoulder_hike']);
    assert.equal(t.snapshot().compensations.shoulder_hike.method,'world_shoulder_rise_v2');
    t.reset({id:'T3-S2',criteria:[],compensations:['shoulder_hike']});
    r={...r,shoulderLine:0,neckGap:.05};
    for(let now=0;now<=650;now+=50)t.sample({now});
    assert.deepEqual(t.active(),[],'Ear-gap shortening alone is not a shoulder rise');
    r={...r,shoulderLine:.08,arm_elevation:90};
    for(let now=700;now<=1400;now+=50)t.sample({now});
    assert.deepEqual(t.active(),[],'Keep the normal arm-lifting allowance');
    r={...r,shoulderLine:NaN};t.sample({now:1450});
    assert.equal(t.currentMetrics.shoulder_hike,undefined,'Missing shoulder evidence must abstain');
  }
});

test('T3 calibrates shoulder rise without model ear coordinates while retaining image head checks',()=>{
  const t=new Tracker(config,'right',{testingReachTrunkLean:true,testingMouthHeadDrop:true,trunkLeanMetrics});
  const pose=torsoPose();
  pose[14]={x:.63,y:.50,z:0,visibility:.99};pose[16]={x:.60,y:.68,z:0,visibility:.99};
  const world=structuredClone(pose);world[7]=null;world[8]=null;
  const samples=Array.from({length:8},(_,i)=>({now:i*300,pose,world,aspect:4/3}));
  assert.equal(t.calibrateSeatedTestingWindow(samples),true);
  assert.ok(Number.isNaN(t.baseline.neckGap));
  assert.ok(Number.isFinite(t.baseline.shoulderLine));
  pose[0].visibility=.1;
  assert.equal(t.calibrateSeatedTestingWindow(samples),false,'The separate head check still needs image landmarks');
});

test('diagnostic observations keep endpoint, peak and target fraction without changing scoring',()=>{
  const t=new Tracker(config,'right'); t.reset(rule);
  let elevation=20;
  t.raw=()=>({arm_elevation:elevation,elbow_extension:150,torso:[0,-.5,0]});
  for(let now=0;now<500;now+=50)t.sample({now,inTarget:false});
  elevation=60;
  for(let now=500;now<1000;now+=50)t.sample({now,inTarget:true});
  const snapshot=t.snapshot();
  assert.equal(snapshot.observations.arm_elevation.endpoint,60);
  assert.equal(snapshot.observations.arm_elevation.max,60);
  assert.equal(snapshot.observations.arm_elevation.min,20);
  assert.equal(snapshot.observations.target_control.target_fraction,.5);
  assert.equal(snapshot.measurements.arm_elevation,undefined);
  t.raw=()=>({});t.sample({now:1100});
  assert.equal(t.snapshot().observations.arm_elevation.samples,20);
  t.reset(rule);assert.deepEqual(t.snapshot().observations,{});
});

test('reach ratio describes extension without assuming real-world distance',()=>{
  const t=new Tracker(config,'right');
  const p=Array.from({length:33},()=>({x:.5,y:.5,z:0,visibility:.99}));
  const w=structuredClone(p);
  w[12]={x:0,y:0,z:0};w[14]={x:.2,y:0,z:0};w[16]={x:.4,y:0,z:0};w[24]={x:0,y:.4,z:0};
  assert.equal(t.raw(p,w).reach_ratio,1);
  w[16]={x:.2,y:.2,z:0};assert.ok(t.raw(p,w).reach_ratio<.71);
  p[16].visibility=.1;assert.equal(t.raw(p,w).reach_ratio,undefined);
});

test('scoring measurements include a bounded time series and target control uses the observed proportion',()=>{
  const t=new Tracker(config,'right');
  t.reset({criteria:[{metric:'target_control',target:.8}],compensations:[]});
  t.raw=()=>({torso:[0,-.5,0]});
  for(let now=0;now<1000;now+=50)t.sample({now,inTarget:now>=250});
  const measurement=t.snapshot().measurements.target_control;
  assert.equal(measurement.value,.75);
  assert.equal(measurement.statistic_source,'sample_proportion');
  assert.equal(measurement.series[0].elapsed_ms,0);
  assert.equal(measurement.series.at(-1).elapsed_ms,900);
  assert.equal(measurement.series.some(point=>point.in_target),true);
  assert.ok(measurement.series.length<=240);
});

test('Testing reach uses an early maximum for both angles even after returning or buffer eviction',()=>{
  const t=new Tracker(config,'right',{peakReachAngles:true});
  const reachRule={id:'T1-S2',criteria:[{metric:'arm_elevation'},{metric:'elbow_extension'}],compensations:[]};
  t.reset(reachRule);
  let arm=20,elbow=90;t.raw=()=>({arm_elevation:arm,elbow_extension:elbow});
  for(let now=0;now<500;now+=50){if(now===150){arm=65;elbow=162;}else{arm=20;elbow=90;}t.sample({now,inTarget:false});}
  for(let now=500;now<100000;now+=50)t.sample({now,inTarget:true});
  const m=t.snapshot().measurements;
  assert.equal(m.arm_elevation.value,65);assert.equal(m.elbow_extension.value,162);
  for(const metric of Object.values(m)){
    assert.equal(metric.statistic_source,'movement_maximum');assert.equal(metric.peak_elapsed_ms,150);
    assert.ok(metric.series.some(p=>p.elapsed_ms===150&&p.value===metric.value));assert.ok(metric.series.length<=240);
  }
  t.reset(reachRule);assert.deepEqual(t.snapshot().measurements,{});
  arm=NaN;elbow=NaN;t.sample({now:100100});assert.deepEqual(t.snapshot().measurements,{});
});

test('maximum option does not change non-reach tasks or target-control scoring',()=>{
  const t=new Tracker(config,'right',{peakReachAngles:true});
  t.reset({id:'T2-S1',criteria:[{metric:'elbow_extension'}],compensations:[]});
  let elbow=170;t.raw=()=>({elbow_extension:elbow});t.sample({now:0});elbow=80;
  for(let now=50;now<1000;now+=50)t.sample({now,inTarget:true});
  assert.equal(t.snapshot().measurements.elbow_extension.value,80);
});

function torsoPose({shoulderScale=1,hipScale=1,faceScale=1,visible=true}={}) {
  const p=Array.from({length:33},()=>({x:.5,y:.5,z:0,visibility:.99}));
  p[0]={x:.5,y:.19,z:0,visibility:.99};
  p[7]={x:.5-.04*faceScale,y:.21,z:0,visibility:.99};
  p[8]={x:.5+.04*faceScale,y:.21,z:0,visibility:.99};
  p[11]={x:.5-.10*shoulderScale,y:.31,z:0,visibility:visible?.99:.1};
  p[12]={x:.5+.10*shoulderScale,y:.31,z:0,visibility:.99};
  p[23]={x:.5-.09*hipScale,y:.70,z:0,visibility:.99};
  p[24]={x:.5+.09*hipScale,y:.70,z:0,visibility:.99};
  return p;
}

test('T3 distinguishes a shoulder hike from trunk lean, including both together',()=>{
  for(const side of ['left','right'])for(const aspect of [4/3,3/4])for(const roll of [0,.25]){
    const cfg={...config,compensations:{trunk_lean:{threshold:12},shoulder_hike:{threshold:12},head_drop:{threshold:15}}};
    const t=new Tracker(cfg,side,{testingReachTrunkLean:true,testingMouthHeadDrop:true,trunkLeanMetrics});
    const upright=torsoPose();
    upright[13]={x:.37,y:.5,z:0,visibility:.99};upright[15]={x:.4,y:.68,z:0,visibility:.99};
    upright[14]={x:.63,y:.5,z:0,visibility:.99};upright[16]={x:.6,y:.68,z:0,visibility:.99};
    const rotate=p=>p.map(v=>{const x=(v.x-.5)*aspect,y=v.y-.5;return {...v,x:.5+(x*Math.cos(roll)-y*Math.sin(roll))/aspect,y:.5+x*Math.sin(roll)+y*Math.cos(roll)};});
    const baselinePose=rotate(upright);
    assert.equal(t.calibrateSeatedTestingWindow(Array.from({length:8},(_,i)=>({now:i*300,pose:baselinePose,world:upright,aspect}))),true);
    for(const kind of ['hike','lean','both']){
      const p=structuredClone(upright),world=structuredClone(upright);
      if(kind!=='lean'){p[side==='left'?11:12].y=.16;world[side==='left'?11:12].y=.16;}
      if(kind!=='hike'){
        for(const i of [11,12])p[i].x=.5+(p[i].x-.5)*1.2;
        for(const i of [7,8])p[i].x=.5+(p[i].x-.5)*1.1;
      }
      const pose=rotate(p);
      t.reset({id:'T3-S2',criteria:[],compensations:['trunk_lean','shoulder_hike','head_drop']});
      for(let now=0;now<=700;now+=50)t.sample({pose,world,now,aspectRatio:aspect});
      assert.equal(t.active().includes('shoulder_hike'),kind!=='lean',`${side}/${aspect}/${roll}/${kind}: hike`);
      assert.equal(t.active().includes('trunk_lean'),kind!=='hike',`${side}/${aspect}/${roll}/${kind}: trunk`);
      assert.equal(t.snapshot().compensations.trunk_lean.method,'image_shoulder_expansion_v3');
      if(kind==='hike'){
        assert.ok(!t.active().includes('head_drop'),'A hike with a stationary head is not head lowering');
        assert.ok(Math.abs(t.trunkLeanReadout(pose,aspect).shoulderScale-1)<1e-8);
        if(aspect===4/3&&roll===0){
          const old=trunkLeanMetrics.newForwardLeanEvidence(trunkLeanMetrics.metricsFromLandmarks(pose,aspect),t.trunkLeanBaseline);
          assert.equal(old.detected,true,'Reproduces diagonal-span false trunk alarm before the fix');
        }
      }
    }
  }
});

test('Testing reach builds the same posture and trunk references from a timed low-FPS window',()=>{
  const t=new Tracker(config,'right',{testingReachTrunkLean:true,trunkLeanMetrics});
  const pose=torsoPose();
  pose[14]={x:.63,y:.50,z:0,visibility:.99};
  pose[16]={x:.60,y:.68,z:0,visibility:.99};
  const samples=Array.from({length:8},(_,i)=>({now:i*300,pose,world:pose,aspect:4/3}));
  assert.equal(t.calibrateTestingReachWindow(samples.slice(0,7)),false);
  assert.equal(t.calibrateTestingReachWindow(samples.map(s=>({...s,now:s.now/2}))),false);
  assert.equal(t.calibrateTestingReachWindow(samples.map(s=>({...s,now:100}))),false);
  assert.equal(t.calibrateTestingReachWindow(samples.map((s,i)=>({...s,now:s.now+(i>3?1000:0)}))),false);
  assert.equal(t.calibrateTestingReachWindow(samples),true);
  assert.deepEqual(t.trunkLeanBaseline,trunkLeanMetrics.baselineFromSamples(samples.map(s=>trunkLeanMetrics.metricsFromLandmarks(s.pose,s.aspect))));
  assert.ok(['width','screenWidth','neckGap','shoulderLine'].every(k=>Number.isFinite(t.baseline[k])));
  assert.equal(t.trunkLeanBaselineFrames.length,8);
  const hidden=structuredClone(pose);hidden[14].visibility=.2;
  assert.equal(t.calibrateTestingReachWindow(samples.map(s=>({...s,pose:hidden}))),false);
});

test('Testing T1 uses the shared calibrated shoulder-or-face lean detector for sustained face-only evidence',()=>{
  const t=new Tracker(config,'right',{testingReachTrunkLean:true,trunkLeanMetrics});
  t.raw=()=>({torso:[0,-.5,0],width:.4,screenWidth:.2,shoulderLine:0,hipLine:0});
  for(let i=0;i<44;i++)t.calibrate(torsoPose(),null,1.5);
  assert.equal(t.trunkLeanBaseline,null);
  t.calibrate(torsoPose(),null,1.5);
  assert.ok(t.trunkLeanBaseline);
  t.reset({id:'T1-S2',criteria:[],compensations:['trunk_lean']});
  const faceOnly=torsoPose({faceScale:1.1});
  for(let now=0;now<=700;now+=50)t.sample({pose:faceOnly,now,aspectRatio:1.5});
  const lean=t.snapshot().compensations.trunk_lean;
  assert.equal(lean.method,'pelvis_normalized_shoulder_or_face_v1');
  assert.ok(lean.face_peak>=7 && lean.face_peak<12);
  assert.ok(lean.shoulder_peak<1);
  assert.ok(lean.max_streak_ms>=500);
  assert.equal(lean.cue_evidence.shoulder.duration_ms,0);
  assert.ok(lean.cue_evidence.face.duration_ms>=500);
  assert.ok(lean.cue_evidence.face.peak>=7);
  assert.deepEqual(t.active(),['trunk_lean']);
});

test('briefly missing hips do not erase clear upright calibration frames',()=>{
  const t=new Tracker(config,'right',{testingReachTrunkLean:true,trunkLeanMetrics});
  t.raw=()=>({torso:[0,-.5,0],width:.4,screenWidth:.2,shoulderLine:0,hipLine:0});
  for(let i=0;i<44;i++){
    t.calibrate(torsoPose(),null,1.5);
    if(i%5===0){const occluded=torsoPose();occluded[23].visibility=.1;t.calibrate(occluded,null,1.5);}
  }
  assert.equal(t.trunkLeanBaseline,null);
  assert.equal(t.trunkLeanBaselineFrames.length,44);
  t.calibrate(torsoPose(),null,1.5);
  assert.ok(t.trunkLeanBaseline);
});

test('Testing T1 does not combine short shoulder and face cue bursts into trunk lean',()=>{
  const t=new Tracker(config,'right',{testingReachTrunkLean:true,trunkLeanMetrics});
  t.raw=()=>({torso:[0,-.5,0],width:.4,screenWidth:.2,shoulderLine:0,hipLine:0});
  for(let i=0;i<45;i++)t.calibrate(torsoPose(),null,1.5);
  t.reset({id:'T1-S2',criteria:[],compensations:['trunk_lean']});
  for(let now=0;now<=950;now+=50) {
    const pose=Math.floor(now/250)%2===0 ? torsoPose({shoulderScale:1.3}) : torsoPose({faceScale:1.12});
    t.sample({pose,now,aspectRatio:1.5});
  }
  const lean=t.snapshot().compensations.trunk_lean;
  assert.ok(lean.shoulder_peak>12 && lean.face_peak>7);
  assert.equal(lean.cue_evidence.shoulder.duration_ms,0);
  assert.equal(lean.cue_evidence.face.duration_ms,0);
  assert.equal(lean.max_streak_ms,0);
  assert.deepEqual(t.active(),[]);
});

test('Testing T1 ignores uniform camera approach, missing torso landmarks and old 3D-only lean',()=>{
  const t=new Tracker(config,'right',{testingReachTrunkLean:true,trunkLeanMetrics});
  let torso=[0,-.5,0];
  t.raw=()=>({torso,width:.4,screenWidth:.2,shoulderLine:0,hipLine:0});
  for(let i=0;i<45;i++)t.calibrate(torsoPose(),null,1.5);
  torso=[.3,-.4,0];
  t.reset({id:'T1-S2',criteria:[],compensations:['trunk_lean']});
  const zoom=torsoPose({shoulderScale:1.2,hipScale:1.2,faceScale:1.2});
  for(let now=0;now<=700;now+=50)t.sample({pose:zoom,now,aspectRatio:1.5});
  assert.deepEqual(t.active(),[]);
  assert.ok(t.snapshot().compensations.trunk_lean.max_value<1);
  const missing=torsoPose({visible:false});
  t.sample({pose:missing,now:750,aspectRatio:1.5});
  assert.deepEqual(t.active(),[]);
  assert.equal(t.snapshot().compensations.trunk_lean.eligible_ms,700);
  t.reset({id:'T2-S2',criteria:[],compensations:['trunk_lean']});
  assert.equal(t.trunkLeanReadout(torsoPose(),1.5).supported,true);
  for(let now=0;now<=700;now+=50)t.sample({pose:torsoPose(),now,aspectRatio:1.5});
  assert.equal(t.snapshot().compensations.trunk_lean.method,undefined);
  assert.deepEqual(t.active(),['trunk_lean']);
});


test('T3 separates shoulder growth from face growth and ignores hip jitter, shrug, translation and face roll',()=>{
  for(const side of ['left','right'])for(const aspect of [4/3,3/4]) {
    const t=new Tracker({...config,compensations:{...config.compensations,head_drop:{threshold:15}}},side,
      {testingReachTrunkLean:true,testingMouthHeadDrop:true,trunkLeanMetrics});
    const p=torsoPose();p[14]={x:.63,y:.5,z:0,visibility:.99};p[16]={x:.6,y:.68,z:0,visibility:.99};
    p[13]={x:.37,y:.5,z:0,visibility:.99};p[15]={x:.4,y:.68,z:0,visibility:.99};
    assert.equal(t.calibrateSeatedTestingWindow(Array.from({length:8},(_,i)=>({now:i*300,pose:p,world:p,aspect}))),true);
    for(const kind of ['face','trunk','both','hips','shrug','nod','lower','roll','shoulder_narrowing']) {
      const changed=structuredClone(p);
      if(['face','trunk','both'].includes(kind))for(const i of [7,8])changed[i].x=.5+(p[i].x-.5)*(kind==='both'?1.5:1.2);
      if(['trunk','both'].includes(kind))for(const i of [11,12])changed[i].x=.5+(p[i].x-.5)*1.2;
      if(kind==='hips')for(const i of [23,24]){changed[i].x=.5+(p[i].x-.5)*.7;changed[i].y+=(i===23?-.08:.08);}
      if(kind==='shrug'){changed[11].y-=.05;changed[12].y-=.15;}
      if(kind==='nod')changed[0].y+=.06;
      if(kind==='lower')for(const i of [0,7,8])changed[i].y+=.10;
      if(kind==='shoulder_narrowing')for(const i of [11,12])changed[i].x=.5+(p[i].x-.5)*.8;
      if(kind==='roll')for(const i of [7,8]){const x=(p[i].x-.5)*aspect;changed[i].x=.5+x*Math.cos(.55)/aspect;changed[i].y=.21+x*Math.sin(.55);}
      t.reset({id:'T3-S2',criteria:[],compensations:['head_drop','trunk_lean']});
      for(let now=0;now<=400;now+=50)t.sample({pose:changed,world:null,now,aspectRatio:aspect});
      assert.deepEqual(t.active(),[],'Short bursts do not count');
      for(let now=450;now<=700;now+=50)t.sample({pose:changed,world:null,now,aspectRatio:aspect});
      assert.equal(t.active().includes('head_drop'),['face','both'].includes(kind),`${side}/${aspect}/${kind}: head`);
      assert.equal(t.active().includes('trunk_lean'),['trunk','both'].includes(kind),`${side}/${aspect}/${kind}: trunk`);
      assert.equal(t.snapshot().compensations.head_drop.method,'image_face_expansion_v4');
      assert.equal(t.snapshot().compensations.trunk_lean.method,'image_shoulder_expansion_v3');
      changed[8].visibility=.1;t.sample({pose:changed,now:750,aspectRatio:aspect});
      assert.ok(!Number.isFinite(t.currentMetrics.head_drop));assert.equal(t.compensations.head_drop.streak,0);
      changed[11].visibility=.1;t.sample({pose:changed,now:800,aspectRatio:aspect});
      assert.ok(!Number.isFinite(t.currentMetrics.trunk_lean));
    }
    const raw=t.raw(p,null,aspect);t.reset({id:'T3-S2',criteria:[],compensations:['head_drop']});
    t.raw=()=>({...raw,faceScreenSpan:raw.faceScreenSpan*1.3});
    for(let now=0;now<=300;now+=50)t.sample({now});
    t.pauseEvidence();
    for(let now=1000;now<=1300;now+=50)t.sample({now});
    assert.deepEqual(t.active(),[],'Separate short movements cannot combine across an instruction');
  }
});
