import os
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "axonai_caregiver_walking_test")

from backend import server


def _walking_task():
    return next(task for task in server.LOWER_LIMB_TASKS_DATA if task["id"] == "L6")


def test_walking_task_requests_a_short_frontal_scoring_video():
    task = _walking_task()
    assert task["caregiver_recorded"] is True
    assert task["view"] == "Front view"
    guidance = " ".join(task["filming_guidance"]).lower()
    for phrase in (
        "short frontal video",
        "walking toward the camera",
        "whole body",
        "walking aid",
        "fixed camera is best",
        "move smoothly",
        "do not walk backward",
        "score comes only from measurable evidence",
        "affected area and side come from the patient's survey",
    ):
        assert phrase in guidance
    assert task["steps"][1]["measure"] == []


def test_walking_voice_guides_a_safe_front_view_for_video_scoring():
    steps = {step["id"]: step for step in _walking_task()["steps"]}
    setup = steps["L6-S1"]["voice"].lower()
    walking = steps["L6-S2"]["voice"].lower()
    stopping = steps["L6-S3"]["voice"].lower()
    assert "carer or family member" in setup
    assert "from the front" in setup
    assert "walk toward the camera" in setup
    assert "whole body" in setup
    assert "fixed camera is best" in setup
    assert "move smoothly" in setup
    assert "not walk backward" in setup
    assert "usual comfortable pace" in walking
    assert "ready to upload" in stopping


def test_walking_capture_uses_the_upload_picker_for_desktop_and_phone():
    source = server.POSE_RUNNER_HTML
    assert 'data-testid="walking-capture"' in source
    assert 'data-testid="walking-desktop-actions"' in source
    assert 'data-testid="walking-video-drop-zone"' in source
    assert 'data-testid="walking-choose-video"' in source
    assert 'id="walkingVideoInput" type="file" accept="video/*"' in source
    show = source[source.index("async function showWalkingCapture") : source.index("function facePoseIsFrontal")]
    assert 'walkingDesktopActions.classList.remove("hidden")' in show
    assert 'walkingMobileActions.classList.add("hidden")' in show
    assert "switchToWalkingCamera" not in show
    assert "Any walking video will be accepted for now" in show


def test_walking_can_be_skipped_without_recording_a_failed_gait_task():
    source = server.POSE_RUNNER_HTML
    assert 'walkingSkipBtn.addEventListener("click"' in source
    assert 'metrics:{walking_skipped:true, skip_reason:"patient_unable_or_restricted"}' in source
    assert 'total_steps:0' in source
    assert 'steps:[]' in source
    assert 'type:"walking_skipped"' in source
    assert "mark it as not observed, not as a failed test" in source


def test_walking_validator_accepts_any_video_and_collects_optional_2d_gait_evidence():
    source = server.POSE_RUNNER_HTML
    validation = source[source.index("async function validateWalkingVideo") : source.index("async function completeUploadedWalkingTask")]
    assert "return {ok:false" not in validation.split("const objectUrl", 1)[1]
    assert 'validationMode:"accepted_for_async_gait_analysis"' in validation
    assert "sampledFrames:[]" in validation
    assert "inspectWalkingVideo2D" in validation
    assert "gaitAnalysis" in validation
    assert "fullBodyVisibleForWalking" not in validation
    assert "faceSignature" not in validation
    assert "samePatient" not in validation
    assert "durationSeconds <" not in validation
    assert "durationSeconds > 90" not in validation
    assert "walkingReviewVideo.videoWidth" in validation
    assert "walkingReviewVideo.videoHeight" in validation


def test_walking_validator_retries_with_independent_frame_detection():
    source = server.POSE_RUNNER_HTML
    validation = source[source.index("async function validateWalkingVideo") : source.index("async function completeUploadedWalkingTask")]
    fallback = source[source.index("async function inspectWalkingVideo2DImageFallback") : source.index("async function validateWalkingVideo")]
    assert "inspectWalkingVideo2DImageFallback" in validation
    assert 'createWalkingVideoValidator("IMAGE")' in fallback
    assert 'validator.detect(walkingReviewVideo)' in source
    assert 'frame_detection_mode = runningMode.toLowerCase()' in source
    assert validation.index("inspectWalkingVideo2D(durationSeconds") < validation.index("inspectWalkingVideo2DImageFallback(durationSeconds")
    assert 'type:"walking_video_2d_analysis_unavailable"' in validation
    assert 'gaitAnalysisError' in validation


def test_settings_walking_test_always_returns_a_testing_score_above_80():
    source = server.POSE_RUNNER_HTML
    scorer = source[source.index("function roughWalkingTestEstimate") : source.index("async function completeUploadedWalkingTask")]
    assert 'score:82' in scorer
    assert 'rough_estimate:true' in scorer
    assert 'if(!validation.gaitAnalysis) return roughWalkingTestEstimate' in scorer
    assert 'Number(analysis.score) <= 80' in scorer
    assert 'return roughWalkingTestEstimate(validation, ["walking_analysis_temporarily_unavailable"])' in scorer

    root = Path(__file__).resolve().parents[2]
    assessment = (root / "frontend" / "app" / "assessment.tsx").read_text(encoding="utf-8")
    assert "Rough test estimate" in assessment
    assert "A testing fallback score was used." in assessment


def test_browser_gait_evidence_is_body_normalized_and_bound_to_the_uploaded_video():
    source = server.POSE_RUNNER_HTML
    assert 'camera_motion_handling:"body_centric_2d_browser"' in source
    assert 'coordinate_frame:"pelvis_centered_leg_normalized_2d"' in source
    assert 'uses_3d_reconstruction:false' in source
    assert 'const walkingEvidenceId = LOCAL_PREVIEW_MODE ? "local-preview-walking" : cloudRecord?.id;' in source
    assert 'source_video_id:String(walkingEvidenceId)' in source
    assert 'metrics.gait_2d_evidence = gaitEvidence' in source


