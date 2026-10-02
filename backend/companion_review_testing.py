"""Synthetic review controls. No accounts, submitted measurements or persistence.

This route is separate from the loopback preview and authenticated assessment
routes. Every result is generated here and labelled as testing data.
"""
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.companion_plan_preview import companion_profile, random_task_results
from backend.function_scoring import score_function_assessment
from backend.function_rehab_plan import select_function_exercises

REVIEW_ORIGIN = "https://rehyn-recovery-companion.onrender.com"
CORE_TASKS = ("T1", "T3", "H4", "H3", "L6")


class ReviewSampleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_ids: list[Literal["T1", "T3", "H4", "H3", "L6"]] = Field(min_length=1, max_length=5)
    goal: Literal["eating", "dressing", "walking_house", "going_out", "other", ""] = ""
    movement: Literal["none", "little_help", "tires", "fairly_well", ""] = ""
    has_helper: bool = True


def build_review_sample(payload, rubrics, catalogue, eligible, rng=None):
    """Use the same pure scorer and plan selector as the local test control."""
    if len(payload.task_ids) != len(set(payload.task_ids)):
        raise HTTPException(status_code=422, detail="Choose each test task once")
    assigned = [tid for tid in CORE_TASKS if tid in payload.task_ids]
    rows = random_task_results(assigned, rng)
    score = score_function_assessment(rows, rubrics, assigned)
    profile = companion_profile({"companion_answers": {
        "main_goal": payload.goal, "arm_hand_movement": payload.movement,
        "help_at_home": "family" if payload.has_helper else "own",
    }}, score)
    # These are invented test movements, never a sitting-safety judgement.
    profile["sitting_ability"] = "independent"
    selection = select_function_exercises(score, profile, eligible)
    plan = []
    for choice in selection["exercises"]:
        exercise = catalogue[choice["code"]]
        plan.append(exercise.model_copy(update={
            **{key: value for key, value in choice.items() if key != "code"},
            "selection_reason": "Selected from generated test movement levels for " + ", ".join(choice["selection_slots"]) + ".",
            "safety_note": "Use a stable seated position and a comfortable range. Stop for pain, dizziness or new weakness.",
        }).model_dump())
    selection.update(exercises=plan, candidate_only=False)
    return {
        "preview_only": True, "testing_random": True, "saved_to_assessment": False,
        "result_provenance": "generated_testing_sample", "assessment_package": "initial",
        "task_results": rows, "metrics": {"function_score": score},
        "rehab_plan": plan, "function_rehab_plan": selection,
        "clinical_review_gate": {"rehab_access": "allowed", "rehab_plan_source": "function_levels"},
    }


def create_review_testing_router(rubrics, catalogue, eligible):
    router = APIRouter()

    @router.post("/api/assessment/review-random-results")
    async def review_random_results(payload: ReviewSampleRequest, request: Request):
        if request.headers.get("origin") != REVIEW_ORIGIN:
            raise HTTPException(status_code=403, detail="Use the companion review site's test control")
        # Reject account context: this endpoint must never become a shortcut for
        # completing an authenticated patient's real assessment.
        if any(name in request.headers for name in ("authorization", "x-user-id")) or request.query_params:
            raise HTTPException(status_code=403, detail="This control creates synthetic review data only")
        return build_review_sample(payload, rubrics, catalogue, eligible)

    return router
