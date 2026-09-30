const {test}=require('node:test');
const assert=require('node:assert/strict');
const {Tracker}=require('../assessment_quality.js');
const {jointArc}=require('../reach_angles.js');
const {diagnostic,flexionArc}=require('../testing_mouth.js');
const {MovementWindow}=require('../testing_mouth.js');
const config={version:'rehyn-task-quality-1',compensations:{head_drop:{threshold:15,label:'Head cue'}}};

test('compensation alert confirms shoulder hiking and retains the 15 score across steps',()=>{
  const {CompensationFlags}=require('../testing_mouth.js');
  const flags=new CompensationFlags();
  const t=new Tracker({version:'test',compensations:{shoulder_hike:{threshold:12}}},'right');
  const rule={id:'T3-S1',criteria:[],compensations:['shoulder_hike']};
  t.reset(rule);let value=20;t.postureReadout=()=>({shoulder_hike:value});
  for(let now=0;now<=400;now+=100)t.sample({now});
  assert.deepEqual(flags.update(t,{scoring:true,fresh:true}).detected,[]);
  t.sample({now:500});
  assert.deepEqual(flags.update(t,{scoring:true,fresh:true}).activeLabels,['Shoulder hiking']);
  assert.equal(flags.update(t).score,15);
  value=0;t.sample({now:600});
  assert.deepEqual(flags.update(t,{scoring:true,fresh:true}).active,[]);
  t.reset({...rule,id:'T3-S2'});
  assert.deepEqual(flags.update(t).labels,['Shoulder hiking']);
  assert.equal(flags.update(t).score,15);
  assert.equal(new CompensationFlags().update(t).score,null,'A new attempt clears the flag');
});

test('alert uses separately confirmed trunk cues and never treats missing tracking as currently active',()=>{
  const {CompensationFlags}=require('../testing_mouth.js');
  const flags=new CompensationFlags();
  const t={rubric:{compensations:['trunk_lean']},config:{compensations:{trunk_lean:{threshold:12}}},active:()=>['trunk_lean'],
    compensations:{trunk_lean:{eligible_ms:1000,max_streak_ms:1000,max_value:8,method:'pelvis_normalized_shoulder_or_face_v1',cue_runs:{
      face:{confirmed_ms:400,confirmed_peak:8},shoulder:{confirmed_ms:400,confirmed_peak:13}}}}};
  assert.equal(flags.update(t).score,null,'Two short cue runs cannot combine into a finding');
  t.compensations.trunk_lean.cue_runs.face.confirmed_ms=500;
  assert.deepEqual(flags.update(t,{scoring:true,fresh:true}).activeLabels,['Trunk lean']);
  assert.deepEqual(flags.update(t,{scoring:true,fresh:false}).active,[]);
  assert.deepEqual(flags.update(t,{scoring:false,fresh:true}).active,[]);
  assert.deepEqual(flags.update(t).labels,['Trunk lean']);
});

test('mouse/posture motion and instructions do not score; reaching/holding does, on either arm',()=>{
  for(const sign of [-1,1]){
    const gate=new MovementWindow(),wrist={x:.5+sign*.15,y:.75},shoulder={x:wrist.x,y:.35},target={x:.5,y:.41};
    gate.begin('T3-S1',wrist,shoulder,target);
    const t=new Tracker({version:'test',compensations:{trunk_lean:{threshold:12}}},'right');
    t.reset({id:'T3-S1',criteria:[],compensations:['trunk_lean']});
    let lean=25;t.postureReadout=()=>({trunk_lean:lean});
    const record=(f,now)=>{if(gate.update({...f,now}))t.sample({now});else t.pauseEvidence();};
    for(let now=0;now<1500;now+=50)record({wrist:{x:wrist.x+.1,y:wrist.y-.04},shoulder:{x:shoulder.x+.1,y:shoulder.y-.04},target,enabled:true,inTarget:false},now);
    assert.deepEqual(t.snapshot().compensations,{},'Whole-body motion with resting arm cannot start scoring');
    const reach={wrist:{x:wrist.x,y:.55},shoulder,target,inTarget:false,enabled:false};
    for(let now=1500;now<2500;now+=50)record(reach,now);
    assert.deepEqual(t.snapshot().compensations,{},'Instruction listening is not scored');
    lean=0;reach.enabled=true;
    for(let now=2500;now<3200;now+=50)record(reach,now);
    assert.equal(t.snapshot().compensations.trunk_lean.max_streak_ms,0);
    lean=25;reach.wrist=target;reach.inTarget=true;
    for(let now=3200;now<3900;now+=50)record(reach,now);
    assert.ok(t.snapshot().compensations.trunk_lean.max_streak_ms>=500,'True lean while reaching/holding still counts');
  }
});

