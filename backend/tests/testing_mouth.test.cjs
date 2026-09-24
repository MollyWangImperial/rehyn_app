const {test}=require('node:test');
const assert=require('node:assert/strict');
const {Tracker}=require('../assessment_quality.js');
const {jointArc}=require('../reach_angles.js');
const {diagnostic,flexionArc}=require('../testing_mouth.js');
const config={version:'rehyn-task-quality-1',compensations:{head_drop:{threshold:15,label:'Head cue'}}};

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
