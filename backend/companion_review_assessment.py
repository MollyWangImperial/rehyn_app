"""Guest movement checks for the companion review site, without patient accounts.

The runner/catalogue are a source snapshot of the local companion's camera runner.
Only guest transport is adapted here. Camera logic, calibration, ladders and
completion presentation remain the local runner's. No database or uploads are used.
"""
import json
from pathlib import Path
from functools import lru_cache
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.companion_plan_preview import companion_profile
from backend.companion_review_quality import score_assessment
from backend.companion_review_testing import CORE_TASKS, REVIEW_ORIGIN
from backend.function_rehab_plan import select_function_exercises
from backend.function_scoring import LADDER_VERSION, score_function_assessment

ROOT = Path(__file__).resolve().parent
MAX_RESULT_BYTES = 1024 * 1024
BACKEND_ORIGIN = "https://rehyn.onrender.com"


@lru_cache(maxsize=1)
def review_catalog():
    return json.loads((ROOT / "companion_review_catalog.json").read_text(encoding="utf-8"))


def _replace_once(html, original, replacement):
    if html.count(original) != 1:
        raise RuntimeError("The guest runner transport anchor changed")
    return html.replace(original, replacement, 1)


@lru_cache(maxsize=1)
def guest_runner_html():
    html = (ROOT / "companion_review_runner.html").read_text(encoding="utf-8")
    html = _replace_once(html, "const LOCAL_PREVIEW_MODE = false;", "const LOCAL_PREVIEW_MODE = true;")
    html = _replace_once(html, 'const CURRENT_USER_ID = URL_PARAMS.get("uid") || "";', 'const CURRENT_USER_ID = "";')
    html = _replace_once(html, 'const localReview={enabled:LOCAL_PREVIEW_MODE,',
                         'const localReview={enabled:false,')
    html = _replace_once(html, '  if(LOCAL_PREVIEW_MODE) taskQuery.set("local_preview", "1");', '')
    html = _replace_once(html, '${API_BASE}/assessment/tasks?${taskQuery.toString()}',
                         '${API_BASE}/assessment/review/tasks?${taskQuery.toString()}')
    html = _replace_once(html, '"/assessment/preview-results?local_preview=1"', '"/assessment/review/results"')
    html = _replace_once(html, '        assigned_task_ids: tasks.map(task => task.id),',
        '''        assigned_task_ids: tasks.map(task => task.id),
        review_options: {
          goal: ["eating", "dressing", "walking_house", "going_out", "other"].includes(URL_PARAMS.get("main_goal")) ? URL_PARAMS.get("main_goal") : "",
          has_helper: ladderFlow.helper === true,
        },''')
    return html


def guest_request(request):
    if any(name in request.headers for name in ("authorization", "x-user-id")) or any(
        name in request.query_params for name in ("uid", "token", "account_generation")
    ):
        raise HTTPException(403, "Use the guest movement check without account credentials")


def assigned_tasks(task_ids):
    requested = list(CORE_TASKS) if task_ids is None else task_ids
    if not requested or len(requested) != len(set(requested)) or not set(requested).issubset(CORE_TASKS):
        raise HTTPException(422, "Choose valid movement check tasks once each")
    return [tid for tid in CORE_TASKS if tid in requested]


class ReviewOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: Literal["eating", "dressing", "walking_house", "going_out", "other", ""] = ""
    has_helper: bool = False


