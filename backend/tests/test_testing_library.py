import os
import sys
import types
from pathlib import Path

from fastapi.testclient import TestClient

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "axonai_testing_library_test")

if "emergentintegrations.llm.chat" not in sys.modules:
    emergent = types.ModuleType("emergentintegrations")
    llm = types.ModuleType("emergentintegrations.llm")
    chat = types.ModuleType("emergentintegrations.llm.chat")

    class _UnavailableChatDependency:
        def __init__(self, *args, **kwargs):
            pass

    chat.LlmChat = _UnavailableChatDependency
    chat.UserMessage = _UnavailableChatDependency
    sys.modules.setdefault("emergentintegrations", emergent)
    sys.modules.setdefault("emergentintegrations.llm", llm)
    sys.modules.setdefault("emergentintegrations.llm.chat", chat)

from backend import server


FRONTEND_ROOT = Path(__file__).resolve().parents[2] / "frontend"


async def _signed_in_user(_headers):
    return {
        "id": "u_testing_library",
        "credits": 1000,
        "consent": {"health_data_consent": True},
        "profile": {"side_affected": "right"},
    }


def test_testing_library_exposes_every_unique_task_and_guided_exercise(monkeypatch):
    monkeypatch.setattr(server, "_user_from_header", _signed_in_user)
    with TestClient(server.app) as client:
        response = client.get("/api/testing/library")

    assert response.status_code == 200
    payload = response.json()
    tasks = [task for package in payload["assessment_packages"] for task in package["tasks"]]
    exercises = payload["exercises"]
    expected_task_ids = {
        task["id"]
        for package_id, package in server.ASSESSMENT_PACKAGES.items()
        if package_id != "initial"
        for task in package["tasks"]
    }
    expected_exercise_ids = {exercise.id for exercise in server.EXERCISE_LIBRARY.values()}

    assert payload["assessment_task_count"] == 25
    assert payload["exercise_count"] == 18
    assert payload["test_runs_are_recorded"] is False
    assert {task["id"] for task in tasks} == expected_task_ids
    assert len(tasks) == len(expected_task_ids)
    assert {exercise["id"] for exercise in exercises} == expected_exercise_ids
    assert expected_exercise_ids == set(server.REHAB_RUNNER_CONFIG)
    assert all(exercise["guided_reps"] > 0 for exercise in exercises)
    assert all(exercise["calibration_instruction"] for exercise in exercises)
    assert all(exercise["training_focus"] for exercise in exercises)
    assert all(exercise["repetition_definition"] for exercise in exercises)
    assert all(exercise["rom_metrics"] for exercise in exercises)
    assert all(exercise["scoring_method"] == server.EXERCISE_SCORING_METHOD for exercise in exercises)
    assert all(exercise["overlay_style"] == server.EXERCISE_OVERLAY_STYLE for exercise in exercises)
    assert all(
        metric["coaching_cue"]
        for exercise in exercises
        for metric in exercise["rom_metrics"]
    )
    assert all(
        metric["label"] and metric["correction"]
        for exercise in exercises
        for metric in exercise["compensation_metrics"]
    )


def test_library_task_launch_bypasses_schedule_only_for_one_explicit_task(monkeypatch):
    monkeypatch.setattr(server, "_user_from_header", _signed_in_user)

    async def schedule_must_not_run(*_args, **_kwargs):
        raise AssertionError("scheduled assessment access should not run in library test mode")

    monkeypatch.setattr(server, "_assessment_access_plan", schedule_must_not_run)
    with TestClient(server.app) as client:
        response = client.get("/api/assessment/tasks?package=hand&task_ids=H1&library_test=true")
        missing_task = client.get("/api/assessment/tasks?package=hand&library_test=true")
        multiple_tasks = client.get("/api/assessment/tasks?package=hand&task_ids=H1,H2&library_test=true")

    assert response.status_code == 200
    assert response.json()["assigned_task_ids"] == ["H1"]
    assert [task["id"] for task in response.json()["tasks"]] == ["H1"]
    assert missing_task.status_code == 422
    assert multiple_tasks.status_code == 422


def test_testing_routes_require_a_signed_in_account(monkeypatch):
    async def no_user(_headers):
        return None

    monkeypatch.setattr(server, "_user_from_header", no_user)
    with TestClient(server.app) as client:
        assert client.get("/api/testing/library").status_code == 401
        assert client.get("/api/assessment/tasks?package=hand&task_ids=H1&library_test=true").status_code == 401


