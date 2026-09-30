"""Execute the real embedded runner functions against synthetic camera landmarks."""
import json
import os
from pathlib import Path
import re
import subprocess

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "rehyn_reach_geometry_test")
from backend import server


def test_patient_relative_target_calibration_drawing_and_hits():
    names = [
        "seatedTestingCalibrationEnabled",
        "distance", "midpoint", "mirrorX", "sideLandmarks", "landmarkIsUsable", "landmarkIsInFrame",
        "medianValue", "shoulderWidth", "isLapTarget", "isMouthTarget", "currentTaskLapStep", "upcomingLapStep",
        "newLapTargetCalibration", "lapWristZoneReason", "lapTargetCandidateStatus", "lapTargetCandidate",
        "updateLapTargetCalibration", "needsForwardReachPlacement", "isForwardReachTarget",
        "calculateForwardReachPlacement", "forwardReachDistance", "getEffectiveTargetXY", "targetCanvasPoint",
        "effectiveRadius", "affectedPoseHandPoints", "affectedReachContactPoints", "closestAffectedReachPointToTarget",
    ]
    functions = []
    for name in names:
        start = re.search(r"^function " + name + r"\(", server.POSE_RUNNER_HTML, re.M).start()
        first_line = server.POSE_RUNNER_HTML[start:].splitlines()[0]
        end = start + len(first_line) if first_line.endswith("}") else server.POSE_RUNNER_HTML.index("\n}", start) + 2
        functions.append(server.POSE_RUNNER_HTML[start:end])
    shared = (Path(server.ROOT_DIR) / "reach_target.js").read_text(encoding="utf-8")
    script = shared + "\n" + "\n".join(functions) + r'''
const assert = require('node:assert/strict');
const tasks = [{id:'T1',steps:[{id:'T1-S4',target:{x:.5,y:.8,landmark:'LAP_DYNAMIC',r:.1}}]}];
let AFFECTED_SIDE='right', currentTaskIdx=0, calibratingAssessment=true;
let lapTargetCalibration, forwardReachPlacement, lapCalibrationDiagnostic={}, dynamicTargetPos=null;
let assessmentLapTarget=null, assessmentLapTargetRadius=null;
function testingReachEnabled(){return false;}
function testingMouthEnabled(){return false;}
const video={videoWidth:640,videoHeight:480};
const LAP_CALIBRATION_MIN_SAMPLES=8,LAP_CALIBRATION_MIN_MS=650;
const postRN=()=>{};
function pose(offset=0){
 const lm=Array.from({length:33},()=>({x:.5+offset,y:.2,visibility:1}));
 for(const [i,x,y] of [[11,.65,.3],[12,.4,.3],[23,.62,.76],[24,.42,.76],[15,.62,.8],[16,.42,.8]]) lm[i]={x:x+offset,y,visibility:1};
 return lm;
}
function calibrate(lm,start=0){for(let now=start;now<=start+1200;now+=50)updateLapTargetCalibration(lm,now);}
for(const side of ['right','left']){
 AFFECTED_SIDE=side; lapTargetCalibration=newLapTargetCalibration(); forwardReachPlacement=null;
 const lm=pose(); calibrate(lm);
 assert.equal(lapTargetCalibration.ready,true); assert.equal(forwardReachPlacement.ready,true);
 const radiusX=forwardReachPlacement.radiusX;
 const affected=sideLandmarks(lm);
 const start=forwardReachPlacement.start, raised=forwardReachPlacement.raised;
 assert.ok(start.x-radiusX>1-affected.wrist.x); assert.equal(start.y,affected.wrist.y);
 assert.ok(raised.x-radiusX>1-affected.shoulder.x); assert.equal(raised.y,affected.shoulder.y);
 assert.ok(Math.abs((start.x-(1-affected.wrist.x))-(raised.x-(1-affected.shoulder.x)))<1e-12);
 assert.notEqual(start.x,raised.x); // Lap and shoulder deliberately have different X anchors.
 for(const id of ['T1-S1','T1-S2','T1-S3']){
   const step={id,target:{x:.64,y:id==='T1-S1'?.6:.4,r:.1,landmark:'WRIST'}};
   const target=getEffectiveTargetXY(step); const expected=id==='T1-S1'?start:raised;
   assert.deepEqual(target,expected);
   const drawn=targetCanvasPoint(step,target); assert.ok(Math.abs(1-drawn.x-target.x)<1e-12);
   const moving=pose(); const ids=side==='right'?[16,18,20,22]:[15,17,19,21];
   ids.forEach(i=>moving[i]={x:1-target.x,y:target.y,visibility:1});
   assert.equal(forwardReachDistance(closestAffectedReachPointToTarget(moving,target),target),0);
   const fixedRadius=effectiveRadius(step,moving);
   moving[side==='right'?12:11].x+=.1; // Moving arm/shoulder cannot drag the locked target.
   updateLapTargetCalibration(moving,1800); assert.deepEqual(getEffectiveTargetXY(step),expected);
   ids.forEach(i=>moving[i].x=1-(target.x-radiusX*1.1));
   assert.ok(forwardReachDistance(closestAffectedReachPointToTarget(moving,target),target)>fixedRadius);
 }
 assert.deepEqual(getEffectiveTargetXY(tasks[0].steps[0]),lapTargetCalibration.target);
 assert.notDeepEqual(start,lapTargetCalibration.target); // Return target is not shifted with start.
}
AFFECTED_SIDE='right';lapTargetCalibration=newLapTargetCalibration();forwardReachPlacement=null;
calibrate(pose(-.31));
assert.equal(lapTargetCalibration.ready,false);assert.equal(lapCalibrationDiagnostic.reason,'need_room_right');
assert.equal(getEffectiveTargetXY({id:'T1-S1',target:{x:.64,y:.6}}),null);
calibrate(pose(),1300);assert.equal(lapTargetCalibration.ready,true);assert.equal(forwardReachPlacement.ready,true);
// A noisy final shoulder frame should not drag the anatomical raised target.
lapTargetCalibration=newLapTargetCalibration();forwardReachPlacement=null;
for(let now=0;now<=650;now+=50){ const lm=pose(); if(now===650){lm[12].x+=.025;lm[12].y+=.02;} updateLapTargetCalibration(lm,now); }
assert.equal(lapTargetCalibration.ready,true);assert.equal(forwardReachPlacement.raised.y,.3);
// Shoulder-relative movement follows a different calibrated posture, not fixed .4/.6 heights.
const changed=pose();changed[12].y=.38;changed[16].y=.72;changed[24].y=.72;
const shifted=calculateForwardReachPlacement(changed,changed[16]);
assert.equal(shifted.ready,true);assert.equal(shifted.start.y,.72);assert.equal(shifted.raised.y,.38);
console.log(JSON.stringify({bothArms:true,separateAnchors:true,calibration:true,reframing:true,locked:true,mirroring:true,hits:true,noisyShoulder:true}));
'''
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert all(json.loads(result.stdout).values())
