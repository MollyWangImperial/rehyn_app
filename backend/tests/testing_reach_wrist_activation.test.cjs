// Execute the runner's real contact/phase/freshness functions, not a copy of its geometry.
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const server=fs.readFileSync(path.join(__dirname,'../server.py'),'utf8');
const flow=fs.readFileSync(path.join(__dirname,'../testing_reach_flow.js'),'utf8');
const targets=require('../reach_target.js');
const adaptation=require('../testing_reach.js');
function fixture(){
  const state={time:2000,step:{id:'T1-S1',target:{x:.5,y:.5}},radius:.1,quality:false};
  const c={state,performance:{now:()=>state.time},RehynReachTarget:targets,RehynTestingReach:adaptation,
    testingReachEnabled:()=>true,getCurrentStep:()=>state.step,
    getEffectiveTargetXY:step=>step.target,targetCanvasPoint:(_step,target)=>target,
    effectiveRadius:()=>state.radius,isLapTarget:step=>step.id==='T1-S4',
    lapTargetCalibration:{ready:true},reachFlow:{lastVideo:1,lastVideoAt:2000,stopped:false,waiting:false,
      step:new adaptation.ReachStep({policy:new adaptation.LoweringPolicy(),id:'T1-S1',base:{x:.5,y:.5},lap:{x:.5,y:.8}})},
    video:{currentTime:1,readyState:4,videoWidth:640,videoHeight:480},lastPoseScanTs:2000,
    voiceFinishedAt:1000,reachVoice:{busy:false,failed:false},calibratingAssessment:false,preAssessmentCalibrationReady:true,
    reachCameraStatus:()=>({ready:state.quality}),AFFECTED_SIDE:'right',correctionVoicePlaying:false,
    stepCompleted:false,syncReachCameraQuality:()=>{},handleReachEvent:event=>{state.event=event},
    document:{getElementById:()=>({textContent:''})}};
  function extract(name){const start=server.indexOf('function '+name+'(');assert.ok(start>=0);return server.slice(start,server.indexOf('\n}',start)+2);}
  vm.createContext(c);
  vm.runInContext([
    extract('testingReachWristContact'),extract('checkTarget'),
    flow.slice(flow.indexOf('function reachCameraFresh('),flow.indexOf('function showReachHelp(')),
    flow.slice(flow.indexOf('function reachCanAttempt('),flow.indexOf('async function handleReachEvent(')),
    flow.slice(flow.indexOf('function tickTestingReach('),flow.indexOf('function finishTestingReachStep(')),
  ].join('\n'),c);
  return c;
}
test('centre, every interior radius and boundary activate for either wrist regardless of unrelated grading landmarks',()=>{
  const c=fixture();
  for(const side of ['left','right'])for(const aspect of [16/9,4/3,3/4,9/16]){
    c.AFFECTED_SIDE=side;c.video.videoWidth=480*aspect;c.video.videoHeight=480;
    const wi=side==='left'?15:16;
    const pose=Array.from({length:33},()=>({x:.5,y:.5,visibility:.1,presence:.1}));
    for(const id of ['T1-S1','T1-S2','T1-S3','T1-S4']){
      c.state.step.id=id;
      for(const fraction of [0,.1,.5,.9,1,1.01])for(let a=0;a<2*Math.PI;a+=Math.PI/4){
        pose[wi]={x:.5+Math.cos(a)*.1*fraction/Math.max(aspect,1),y:.5+Math.sin(a)*.1*fraction/Math.max(1/aspect,1),visibility:.99,presence:.99};
        assert.equal(c.reachCanAttempt(),true);
        assert.equal(c.reachContactFrameValid(pose,c.state.time),true);
        assert.equal(c.checkTarget(pose),fraction<=1,`${side} ${aspect} ${id} ${fraction} ${a}`);
        assert.equal(c.reachCanMeasure(),false,'unreliable grading inputs must not be scored');
        assert.equal(c.reachObservation(pose,c.state.time),null);
      }
    }
  }
});
test('missing wrist, frozen camera, incomplete calibration, speech, pause and moving target cannot complete a hit',()=>{
  const c=fixture(),pose=Array.from({length:33},()=>({x:.5,y:.5,visibility:.99,presence:.99}));
  pose[16].visibility=.49;assert.equal(c.checkTarget(pose),false);assert.equal(c.reachContactFrameValid(pose,2000),false);
  pose[16].visibility=.99;pose[16].presence=.49;assert.equal(c.checkTarget(pose),false);
  pose[16].presence=.99;c.state.time=2300;c.lastPoseScanTs=2300;
  assert.equal(c.reachContactFrameValid(pose,2300),false,'no repeated credit for a frozen video frame');
  c.video.currentTime=2;assert.equal(c.reachContactFrameValid(pose,2300),true);
  c.state.time=2600;c.video.currentTime=3;assert.equal(c.reachContactFrameValid(pose,2600),false,'stale pose is rejected');
  c.lastPoseScanTs=2600;c.voiceFinishedAt=0;assert.equal(c.checkTarget(pose),false);
  c.voiceFinishedAt=2500;assert.equal(c.checkTarget(pose),false,'350 ms voice settle gate');
  c.voiceFinishedAt=1000;
  for(const [object,key,value] of [[c,'calibratingAssessment',true],[c,'preAssessmentCalibrationReady',false],
    [c.reachFlow,'stopped',true],[c.reachFlow,'waiting',true],[c.reachVoice,'busy',true],[c.reachVoice,'failed',true],
    [c.reachFlow.step,'phase','lowering']]){
    const old=object[key];object[key]=value;assert.equal(c.reachCanAttempt(),false,key);object[key]=old;
  }
  c.state.step.id='T1-S4';c.lapTargetCalibration.ready=false;assert.equal(c.checkTarget(pose),false);
});
test('RL cannot interrupt a valid centre hold just as the unsuccessful-attempt timer expires',()=>{
  const c=fixture();c.state.quality=true;
  const pose=Array.from({length:33},()=>({x:.5,y:.5,visibility:.99,presence:.99}));
  Object.assign(pose[11],{x:.3,y:.3});Object.assign(pose[12],{x:.7,y:.3});
  Object.assign(pose[23],{x:.4,y:.8});Object.assign(pose[24],{x:.6,y:.8});
  c.reachFlow.step.elapsed=adaptation.CONFIG.attemptMs-1;c.reachFlow.step.last=1950;
  assert.ok(c.reachObservation(pose,2000));c.tickTestingReach(pose,2000);
  assert.equal(c.state.event,undefined);assert.equal(c.reachFlow.step.phase,'attempt');
  assert.equal(c.reachFlow.step.elapsed,adaptation.CONFIG.attemptMs-1);
  pose[16].x=.85;c.state.time=2050;c.lastPoseScanTs=2050;c.video.currentTime=2;
  c.tickTestingReach(pose,2050);assert.equal(c.state.event,'encourage');
});
