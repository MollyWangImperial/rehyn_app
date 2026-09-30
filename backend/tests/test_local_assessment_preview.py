import asyncio
import os

import pytest
from starlette.requests import Request
from fastapi import HTTPException

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "rehyn_preview_tests")
from backend import server
from backend.local_assessment_preview import is_local_assessment_preview


def request(*, host="127.0.0.1", client="127.0.0.1", query="local_preview=1", headers=None):
    return Request({"type": "http", "method": "GET", "scheme": "http",
                    "path": "/api/pose/runner", "query_string": query.encode(),
                    "server": (host, 8001), "client": (client, 40000),
                    "headers": [(b"host", f"{host}:8001".encode()),
                                *((k.encode(), v.encode()) for k, v in (headers or {}).items())]})


@pytest.fixture(autouse=True)
def preview_env(monkeypatch):
    monkeypatch.setenv("REHYN_LOCAL_ASSESSMENT_PREVIEW", "1")
    monkeypatch.delenv("RENDER", raising=False)


@pytest.mark.parametrize("args", [
    {"host": "rehyn.com"}, {"client": "192.168.1.2"}, {"query": ""},
    {"query": "local_preview=1&uid=patient"},
    {"headers": {"x-user-id": "patient"}},
    {"headers": {"origin": "https://example.com"}},
    {"headers": {"x-forwarded-for": "127.0.0.1"}},
    {"headers": {"forwarded": "for=127.0.0.1"}},
])
def test_preview_rejects_nonlocal_and_account_requests(args):
    assert not is_local_assessment_preview(request(**args))


def test_preview_requires_explicit_server_opt_in(monkeypatch):
    assert is_local_assessment_preview(request(headers={"origin": "http://127.0.0.1:3000"}))
    monkeypatch.delenv("REHYN_LOCAL_ASSESSMENT_PREVIEW")
    assert not is_local_assessment_preview(request())
    monkeypatch.setenv("REHYN_LOCAL_ASSESSMENT_PREVIEW", "1")
    monkeypatch.setenv("RENDER", "true")
    assert not is_local_assessment_preview(request())


def test_preview_loads_real_initial_tasks_without_account_access(monkeypatch):
    async def unexpected(*args):
        raise AssertionError("Preview must not access patient accounts")
    monkeypatch.setattr(server, "_user_from_header", unexpected)
    result = asyncio.run(server.get_tasks(request(), package="initial"))
    assert result["preview_only"] is True
    assert [t["id"] for t in result["tasks"]] == [t["id"] for t in server.INITIAL_ASSESSMENT_TASKS]
    assert all(t["steps"] for t in result["tasks"])
    with pytest.raises(HTTPException) as error:
        asyncio.run(server.get_tasks(request(), package="initial", task_ids="made-up"))
    assert error.value.status_code == 422


def test_normal_anonymous_request_still_requires_sign_in(monkeypatch):
    async def no_user(*args):
        return None
    monkeypatch.setattr(server, "_user_from_header", no_user)
    with pytest.raises(HTTPException) as error:
        asyncio.run(server.get_tasks(request(query=""), package="initial"))
    assert error.value.status_code == 401


def test_runner_sets_preview_only_for_authorized_local_request():
    html = asyncio.run(server.pose_runner(request())).body.decode()
    assert "const LOCAL_PREVIEW_MODE = true;" in html
    assert 'taskQuery.set("local_preview", "1")' in html
    normal = asyncio.run(server.pose_runner(request(query=""))).body.decode()
    assert "const LOCAL_PREVIEW_MODE = false;" in normal


def preview_payload(task=None):
    task = task or {"task_id": "L6", "completed_steps": 0, "total_steps": 0,
                    "steps": [], "metrics": {"walking_skipped": True}}
    return server.AssessmentSubmit(assessment_package="initial", assigned_task_ids=[task["task_id"]], task_results=[task])


def test_preview_results_are_stateless_and_skipped_walking_is_unscored(monkeypatch):
    class NoDatabase:
        def __getattr__(self, name):
            raise AssertionError("Preview must not access the database")
    async def no_accounts(*args):
        raise AssertionError("Preview must not access accounts")
    monkeypatch.setattr(server, "db", NoDatabase())
    monkeypatch.setattr(server, "_user_from_header", no_accounts)
    result = asyncio.run(server.preview_assessment_results(preview_payload(), request()))
    assert result["preview_only"] and not result["saved_to_assessment"]
    assert "id" not in result
    assert result["metrics"]["task_quality"]["tasks"][0]["score"] is None


@pytest.mark.parametrize("args", [{"query": ""}, {"host": "rehyn.com"},
                                      {"client": "192.168.1.2"}, {"headers": {"origin": "https://example.com"}}])
def test_preview_scoring_rejects_nonlocal_requests(args):
    with pytest.raises(HTTPException) as error:
        asyncio.run(server.preview_assessment_results(preview_payload(), request(**args)))
    assert error.value.status_code == 403


def test_preview_scoring_uses_existing_patient_rubric():
    from backend.tests.test_assessment_task_quality import task
    payload = preview_payload(task("T1"))
    result = asyncio.run(server.preview_assessment_results(payload, request()))
    expected = server.score_assessment(payload.task_results, server.ASSESSMENT_RUBRICS, ["T1"])
    assert result["metrics"]["task_quality"] == expected
    assert expected["tasks"][0]["score"] == 100


def test_preview_does_not_accept_a_client_supplied_walking_score():
    payload = preview_payload()
    payload.task_results[0].metrics = {"gait_analysis": {"score": 100, "status": "scored"}}
    result = asyncio.run(server.preview_assessment_results(payload, request()))
    assert "gait_analysis" not in result["task_results"][0]["metrics"]
    assert result["metrics"]["task_quality"]["tasks"][0]["score"] is None


@pytest.mark.parametrize("mutation", ["duplicate_task", "wrong_task", "duplicate_step", "wrong_step"])
def test_preview_scoring_rejects_mismatched_evidence(mutation):
    payload = preview_payload()
    if mutation == "duplicate_task":
        payload.task_results *= 2
    elif mutation == "wrong_task":
        payload.assigned_task_ids = ["T1"]
    else:
        step = server.TaskStepResult(step_id="L6-S1" if mutation == "duplicate_step" else "T1-S1", completed=True)
        payload.task_results[0].steps = [step, step] if mutation == "duplicate_step" else [step]
    with pytest.raises(HTTPException) as error:
        asyncio.run(server.preview_assessment_results(payload, request()))
    assert error.value.status_code == 422
