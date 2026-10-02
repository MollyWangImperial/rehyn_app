"""Pure, versioned daily-function scoring; not a clinical scale.

Target completion is the ruler. Angles remain in the separate task-quality
report. Legacy records are derived on read, never changed by this module.
"""
from decimal import Decimal, ROUND_HALF_UP
from math import isfinite
from collections.abc import Mapping

try:
    from .assessment_quality import compensation_checks, task_domain, value
except ImportError:  # Direct backend-module imports used by the deployment entry.
    from assessment_quality import compensation_checks, task_domain, value


VERSION = "rehyn-function-level-1"
LADDER_VERSION = "rehyn-ladder-1"
LEVEL_POINTS = {0: 0, 1: 25, 2: 50, 3: 75, 4: 100}  # engineering default, needs clinician review
LEVEL_LABELS = {None: "Not measured", 0: "Not yet", 1: "Getting started", 2: "Partly", 3: "Can do", 4: "Can do well"}
# Rung names are the evidence contract for new ladder sessions.
TASK_RUNGS = {  # engineering default, needs clinician review
    "T1": ("r80", "r120", "r160"),
    "T3": ("chest", "mouth"), "H4": ("partial", "full"), "H3": ("partial", "full"),
}
# Earlier five-height records remain readable without rewriting their evidence.
LEGACY_REACH_RUNGS = ("r40", "r55", "r70", "r85", "r100")
LEGACY_REACH_LADDERS = (LEGACY_REACH_RUNGS, ("r40", "r70", "r100"))
LEGACY_MAIN_STEPS = {tid: f"{tid}-S2" for tid in ("T1", "T2", "T3", "H1", "H3", "H4")}
TASK_LABELS = {
    "T1": "Seated reach", "T2": "Arm raise", "T3": "Hand to mouth",
    "H1": "Hand opening", "H4": "Hand opening and closing", "H3": "Pinch", "L6": "Walking",
}
POSTURE_CHECKS = {
    "T1": ("trunk_lean", "shoulder_hike"), "T2": ("trunk_lean", "shoulder_hike"),
    "T3": ("trunk_lean", "shoulder_hike", "head_drop"),
    "H1": ("trunk_lean", "shoulder_hike", "wrist_bend"),
    "H4": ("trunk_lean", "shoulder_hike", "wrist_bend"),
    "H3": ("trunk_lean", "shoulder_hike", "wrist_bend"),
}
COMPENSATION_ACTIONS = {
    "trunk_lean": "leaning forward", "shoulder_hike": "lifting your shoulder",
    "head_drop": "bringing your head to your hand", "wrist_bend": "bending your wrist",
}
DAILY_ACTIVITY_TASKS = {  # engineering default, needs clinician review
    "Eating and drinking": ("T3", "H4"),
    "Dressing": ("T1", "H4", "H3"),
    "Grooming and self-care": ("T1", "T3", "H4"),
    "Moving around": ("L6",),
}
COMPARABLE_TASK_IDS = frozenset(("T1", "T3", "H4", "H3", "L6"))
VALID_STATUSES = frozenset(("detected", "not_detected", "not_measured"))
VALID_ASSIST = (None, "helper", "self")


def _mapping(raw):
    return raw if isinstance(raw, Mapping) else {}


def _number(raw):
    return raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) and isfinite(raw) else None


def _mean(values):
    values = [Decimal(str(item)) for item in values if item is not None]
    return sum(values) / len(values) if values else None


def round_score(score, *, display=False):
    """Round the unrounded mean once; do not average already-rounded area scores."""
    if score is None:
        return None
    rounded = Decimal(str(score)).quantize(Decimal("1" if display else "0.1"), rounding=ROUND_HALF_UP)
    return int(rounded) if display else float(rounded)


def _checks(task_id, raw):
    raw = _mapping(raw)
    return {cid: raw[cid] if isinstance(raw.get(cid), str) and raw[cid] in VALID_STATUSES else "not_measured"
            for cid in POSTURE_CHECKS.get(task_id, ())}


