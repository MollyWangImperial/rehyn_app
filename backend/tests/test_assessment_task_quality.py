from copy import deepcopy
import math
import os
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "rehyn_quality_test")
from backend.server import ASSESSMENT_RUBRICS, build_functional_metrics
from backend.assessment_quality import VERSION, score_assessment, score_step
from backend.encouragement import compute_rewards


def task(tid, fraction=1):
    rule = ASSESSMENT_RUBRICS[tid]
    steps = []
    for step in rule["steps"]:
        q = {"version": VERSION, "measurements": {c["metric"]: {"value": c["target"] * fraction, "samples": 10} for c in step["criteria"]},
             "compensations": {cid: {"eligible_ms": 900, "max_value": 0, "max_streak_ms": 0} for cid in step["compensations"]}}
        steps.append({"step_id": step["id"], "completed": True, "duration_ms": 1600, "metrics": {"quality": q}})
    return {"task_id": tid, "completed_steps": len(steps), "total_steps": len(steps), "steps": steps}


def test_every_task_has_a_rubric_and_equal_module_weight_not_step_weight():
    results = [task(tid) for tid in ASSESSMENT_RUBRICS]
    quality = score_assessment(results, ASSESSMENT_RUBRICS)
    assert all(m["score"] == 100 for m in quality["modules"].values())
    a, b = task("T1"), task("T7", .5)
    quality = score_assessment([a, b], ASSESSMENT_RUBRICS)
    assert quality["modules"]["upper_limb"]["score"] == 80
    assert [t["module_weight"] for t in quality["tasks"]] == [50, 50]


def test_seated_forward_reach_uses_requested_step_references_without_changing_other_tasks():
    steps = ASSESSMENT_RUBRICS["T1"]["steps"]
    assert [[(c["metric"], c["target"]) for c in step["criteria"]] for step in steps[:3]] == [
        [("elbow_extension", 120)],
        [("elbow_extension", 110), ("arm_elevation", 50)],
        [("elbow_extension", 110), ("arm_elevation", 50)],
    ]
    assert [(c["metric"], c["target"]) for c in ASSESSMENT_RUBRICS["T2"]["steps"][1]["criteria"]] == [
        ("elbow_extension", 150), ("arm_elevation", 100),
    ]


def test_forward_reach_scores_the_first_elbow_and_both_later_angles():
    reach = task("T1")
    measurements = [step["metrics"]["quality"]["measurements"] for step in reach["steps"]]
    measurements[0]["elbow_extension"]["value"] = 90  # 75% of 120°
    measurements[0]["arm_elevation"] = {"value": 90, "samples": 10}  # diagnostic only
    measurements[1]["elbow_extension"]["value"] = 55  # 50% of 110°
    measurements[2]["arm_elevation"]["value"] = 25  # 50% of 50°
    scored = score_assessment([reach], ASSESSMENT_RUBRICS)["tasks"][0]
    assert [step["score"] for step in scored["steps"]] == [80, 80, 80, 100]
    assert scored["score"] == 85


def test_return_to_lap_uses_target_completion_only():
    rubric = ASSESSMENT_RUBRICS["T1"]["steps"][3]
    assert rubric["scoring_method"] == "target_completion"
    assert rubric["criteria"] == [] and rubric["compensations"] == []
    completed = {"step_id": "T1-S4", "completed": True, "duration_ms": 1500, "metrics": {}}
    assert score_step(completed, rubric)["score"] == 100
    completed["completed"] = False
    assert score_step(completed, rubric)["score"] == 0
    assert score_step(None, rubric)["score"] is None


def test_reached_target_with_bent_elbow_does_not_receive_full_credit():
    reach = task("T1")
    reach["steps"][1]["metrics"]["quality"]["measurements"]["elbow_extension"]["value"] = 90
    q = score_assessment([reach], ASSESSMENT_RUBRICS)
    assert q["tasks"][0]["steps"][1]["score"] == 92.7
    assert q["modules"]["upper_limb"]["score"] == 98.2


def test_sustained_compensation_affects_only_that_step_and_assistance_halves_task():
    reach = task("T1")
    c = reach["steps"][1]["metrics"]["quality"]["compensations"]["shoulder_hike"]
    c.update(max_value=25, max_streak_ms=100)
    assert score_assessment([reach], ASSESSMENT_RUBRICS)["tasks"][0]["score"] == 100
    c["max_streak_ms"] = 600
    q = score_assessment([reach], ASSESSMENT_RUBRICS)
    assert q["tasks"][0]["score"] == 95
    reach["metrics"] = {"assisted": True}
    assert score_assessment([reach], ASSESSMENT_RUBRICS)["tasks"][0]["score"] == 47.5


