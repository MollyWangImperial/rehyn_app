import os
import subprocess

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "axonai_pre_assessment_calibration_test")

from backend import server


def test_runner_has_a_patient_facing_seated_calibration_gate():
    source = server.POSE_RUNNER_HTML
    for marker in (
        'id="calibrationOverlay"',
        'data-testid="assessment-calibration"',
        "affected hand resting on the visible part of your lap",
        "Face, shoulders, and affected arm are visible",
        "Affected hand and part of your lap are visible",
        "You do not need to show your knees or your full lap",
        'data-testid="calibration-auto-status"',
        "Assessment will start automatically",
        "Calibration complete. Starting assessment",
    ):
        assert marker in source


def test_calibration_accepts_a_partial_lap_when_the_affected_hand_and_hip_are_visible():
    source = server.POSE_RUNNER_HTML
    assert "function calibrationLandmarkStatus(lm)" in source
    assert "const faceVisible = [lm[0], lm[9], lm[10]].some" in source
    assert "[lm[11], lm[12], affected.elbow, affected.wrist]" in source
    assert "[affected.hip, affected.wrist]" in source
    assert "[lm[23], lm[24], affected.knee]" not in source
    assert "lapTargetCalibration.ready && lapTargetCalibration.target" in source
    assert "cameraReady && armVisible && seatedAnchorsVisible && lapReady" in source
    assert "function landmarkIsInFrame(point" in source
    assert "point.x >= margin && point.x <= 1 - margin" in source
    assert "point.y >= margin && point.y <= 1 - margin" in source
    assert ".every(point => landmarkIsInFrame(point, visibility))" in source


def test_calibration_explains_why_a_visible_hand_has_not_passed():
    source = server.POSE_RUNNER_HTML
    for marker in (
        "function lapTargetCandidateStatus(lm)",
        'reason:"wrong_hand_on_lap"',
        'reason:"affected_lap_not_visible"',
        'return "hand_too_high"',
        'return "hand_too_low"',
        'return "hand_too_far_side"',
        "status.lapGuidance",
    ):
        assert marker in source


def test_calibration_runs_before_task_one_and_is_not_recorded_as_task_motion():
    source = server.POSE_RUNNER_HTML
    start_handler = source[source.index("async function beginAssessmentSetup()") : source.index('markerConfirmBtn.addEventListener')]
    assert "calibratingAssessment = shouldRunSeatedCalibration();" in start_handler
    assert start_handler.index("requestAnimationFrame(loop);") < start_handler.index("await playVoice(testingMouthEnabled() ? MOUTH_TEST_VOICE.calibration : testingReachEnabled() ? TESTING_REACH_CALIBRATION_INSTRUCTION : CALIBRATION_INSTRUCTION);")
    assert "await startStep();" in start_handler
    assert "prefetchVoice(CALIBRATION_COMPLETE_INSTRUCTION);" in start_handler
    assert "if(!running || calibratingAssessment || motionFrames.length >= MAX_MOTION_FRAMES) return;" in source
    assert "if(calibratingAssessment){\n    requestAnimationFrame(loop);\n    return;" in source


def test_calibration_auto_starts_with_a_position_hold_instruction():
    source = server.POSE_RUNNER_HTML
    assert "function completePreAssessmentCalibration()" in source
    assert "if(calibrationInstructionFinished) void completePreAssessmentCalibration();" in source
    assert "await playVoice(CALIBRATION_COMPLETE_INSTRUCTION);" in source
    assert "Stay seated in this position and do not move the camera" in source
    assert "automatic:true" in source
    assert 'calibrationBeginBtn.addEventListener("click"' not in source
    assert 'data-testid="calibration-begin"' not in source


def test_calibrated_lap_is_drawn_live_and_preserved_for_first_task():
    source = server.POSE_RUNNER_HTML
    assert 'ctx.strokeStyle = "#7FE5A3";' in source
    assert "preservePreAssessmentLapCalibration = true;" in source
    assert "if(preservePreAssessmentLapCalibration && currentTaskLapStep()){" in source
    assert "}else if(!preservePreAssessmentLapCalibration){\n      lapTargetCalibration = newLapTargetCalibration();" in source
    assert 'type:"assessment_calibrated"' in source
    assert "lap_target:lapTargetCalibration.target" in source