def _next_step(task_id, level, checks, reason):
    if task_id == "H3" and reason == "full_pinch_alone":
        return "Well done—you completed the pinch on your own. Next: build on this in everyday tasks."
    if level == 3:
        actions = [COMPENSATION_ACTIONS[cid] for cid, status in checks.items() if status == "detected"]
        if actions:
            return ("Well done—you completed the movement on your own. We detected " + " and ".join(actions)
                    + ", so full marks need the same movement with steadier posture. Next: practise without " + " or ".join(actions) + ".")
        return "Well done—you completed the movement on your own. Your posture wasn’t clear enough to confirm full marks. Next: repeat with your posture clearly in view."
    if reason == "prerequisite_not_met":
        return "Keep going—we’ll build hand opening first, then practise pinch."
    if level == 1:
        return ("Good effort—you completed the movement with help. Full marks require the full movement on your own. Next: try the easiest version on your own."
                if reason == "assisted_movement" else
                "Good effort—you got the movement started. The target wasn’t completed yet. Next: practise a small, supported movement.")
    return {
        None: "Thank you for trying. We couldn’t measure this movement clearly. Next: adjust the camera and try again.",
        0: "Thank you for giving it a try. We did not record a completed movement today. We'll start with supported movement.",
        2: ("Good effort—you reached an easier target on your own. The highest target wasn’t completed yet. Next: one circle higher."
            if task_id == "T1" else "Good effort—you completed an easier version on your own. The full movement wasn’t completed yet. Next: one step further."),
        4: "Well done—you completed the full movement on your own with steady posture. Next: build on this in everyday tasks.",
    }[level]


def _result(task_id, level, reason, *, derived=False, checks=None, **evidence):
    checks = checks or {}
    return {
        "task_id": task_id, "task_label": TASK_LABELS.get(task_id, task_id),
        "domain": task_domain(task_id), "level": level, "label": LEVEL_LABELS[level],
        "points": LEVEL_POINTS.get(level), "reason": reason, "derived": derived,
        "measured": level is not None, "best_alone": None, "best_assisted": None,
        "compensations": checks,
        "unmeasured_compensations": [cid for cid, status in checks.items() if status == "not_measured"],
        "next_step": _next_step(task_id, level, checks, reason), "clinical_measure": False,
        **evidence,
    }


def _full_result(task_id, checks, **evidence):
    if "detected" in checks.values():
        return _result(task_id, 3, "full_rung_alone_compensated", checks=checks, **evidence)
    if not checks or "not_measured" in checks.values():
        return _result(task_id, 3, "full_rung_alone_posture_unobserved", checks=checks, **evidence)
    return _result(task_id, 4, "full_rung_alone_clean", checks=checks, **evidence)


def task_level(task, rubric=None):
    """Score a task from completed attempts, not a claimed best-rung summary."""
    task_id = str(value(task, "task_id", ""))
    metrics = _mapping(value(task, "metrics"))
    if task_id == "L6":
        return _walking_result(task)
    if "ladder" not in metrics:
        return level_from_legacy(task, rubric)
    ladder = _mapping(metrics["ladder"])
    if ladder.get("version") != LADDER_VERSION or task_id not in TASK_RUNGS:
        return _result(task_id, None, "unsupported_ladder")
    protocol = ladder.get("protocol", "open_close_v1") if task_id == "H4" else None
    task_metadata = {"protocol": protocol, "task_label": "Hand opening" if protocol in {"hand_open_at_mouth_v1", "hand_open_at_chest_v2"}
                     else TASK_LABELS[task_id]} if task_id == "H4" else {}
    if task_id == "H4" and protocol not in {"open_close_v1", "hand_open_at_mouth_v1", "hand_open_at_chest_v2"}:
        return _result(task_id, None, "unsupported_ladder")
    if ladder.get("prerequisite_not_met") is True and task_id == "H3":
        return _result(task_id, 0, "prerequisite_not_met", prerequisite_not_met=True,
                       label="Not yet: comes after hand opening")
    if ladder.get("measured") is not True:
        return _result(task_id, None, "tracking_unavailable", **task_metadata)
    rungs = TASK_RUNGS[task_id]
    if task_id == "T1":
        rungs = next((old for old in LEGACY_REACH_LADDERS if ladder.get("rungs") == list(old)), rungs)
    if ladder.get("rungs") != list(rungs) or ladder.get("full_rung") != rungs[-1]:
        return _result(task_id, None, "invalid_rung_evidence")
    attempts = ladder.get("attempts")
    if not isinstance(attempts, list) or any(
        not isinstance(attempt, Mapping) or attempt.get("rung") not in rungs
        or "assist" not in attempt or attempt["assist"] not in VALID_ASSIST
        or not isinstance(attempt.get("completed"), bool) for attempt in attempts
    ):
        return _result(task_id, None, "invalid_attempt_evidence")
    completed = [attempt for attempt in attempts if attempt["completed"]]
    alone = [attempt for attempt in completed if attempt["assist"] is None]
    assisted = [attempt for attempt in completed if attempt["assist"] is not None]
    best = lambda rows: max((row["rung"] for row in rows), key=rungs.index, default=None)
    evidence = {
        **task_metadata,
        "full_rung": rungs[-1],
        "best_alone": best(alone), "best_assisted": best(assisted),
        "movement_seen": bool(completed) or ladder.get("movement_seen") is True,
        "stretch_completed": task_id == "T1" and ladder.get("stretch_completed") is True,
    }
    full = [attempt for attempt in alone if attempt["rung"] == rungs[-1]]
    if full:
        if task_id == "H3":
            # Pinch posture/steadiness criteria are deferred by the user. Keep
            # observations for review without claiming they reduce this score.
            return _result(task_id, 4, "full_pinch_alone", posture_scored=False,
                           posture_observations=_checks(task_id, full[-1].get("compensations")), **evidence)
        # A later clean full completion demonstrates the higher ability.
        candidates = [_full_result(task_id, _checks(task_id, attempt.get("compensations")), **evidence) for attempt in full]
        return max(candidates, key=lambda row: row["level"])
    if alone:
        achieved = max(alone, key=lambda row: rungs.index(row["rung"]))
        return _result(task_id, 2, "easier_rung_alone", checks=_checks(task_id, achieved.get("compensations")), **evidence)
    if assisted:
        achieved = max(assisted, key=lambda row: rungs.index(row["rung"]))
        return _result(task_id, 1, "assisted_movement", checks=_checks(task_id, achieved.get("compensations")), **evidence)
    if evidence["movement_seen"]:
        return _result(task_id, 1, "movement_seen", **evidence)
    return _result(task_id, 0, "no_movement_seen", **evidence)


