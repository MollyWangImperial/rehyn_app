import os
import random

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "rehyn_guest_tests")
from backend import server
from backend.companion_plan_preview import random_task_results
from backend.companion_review_assessment import (
    BACKEND_ORIGIN, create_guest_review_router, guest_runner_html, review_catalog,
)
from backend.function_scoring import score_function_assessment
from backend.companion_review_quality import VERSION, compensation_checks


@pytest.fixture
def client(monkeypatch):
    class NoDatabase:
        def __getattr__(self, name):
            raise AssertionError("Guest checks must not access patient records")
    async def no_accounts(*args):
        raise AssertionError("Guest checks must not access patient accounts")
    monkeypatch.setattr(server, "db", NoDatabase())
    monkeypatch.setattr(server, "_user_from_header", no_accounts)
    app = FastAPI()
    app.include_router(server.api_router)
    app.include_router(create_guest_review_router(server))
    return TestClient(app)


def result_body(task_ids=("T1", "T3", "H4", "H3", "L6")):
    return {"assessment_package": "initial", "affected_side": "left", "assigned_task_ids": list(task_ids),
            "task_results": random_task_results(task_ids, random.Random(5)),
            "review_options": {"goal": "eating", "has_helper": False}}


def post(client, body):
    return client.post("/api/assessment/review/results", headers={"origin": BACKEND_ORIGIN}, json=body)


def test_guest_tasks_need_no_sign_in_and_keep_opening_before_pinch(client):
    response = client.get("/api/assessment/review/tasks?package=initial&task_ids=T1,T3,H3,H4,L6")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    data = response.json()
    assert data["preview_only"] and data["review_site"]
    assert [task["id"] for task in data["tasks"]] == ["T1", "T3", "H4", "H3", "L6"]
    assert data["tasks"] == review_catalog()["tasks"]


def test_guest_runner_retains_local_ladders_and_never_uses_patient_uploads(client):
    response = client.get("/api/pose/review-runner?ladder=1&affected_side=left&task_ids=T1,T3,H4,H3")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert "const LOCAL_PREVIEW_MODE = true;" in html
    assert 'const CURRENT_USER_ID = "";' in html
    assert "const localReview={enabled:false," in html
    assert "RehynAssessmentLadder" in html and "ladderEvidenceResult" in html
    assert '${API_BASE}/assessment/review/tasks?' in html
    assert '"/assessment/review/results"' in html
    assert 'assessment_preview_complete", assessment:data' in html
    assert "frame-ancestors https://rehyn-recovery-companion.onrender.com" == response.headers["content-security-policy"]
    assert "if(LIBRARY_TEST_MODE || LOCAL_PREVIEW_MODE) return;" in html
    assert "if(LOCAL_PREVIEW_MODE) return null;" in html


def test_guest_completion_uses_original_scores_and_returns_a_browser_report_without_safety_clearance(client):
    body = result_body()
    response = post(client, body)
    assert response.status_code == 200
    report = response.json()
    assert report["preview_only"] and report["review_site"]
    assert report["saved_to_assessment"] is False and "id" not in report
    assert report["metrics"]["function_score"] == score_function_assessment(
        report["task_results"], review_catalog()["rubrics"], body["assigned_task_ids"])
    assert len(report["metrics"]["task_quality"]["tasks"]) == 5
    assert report["metrics"]["function_score"]["tasks"][-1]["points"] is None
    assert isinstance(report["rehab_plan"], list)
    assert report["function_rehab_plan"]["candidate_only"] is True
    assert report["clinical_review_gate"]["rehab_access"] == "blocked"


@pytest.mark.parametrize("query", ["package=upper_limb", "package=initial&task_ids=T2", "task_ids=T1,T1",
                                  "task_ids=", "library_test=1", "uid=patient"])
def test_guest_selection_rejects_other_packages_accounts_and_invalid_assignments(client, query):
    assert client.get("/api/assessment/review/tasks?" + query).status_code in (403, 422)


@pytest.mark.parametrize("mutation", ["duplicate_task", "wrong_task", "wrong_step", "duplicate_step",
                                      "old_ladder", "many_attempts", "raw_frames", "account", "bad_side"])
def test_guest_completion_rejects_mismatched_or_unbounded_data(client, mutation):
    body = result_body()
    if mutation == "duplicate_task":
        body["task_results"][1] = body["task_results"][0]
    elif mutation == "wrong_task":
        body["task_results"][0]["task_id"] = "T2"
    elif mutation == "wrong_step":
        body["task_results"][0]["steps"][0]["step_id"] = "H4-R1"
    elif mutation == "duplicate_step":
        body["task_results"][0]["steps"] *= 2
    elif mutation == "old_ladder":
        body["task_results"][0]["metrics"]["ladder"]["version"] = "invented"
    elif mutation == "many_attempts":
        body["task_results"][0]["metrics"]["ladder"]["attempts"] *= 6
    elif mutation == "raw_frames":
        body["motion_data"] = {"frames": []}
    elif mutation == "account":
        body["user_id"] = "patient"
    else:
        body["affected_side"] = "both"
    assert post(client, body).status_code == 422


def test_guest_score_does_not_trust_client_walking_marks(client):
    body = result_body(("L6",))
    body["task_results"][0]["metrics"]["gait_analysis"] = {"status": "scored", "score": 100}
    report = post(client, body).json()
    assert "gait_analysis" not in report["task_results"][0]["metrics"]
    assert report["metrics"]["function_score"]["display_total"] is None


@pytest.mark.parametrize("kind,expected", [
    ("both", "detected"), ("shoulder_only", "not_detected"),
    ("face_only", "not_detected"), ("brief", "not_detected"), ("missing", "not_measured"),
])
def test_guest_quality_keeps_local_joint_face_and_shoulder_evidence(kind, expected):
    cues = {"shoulder": {"duration_ms": 650, "peak": 20}, "face": {"duration_ms": 650, "peak": 10}}
    if kind == "shoulder_only":
        cues["face"]["peak"] = 0
    elif kind == "face_only":
        cues["shoulder"]["peak"] = 0
    elif kind == "brief":
        cues["face"]["duration_ms"] = 300
    elif kind == "missing":
        cues = {}
    step = {"metrics": {"quality": {"version": VERSION, "compensations": {"trunk_lean": {
        "method": "image_face_shoulder_growth_v1", "eligible_ms": 900, "max_streak_ms": 650,
        "max_value": 20, "shoulder_peak": 20, "face_peak": 10, "cue_evidence": cues,
    }}}}}
    check = compensation_checks(step, {"id": "T1-S2", "compensations": ["trunk_lean"]})[0]
    assert check["status"] == expected


def test_guest_post_requires_review_origin_and_rejects_account_credentials(client):
    for headers in ({}, {"origin": "https://elsewhere.example"},
                    {"origin": BACKEND_ORIGIN, "authorization": "Bearer patient"},
                    {"origin": BACKEND_ORIGIN, "x-user-id": "patient"}):
        assert client.post("/api/assessment/review/results", headers=headers, json=result_body()).status_code == 403
    assert client.post("/api/assessment/review/results", headers={"origin": BACKEND_ORIGIN},
                       content=b"x" * (1024 * 1024 + 1)).status_code == 413


def test_authenticated_assessment_routes_still_require_sign_in(client, monkeypatch):
    async def no_user(*args):
        return None
    monkeypatch.setattr(server, "_user_from_header", no_user)
    assert client.get("/api/assessment/tasks?package=initial").status_code == 401
    assert client.post("/api/assessment/submit", json={"task_results": []}).status_code == 401
