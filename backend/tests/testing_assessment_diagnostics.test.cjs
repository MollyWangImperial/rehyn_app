const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const diagnostics = require('../testing_assessment_diagnostics.js');

function hand() {
  const points = Array.from({length:21}, () => ({x:.5,y:.5,z:0}));
  points[5] = {x:.4,y:.5,z:0};
  points[17] = {x:.6,y:.5,z:0};
  points[4] = {x:.45,y:.2,z:0};
  points[8] = {x:.55,y:.2,z:0};
  for (const [mcp,pip,dip,tip] of [[5,6,7,8],[9,10,11,12],[13,14,15,16],[17,18,19,20]]) {
    points[pip] = {x:points[mcp].x,y:.4,z:0};
    points[dip] = {x:points[mcp].x,y:.3,z:0};
    points[tip] = {x:points[mcp].x,y:.2,z:0};
  }
  points[8] = {x:.55,y:.2,z:0};
  return points;
}

test('four non-T1/T3 camera tasks have read-only diagnostics using rubric criteria', () => {
  assert.deepEqual([...diagnostics.TASKS], ['T2','H1','H3','H4']);
  const h = hand();
  for (const taskId of diagnostics.TASKS) {
    const metric = taskId === 'T2' ? 'arm_elevation' : taskId === 'H3' ? 'pinch' : 'hand_open';
    const result = diagnostics.readout(taskId,{criteria:[{metric,label:metric,target:.8,unit:'ratio'}],compensations:['trunk_lean']},
      {[metric]:.6,trunk_lean:8},h);
    assert.equal(result.criteria[0].value,.6);
    assert.equal(result.criteria[0].target,.8);
    assert.equal(result.posture[0].value,8);
    assert.ok(result.hand.pinchGap>0);
  }
  assert.equal(diagnostics.readout('T1',{}, {},h),null);
  assert.equal(diagnostics.readout('T3',{}, {},h),null);
});

test('finger angle and pinch gap abstain when landmarks are unavailable', () => {
  assert.equal(diagnostics.angle3D({x:0,y:0,z:0},{x:1,y:0,z:0},{x:2,y:0,z:0}),180);
  assert.equal(diagnostics.handReadout(null),null);
  assert.equal(diagnostics.handReadout(hand().slice(0,10)),null);
  assert.equal(diagnostics.readout('H3',{criteria:[],compensations:[]},null,null).hand,null);
});

test('Testing runner loads the diagnostic overlay without replacing the T1 overlay', () => {
  const server = fs.readFileSync(path.join(__dirname,'../server.py'),'utf8');
  const panel = fs.readFileSync(path.join(__dirname,'../testing_reach_ui.html'),'utf8');
  assert.match(server,/drawTestingReachAngles\(anglesFresh/);
  assert.match(server,/drawTestingAssessmentAngles\(anglesFresh/);
  assert.match(server,/testing_assessment_diagnostics\.js/);
  assert.match(panel,/id="testingAssessmentAngles"/);
  assert.match(server,/id="testingReachAngles"/);
});

test('live runner panel renders each task with its scored metric and contextual angles', () => {
  const server = fs.readFileSync(path.join(__dirname,'../server.py'),'utf8');
  const first = server.indexOf('function drawTestingAssessmentAngles(');
  const last = server.indexOf('function drawOverlay(landmarks){',first);
  const render = server.slice(first,last);
  for (const taskId of diagnostics.TASKS) {
    const nodes = new Map();
    const element = id => nodes.get(id) || nodes.set(id,{textContent:'',classList:{toggle(){}}}).get(id);
    const metric = taskId === 'T2' ? 'arm_elevation' : taskId === 'H3' ? 'pinch' : 'hand_open';
    const sample = {[metric]:.6,arm_elevation:84,elbow_extension:156,hand_open:.66,
      hand_closed:.7,pinch:.6,open_close_cycle:1,trunk_lean:8};
    const ctx = {save(){},restore(){},translate(){},scale(){},beginPath(){},moveTo(){},lineTo(){},stroke(){},arc(){}};
    const env = {document:{getElementById:element},LIBRARY_TEST_MODE:true,tasks:[{id:taskId,title:taskId}],currentTaskIdx:0,
      latestPoseWorldLandmarks:null,latestHandLandmarks:hand(),latestHandSeenAt:1050,
      assessmentQuality:{currentMetrics:sample,lastTime:1000,rubric:{criteria:[{metric,label:metric,target:.8,unit:'ratio'}],
        compensations:['trunk_lean']},compensations:{trunk_lean:{active:false}}},
      performance:{now(){return 1100}},video:{videoWidth:1280,videoHeight:720},
      canvas:{width:1280,height:720,clientWidth:640},ctx,getCurrentStep(){return {caption:'Attempt'}},
      RehynTestingAssessmentDiagnostics:diagnostics,
      RehynReachAngles:{assessmentReadout(){return {tracking:false}},drawAngleArcs(){}},
      window:{REHYN_ASSESSMENT_RUBRIC:{compensations:{trunk_lean:{label:'Trunk lean',threshold:12}}}}};
    vm.runInNewContext(render+';drawTestingAssessmentAngles(null,null)',env);
    assert.match(element('testingAssessmentCriteria').textContent,new RegExp(metric));
    assert.match(element('testingAssessmentPosture').textContent,/Trunk lean/);
    if (taskId === 'T2') assert.match(element('testingAssessmentAnglesText').textContent,/Arm elevation/);
    if (taskId === 'H3') assert.match(element('testingAssessmentAnglesText').textContent,/Thumb-index gap/);
    if (taskId === 'H1' || taskId === 'H4') assert.match(element('testingAssessmentAnglesText').textContent,/PIP \/ DIP/);
  }
});