def level_from_legacy(task, rubric=None):
    """Derive a level without rewriting old records or using angle attainment."""
    task_id = str(value(task, "task_id", ""))
    if task_id == "L6":
        return _walking_result(task)
    main_id = LEGACY_MAIN_STEPS.get(task_id)
    if main_id is None:
        return _result(task_id, None, "unsupported_task", derived=True)
    steps = list(value(task, "steps", []) or [])
    completed = [step for step in steps if value(step, "completed") is True]
    metrics = _mapping(value(task, "metrics"))
    if metrics.get("measured") is False:
        return _result(task_id, None, "tracking_unavailable", derived=True)
    if not completed:
        # Completion counts alone cannot tell which target was reached.
        if (value(task, "completed_steps", 0) or 0) > 0:
            return _result(task_id, None, "missing_step_evidence", derived=True)
        return _result(task_id, 0, "no_completed_steps", derived=True)
    if metrics.get("assisted") is True:
        return _result(task_id, 1, "assisted_movement", derived=True)
    main = next((step for step in completed if value(step, "step_id") == main_id), None)
    if main is not None:
        required = list(POSTURE_CHECKS[task_id])
        main_checks = compensation_checks(main, {"id": main_id, "compensations": required})
        checks = {check["id"]: check["status"] for check in main_checks}
        rules = {rule["id"]: rule for rule in _mapping(rubric).get("steps", [])}
        # Confirmed compensation during the movement still counts, even if the
        # main target was subsequently reached. Do not inspect return-to-lap.
        for step in steps:
            sid = value(step, "step_id", "")
            rule = rules.get(sid)
            if not rule:
                if sid not in {f"{task_id}-S1", main_id}:
                    continue
                rule = {"id": sid, "compensations": required}
            for check in compensation_checks(step, rule):
                if check["id"] in checks and check["status"] == "detected":
                    checks[check["id"]] = "detected"
        return _full_result(task_id, checks, derived=True)
    if any(value(step, "step_id") == f"{task_id}-S1" for step in completed):
        return _result(task_id, 2, "earlier_step_alone", derived=True)
    return _result(task_id, None, "missing_target_evidence", derived=True)


def _walking_result(task):
    metrics = _mapping(value(task, "metrics"))
    gait = _mapping(metrics.get("gait_analysis"))
    score = _number(gait.get("score")) if gait.get("status") == "scored" else None
    if metrics.get("walking_skipped") is True or score is None or not LEVEL_POINTS[0] <= score <= LEVEL_POINTS[4]:
        return _result("L6", None, "walking_not_measured", label="Not assessed")
    # Gait keeps its established continuous score; never invent a function level.
    return _result("L6", None, "existing_gait_score", points=score, measured=True,
                   label="Walking score", next_step=None, gait_analysis_version=gait.get("version"))


