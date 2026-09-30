const {test}=require('node:test');
const assert=require('node:assert/strict');
const Q=require('../testing_reach_camera_quality.js');
test('after setup, colour similarity alone is advisory but bad light/angles still pause',()=>{
  const args={now:200,measuredAt:190,image:{issue:'contrast'},frame:{issue:null}};
  assert.equal(Q.runtimeStatus(args).ready,true);
  assert.equal(Q.runtimeStatus(args).advisory,'contrast');
  for(const issue of ['angles','landmarks'])assert.equal(Q.runtimeStatus({...args,frame:{issue}}).ready,false);
  for(const issue of ['dark','bright','backlit','pixels'])assert.equal(Q.runtimeStatus({...args,image:{issue}}).ready,false);
  assert.equal(Q.runtimeStatus({...args,now:1000}).ready,false);
});
function fixture({shirt=[40,100,140],background=[190,180,150],face=[160,125,100]}={}){
  const pose=Array.from({length:33},()=>({x:.5,y:.2,z:0,visibility:.99,presence:.99}));
  for(const [i,x,y] of [[0,.5,.18],[7,.45,.18],[8,.55,.18],[11,.35,.35],[12,.65,.35],
    [13,.3,.5],[14,.7,.5],[15,.34,.72],[16,.66,.72],[23,.42,.74],[24,.58,.74]])Object.assign(pose[i],{x,y});
  const width=320,height=240,data=new Uint8ClampedArray(width*height*4),mask=new Float32Array(width*height);
  for(let y=0;y<height;y++)for(let x=0;x<width;x++){
    const nx=x/width,ny=y/height,head=nx>.435&&nx<.565&&ny>.12&&ny<.28;
    const body=nx>.32&&nx<.68&&ny>=.3;
    const c=head?face:body?shirt:background,i=y*width+x;
    data.set([...c,255],i*4);mask[i]=head||body?1:0;
  }
  const metrics={arm_elevation:25,elbow_extension:120,width:.3,screenWidth:.3,shoulderLine:0,neckGap:.16,torso:[0,-.4,0]};
  const image={data,width,height};
  return {pose,world:structuredClone(pose),metrics,image,mask:{data:mask,width,height}};
}
function inspect(f){return Q.inspectImage(f.image,f.pose,f.mask);}
test('hand-to-mouth preflight also requires the nose and head cue used by its rubric',()=>{
  const f=fixture();Object.assign(f.metrics,{headPitch:0,elbow_flexion:60});
  assert.equal(Q.gradingFrame(f.pose,f.world,f.metrics,'right',true).issue,null);
  f.pose[0].visibility=.5;
  assert.match(Q.gradingFrame(f.pose,f.world,f.metrics,'right',true).detail,/Nose visibility/);
  assert.equal(Q.gradingFrame(f.pose,f.world,f.metrics,'right').issue,null,'T1 retains its existing required points');
  f.pose[0].visibility=.99;f.metrics.headPitch=NaN;
  assert.match(Q.gradingFrame(f.pose,f.world,f.metrics,'right',true).checks.angles.detail,/Head position/);
});
test('lighting, contrast, landmark confidence and available angles report independently',()=>{
  const dark=inspect(fixture({face:[15,15,15]}));
  assert.equal(dark.checks.lighting.ready,false);
  assert.equal(dark.checks.contrast.ready,true);
  const matching=inspect(fixture({shirt:[150,120,110],background:[153,121,112]}));
  assert.equal(matching.checks.lighting.ready,true);
  assert.equal(matching.checks.contrast.ready,false);
  const f=fixture();f.pose[24].visibility=.70;
  const frame=Q.gradingFrame(f.pose,f.world,f.metrics);
  assert.equal(frame.checks.landmarks.ready,false);
  assert.match(frame.checks.landmarks.detail,/Right hip visibility 0.70 < 0.75/);
  assert.equal(frame.checks.angles.ready,true);
  f.pose[24].visibility=.99;f.metrics.elbow_extension=NaN;
  const missing=Q.gradingFrame(f.pose,f.world,f.metrics);
  assert.equal(missing.checks.landmarks.ready,true);
  assert.equal(missing.checks.angles.ready,false);
  assert.match(missing.checks.angles.detail,/Elbow extension/);
});

