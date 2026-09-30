const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');const vm=require('node:vm');const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../server.py'),'utf8');
const start=source.indexOf('function drawOverlay(landmarks){');
// This branch returns before target rendering during calibration, so it can
// exercise the actual renderer with a minimal canvas instead of a webcam.
const end=source.indexOf('  const step = getCurrentStep();',start);
const renderer=source.slice(start,end)+'}';
test('T1 Testing shows the selected arm and visible face landmarks with the ear-width cue',()=>{
 for(const side of ['left','right'])for(const testing of [true,false]){
  const arcs=[],lines=[],utils=[];
  const lm=Array.from({length:33},(_,i)=>({x:i/34,y:.5,visibility:1}));
  const ctx={clearRect(){},save(){},restore(){},beginPath(){},stroke(){},fill(){},moveTo(...p){lines.push({kind:'start',point:p,color:this.strokeStyle})},lineTo(...p){lines.push({kind:'end',point:p,color:this.strokeStyle})},arc(...p){arcs.push({point:p,color:this.fillStyle})}};
  const env={ctx,canvas:{width:680,height:480},LIBRARY_TEST_MODE:testing,tasks:[{id:'T1'}],currentTaskIdx:0,AFFECTED_SIDE:side,
   sideLandmarks:(p,s)=>s==='left'?{shoulder:p[11],elbow:p[13],wrist:p[15]}:{shoulder:p[12],elbow:p[14],wrist:p[16]},
   drawingUtils:{drawLandmarks(){utils.push('landmarks')},drawConnectors(){utils.push('connectors')}},PoseLandmarker:{POSE_CONNECTIONS:[]},
   testingMouthEnabled:()=>false,latestHandLandmarks:null,calibratingAssessment:true,lapStatus:{classList:{add(){}}},lapTargetCalibration:{ready:false}};
  vm.runInNewContext(renderer+';drawOverlay(lm)',{...env,lm});
  if(testing){assert.equal(arcs.length,14);assert.equal(lines.length,6);assert.deepEqual(utils,[]);
   assert.deepEqual(arcs.slice(0,3).map(a=>a.point[0]),(side==='left'?[11,13,15]:[12,14,16]).map(i=>lm[i].x*680));
   assert.deepEqual(arcs.slice(3).map(a=>a.point[0]),Array.from({length:11},(_,i)=>lm[i].x*680));
   assert.deepEqual(lines.slice(-2).map(line=>line.point[0]),[lm[7].x*680,lm[8].x*680]);
   assert.equal(lines[4].color,'#ff6e91');
   assert.equal(arcs[10].point[2],5);
   assert.equal(arcs[11].point[2],5);
  }else{assert.deepEqual(utils,['landmarks','connectors']);assert.equal(arcs.length,0);}
 }
});

test('T1 Testing hides low-confidence facial points and does not draw a misleading ear-width line',()=>{
 const lm=Array.from({length:33},(_,i)=>({x:i/34,y:.5,visibility:1}));
 lm[8].visibility=.2;
 const arcs=[],lines=[];
 const ctx={clearRect(){},save(){},restore(){},beginPath(){},stroke(){},fill(){},moveTo(...p){lines.push(p)},lineTo(...p){lines.push(p)},arc(...p){arcs.push(p)}};
 const env={ctx,canvas:{width:680,height:480},LIBRARY_TEST_MODE:true,tasks:[{id:'T1'}],currentTaskIdx:0,AFFECTED_SIDE:'right',
  sideLandmarks:p=>({shoulder:p[12],elbow:p[14],wrist:p[16]}),drawingUtils:{drawLandmarks(){},drawConnectors(){}},PoseLandmarker:{POSE_CONNECTIONS:[]},
  testingMouthEnabled:()=>false,latestHandLandmarks:null,calibratingAssessment:true,lapStatus:{classList:{add(){}}},lapTargetCalibration:{ready:false}};
 vm.runInNewContext(renderer+';drawOverlay(lm)',{...env,lm});
 assert.equal(arcs.length,13);
 assert.equal(lines.length,4);
 assert.equal(arcs.some(a=>a[0]===lm[8].x*680),false);
});

test('live Testing readout displays the exact shared shoulder and face cues separately',()=>{
 const first=source.indexOf('function drawTestingReachAngles(landmarks, world=latestPoseWorldLandmarks){');
 const last=source.indexOf('function drawOverlay(landmarks){',first);
 const render=source.slice(first,last);
 const elements=new Map();
 const element=id=>elements.get(id)||elements.set(id,{textContent:'',classList:{toggle(){}}}).get(id);
 const calls=[];
 const quality={trunkLeanBaseline:{},compensations:{trunk_lean:{cue_runs:{shoulder:{confirmed_ms:650},face:{confirmed_ms:0}}}},
  trunkLeanReadout(pose,aspect){calls.push([pose,aspect]);return {supported:true,detected:true,faceScale:1.093,cues:{pelvisNormalizedShoulderScale:13.4,pelvisNormalizedFaceScale:8.2}};}};
 const env={document:{getElementById:element},assessmentQuality:quality,LIBRARY_TEST_MODE:true,tasks:[{id:'T1'}],currentTaskIdx:0,
  video:{videoWidth:1280,videoHeight:720},canvas:{width:1280,height:720,clientWidth:640},ctx:{save(){},translate(){},scale(){},restore(){}},
  latestPoseWorldLandmarks:null,RehynReachAngles:{assessmentReadout(){return {}},drawAngleArcs(){}}};
 vm.runInNewContext(render+';drawTestingReachAngles(pose,null)',{...env,pose:[{x:.5,y:.5}]});
 assert.equal(calls.length,1);
 assert.equal(calls[0][1],1280/720);
 assert.equal(element('liveTrunkLeanShoulder').textContent,'13.4°');
 assert.equal(element('liveTrunkLeanFace').textContent,'8.2°');
 assert.equal(element('liveTrunkLeanFaceScale').textContent,'1.0930×');
 assert.equal(element('liveTrunkLeanShoulderState').textContent,'Confirmed for this step (0.65 s)');
 assert.equal(element('liveTrunkLeanFaceState').textContent,'Above threshold now · hold for 0.5 s');
 assert.equal(element('liveTrunkLeanState').textContent,'Trunk lean counted in this step');
});