def daily_activity_levels(scored_tasks, assigned_task_ids=None):
    """Use the weakest assigned prerequisite; missing evidence stays estimated."""
    tasks = {task["task_id"]: task for task in scored_tasks}
    assigned = set(assigned_task_ids if assigned_task_ids is not None else tasks)
    activities = []
    for activity, prerequisites in DAILY_ACTIVITY_TASKS.items():
        needed = [tid for tid in prerequisites if tid in assigned]
        rows = [tasks.get(tid) for tid in needed]
        base = {"activity": activity, "task_ids": needed, "level": None, "label": "Not measured",
                "limited_by": [], "clinical_measure": False}
        if not needed:
            activities.append({**base, "status": "not_assessed"})
        elif any(row is None or row["points"] is None for row in rows):
            activities.append({**base, "status": "estimated"})
        elif prerequisites == ("L6",):
            activities.append({**base, "status": "complete", "label": "Walking score", "score": rows[0]["points"]})
        else:
            weakest = min(row["level"] for row in rows)
            activities.append({**base, "status": "complete", "level": weakest, "label": LEVEL_LABELS[weakest],
                               "limited_by": [row["task_id"] for row in rows if row["level"] == weakest]})
    return activities


def _area_means(tasks):
    return {domain: _mean([task["points"] for task in tasks if task["domain"] == domain])
            for domain in ("upper_limb", "hand", "lower_limb") if any(task["domain"] == domain for task in tasks)}


def compare_function_scores(current, baseline):
    """Compare shared core tasks only, with totals only across the same areas."""
    old = {row["task_id"]: row for row in baseline["tasks"]}
    pairs = [(row, old[row["task_id"]]) for row in current["tasks"]
             if row["task_id"] in COMPARABLE_TASK_IDS and row["task_id"] in old
             and (row["task_id"] != "T1" or row.get("full_rung", "r100") == old["T1"].get("full_rung", "r100"))
             and (row["task_id"] != "H4" or row.get("protocol", "open_close_v1") == old["H4"].get("protocol", "open_close_v1"))
             and row["points"] is not None and old[row["task_id"]]["points"] is not None]
    now_means = _area_means([pair[0] for pair in pairs])
    old_means = _area_means([pair[1] for pair in pairs])
    current_areas = {key for key, area in current["areas"].items() if area["score"] is not None}
    baseline_areas = {key for key, area in baseline["areas"].items() if area["score"] is not None}
    reach_benchmark_changed = any(row['task_id'] == 'T1' and 'T1' in old
                                  and row.get('full_rung', 'r100') != old['T1'].get('full_rung', 'r100')
                                  for row in current['tasks'])
    hand_benchmark_changed = any(row['task_id'] == 'H4' and 'H4' in old
                                and row.get('protocol', 'open_close_v1') != old['H4'].get('protocol', 'open_close_v1')
                                for row in current['tasks'])
    comparable = not reach_benchmark_changed and not hand_benchmark_changed and bool(pairs) and current_areas == baseline_areas == set(now_means)
    return {
        "task_ids": [pair[0]["task_id"] for pair in pairs], "total_comparable": comparable,
        "total_change": round_score(_mean(now_means.values()) - _mean(old_means.values())) if comparable else None,
        "areas": {domain: {"change": round_score(now_means[domain] - old_means[domain]), "clinical_measure": False}
                  for domain in now_means}, "clinical_measure": False,
    }


def score_function_assessment(task_results, rubrics=None, assigned_task_ids=None, *, baseline=None):
    """Return one level per camera task and an equally weighted mean of areas."""
    submitted = {str(value(task, "task_id", "")): task for task in task_results}
    assigned = list(dict.fromkeys(assigned_task_ids if assigned_task_ids is not None else submitted))
    rubrics = rubrics or {}
    tasks = [task_level(submitted[tid], rubrics.get(tid)) if tid in submitted
             else _result(tid, None, "task_missing") for tid in assigned]
    means = _area_means(tasks)
    areas = {domain: {
        "score": round_score(mean), "display_score": round_score(mean, display=True),
        "partial": any(task["points"] is None for task in tasks if task["domain"] == domain),
        "task_ids": [task["task_id"] for task in tasks if task["domain"] == domain], "clinical_measure": False,
    } for domain, mean in means.items()}
    total = _mean(means.values())
    result = {
        "version": VERSION, "total": round_score(total), "display_total": round_score(total, display=True),
        "areas": areas, "tasks": tasks, "daily_activities": daily_activity_levels(tasks, assigned),
        "clinical_measure": False,
    }
    if baseline is not None:
        result["comparison"] = compare_function_scores(result, baseline)
    return result
