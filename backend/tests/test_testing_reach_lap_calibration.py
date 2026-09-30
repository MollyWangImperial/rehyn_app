"""Exercise the actual Testing reach calibration against synthetic camera frames."""
import os
import re
import subprocess
import pytest
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "rehyn_testing_reach_lap_calibration_test")

from backend import server


@pytest.mark.parametrize("task_id", ["T1", "T3"])
def test_testing_reach_locks_a_stable_wrist_after_two_seconds_without_hip_or_face(task_id):
    names = [
        "seatedTestingCalibrationEnabled",
        "distance", "midpoint", "mirrorX", "sideLandmarks", "landmarkIsUsable",
        "landmarkIsInFrame", "medianValue", "shoulderWidth", "isLapTarget",
        "currentTaskLapStep", "upcomingLapStep", "newLapTargetCalibration",
        "lapWristZoneReason", "lapTargetCandidateStatus", "lapTargetCandidate",
        "updateLapTargetCalibration", "updateTestingReachPlacement",
        "updateTestingReachLapCalibration", "needsForwardReachPlacement",
        "calculateForwardReachPlacement", "calibrationLandmarkStatus",
    ]
    functions = []
    for name in names:
        match = re.search(r"^function " + name + r"\(", server.POSE_RUNNER_HTML, re.M)
        assert match, name
        start = match.start()
        first_line = server.POSE_RUNNER_HTML[start:].splitlines()[0]
        end = start + len(first_line) if first_line.endswith("}") else server.POSE_RUNNER_HTML.index("\n}", start) + 2
        functions.append(server.POSE_RUNNER_HTML[start:end])
    shared = (Path(server.ROOT_DIR) / "reach_target.js").read_text(encoding="utf-8")
    script = shared + "\n" + "\n".join(functions) + r'''
const assert=require('node:assert/strict');
const tasks=[{id:'T1',steps:[{id:'T1-S4',target:{landmark:'LAP_DYNAMIC'}}]}];
let AFFECTED_SIDE='right',currentTaskIdx=0,calibratingAssessment=true;
let lapTargetCalibration,forwardReachPlacement,lapCalibrationDiagnostic={},dynamicTargetPos=null;
const TESTING_REACH_LAP_MIN_MS=2000,LAP_CALIBRATION_MIN_SAMPLES=8,LAP_CALIBRATION_MIN_MS=650;
const video={readyState:2,videoWidth:640,videoHeight:480};
const events=[];function postRN(event){events.push(event);}
function testingReachEnabled(){return true;}
function testingMouthEnabled(){return false;}
function pose(wristX=.42){
 const lm=Array.from({length:33},()=>({x:.5,y:.5,visibility:0}));
 lm[11]={x:.65,y:.3,visibility:1};lm[12]={x:.4,y:.3,visibility:1};
 lm[16]={x:wristX,y:.76,visibility:1};
 return lm;
}
function reset(){lapTargetCalibration=newLapTargetCalibration();forwardReachPlacement=null;events.length=0;}
reset();
const lm=pose();
assert.equal(lapTargetCandidateStatus(lm).candidate.x,.42);
for(let now=0;now<2000;now+=50)updateLapTargetCalibration(lm,now);
assert.equal(lapTargetCalibration.ready,false);
assert.match(lapCalibrationDiagnostic.guidance,/of 2 seconds/);
updateLapTargetCalibration(lm,2000);
assert.equal(lapTargetCalibration.ready,true);
assert.equal(lapTargetCalibration.target.x,.42);
assert.equal(lapTargetCalibration.target.y,.76);
assert.equal(forwardReachPlacement.ready,true);
assert.equal(events.length,1);
assert.equal(events[0].type,'lap_target_calibrated');
const faceAndArm=pose();faceAndArm[0]={x:.5,y:.15,visibility:1};
faceAndArm[14]={x:.42,y:.5,visibility:1};
assert.equal(calibrationLandmarkStatus(faceAndArm).ready,true);
// The wrist can be located before shoulders are available; placement retries
// later without shifting the locked lap point.
reset();const shoulderHidden=pose();shoulderHidden[11].visibility=0;shoulderHidden[12].visibility=0;
for(let now=0;now<=2000;now+=50)updateLapTargetCalibration(shoulderHidden,now);
assert.equal(lapTargetCalibration.ready,true);
assert.equal(forwardReachPlacement,null);
assert.equal(lapCalibrationDiagnostic.reason,'shoulders_not_visible');
updateLapTargetCalibration(pose(),2050);
assert.equal(forwardReachPlacement.ready,true);
assert.equal(lapTargetCalibration.target.x,.42);
// A hand moving within the two-second interval cannot set a lap point.
reset();for(let now=0;now<=2000;now+=50)updateLapTargetCalibration(pose(now<900?.42:.57),now);
assert.equal(lapTargetCalibration.ready,false);
for(let now=2050;now<=3000;now+=50)updateLapTargetCalibration(pose(.57),now);
assert.equal(lapTargetCalibration.ready,true);
assert.equal(lapTargetCalibration.target.x,.57);
// Missing or off-frame wrist data is not a stable hand detection.
reset();const missing=pose();missing[16].visibility=0;
for(let now=0;now<=3000;now+=50)updateLapTargetCalibration(missing,now);
assert.equal(lapTargetCalibration.ready,false);
assert.equal(lapCalibrationDiagnostic.reason,'affected_hand_not_visible');
// Regression: 240 ms left the old 2.1 s rolling window permanently at 1.92 s;
// 300 ms reset the old 250 ms tracking-gap check on every usable frame.
for(const interval of [240,300,400]){
 reset();let lockedAt=null;
 for(let now=0;now<=3200;now+=interval){
   updateLapTargetCalibration(pose(),now);
   if(lapTargetCalibration.ready){lockedAt=now;break;}
 }
 assert.ok(lockedAt>=2000 && lockedAt<=2800,`stable lap at ${interval} ms cadence: ${lockedAt}`);
}
// One model outlier must not discard a genuinely stable two-second hold.
reset();for(let now=0;now<=2000;now+=50)updateLapTargetCalibration(pose(now===1000?.59:.42),now);
assert.equal(lapTargetCalibration.ready,true);
assert.equal(lapTargetCalibration.target.x,.42);
// Prolonged loss cannot bridge two short holds.
reset();for(let now=0;now<=1000;now+=100)updateLapTargetCalibration(pose(),now);
for(let now=2000;now<=3000;now+=100)updateLapTargetCalibration(pose(),now);
assert.equal(lapTargetCalibration.ready,false);
// Reframing after a locked lap updates it; an isolated noisy frame does not.
reset();for(let now=0;now<=2000;now+=50)updateLapTargetCalibration(pose(),now);
updateLapTargetCalibration(pose(.59),2050);updateLapTargetCalibration(pose(),2100);
assert.equal(lapTargetCalibration.ready,true);
for(let now=2200;now<=2800;now+=100)updateLapTargetCalibration(pose(.59),now);
assert.equal(lapTargetCalibration.ready,false);
console.log('stable wrist, low FPS, isolated outlier, movement, tracking loss and reframing passed');
'''
    if task_id == "T3":
        script = script.replace("id:'T1'", "id:'T3'").replace("id:'T1-S4'", "id:'T3-S4'")
        script = script.replace("function testingReachEnabled(){return true;}", "function testingReachEnabled(){return false;}")
        script = script.replace("function testingMouthEnabled(){return false;}", "function testingMouthEnabled(){return true;}\nconst mouthTargetCalibration={locked:true};")
        script = script.replace("assert.equal(forwardReachPlacement.ready,true);", "assert.equal(forwardReachPlacement,null);")
        script = script.replace("assert.equal(lapCalibrationDiagnostic.reason,'shoulders_not_visible');", "assert.ok(lapTargetCalibration.ready);")
    result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout
