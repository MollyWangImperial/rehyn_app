"""Stateless companion preview helpers. They never read accounts or recordings."""
import random
from .function_scoring import LADDER_VERSION, POSTURE_CHECKS, TASK_RUNGS


def companion_profile(parameters, score):
    """Map existing answers and observed movements; never infer sitting safety."""
    profile = {key: value for key, value in parameters.items() if key != "companion_answers"}
    answers = parameters.get("companion_answers") or {}
    levels = {row["task_id"]: row.get("level") for row in score.get("tasks", [])}
    movement = answers.get("arm_hand_movement")
    profile.setdefault("affected_arm_movement", {
        "none": "no_movement", "little_help": "help_only", "tires": "some_movement", "fairly_well": "most_movements",
    }.get(movement, "some_movement" if any((levels.get(tid) or 0) >= 2 for tid in ("T1", "T3")) else "not_sure"))
    profile.setdefault("affected_hand_movement", "some_finger_movement" if any((levels.get(tid) or 0) >= 2 for tid in ("H4", "H3")) else "very_little_movement")
    helper = answers.get("help_at_home")
    if helper:
        profile.setdefault("has_caregiver", helper != "own")
    goal = answers.get("main_goal")
    if isinstance(goal, str) and goal:
        profile.setdefault("primary_goal", goal)
        profile.setdefault("patient_priorities", [goal])
    return profile


def random_task_results(task_ids, rng=None):
    """Generate labelled test evidence, then let the normal scorer calculate marks."""
    rng = rng or random.SystemRandom()
    rows = []
    hand_ready = True
    for tid in task_ids:
        if tid == "L6":
            rows.append({"task_id": tid, "completed_steps": 0, "total_steps": 0, "steps": [],
                         "metrics": {"walking_skipped": True, "generated_testing_sample": True}})
            continue
        level = rng.choice((0, 1, 2, 3, 4))
        prerequisite_not_met = tid == "H3" and not hand_ready
        if prerequisite_not_met:
            level = 0
        if tid == "H4":
            hand_ready = level >= 2
        rung = TASK_RUNGS[tid][0] if level == 2 else TASK_RUNGS[tid][-1]
        checks = {cid: "not_detected" for cid in POSTURE_CHECKS[tid]}
        if level == 3:
            checks["trunk_lean"] = "detected"
        attempt = {"rung": rung, "completed": level > 0, "assist": "helper" if level == 1 else None,
                   "compensations": checks, "near": level > 0, "duration_ms": 5200}
        rows.append({"task_id": tid, "completed_steps": int(level > 0), "total_steps": 1,
                     "duration_ms": 5200, "steps": [{"step_id": tid + "-R1", "completed": level > 0, "metrics": {}}],
                     "metrics": {"generated_testing_sample": True, "ladder": {
                         "version": LADDER_VERSION, "rungs": list(TASK_RUNGS[tid]), "full_rung": TASK_RUNGS[tid][-1],
                         "start_rung": rung, "attempts": [attempt], "best_alone": None, "best_assisted": None,
                         "movement_seen": level > 0, "stretch_completed": False, "prerequisite_not_met": prerequisite_not_met,
                         "measured": True, "stopped_by": "full_rung", **({"protocol": "hand_open_at_chest_v2"} if tid == "H4" else {}),
                     }}})
    return rows
