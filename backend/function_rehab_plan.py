"""Deterministic exercise slots from observed function levels, not angle scores."""
TASK_CODES = {"T1": "REACH_INCOMPLETE", "T3": "H2M_IMPAIRED", "H4": "HAND_OPENING", "H3": "PINCH_IMPAIRED"}
GOAL_TASKS = {"eating": ("T3", "H4"), "dressing": ("H3", "T1", "H4"),
              "grooming": ("T1", "T3", "H4"), "drinking": ("T3", "H4")}
SLOT_COUNT = 4  # engineering default, needs clinician review


def select_function_exercises(score, profile, eligible):
    """Return up to four distinct safe exercises plus caregiver domains.

    Two lowest tasks, a goal task and a success task each get a slot. Duplicate
    exercises are combined, never replaced by an unrelated filler. Walking
    prescription and exercise-session scoring are outside this policy.
    """
    priorities = profile.get("patient_priorities") or [profile.get("primary_goal", "")]
    if isinstance(priorities, str):
        priorities = [priorities]
    goal = str(priorities[0] if priorities else "").lower()
    rows = [row for row in score.get("tasks", []) if row.get("task_id") in TASK_CODES and row.get("level") is not None]
    ranked = sorted(rows, key=lambda row: (row["level"], list(TASK_CODES).index(row["task_id"])))
    candidates = [(row, "building") for row in ranked[:2]]
    goal_tasks = next((tasks for key, tasks in GOAL_TASKS.items() if key in goal), ())
    goal_row = next((row for tid in goal_tasks for row in ranked if row["task_id"] == tid), None)
    if goal_row:
        candidates.append((goal_row, "goal"))
    if ranked:
        candidates.append((ranked[-1], "success"))
    selected, caregiver_domains = [], []
    for row, slot in candidates[:SLOT_COUNT]:
        tid, level = row["task_id"], row["level"]
        if level <= 1:
            domain = "hand" if tid.startswith("H") else "upper_limb"
            if domain not in caregiver_domains:
                caregiver_domains.append(domain)
            continue
        code = TASK_CODES[tid]
        if level == 3:
            checks = row.get("compensations") or {}
            code = "TRUNK_COMP" if checks.get("trunk_lean") == "detected" else "SHOULDER_HIKE" if checks.get("shoulder_hike") == "detected" else code
        elif level == 4:
            code = "PINCH_IMPAIRED" if tid == "H3" else "GROSS_GRASP"
        if not eligible(code, profile):
            # No escalation to a different activity after a safety rejection.
            continue
        existing = next((item for item in selected if item["code"] == code), None)
        if existing:
            existing["selection_slots"].append(slot)
            continue
        selected.append({"code": code, "source_task": tid, "function_level": level,
                         "selection_slots": [slot], "linked_goal": goal or None,
                         "difficulty": "easy" if level == 2 else "medium",
                         "target_rung": row.get("best_alone") if level == 2 else None,
                         "clinical_measure": False})
    return {"version": "rehyn-function-plan-1", "exercises": selected,
            "caregiver_domains": caregiver_domains, "clinical_measure": False}