test('a recovered view retains the exact reset reason and shows each current check',()=>{
  const f=fixture(),image=inspect(f),frame=Q.gradingFrame(f.pose,f.world,f.metrics),g=new Q.Gate();
  for(let now=0;now<=1000;now+=100)g.update({now,image,frame,baselineReady:false});
  f.pose[24].visibility=.70;
  const bad=Q.gradingFrame(f.pose,f.world,f.metrics);
  g.update({now:1100,image,frame:bad,baselineReady:false});
  const recovered=g.update({now:1200,image,frame,baselineReady:false});
  assert.equal(recovered.resetCount,1);
  assert.match(recovered.lastReset.detail,/Right hip visibility 0.70 < 0.75/);
  for(const key of ['lighting','contrast','landmarks','angles'])assert.equal(recovered.checks[key].ready,true,key);
  assert.notEqual(recovered.checks.stability.ready,true);
  assert.notEqual(recovered.checks.baseline.ready,true);
  const stale=g.status(2000);
  for(const check of Object.values(stale.checks))assert.notEqual(check.ready,true);
  for(let now=2100;now<=4300;now+=100)g.update({now,image,frame,baselineReady:true});
  const done=g.status(4300);
  assert.equal(done.ready,true);
  for(const check of Object.values(done.checks))assert.equal(check.ready,true);
});

test('stability reset explains which angle or camera position exceeded its limit',()=>{
  const f=fixture(),image=inspect(f),frame=Q.gradingFrame(f.pose,f.world,f.metrics),g=new Q.Gate();
  for(let now=0;now<=1000;now+=100)g.update({now,image,frame,baselineReady:true});
  const bad=g.update({now:1100,image,frame:{...frame,angles:[25,150]},baselineReady:true});
  assert.equal(bad.checks.landmarks.ready,true);
  assert.equal(bad.checks.stability.ready,false);
  assert.match(bad.checks.stability.detail,/elbow extension varied 30.0° \(limit 18°\)/);
  assert.match(bad.lastReset.detail,/elbow extension/);
});
test('clear front lighting and contrasting shoulders pass pixel checks',()=>{
  const f=fixture();const r=inspect(f);
  assert.equal(r.issue,null);assert.ok(r.contrast.every(v=>v>12));
  assert.equal(Q.gradingFrame(f.pose,f.world,f.metrics).issue,null);
});
test('darkness, overexposure, backlight and matching clothing are different guidance',()=>{
  assert.equal(inspect(fixture({shirt:[10,10,10],face:[15,15,15]})).issue,'dark');
  assert.equal(inspect(fixture({face:[255,255,255]})).issue,'bright');
  assert.equal(inspect(fixture({face:[65,65,65],background:[240,240,240]})).issue,'backlit');
  assert.equal(inspect(fixture({shirt:[150,120,110],background:[153,121,112]})).issue,'contrast');
  assert.match(Q.MESSAGES.contrast,/contrasting clothing colour/);
});
test('a black or white shirt alone is not bad lighting; same brightness different hue can pass',()=>{
  assert.equal(inspect(fixture({shirt:[5,5,5]})).issue,null);
  assert.equal(inspect(fixture({shirt:[250,250,250]})).issue,null);
  assert.equal(inspect(fixture({shirt:[50,100,170],background:[170,90,50]})).issue,null);
});
test('pixel, segmentation and cropped background failures cannot silently pass',()=>{
  const f=fixture();assert.equal(Q.inspectImage(null,f.pose,f.mask).issue,'pixels');
  assert.equal(Q.inspectImage(f.image,f.pose,null).issue,'segmentation');
  f.mask.data.fill(1);assert.equal(inspect(f).issue,'segmentation');
});
test('T3 shoulder-rise quality does not require an ear gap or model ear coordinates',()=>{
  const f=fixture();f.metrics.headPitch=0;f.metrics.elbow_flexion=60;
  delete f.metrics.neckGap;f.world[7]=null;f.world[8]=null;
  assert.equal(Q.gradingFrame(f.pose,f.world,f.metrics,'right',true).issue,null);
  assert.ok(Q.gradingFrame(f.pose,f.world,f.metrics,'right').issue,'T1 still uses its existing ear-gap check');
  f.pose[8].visibility=.2;
  assert.equal(Q.gradingFrame(f.pose,f.world,f.metrics,'right',true).issue,'landmarks','The separate face/head checks still need visible ears');
});