def test_missing_tasks_and_tracking_are_not_excluded_to_inflate_score():
    q = score_assessment([task("T1")], ASSESSMENT_RUBRICS, ["T1", "T2"])
    assert q["modules"]["upper_limb"]["score"] is None
    assert q["modules"]["upper_limb"]["earned_score"] == 50
    assert q["tasks"][0]["module_weight"] == 50
    for invalid in [None, math.nan, True]:
        reach = task("T1")
        reach["steps"][0]["metrics"]["quality"]["measurements"]["elbow_extension"]["value"] = invalid
        assert score_assessment([reach], ASSESSMENT_RUBRICS)["tasks"][0]["score"] is None


def test_partial_mouth_step_preserves_upper_limb_points_and_equal_task_shares():
    mouth = task("T3")
    mouth["steps"][1]["metrics"] = {}
    quality = score_assessment([task("T1"), task("T2"), mouth, task("H1")], ASSESSMENT_RUBRICS,
                               ["T1", "T2", "T3", "H1", "L6"])
    upper = quality["modules"]["upper_limb"]
    assert upper["score"] is None
    assert upper["earned_score"] == 91.7
    assert (upper["measured_steps"], upper["total_steps"]) == (11, 12)
    mouth_score = quality["tasks"][2]
    assert mouth_score["earned_score"] == 75
    assert mouth_score["steps"][1]["score"] is None
    assert [t["module_weight"] for t in quality["tasks"][:3]] == [33.33] * 3
    assert mouth_score["earned_module_points"] == 25
    assert quality["modules"]["hand"]["score"] == 100
    assert quality["modules"]["lower_limb"]["earned_score"] is None

    mouth["metrics"] = {"assisted": True}
    assisted = score_assessment([mouth], ASSESSMENT_RUBRICS)
    assert assisted["tasks"][0]["earned_score"] == 50  # (50 + missing + 50 + 100) / 4


def test_zero_measured_score_is_distinct_from_missing_evidence():
    reach = task("T1", 0)
    for step in reach["steps"]:
        step["completed"] = False
    assert score_assessment([reach], ASSESSMENT_RUBRICS)["modules"]["upper_limb"]["earned_score"] == 0
    for step in reach["steps"]:
        step["metrics"] = {}
    assert score_assessment([reach], ASSESSMENT_RUBRICS)["modules"]["upper_limb"]["earned_score"] == 0
    reach["steps"] = []
    assert score_assessment([reach], ASSESSMENT_RUBRICS)["modules"]["upper_limb"]["earned_score"] is None


def test_old_assessment_remains_completed_but_does_not_invent_rom():
    old = task("T1")
    for step in old["steps"]:
        step["metrics"] = {}
    metrics = build_functional_metrics([old])
    assert metrics["reach_completion"] == 1
    assert metrics["task_quality"]["modules"]["upper_limb"]["score"] is None


def test_rewards_once_per_day_and_per_assessment_not_repetition_or_quality():
    activities = [{"completed_at": "2026-09-06T10:00:00Z", "completed_reps": 500, "quality_reps": 500}]*8
    initial = {"id": "initial", "created_at": "2026-09-01T10:00:00Z", "assessment_package": "initial", "task_results": [task("T1")]}
    followup = {**deepcopy(initial), "id": "follow-up", "assessment_package": "upper_limb"}
    sample = {**initial, "id": "sample", "testing_shortcut": True}
    incomplete = {**initial, "id": "unfinished", "assigned_task_ids": ["T1", "T2"]}
    rewards = compute_rewards(activities, daily_checkins={"2026-09-06": {"status": "complete"}}, assessments=[initial, initial, followup, sample, incomplete])
    assert rewards["points"] == 10 + 20 + 20 + 2
    assert rewards["breakdown"]["assessments_completed"] == 2
    assert compute_rewards(activities)["points"] == 0


