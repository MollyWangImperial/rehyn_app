import os

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "rehyn_testing_diagnostics_test")

from backend.assessment_quality import VERSION, testing_task_report as make_testing_report
from backend.server import ASSESSMENT_RUBRICS


def _task(task_id):
    steps = []
    for rule in ASSESSMENT_RUBRICS[task_id]["steps"]:
        quality = {
            "version": VERSION,
            "measurements": {
                criterion["metric"]: {
                    "samples": 10,
                    "value": criterion["target"],
                    "statistic_source": "target_median",
                    "series": [{"elapsed_ms": 100, "value": criterion["target"], "in_target": True}],
                }
                for criterion in rule["criteria"]
            },
            "compensations": {
                name: {"eligible_ms": 900, "max_value": 0, "max_streak_ms": 0}
                for name in rule["compensations"]
            },
        }
        steps.append({"step_id": rule["id"], "completed": True, "duration_ms": 1000,
                      "metrics": {"quality": quality}})
    return {"task_id": task_id, "steps": steps, "completed_steps": len(steps),
            "total_steps": len(steps), "duration_ms": 3000}


def test_four_testing_tasks_report_actual_references_and_score_evidence():
    for task_id in ("T2", "H1", "H3", "H4"):
        task = _task(task_id)
        report = make_testing_report(task, ASSESSMENT_RUBRICS)["task"]
        assert report["score"] == 100
        for row in report["steps"]:
            assert row["score"] == 100
            assert all(item["valid_samples"] == 10 for item in row["criteria"])
            assert all(item["statistic_source"] == "target_median" for item in row["criteria"])
            assert all(item["eligible_ms"] == 900 for item in row["compensations"])
        first_metric = task["steps"][0]["metrics"]["quality"]["measurements"]
        first_key = next(iter(first_metric))
        first_metric[first_key]["value"] *= .5
        scored = make_testing_report(task, ASSESSMENT_RUBRICS)["task"]
        assert scored["steps"][0]["score"] == 60
        assert scored["score"] == round((60 + 100 * (len(task["steps"]) - 1)) / len(task["steps"]), 1)


def test_testing_report_explains_sustained_form_penalty_and_missing_evidence():
    task = _task("H3")
    checks = task["steps"][0]["metrics"]["quality"]["compensations"]
    checks["trunk_lean"].update(max_value=20, max_streak_ms=400)
    assert make_testing_report(task, ASSESSMENT_RUBRICS)["task"]["steps"][0]["score"] == 100
    checks["trunk_lean"]["max_streak_ms"] = 600
    row = make_testing_report(task, ASSESSMENT_RUBRICS)["task"]["steps"][0]
    assert row["score"] == 80
    assert row["calculation"]["form_factor"] == .8
    assert next(check for check in row["compensations"] if check["id"] == "trunk_lean")["max_streak_ms"] == 600
    task["steps"][0]["metrics"]["quality"]["measurements"]["target_control"]["samples"] = 4
    assert make_testing_report(task, ASSESSMENT_RUBRICS)["task"]["score"] is None