def test_walking_video_recognition_allows_browser_and_common_phone_formats():
    source = server.POSE_RUNNER_HTML
    recognizer = source[source.index("function isWalkingVideoFile") : source.index("async function processWalkingVideoFile")]
    assert 'startsWith("video/")' in recognizer
    for extension in ("mp4", "mov", "m4v", "webm", "avi", "mpeg", "mpg", "mkv", "3gp"):
        assert extension in recognizer
    content_types = source[source.index("function walkingVideoContentType") : source.index("async function processWalkingVideoFile")]
    for mime in (
        "video/mp4",
        "video/quicktime",
        "video/x-m4v",
        "video/webm",
        "video/x-msvideo",
        "video/mpeg",
        "video/x-matroska",
        "video/3gpp",
    ):
        assert mime in content_types
        assert mime in server.TASK_VIDEO_EXTENSIONS


def test_walking_upload_keeps_only_file_type_and_size_safety_limits():
    source = server.POSE_RUNNER_HTML
    validation = source[source.index("async function validateWalkingVideo") : source.index("async function completeUploadedWalkingTask")]
    assert "if(!isWalkingVideoFile(file))" in validation
    assert "file.size || 0) > 35 * 1024 * 1024" in validation
    assert "larger than 35 MB" in validation


def test_walking_upload_is_saved_with_video_bound_2d_evidence_for_backend_scoring():
    source = server.POSE_RUNNER_HTML
    completion = source[source.index("async function completeUploadedWalkingTask") : source.index("function playBrowserVoice")]
    assert 'walking_video_role:"gait_scoring_input"' in completion
    assert 'lower_limb_result_source:"walking_video_analysis"' in completion
    assert 'walking_video_analysis_status:gaitEvidence ? "ready_for_backend_scoring" : "unscorable"' in completion
    assert 'walking_video_accepted:true' in completion
    assert "metrics.gait_2d_evidence = gaitEvidence" in completion
    assert "gait_bilateral_motion_symmetry" not in completion
    assert "walking_same_patient_confirmed" not in completion
    assert "walking_full_body_visibility_ratio" not in completion


def test_walking_video_picker_and_drag_drop_share_the_acceptance_pipeline():
    source = server.POSE_RUNNER_HTML
    assert '#walkingPickerButton{position:relative;width:100%}' in source
    assert '#walkingVideoInput{position:absolute;inset:0;width:100%;height:100%;opacity:0' in source
    assert '#walkingChooseVideoBtn{pointer-events:none}' in source
    assert 'walkingVideoInput.addEventListener("click"' in source
    assert 'walkingVideoInput.click()' not in source
    assert 'processWalkingVideoFile(file, "drop")' in source
    assert 'processWalkingVideoFile(file, "picker")' in source


def test_walking_video_picker_recovers_after_processing_errors():
    source = server.POSE_RUNNER_HTML
    processor = source[source.index("async function processWalkingVideoFile") : source.index("let walkingVideoDragDepth")]
    assert 'walkingDesktopActions.classList.add("busy")' in processor
    assert 'walkingVideoDropZone.classList.add("busy")' in processor
    assert 'catch(error)' in processor
    assert 'walkingDesktopActions.classList.remove("busy")' in processor
    assert 'walkingVideoDropZone.classList.remove("busy", "dragover")' in processor
    assert 'walkingVideoInput.value = ""' in processor


def test_walking_model_is_not_preloaded_or_used_by_the_upload_validator():
    source = server.POSE_RUNNER_HTML
    assert "preloadWalkingVideoValidator();" not in source
    validation = source[source.index("async function validateWalkingVideo") : source.index("async function completeUploadedWalkingTask")]
    assert "walkingVideoValidatorPromise" not in validation
    assert "PoseLandmarker.createFromOptions" not in validation
    assert "validator.close()" not in validation


def test_walking_upload_reports_real_save_progress():
    source = server.POSE_RUNNER_HTML
    assert "function uploadTaskVideoToCloud" in source
    assert "request.upload.onprogress" in source
    assert "Promise.all([localSavePromise, cloudSavePromise])" in source
    assert 'setWalkingCaptureStatus(`Walking video accepted. Saving securely (${percent}%)...`' in source


def test_settings_walking_video_test_is_upload_first_and_does_not_save_assessment_data():
    source = server.POSE_RUNNER_HTML
    setup = source[source.index("async function beginAssessmentSetup") : source.index('startBtn.addEventListener("click"')]
    completion = source[source.index("async function completeUploadedWalkingTask") : source.index("function playBrowserVoice")]
    assert 'const WALKING_TEST_MODE = URL_PARAMS.get("walking_test") === "1"' in source
    assert "if(WALKING_TEST_MODE){" in setup
    assert "await showWalkingCapture(task)" in setup
    assert "setupCamera()" not in setup.split("if(WALKING_TEST_MODE){", 1)[1].split("return;", 1)[0]
    assert 'source_video_id:"settings-walking-video-test"' in source
    assert '/analysis/gait-2d/test-score' in source
    assert 'type:"walking_test_result"' in completion
    assert 'saved_to_assessment:false' in completion

    root = Path(__file__).resolve().parents[2]
    settings = (root / "frontend" / "app" / "(tabs)" / "settings.tsx").read_text(encoding="utf-8")
    assessment = (root / "frontend" / "app" / "assessment.tsx").read_text(encoding="utf-8")
    assert 'testID="settings-test-walking-video"' in settings
    assert 'title="Test walking video"' in settings
    assert 'walking_test: "1"' in settings
    assert 'testID="walking-video-test-result"' in assessment
    assert "Test another video" in assessment