def test_quality_evidence_and_scores_survive_account_save_and_reload(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from starlette.requests import Request
    from backend import server

    stored = []
    completed = []
    user = {"id": "quality-patient", "email": "quality@example.com", "profile": {}}

    async def signed_in(*_args):
        return user

    async def access(*_args):
        return {"trigger": "initial", "task_ids": ["T1"]}

    async def noop(*_args, **_kwargs):
        return None

    async def videos(*_args):
        return {}

    async def insert(doc):
        stored.append(deepcopy(doc))

    async def complete(account, created_at):
        completed.append((account["id"], created_at))

    async def owned(assessment_id, *_args):
        return next(deepcopy(doc) for doc in stored if doc["id"] == assessment_id and doc["user_id"] == user["id"])

    monkeypatch.setattr(server, "_user_from_header", signed_in)
    monkeypatch.setattr(server, "_assessment_access_plan", access)
    monkeypatch.setattr(server, "consume_credits", noop)
    monkeypatch.setattr(server, "_latest_task_videos", videos)
    monkeypatch.setattr(server, "_record_initial_assessment_completion", complete)
    monkeypatch.setattr(server, "_mark_functional_issue_assessed", noop)
    monkeypatch.setattr(server, "_record_alira_action", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_owned_assessment_doc", owned)
    monkeypatch.setattr(server, "db", SimpleNamespace(assessments=SimpleNamespace(insert_one=insert)))
    monkeypatch.setattr(server, "LOCAL_GPU_WORKER_URL", "")
    request = Request({"type": "http", "method": "POST", "path": "/api/assessment/submit", "headers": []})
    original = task("T1", .75)
    payload = server.AssessmentSubmit(affected_side="right", assessment_package="initial", assigned_task_ids=["T1"], task_results=[original])
    saved = asyncio.run(server.submit_assessment(payload, request))
    reloaded = asyncio.run(server.get_patient_assessment_summary(saved.id, request))
    assert stored[0]["user_id"] == user["id"]
    assert stored[0]["account_email"] == user["email"]
    assert stored[0]["task_results"][0]["steps"][1]["metrics"]["quality"] == original["steps"][1]["metrics"]["quality"]
    assert reloaded["functional_metrics"]["task_quality"] == saved.metrics["task_quality"]
    assert reloaded["functional_metrics"]["task_quality"]["modules"]["upper_limb"]["score"] == 85
    assert completed == [(user["id"], saved.created_at)]

    # Reopening an older saved snapshot recomputes partial points from its
    # original evidence, even when the stored aggregate score is unavailable.
    stored[0]["task_results"][0]["steps"][0]["metrics"] = {}
    stored[0]["metrics"]["task_quality"] = {"modules": {"upper_limb": {"score": None}}}
    partial = asyncio.run(server.get_patient_assessment_summary(saved.id, request))
    module = partial["functional_metrics"]["task_quality"]["modules"]["upper_limb"]
    assert module["score"] is None
    assert module["earned_score"] == 65
    assert module["measured_steps"] == 3


def test_between_task_celebration_is_brief_and_advances_without_a_button():
    from backend import server

    html = server.POSE_RUNNER_HTML
    assert '<div class="star">&#11088;</div>' in html
    assert "Wonderful work!" in html
    assert "assessmentTaskStatistics" not in html
    assert "assessmentStatisticsContinue" not in html
    assert "Continue to next task" not in html
    assert "Save assessment and view results" not in html
    assert "if(voiceMs < minDisplayMs) await new Promise" in html
    assert "currentTaskIdx += 1;" in html
    assert "#celebrate{overflow:auto;padding:24px 16px;box-sizing:border-box;justify-content:center" in html
    assert "justify-content:flex-start;background:#244d3c" not in html


def test_elbow_extension_uses_the_aspect_corrected_image_plane_angle():
    from backend import server

    assessment_html = server.POSE_RUNNER_HTML
    rehab_html = server.REHAB_RUNNER_HTML_TEMPLATE
    assert "const elbowAspect=video.videoWidth>0 && video.videoHeight>0 ? video.videoWidth/video.videoHeight : 1;" in rehab_html
    assert "raw.elbow_extension=angle(elbowPoint(lm[ACTIVE.shoulder]),elbowPoint(lm[ACTIVE.elbow]),elbowPoint(lm[ACTIVE.wrist]));" in rehab_html
    assert "assessmentQuality.calibrate(landmarks, latestPoseWorldLandmarks, qualityAspect);" in assessment_html
    assert "aspectRatio:qualityAspect" in assessment_html