def test_frontend_library_and_runners_keep_test_results_out_of_progress():
    settings = (FRONTEND_ROOT / "app" / "(tabs)" / "settings.tsx").read_text(encoding="utf-8")
    library = (FRONTEND_ROOT / "app" / "testing-library.tsx").read_text(encoding="utf-8")
    assessment = (FRONTEND_ROOT / "app" / "assessment.tsx").read_text(encoding="utf-8")
    exercise = (FRONTEND_ROOT / "app" / "exercise.tsx").read_text(encoding="utf-8")
    runner = server.POSE_RUNNER_HTML

    assert 'testID="settings-testing-library"' in settings
    assert 'router.push("/testing-library" as never)' in settings
    assert 'library_test: "1"' in library
    assert 'task_ids: task.id' in library
    assert 'plan_id: "library-test"' in library
    assert 'if (!isLibraryTest && msg.task_id && userIdRef.current)' in assessment
    assert 'const skipSeparateCameraTest = walking_test === "1" || library_test === "1";' in assessment
    assert 'if (!cameraReady && !skipSeparateCameraTest)' in assessment
    assert 'msg.type === "library_test_complete"' in assessment
    assert 'if (isLibraryTest) return;' in exercise
    assert 'if (!isLibraryTest) {' in exercise
    assert 'const LIBRARY_TEST_MODE = URL_PARAMS.get("library_test") === "1";' in runner
    assert 'stepTitle.textContent = "Single task test";' in runner
    assert 'if(LIBRARY_TEST_MODE || LOCAL_PREVIEW_MODE) return Promise.resolve(null);' in runner
    assert 'if(LIBRARY_TEST_MODE || LOCAL_PREVIEW_MODE) return;' in runner
    assert 'if(LIBRARY_TEST_MODE) taskQuery.set("library_test", "1");' in runner
    assert 'if(!LIBRARY_TEST_MODE && !LOCAL_PREVIEW_MODE){' in runner
    assert 'postRN({type:"library_test_complete"' in runner
    assert 'if((LIBRARY_TEST_MODE || WALKING_TEST_MODE) && !window.__rehynStartRequested)' in runner
    assert "await setupPose();" in runner
    assert "RTMPose" not in runner
    assert "function newForwardLeanEvidence" in runner
    assert runner.index("function newForwardLeanEvidence") < runner.index("class Tracker") < runner.index("const assessmentQuality =")
    shared_trunk_detector = (FRONTEND_ROOT.parent / "testing" / "trunk-lean-comparison" / "trunk_lean_metrics.js").read_text(encoding="utf-8").strip()
    assert shared_trunk_detector in runner
    assert "this.trunkLeanMetrics.newForwardLeanEvidence(frame,this.trunkLeanBaseline," in runner
    assert 'testingReachTrunkLean:LIBRARY_TEST_MODE && ASSIGNED_TASK_IDS.length===1 && ["T1","T3"].includes(ASSIGNED_TASK_IDS[0])' in runner
    assert 'id="liveTrunkLeanState"' in runner


def test_onboarding_text_fields_submit_with_enter():
    onboarding = (FRONTEND_ROOT / "app" / "onboarding.tsx").read_text(encoding="utf-8")
    assert 'returnKeyType={idx === steps.length - 1 ? "done" : "next"}' in onboarding
    assert "onSubmitEditing={() => {" in onboarding
    assert "if (!saving && !loadingProfile && canContinue()) void onContinue();" in onboarding


def test_patient_testing_list_matches_initial_tasks_without_walking(monkeypatch):
    monkeypatch.setattr(server, "_user_from_header", _signed_in_user)
    with TestClient(server.app) as client:
        result = client.get("/api/testing/library?patient_tasks_only=true").json()
    tasks = [task for package in result["assessment_packages"] for task in package["tasks"]]
    assert [task["id"] for task in tasks] == ["T1", "T2", "T3", "H1", "H3", "H4"]
    assert result["assessment_task_count"] == 6
    assert tasks[0]["title"] == "Seated Forward Reach"


def _measured_reach():
    from backend.assessment_quality import VERSION
    steps = []
    for rule in server.ASSESSMENT_RUBRICS["T1"]["steps"]:
        steps.append({"step_id": rule["id"], "completed": True, "duration_ms": 2000,
                      "metrics": {"quality": {"version": VERSION,
                          "measurements": {c["metric"]: {"value": c["target"], "samples": 10,
                              "statistic_source": "sample_proportion" if c["metric"] == "target_control" else "target_median",
                              "series": [{"elapsed_ms": index * 100, "value": c["target"] * (index + 1) / 10,
                                          "in_target": index >= 5} for index in range(10)]} for c in rule["criteria"]},
                          "compensations": {cid: {"eligible_ms": 900, "max_value": 0, "max_streak_ms": 0} for cid in rule["compensations"]},
                          "observations": {"arm_elevation": {"samples": 10, "median": 50, "endpoint": 60, "min": 10, "max": 70}}
                      }}})
    return {"task_id": "T1", "completed_steps": 4, "total_steps": 4, "duration_ms": 8000, "steps": steps}


