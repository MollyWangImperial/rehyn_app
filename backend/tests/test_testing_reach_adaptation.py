from backend.assessment_quality import VERSION, build_rubrics, testing_task_report as report

TASK = {"id": "T1", "title": "Seated Forward Reach", "steps": [
    {"id": f"T1-S{i}", "caption": f"Step {i}", "target": {"landmark": "LAP_DYNAMIC" if i == 4 else "WRIST"}}
    for i in range(1, 5)]}
RULES = build_rubrics([TASK])


def attempt(level=0, assisted=False):
    return {"task_id": "T1", "steps": [{"step_id": rule["id"], "completed": True, "duration_ms": 1000, "metrics": {
        "quality": {"version": VERSION, "measurements": {r["metric"]: {"value": r["target"], "samples": 10} for r in rule["criteria"]},
                    "compensations": {c: {"eligible_ms": 1000, "max_value": 0, "max_streak_ms": 0} for c in rule["compensations"]}},
        "testing_reach": {"version": "testing-reach-adaptation-1", "final_level": level, "assisted": assisted, "reductions": []}}}
        for rule in RULES["T1"]["steps"]]}


def test_fixed_references_and_reduced_movement_points_with_per_step_assistance():
    full = report(attempt(), RULES)["task"]
    easier = report(attempt(2), RULES)["task"]
    assisted = report(attempt(2, True), RULES)["task"]
    assert full["score"] == 100
    assert [s["score"] for s in easier["steps"]] == [76, 76, 76, 100]
    assert easier["score"] == 82
    assert assisted["score"] == 53.5  # return-to-lap remains completion-only
    assert assisted["steps"][3]["score"] == 100
    assert assisted["steps"][3]["calculation"]["assistance_factor"] == 1
    assert easier["steps"][1]["criteria"][0]["target"] == 110
    assert easier["steps"][1]["calculation"]["raw_range_points"] == 80
    assert easier["steps"][1]["calculation"]["range_points"] == 56


def test_missing_tracking_is_not_zero_function_or_perfect_form():
    data = attempt()
    data["steps"][1]["metrics"]["quality"] = {}
    result = report(data, RULES)["task"]
    assert result["score"] is None
    assert result["steps"][1]["score"] is None
    assert result["measured_steps"] == 3


def test_invalid_adaptation_does_not_award_full_credit_and_legacy_scoring_is_preserved():
    data = attempt()
    data["steps"][0]["metrics"]["testing_reach"]["final_level"] = -1
    assert report(data, RULES)["task"]["score"] is None
    data = attempt()
    for s in data["steps"]:
        del s["metrics"]["testing_reach"]
    assert report(data, RULES)["task"]["score"] == 100


def test_face_only_comparison_lean_reduces_testing_reach_score():
    data = attempt()
    face_only = data["steps"][1]["metrics"]["quality"]["compensations"]["trunk_lean"]
    face_only.update(method="pelvis_normalized_shoulder_or_face_v1", max_value=8.1,
                     max_streak_ms=600, face_peak=8.1, shoulder_peak=0)
    step = report(data, RULES)["task"]["steps"][1]
    check = next(item for item in step["compensations"] if item["id"] == "trunk_lean")
    assert check["status"] == "detected"
    assert check["face_threshold"] == 7
    assert check["face_peak"] == 8.1
    assert step["score"] == 100
    assert report(data, RULES)["task"]["score"] == 15
    assert report(data, RULES)["task"]["score_before_compensation"] == 100

    face_only.pop("method")
    assert report(data, RULES)["task"]["steps"][1]["score"] == 100


def test_one_or_multiple_compensations_override_the_entire_exercise():
    for ids in (("trunk_lean",), ("shoulder_hike",), ("trunk_lean", "shoulder_hike")):
        data = attempt(2, True)
        for cid in ids:
            data["steps"][0]["metrics"]["quality"]["compensations"][cid].update(max_value=20, max_streak_ms=600)
        task = report(data, RULES)["task"]
        assert [s["score"] for s in task["steps"]] == [38, 38, 38, 100]
        assert task["score_before_compensation"] == 53.5
        assert task["score"] == task["earned_score"] == 15
        assert len(task["compensation_override"]["detected"]) == len(ids)


def test_exact_override_even_for_low_average_and_brief_noise_does_not_count():
    data = attempt()
    for step in data["steps"]:
        step["completed"] = False
        for measurement in step["metrics"]["quality"]["measurements"].values():
            measurement["value"] = 0
    first = data["steps"][0]
    check = first["metrics"]["quality"]["compensations"]["shoulder_hike"]
    check.update(max_value=20, max_streak_ms=600)
    assert report(data, RULES)["task"]["score_before_compensation"] == 0
    assert report(data, RULES)["task"]["score"] == 15
    check["max_streak_ms"] = 499
    assert report(data, RULES)["task"]["compensation_override"] is None
    assert report(data, RULES)["task"]["score"] == 0


def test_testing_requires_both_compensation_checks_and_keeps_standard_assessment_unchanged():
    from backend.assessment_quality import score_assessment
    data = attempt()
    checks = data["steps"][1]["metrics"]["quality"]["compensations"]
    checks["shoulder_hike"].update(max_value=20, max_streak_ms=600)
    assert score_assessment([data], RULES)["tasks"][0]["steps"][1]["score"] == 80
    del checks["trunk_lean"]
    assert report(data, RULES)["task"]["steps"][1]["score"] is None
    assert report(data, RULES)["task"]["score"] == 15
    assert report(data, RULES)["task"]["score_before_compensation"] is None


def test_compensation_override_applies_without_adaptation_metadata():
    data = attempt()
    for step in data["steps"]:
        del step["metrics"]["testing_reach"]
    data["steps"][2]["metrics"]["quality"]["compensations"]["shoulder_hike"].update(max_value=18, max_streak_ms=700)
    assert report(data, RULES)["task"]["score"] == 15


def test_compensation_during_return_overrides_total_but_keeps_lap_points():
    data = attempt()
    data["steps"][3]["metrics"]["quality"]["compensations"]["shoulder_hike"] = {
        "eligible_ms": 1000, "max_value": 20, "max_streak_ms": 700}
    task = report(data, RULES)["task"]
    assert task["steps"][3]["score"] == 100
    assert task["score"] == 15
    assert task["compensation_override"]["detected"][0]["step_id"] == "T1-S4"
