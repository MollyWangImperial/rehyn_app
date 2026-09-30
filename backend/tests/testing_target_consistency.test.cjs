const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../server.py'),'utf8').replaceAll('\r\n','\n');
const mouth=fs.readFileSync(path.join(__dirname,'../testing_mouth_flow.js'),'utf8');
const targets=require('../reach_target.js');
function fn(text,name){const start=text.indexOf('function '+name+'(');assert.ok(start>=0);return text.slice(start,text.indexOf('\n}',start)+2);}
function fixture(tid){
  const operations=[],step={id:tid+'-S1',target:{x:.5,y:.4,r:.1,landmark:'CHEST'},hold_ms:1000};
  let arc;
  const ctx={clearRect(){},save(){},restore(){},beginPath(){},moveTo(){},lineTo(){},translate(){},scale(){},setLineDash(value){this.dash=value;},
    arc(...args){arc=args;},stroke(){operations.push({arc,color:this.strokeStyle,width:this.lineWidth,dash:this.dash});},
    fill(){operations.push({arc,fill:this.fillStyle});}};
  const c={ctx,operations,step,canvas:{width:640,height:480},LIBRARY_TEST_MODE:true,tasks:[{id:tid}],currentTaskIdx:0,
    assessmentQuality:{draw(){}},latestHandLandmarks:null,calibratingAssessment:false,stepCompleted:false,voiceFinishedAt:1000,
    targetCompletion:null,targetMotionPreference:{matches:false},running:true,
    inTargetSince:null,lastInTargetTs:0,now:2000,enabled:true,fresh:true,contact:false,correctionVoicePlaying:false,
    performance:{now:()=>c.now},testingReachEnabled:()=>tid==='T1',testingMouthEnabled:()=>tid==='T3',handToMouthTaskEnabled:()=>tid==='T3',
    reachCanAttempt:()=>c.enabled,mouthCanMeasure:()=>c.enabled,reachContactFrameValid:()=>c.fresh,mouthContactFrameValid:()=>c.fresh,
    checkTarget:()=>c.contact,getCurrentStep:()=>step,getEffectiveTargetXY:()=>step.target,targetCanvasPoint:(_s,p)=>p,
    effectiveRadius:()=>.1,isWalkingTask:()=>false,isLapTarget:()=>false,isMouthTarget:()=>false,
    lapStatus:{classList:{add(){}}},celebrateEl:{classList:{contains:()=>false}},RehynReachTarget:targets};
  vm.createContext(c);vm.runInContext(fn(source,'testingTargetState')+'\n'+fn(source,'completeMovementTarget')+'\n'+fn(source,'drawOverlay'),c);
  c.draw=()=>{operations.length=0;c.drawOverlay(null);return structuredClone(operations);};
  return c;
}
test('T1 and T3 render identical fixed boundaries, contact colours, inset progress and transitions',()=>{
  const a=fixture('T1'),b=fixture('T3');
  for(const state of [{},{contact:true,inTargetSince:1500},{contact:false},{enabled:false},{enabled:true,fresh:false},
    {fresh:true,stepCompleted:true},{stepCompleted:false,voiceFinishedAt:0}]){
    Object.assign(a,state);Object.assign(b,state);assert.deepEqual(a.draw(),b.draw());
  }
  Object.assign(a,{voiceFinishedAt:1000,contact:true,inTargetSince:1500});
  const draw=a.draw();assert.equal(draw[0].color,'#7FE5A3');assert.equal(draw[0].width,6);
  assert.equal(draw[0].arc[2],48);assert.equal(draw[2].fill,'#fff');
  assert.equal(draw[3].arc[2],48*.82);assert.equal(draw[3].color,'#3C8255');assert.equal(draw[3].width,8);
  a.now=2120;assert.equal(a.draw()[0].arc[2],48,'No pulsing or expansion of the hit boundary');
  a.contact=false;assert.equal(a.draw()[0].color,'#E18E6D');
  a.enabled=false;assert.equal(a.draw().length,4,'Paused targets retain only a muted breath, with no hold progress');
  a.stepCompleted=true;assert.deepEqual(a.draw(),[]);
  a.stepCompleted=false;a.voiceFinishedAt=0;
  const preview=a.draw();assert.equal(preview.length,4);assert.equal(preview[0].color,'rgba(225,142,109,0.45)');
  assert.deepEqual(preview[0].dash,[10,8]);assert.equal(a.testingTargetState(null,a.now).armed,false);
});
test('T3 counts centre, interior and edge at chest, mouth and lap, with correct side and aspect ratio',()=>{
  for(const side of ['left','right'])for(const aspect of [16/9,3/4])for(const name of ['CHEST','MOUTH','LAP_DYNAMIC']){
    const wi=side==='left'?15:16,fi=side==='left'?19:20;
    const pose=Array.from({length:33},()=>({x:.5,y:.4,visibility:.1,presence:.1}));
    const step={id:'T3-S1',target:{x:.5,y:.4,landmark:name}};
    const c={performance:{now:()=>2000},voiceFinishedAt:1000,testingReachEnabled:()=>false,testingMouthEnabled:()=>true,handToMouthTaskEnabled:()=>true,
      getCurrentStep:()=>step,isLapTarget:()=>name==='LAP_DYNAMIC',isMouthTarget:()=>name==='MOUTH',
      lapTargetCalibration:{ready:true},mouthTargetCalibration:{locked:true},getEffectiveTargetXY:()=>step.target,
      targetCanvasPoint:(_s,p)=>p,effectiveRadius:()=>.1,RehynReachTarget:targets,AFFECTED_SIDE:side,
      video:{videoWidth:480*aspect,videoHeight:480},affectedMouthContactPoints:p=>p?[wi,fi].map(i=>p[i]).filter(p=>p.visibility>=.65):[]};
    vm.createContext(c);vm.runInContext(fn(mouth,'testingMouthContact')+'\n'+fn(source,'checkTarget'),c);
    const index=name==='MOUTH'?fi:wi;
    for(const fraction of [0,.5,1,1.001])for(const angle of [0,Math.PI/2,Math.PI,Math.PI*1.5]){
      pose[index]={x:.5+Math.cos(angle)*.1*fraction/Math.max(1,aspect),y:.4+Math.sin(angle)*.1*fraction/Math.max(1,1/aspect),visibility:.99,presence:.99};
      assert.equal(c.checkTarget(pose),fraction<=1,`${side} ${aspect} ${name} ${fraction}`);
    }
    pose[index].visibility=.1;pose[index].presence=.1;pose[side==='left'?16:15]={...step.target,visibility:1,presence:1};
    assert.equal(c.checkTarget(pose),false,'The unaffected hand cannot activate');
    assert.equal(c.checkTarget(null),false);
  }
});
test('both tasks use the same hold continuity, 350 ms edge grace and completion delay',()=>{
  const start=source.indexOf('  if(testingTarget && !testingTarget.armed)');
  const end=source.indexOf('\n  requestAnimationFrame(loop);\n}',start);
  const code='function tick(){'+source.slice(start,end)+'}';
  for(const tid of ['T1','T3']){
    const c=fixture(tid),transitions=[];
    Object.assign(c,{testingTarget:{armed:true},inTarget:true,nearMissStartedAt:null,nearMissReason:'',landmarks:null,
      navigator:{vibrate(){}},setTimeout:(_f,ms)=>transitions.push(ms),nextStep(){},requestAnimationFrame(){},loop(){},handleTargetNearMiss(){}});
    vm.runInContext(code,c);
    c.now=100;c.tick();assert.equal(c.inTargetSince,100);
    c.now=300;c.tick();c.inTarget=false;c.now=600;c.tick();assert.equal(c.inTargetSince,100);
    c.now=651;c.tick();assert.equal(c.inTargetSince,null,'Longer departures reset');
    c.inTarget=true;c.now=700;c.tick();c.now=1600;c.tick();assert.equal(c.stepCompleted,false);
    c.now=1700;c.tick();assert.equal(c.stepCompleted,true);assert.deepEqual(transitions,[targets.completionDurationMs]);
    c.testingTarget.armed=false;c.tick();assert.equal(c.inTargetSince,null,'Pausing clears progress');
  }
});