def test_packages_without_an_upcoming_lap_step_can_start_normally():
    source = server.POSE_RUNNER_HTML
    assert "function upcomingLapStep()" in source
    assert "function shouldRunSeatedCalibration(){\n  return !!upcomingLapStep();" in source
    assert "if(calibratingAssessment){" in source


def test_browser_hook_can_drive_the_real_calibration_gate():
    source = server.POSE_RUNNER_HTML
    assert "applyCalibrationSequence:" in source
    assert "updatePreAssessmentCalibrationUI(landmarks);" in source
    assert "autoStarting:calibrationAutoStartInProgress" in source
    assert "statusText:calibrationAutoStatus.textContent" in source


def test_testing_calibration_requires_posture_and_camera_quality_without_timeout_bypass():
    source = server.POSE_RUNNER_HTML
    start = source.index("function updatePreAssessmentCalibrationUI(lm){")
    end = source.index("\n}\n\nasync function completePreAssessmentCalibration()", start) + 2
    function = source[start:end]
    script = function + r'''
const assert=require('node:assert/strict');
const TESTING_REACH_TRUNK_BASELINE_WAIT_MS=6000;
let fakeNow=0,testingReachTrunkBaselineWaitSince=null;
const performance={now:()=>fakeNow};
let calibratingAssessment=true,preAssessmentCalibrationReady=false;
let calibrationAutoStartInProgress=false,calibrationInstructionFinished=true;
const lapTargetCalibration={ready:true};let forwardReachPlacement={ready:true};
const assessmentQuality={trunkLeanBaseline:null,trunkLeanBaselineFrames:[],
  trunkLeanMetrics:{metricsFromLandmarks:()=>({reason:'Keep both shoulders and both hips in view.'})}};
const video={videoWidth:640,videoHeight:480};
const calibrationCamera={},calibrationArm={},calibrationSeat={},calibrationLap={},calibrationQuality={};
const calibrationProgressFill={style:{}},calibrationTitle={textContent:''},calibrationLead={textContent:''};
const calibrationAutoStatus={textContent:'',classList:{add(){},remove(){}}};
function testingReachEnabled(){return true;}
function testingMouthEnabled(){return false;}
function seatedTestingCalibrationEnabled(){return true;}
let qualityReady=false;
function reachCameraStatus(){return {ready:qualityReady,message:'Improve front lighting and keep both hips visible.'};}
function calibrationLandmarkStatus(){return {ready:forwardReachPlacement.ready,cameraReady:true,armVisible:true,
  seatedAnchorsVisible:true,lapReady:forwardReachPlacement.ready,
  lapGuidance:'Move slightly left to leave room for the reach circles.'};}
function setCalibrationCheck(){}
function syncReachCalibrationChecks(){}
let starts=0;function completePreAssessmentCalibration(){starts++;}
updatePreAssessmentCalibrationUI([]);
assert.equal(preAssessmentCalibrationReady,false);
assert.match(calibrationLead.textContent,/both hips/);
fakeNow=5999;updatePreAssessmentCalibrationUI([]);
assert.equal(starts,0);
fakeNow=6001;updatePreAssessmentCalibrationUI([]);
assert.equal(preAssessmentCalibrationReady,false);
assert.equal(starts,0);
fakeNow=60000;updatePreAssessmentCalibrationUI([]);
assert.equal(starts,0);
assessmentQuality.trunkLeanBaseline={};updatePreAssessmentCalibrationUI([]);
assert.equal(starts,0);
qualityReady=true;updatePreAssessmentCalibrationUI([]);
assert.equal(starts,1);
forwardReachPlacement={ready:false};preAssessmentCalibrationReady=false;starts=0;
fakeNow=12000;updatePreAssessmentCalibrationUI([]);
assert.equal(starts,0);
assert.equal(calibrationTitle.textContent,'Positioning the reach circles');
assert.match(calibrationAutoStatus.textContent,/Lap point saved.*Move slightly left/);
'''
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