test('a pause cannot join compensation runs, and idle rest outside the circle closes the movement window',()=>{
  const t=new Tracker(config,'right');t.reset({id:'T3-S1',criteria:[],compensations:['head_drop']});
  t.postureReadout=()=>({head_drop:20});
  for(const now of [0,100,200,300])t.sample({now});
  t.pauseEvidence();
  for(const now of [350,450,550,650])t.sample({now});
  assert.equal(t.snapshot().compensations.head_drop.max_streak_ms,300);
  const gate=new MovementWindow(),shoulder={x:.5,y:.3},target={x:.5,y:.4};
  gate.begin('T3-S1',{x:.5,y:.7},shoulder,target);
  const frame={wrist:{x:.5,y:.55},shoulder,target,inTarget:false,enabled:true};
  assert.equal(gate.update({...frame,now:0}),true);
  assert.equal(gate.update({...frame,now:1600}),false);
  assert.equal(gate.update({...frame,now:1700}),false);
  assert.equal(gate.update({...frame,wrist:target,inTarget:true,now:1800}),true);
  gate.begin('T3-S3',target,shoulder,target);
  assert.equal(gate.update({...frame,wrist:target,inTarget:true,now:1900}),true,'Hold step needs no extra arm movement');
});

test('contact starts the scoring window even when the arm arrived during narration',()=>{
  const gate=new MovementWindow(),target={x:.5,y:.4},shoulder={x:.5,y:.3};
  gate.begin('T3-S1',target,shoulder,target);
  assert.equal(gate.update({wrist:target,shoulder,target,inTarget:true,enabled:false,now:100}),false);
  assert.equal(gate.update({wrist:target,shoulder,target,inTarget:true,enabled:true,now:500}),true);
  assert.equal(gate.update({wrist:{x:.54,y:.4},shoulder,target,inTarget:true,enabled:true,now:1100}),true);
});