test('breathing changes the light, not the contact edge, and respects reduced motion',()=>{
  const c=fixture('T1');
  c.now=2800;const first=c.draw();c.now=4200;const second=c.draw();
  assert.deepEqual(first[0],second[0],'The hit boundary remains fixed');
  assert.notDeepEqual(first.slice(-2),second.slice(-2),'The decorative halo visibly breathes');
  c.targetMotionPreference.matches=true;
  c.now=2800;const still=c.draw();c.now=4200;assert.deepEqual(c.draw(),still);
});

test('completion freezes the reached circle, plays once, retires it, and cannot advance after exit or a new step',()=>{
  for(const tid of ['T1','T3']){
    const c=fixture(tid),timers=[];let advances=0;
    Object.assign(c,{navigator:{vibrate(){}},setTimeout:f=>timers.push(f),nextStep:()=>advances++});
    c.stepCompleted=true;c.completeMovementTarget(c.step,null,c.now);
    c.now+=300;const completed=c.draw();
    assert.equal(completed[1].color,'#7FE5A3');
    c.step.target.x=.8;c.step.target.y=.6;
    assert.deepEqual(c.draw(),completed,'Completion stays where the patient reached it');
    c.now+=targets.completionDurationMs;assert.deepEqual(c.draw(),[],'Completed circle disappears');
    timers.shift()();assert.equal(advances,1);
    c.completeMovementTarget(c.step,null,c.now);c.running=false;
    timers.shift()();assert.equal(advances,1,'Exit cancels pending advancement');
    c.running=true;c.completeMovementTarget(c.step,null,c.now);c.targetCompletion=null;
    timers.shift()();assert.equal(advances,1,'A replaced step cancels stale advancement');
  }
});