test('each required grading landmark and either angle can block start, on both sides',()=>{
  for(const side of ['left','right'])for(const i of [7,8,11,12,23,24,...(side==='left'?[13,15]:[14,16])]){
    const f=fixture();f.pose[i].visibility=.4;
    assert.equal(Q.gradingFrame(f.pose,f.world,f.metrics,side).issue,'landmarks');
  }
  for(const k of ['arm_elevation','elbow_extension','neckGap','shoulderLine']){
    const f=fixture();f.metrics[k]=NaN;
    assert.equal(Q.gradingFrame(f.pose,f.world,f.metrics).issue,'angles');
  }
  const f=fixture();f.pose[14]={...f.pose[16]};
  assert.equal(Q.gradingFrame(f.pose,f.world,f.metrics).issue,'angles');
});
test('clear camera window builds alongside the reference; missing reference never permits starting',()=>{
  const f=fixture(),g=new Q.Gate(),image=inspect(f),frame=Q.gradingFrame(f.pose,f.world,f.metrics);
  for(let now=0;now<=10000;now+=100)assert.equal(g.update({now,image,frame,baselineReady:false}).ready,false);
  assert.equal(g.clearReady,true);
  assert.equal(g.update({now:10100,image,frame,baselineReady:true}).ready,true);
  assert.equal(g.status(12700).ready,false);
  assert.equal(g.status(12700).issue,'stale');
});

test('low-FPS quality windows retain the two-second boundary and enough distinct frames',()=>{
  const f=fixture(),image=inspect(f),frame=Q.gradingFrame(f.pose,f.world,f.metrics);
  for(const interval of [240,300,400]){
    const g=new Q.Gate();let readyAt=null;
    for(let now=0;now<3000;now+=interval){
      const status=g.update({now,image,frame,baselineReady:true});
      if(status.ready){readyAt=now;break;}
    }
    assert.ok(readyAt>=2000 && readyAt<=2800);
    assert.ok(g.frames.at(-1).now-g.frames[0].now>=2000);
    assert.ok(g.frames.length>=8);
  }
});
test('lighting loss, unstable angles, tracking gaps and repeated frozen frames reset the hold',()=>{
  const f=fixture(),image=inspect(f),frame=Q.gradingFrame(f.pose,f.world,f.metrics);
  for(const bad of [{image:{issue:'dark'}},{frame:{issue:'landmarks'}}]){
    const g=new Q.Gate();for(let now=0;now<=2100;now+=100)g.update({now,image,frame,baselineReady:true});
    assert.equal(g.ready,true);assert.equal(g.update({now:2200,image,frame,baselineReady:true,...bad}).ready,false);
    assert.equal(g.update({now:2300,image,frame,baselineReady:true}).ready,false);
  }
  const g=new Q.Gate();for(let now=0;now<=2100;now+=100)g.update({now,image,frame,baselineReady:true});
  assert.equal(g.update({now:2200,image,frame:{...frame,angles:[60,60]},baselineReady:true}).issue,'unstable');
  assert.equal(g.update({now:4000,image,frame,baselineReady:true}).ready,false);
  const frozen=new Q.Gate();for(let n=0;n<100;n++)frozen.update({now:100,image,frame,baselineReady:true});
  assert.equal(frozen.ready,false);
});