def build_guest_report(payload, service):
    catalog = review_catalog()
    assigned = assigned_tasks(payload.assigned_task_ids)
    submitted = [task.task_id for task in payload.task_results]
    if len(submitted) != len(set(submitted)) or set(submitted) != set(assigned):
        raise HTTPException(422, "Results must match the selected movement check tasks")
    for task in payload.task_results:
        ladder = task.metrics.get("ladder")
        if isinstance(ladder, dict):
            attempts = ladder.get("attempts") or []
            if task.task_id == "L6" or ladder.get("version") != LADDER_VERSION or not isinstance(attempts, list) or len(attempts) > 5:
                raise HTTPException(422, "Unsupported movement ladder evidence")
            allowed = {f"{task.task_id}-R{i + 1}" for i in range(len(attempts))}
        else:
            allowed = {step["id"] for step in catalog["rubrics"][task.task_id]["steps"]}
        steps = [step.step_id for step in task.steps]
        if len(steps) != len(set(steps)) or not set(steps).issubset(allowed):
            raise HTTPException(422, "Invalid movement check steps")
        task.metrics.pop("gait_analysis", None)
        evidence = task.metrics.pop("gait_2d_evidence", None)
        if task.task_id == "L6" and not task.metrics.get("walking_skipped"):
            stage = service._validated_browser_gait_evidence(evidence, "local-preview-walking", task.duration_ms)
            if stage:
                task.metrics["gait_analysis"] = service.score_gait_features(stage)
    score = score_function_assessment(payload.task_results, catalog["rubrics"], assigned)
    profile = companion_profile({"companion_answers": {
        "main_goal": payload.review_options.goal,
        "help_at_home": "family" if payload.review_options.has_helper else "own",
    }}, score)
    # Match the local preview: show candidate choices when sitting support is
    # unknown, without inventing a clinical clearance or allowing their launch.
    selection = select_function_exercises(score, profile, lambda code, actual:
        service._survey_candidate_is_eligible(code, {**actual, "sitting_ability": "independent"}))
    plan = [service.EXERCISE_LIBRARY[item["code"]].model_copy(update={
        **{key: value for key, value in item.items() if key != "code"},
        "selection_reason": "Candidate from the observed movement levels.",
        "safety_note": "Review sitting support with your care team before starting this exercise.",
    }).model_dump() for item in selection["exercises"]]
    selection.update(exercises=plan, candidate_only=True)
    return {
        "preview_only": True, "review_site": True, "saved_to_assessment": False,
        "assessment_package": "initial", "task_results": [task.model_dump() for task in payload.task_results],
        "metrics": {"task_quality": score_assessment(payload.task_results, catalog["rubrics"], assigned),
                    "function_score": score},
        "rehab_plan": plan, "function_rehab_plan": selection,
        "clinical_review_gate": {"rehab_access": "blocked", "therapist_confirmation_required": True,
            "patient_message": "These are exercise choices to review. Your sitting support needs have not yet been confirmed."},
    }


def create_guest_review_router(service):
    class GuestResultRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        task_results: list[service.TaskResult] = Field(min_length=1, max_length=5)
        affected_side: Literal["left", "right"] = "right"
        assessment_package: Literal["initial"] = "initial"
        assigned_task_ids: list[Literal["T1", "T3", "H4", "H3", "L6"]] = Field(min_length=1, max_length=5)
        review_options: ReviewOptions = Field(default_factory=ReviewOptions)

    router = APIRouter()

    @router.get("/api/pose/review-runner", response_class=HTMLResponse)
    async def review_runner(request: Request):
        guest_request(request)
        return HTMLResponse(guest_runner_html(), headers={"Cache-Control": "no-store",
            "Content-Security-Policy": "frame-ancestors " + REVIEW_ORIGIN})

    @router.get("/api/assessment/review/tasks")
    async def review_tasks(request: Request, package: Literal["initial"] = "initial", task_ids: str | None = None):
        guest_request(request)
        if set(request.query_params) - {"package", "task_ids"}:
            raise HTTPException(422, "Unsupported guest task options")
        assigned = assigned_tasks(None if task_ids is None else [tid.strip() for tid in task_ids.split(",") if tid.strip()])
        catalog = review_catalog()
        return {**{key: catalog[key] for key in ("package_id", "package_title", "package_subtitle")},
            "tasks": [task for task in catalog["tasks"] if task["id"] in assigned],
            "assigned_task_ids": assigned, "voice_id": service.TTS_VOICE,
            "packages": [], "preview_only": True, "review_site": True}

    @router.post("/api/assessment/review/results")
    async def review_results(request: Request):
        guest_request(request)
        if request.headers.get("origin") not in {REVIEW_ORIGIN, BACKEND_ORIGIN} or request.query_params:
            raise HTTPException(403, "Complete the check in the companion review site")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_RESULT_BYTES:
                raise HTTPException(413, "Movement summary is too large")
        try:
            payload = GuestResultRequest.model_validate_json(body)
        except ValidationError:
            raise HTTPException(422, "Invalid movement summary") from None
        return build_guest_report(payload, service)

    return router