test('mouth hand model activates with fingers above the wrist, rejects the other hand and expired frames',()=>{
  for(const side of ['left','right']) {
    const wi=side==='left'?15:16,oi=side==='left'?16:15;
    const pose=Array.from({length:33},()=>({x:.25,y:.5,visibility:.99}));
    pose[wi]={x:.5,y:.48,visibility:.99};pose[oi]={x:.2,y:.4,visibility:.99};
    pose[11]={x:.3,y:.35,visibility:.99};pose[12]={x:.7,y:.35,visibility:.99};
    const step={target:{x:.5,y:.3,landmark:'MOUTH'}};
    const c={AFFECTED_SIDE:side,latestPoseLandmarks:pose,latestHandLandmarks:null,latestHandSeenAt:1000,now:1100,
      performance:{now:()=>c.now},latestHandedness:'',affectedHandTrackWrist:null,affectedHandTrackSeenAt:0,
      testingMouthEnabled:()=>true,handToMouthTaskEnabled:()=>true,isMouthTarget:()=>true,getCurrentStep:()=>step,getEffectiveTargetXY:()=>step.target,
      targetCanvasPoint:(_s,p)=>p,video:{videoWidth:640,videoHeight:480},RehynReachTarget:targets,
      landmarkIsUsable:(p,v)=>!!p&&p.visibility>=v,handLandmarkFreshMs:()=>220,
      sideLandmarks:(p,s)=>({wrist:p[s==='left'?15:16]}),affectedPoseHandPoints:()=>[pose[wi]],
      distance:(a,b)=>Math.hypot(a.x-b.x,a.y-b.y)};
    vm.createContext(c);vm.runInContext(['anatomicalHandedness','selectAffectedHandDetection','affectedMouthContactPoints','rawHandPalmCenter'].map(n=>fn(source,n)).join('\n')+'\n'+fn(mouth,'testingMouthContact'),c);
    const hand=Array.from({length:21},()=>({x:.5,y:.45}));hand[0]={...pose[wi]};hand[8]={...step.target};
    const other=hand.map(p=>({...p}));other[0]={...pose[oi]};
    const selected=c.selectAffectedHandDetection({landmarks:[other,hand],handednesses:[[{categoryName:side==='left'?'Left':'Right',score:.99}],[{categoryName:side==='left'?'Right':'Left',score:.99}]]},1000);
    assert.equal(selected.index,1);c.latestHandLandmarks=selected.landmarks;
    assert.equal(c.testingMouthContact(pose).distance,0,'Fingertip in centre without moving wrist up');
    c.now=1300;assert.ok(c.testingMouthContact(pose).distance>.1,'Expired hand landmarks do not activate');
    assert.equal(c.selectAffectedHandDetection({landmarks:[other]},1300),null,'Other hand at mouth rejected');
  }
});


test('normal and Testing T3 share the canonical chest, mouth and lap geometry',()=>{
  const definitions=[{id:'T3-S1',target:{x:.5,y:.48,r:.11,landmark:'CHEST'},hold_ms:1000},
    {id:'T3-S2',target:{x:.5,y:.3,r:.1,landmark:'MOUTH'},hold_ms:1500},
    {id:'T3-S3',target:{x:.5,y:.3,r:.1,landmark:'MOUTH'},hold_ms:1500},
    {id:'T3-S4',target:{x:.5,y:.78,r:.1,landmark:'LAP_DYNAMIC'},hold_ms:1500}];
  const expected=[{x:.5,y:.48,r:.175},{x:.48,y:.23,r:.12},{x:.48,y:.23,r:.12},{x:.65,y:.73,r:.13}];
  for(const testing of [false,true]) {
    const c={testingReachEnabled:()=>false,testingMouthEnabled:()=>testing,handToMouthTaskEnabled:()=>true,
      isLapTarget:s=>s.target.landmark==='LAP_DYNAMIC',isForwardReachTarget:()=>false,isHandTask:()=>false,
      isCenteredArmStartStep:s=>s.id==='T3-S1',updateMouthTargetCalibration:()=>({x:.48,y:.23}),latestPoseLandmarks:[],
      assessmentLapTarget:{x:.65,y:.73},assessmentLapTargetRadius:.13,shoulderWidth:()=>.25,
      mouthFlow:{chestTarget:{x:.1,y:.2},chestRadius:.01}};
    vm.createContext(c);vm.runInContext(fn(source,'getEffectiveTargetXY')+'\n'+fn(source,'effectiveRadius'),c);
    definitions.forEach((step,i)=>{assert.deepEqual(JSON.parse(JSON.stringify(c.getEffectiveTargetXY(step))),{x:expected[i].x,y:expected[i].y});
      assert.ok(Math.abs(c.effectiveRadius(step,[])-expected[i].r)<1e-9);});
  }
  const normal=fixture('T3'),testing=fixture('T3');normal.LIBRARY_TEST_MODE=false;normal.testingMouthEnabled=()=>false;
  for(const state of [{voiceFinishedAt:0},{voiceFinishedAt:1000},{contact:true,inTargetSince:1500},{stepCompleted:true}]) {
    Object.assign(normal,state);Object.assign(testing,state);assert.deepEqual(normal.draw(),testing.draw());
  }
});