test('bend arc agrees with aspect-corrected elbow flexion on both sides, including mirroring',()=>{
  for(const side of ['left','right'])for(const aspect of [4/3,9/16,16/9]) {
    const tracker=new Tracker(config,side), {s,e,w}=tracker.a;
    const pose=Array.from({length:33},()=>({x:.5,y:.5,z:0,visibility:1}));
    pose[s]={x:.3,y:.2,z:0,visibility:1};pose[e]={x:.45,y:.6,z:0,visibility:1};pose[w]={x:.6,y:.3,z:0,visibility:1};
    for(const mirrored of [false,true]) {
      const p=pose.map(p=>({...p,x:mirrored?1-p.x:p.x}));
      assert.ok(Math.abs(flexionArc(p,side,aspect,jointArc).degrees-tracker.raw(p,null,aspect).elbow_flexion)<1e-8);
    }
    pose[e].visibility=.1;assert.equal(flexionArc(pose,side,aspect,jointArc),null);
  }
});
test('T3 live statistic equals submitted target median, then abstains with fewer than five samples',()=>{
  const t=new Tracker(config,'right',{peakReachAngles:true});
  t.reset({id:'T3-S2',criteria:[{metric:'elbow_flexion',target:110}],compensations:[]});
  let value=0;t.raw=()=>({elbow_flexion:value});
  for(let i=0;i<10;i++){value=i<5?140:100+i;t.sample({now:i*50,inTarget:i>=5});}
  assert.equal(t.snapshot().measurements.elbow_flexion.statistic_source,'target_median');
  assert.equal(diagnostic(t).criteria[0].value,107);
  assert.equal(diagnostic(t,false).criteria[0].current,null);
  assert.equal(diagnostic(t,false).criteria[0].value,107);
  t.reset({id:'T3-S2',criteria:[{metric:'elbow_flexion',target:110}],compensations:[]});
  t.sample({now:0,inTarget:true});assert.equal(diagnostic(t).criteria[0].value,null);
});
test('target control includes early valid samples beyond the 600-sample display window',()=>{
  const t=new Tracker(config,'right');t.reset({id:'T3-S4',criteria:[{metric:'target_control',target:.8}],compensations:[]});
  t.raw=()=>({torso:[0,1,0]});
  for(let i=0;i<800;i++)t.sample({now:i*50,inTarget:i>=200});
  assert.equal(diagnostic(t).criteria[0].value,.75);
});
test('head proxy requires sustained evidence; missing frames do not appear as zero or accumulate a hit',()=>{
  const t=new Tracker(config,'right');
  t.baseline={torso:[0,-1,0],screenWidth:.4,headPitch:0};
  t.reset({id:'T3-S2',criteria:[],compensations:['head_drop']});
  let tracked=true;t.raw=()=>tracked?{torso:[0,-1,0],screenWidth:.4,headPitch:.3}:{};
  for(let i=0;i<5;i++)t.sample({now:i*100});
  assert.notEqual(diagnostic(t).compensations[0].status,'Detected this step');
  tracked=false;t.sample({now:500});assert.equal(diagnostic(t).compensations[0].current,null);
  tracked=true;for(let i=6;i<13;i++)t.sample({now:i*100});
  const c=diagnostic(t).compensations[0];assert.equal(c.status,'Detected this step');assert.equal(c.unit,'proxy');assert.equal(c.current,18);
});
test('T3 calibration uses a clear timed window and live diagnostics never add grading samples',()=>{
  const t=new Tracker(config,'right');
  const raw={torso:[0,-1,0],width:.3,screenWidth:.3,shoulderLine:0,neckGap:.2,headPitch:0,arm_elevation:20,elbow_extension:160};
  t.raw=()=>({...raw});
  const frames=Array.from({length:8},(_,i)=>({now:i*300}));
  assert.equal(t.calibrateSeatedTestingWindow(frames.slice(0,7)),false);
  assert.equal(t.calibrateSeatedTestingWindow(frames),true);
  assert.equal(t.trunkLeanBaseline,null,'T3 keeps its existing torso-vector method');
  t.reset({id:'T3-S1',criteria:[{metric:'arm_elevation',target:30}],compensations:['head_drop']});
  raw.arm_elevation=40;raw.headPitch=.3;
  const live=t.postureReadout(t.raw());
  const view=diagnostic(t,true,live);
  assert.equal(view.criteria[0].current,40);assert.equal(view.criteria[0].value,null);
  assert.equal(view.compensations[0].current,18);assert.equal(view.compensations[0].eligible,0);
  t.sample({now:2200});assert.equal(t.currentMetrics.head_drop,live.head_drop);
  raw.headPitch=NaN;assert.equal(t.calibrateSeatedTestingWindow(frames),false);
  assert.match(t.testingReachCalibrationIssue,/Head position/);
});


test('head approaching a raised hand starts a mouth attempt, but mouse/lap posture and speech do not',()=>{
  const {headApproachesRaisedHand,MovementWindow}=require('../testing_mouth.js');
  const input={cue:20,nose:{x:.5,y:.37},hand:{x:.5,y:.39},shoulderY:.4,aspect:4/3};
  assert.equal(headApproachesRaisedHand(input),true);
  assert.equal(headApproachesRaisedHand({...input,hand:{x:.5,y:.7}}),false);
  assert.equal(headApproachesRaisedHand({...input,cue:NaN}),false);
  assert.equal(headApproachesRaisedHand({...input,cue:10}),false);
  assert.equal(headApproachesRaisedHand({...input,hand:{x:.1,y:.39}}),false);
  const w=new MovementWindow(),wrist={x:.5,y:.45},shoulder={x:.5,y:.4},target={x:.5,y:.25};
  w.begin('T3-S2',wrist,shoulder,target);
  assert.equal(w.update({wrist,shoulder,target,headApproach:true,enabled:false,now:0}),false);
  for(let now=50;now<=2000;now+=50)assert.equal(w.update({wrist,shoulder,target,headApproach:true,enabled:true,now}),true);
});