def test_testing_report_explains_score_without_persisting_or_charging(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(server, "_user_from_header", _signed_in_user)
    # Any storage use fails: this route must be a pure calculation.
    monkeypatch.setattr(server, "db", SimpleNamespace())
    async def forbidden(*args, **kwargs):
        raise AssertionError("Test run must not affect the patient account")
    monkeypatch.setattr(server, "consume_credits", forbidden)
    monkeypatch.setattr(server, "_record_initial_assessment_completion", forbidden)
    result = _measured_reach()
    reach = result["steps"][1]["metrics"]["quality"]
    reach["measurements"]["elbow_extension"]["value"] = 90
    reach["compensations"]["trunk_lean"].update(max_value=20, max_streak_ms=700)
    with TestClient(server.app) as client:
        response = client.post("/api/testing/assessment-score", json=result)
    assert response.status_code == 200
    report = response.json()
    assert report["recorded"] is False and report["clinical_measure"] is False
    assert report["task"]["score"] == 15
    assert report["task"]["compensation_override"]["detected"][0]["id"] == "trunk_lean"
    assert report["completed_steps"] == 4 and report["duration_ms"] == 8000
    step = report["task"]["steps"][1]
    assert step["score"] == 92.7
    assert step["calculation"] == {"completion_points": 20, "range_points": 72.727,
                                   "detected_compensations": 1, "form_factor": 1}
    shoulder = next(row for row in step["measurements"] if row["metric"] == "arm_elevation")
    assert shoulder["endpoint"] == 60 and shoulder["max"] == 70
    assert next(row for row in step["measurements"] if row["metric"] == "wrist_bend")["median"] is None
    criterion = step["criteria"][0]
    assert criterion["statistic_source"] == "target_median"
    assert len(criterion["series"]) == 10 and criterion["series"][-1]["in_target"] is True


def test_testing_reach_reports_the_sustained_trunk_cue_that_caused_the_penalty():
    from backend.assessment_quality import score_step
    result = _measured_reach()
    step = result["steps"][1]
    raw = step["metrics"]["quality"]["compensations"]["trunk_lean"]
    raw.update(method="pelvis_normalized_shoulder_or_face_v1", eligible_ms=900,
               max_value=9.2, max_streak_ms=650, shoulder_peak=18, face_peak=9.2,
               cue_evidence={"shoulder": {"duration_ms": 0, "peak": None},
                             "face": {"duration_ms": 650, "peak": 9.2}})
    rubric = server.ASSESSMENT_RUBRICS["T1"]["steps"][1]
    scored = score_step(step, rubric)
    lean = next(check for check in scored["compensations"] if check["id"] == "trunk_lean")
    assert lean["status"] == "detected"
    assert lean["confirmed_cues"] == {"face": {"duration_ms": 650, "peak": 9.2, "threshold": 7}}
    assert scored["score"] == 80

    # Even high peaks do not justify a penalty if neither cue lasted 0.5 s.
    raw["cue_evidence"] = {"shoulder": {"duration_ms": 300, "peak": 18},
                           "face": {"duration_ms": 300, "peak": 9.2}}
    scored = score_step(step, rubric)
    assert next(check for check in scored["compensations"] if check["id"] == "trunk_lean")["status"] == "not_detected"
    assert scored["score"] == 100


def test_testing_report_preserves_missing_evidence_and_rejects_wrong_steps(monkeypatch):
    monkeypatch.setattr(server, "_user_from_header", _signed_in_user)
    result = _measured_reach()
    result["steps"][1]["metrics"] = {}
    with TestClient(server.app) as client:
        report = client.post("/api/testing/assessment-score", json=result).json()
        assert report["task"]["score"] is None
        assert report["task"]["earned_score"] == 75
        assert report["task"]["steps"][1]["score"] is None
        assert all(row["median"] is None for row in report["task"]["steps"][1]["measurements"])
        result["steps"][0]["step_id"] = "H1-S1"
        assert client.post("/api/testing/assessment-score", json=result).status_code == 422
        result = _measured_reach()
        result["steps"].append(result["steps"][0])
        assert client.post("/api/testing/assessment-score", json=result).status_code == 422
        result["task_id"] = "L6"
        assert client.post("/api/testing/assessment-score", json=result).status_code == 422


def test_testing_report_requires_sign_in(monkeypatch):
    async def no_user(_headers):
        return None
    monkeypatch.setattr(server, "_user_from_header", no_user)
    with TestClient(server.app) as client:
        assert client.post("/api/testing/assessment-score", json=_measured_reach()).status_code == 401


def test_testing_peak_angle_report_keeps_the_maximum_and_its_time(monkeypatch):
    monkeypatch.setattr(server, "_user_from_header", _signed_in_user)
    result = _measured_reach()
    measurement=result['steps'][0]['metrics']['quality']['measurements']['elbow_extension']
    measurement.update(value=128,statistic_source='movement_maximum',peak_elapsed_ms=200,
                       series=[{'elapsed_ms':0,'value':100,'in_target':False},
                               {'elapsed_ms':200,'value':128,'in_target':False},
                               {'elapsed_ms':1000,'value':105,'in_target':True}])
    with TestClient(server.app) as client:
        report=client.post('/api/testing/assessment-score',json=result).json()
    step=report['task']['steps'][0]
    assert step['criteria'][0]['observed'] == 128
    assert step['criteria'][0]['target'] == 120
    assert step['criteria'][0]['statistic_source'] == 'movement_maximum'
    assert step['criteria'][0]['peak_elapsed_ms'] == 200
    assert step['score'] == 100
