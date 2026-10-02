import os
import random

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "rehyn_review_tests")
from backend import server
from backend.companion_review_testing import (
    REVIEW_ORIGIN, ReviewSampleRequest, build_review_sample, create_review_testing_router,
)
from backend.function_rehab_plan import select_function_exercises
from backend.function_scoring import score_function_assessment


@pytest.fixture
def client(monkeypatch):
    class NoDatabase:
        def __getattr__(self, name):
            raise AssertionError("Review samples must not access patient data")
    async def no_accounts(*args):
        raise AssertionError("Review samples must not access patient accounts")
    monkeypatch.setattr(server, "db", NoDatabase())
    monkeypatch.setattr(server, "_user_from_header", no_accounts)
    app = FastAPI()
    app.include_router(create_review_testing_router(
        server.ASSESSMENT_RUBRICS, server.EXERCISE_LIBRARY, server._survey_candidate_is_eligible,
    ))
    return TestClient(app)


BODY = {"task_ids": ["T1", "T3", "H4", "H3", "L6"], "goal": "eating", "movement": "fairly_well", "has_helper": False}
PATH = "/api/assessment/review-random-results"


def test_hosted_control_returns_labelled_generated_marks_and_a_real_catalogue_plan(client):
    response = client.post(PATH, headers={"origin": REVIEW_ORIGIN}, json=BODY)
    assert response.status_code == 200
    report = response.json()
    assert report["preview_only"] and report["testing_random"]
    assert report["saved_to_assessment"] is False and "id" not in report
    assert report["result_provenance"] == "generated_testing_sample"
    assert [row["task_id"] for row in report["task_results"]] == BODY["task_ids"]
    assert report["metrics"]["function_score"]["tasks"][-1]["points"] is None
    assert all(exercise["id"] in {ex.id for ex in server.EXERCISE_LIBRARY.values()} for exercise in report["rehab_plan"])


@pytest.mark.parametrize("headers,query", [
    ({}, ""), ({"origin": "https://example.com"}, ""),
    ({"origin": REVIEW_ORIGIN, "authorization": "Bearer patient"}, ""),
    ({"origin": REVIEW_ORIGIN, "x-user-id": "patient"}, ""),
    ({"origin": REVIEW_ORIGIN}, "?uid=patient"),
])
def test_no_account_shortcuts_or_unrelated_origins(client, headers, query):
    assert client.post(PATH + query, headers=headers, json=BODY).status_code == 403


@pytest.mark.parametrize("body", [
    {**BODY, "task_results": [{"metrics": {"patient": "private"}}]},
    {**BODY, "patient_parameters": {"name": "patient"}},
    {**BODY, "goal": "private free text"}, {**BODY, "task_ids": ["T2"]},
    {**BODY, "task_ids": ["T1", "T1"]}, {**BODY, "task_ids": []},
])
def test_only_bounded_test_options_are_accepted(client, body):
    assert client.post(PATH, headers={"origin": REVIEW_ORIGIN}, json=body).status_code == 422


def test_partial_assignment_does_not_invent_arm_hand_or_walking_marks(client):
    result = client.post(PATH, headers={"origin": REVIEW_ORIGIN}, json={**BODY, "task_ids": ["L6"], "movement": "none"}).json()
    assert result["metrics"]["function_score"]["display_total"] is None
    assert set(result["metrics"]["function_score"]["areas"]) == {"lower_limb"}
    assert result["rehab_plan"] == []


def test_uses_existing_function_scorer_and_selector_without_new_score_rules():
    report = build_review_sample(ReviewSampleRequest(**BODY), server.ASSESSMENT_RUBRICS,
                                server.EXERCISE_LIBRARY, server._survey_candidate_is_eligible, random.Random(5))
    score = score_function_assessment(report["task_results"], server.ASSESSMENT_RUBRICS, BODY["task_ids"])
    assert report["metrics"]["function_score"] == score
    profile = {"primary_goal": "eating", "patient_priorities": ["eating"], "has_caregiver": False,
               "affected_arm_movement": "most_movements", "affected_hand_movement": "some_finger_movement", "sitting_ability": "independent"}
    selection = select_function_exercises(score, profile, server._survey_candidate_is_eligible)
    assert [exercise["id"] for exercise in report["rehab_plan"]] == [server.EXERCISE_LIBRARY[choice["code"]].id for choice in selection["exercises"]]
