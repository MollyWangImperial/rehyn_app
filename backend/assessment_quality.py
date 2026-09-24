"""Versioned camera task-quality rubric, not a standardized clinical scale.

Targets are engineering reference ranges for the guided task, not prescribed ROM.
Missing observations never become perfect form or zero impairment. Existing saved
completion records remain valid even when they predate this measurement schema.
"""
from math import isfinite
from typing import Any, Mapping

VERSION = "rehyn-task-quality-1"
MIN_SAMPLES = 5
COMPENSATIONS = {
    "trunk_lean": {"label": "Trunk lean", "threshold": 12, "cue": "Keep your trunk comfortably upright; reach with your arm."},
    "shoulder_hike": {"label": "Excess shoulder lift", "threshold": 12, "cue": "Relax your shoulder and use a comfortable reach."},
    "head_drop": {"label": "Head moving toward hand", "threshold": 15, "cue": "Keep your head comfortable and bring your hand toward your mouth."},
    "wrist_bend": {"label": "Wrist alignment change", "threshold": 25, "cue": "Keep your wrist comfortably in line with your forearm."},
    "hip_hike": {"label": "Pelvis hiking", "threshold": 10, "cue": "Keep your pelvis level and take a smaller comfortable step."},
}


def task_domain(task_id):
    if task_id.startswith("H") or task_id in {"T4", "T5", "T6"}:
        return "hand"
    return "lower_limb" if task_id.startswith(("L", "B")) else "upper_limb"


def criterion(metric, target, label, unit="deg"):
    return {"metric": metric, "target": target, "label": label, "unit": unit}


def step_rubric(task, step):
    sid = step["id"]
    tid = task["id"]
    target = step.get("target", {}).get("landmark")
    if tid == "T1" and sid == "T1-S4":
        return {"id": sid, "label": step.get("caption", sid), "criteria": [],
                "compensations": [], "scoring_method": "target_completion"}
    criteria = []
    if tid == "T1" and sid == "T1-S1":
        criteria = [criterion("elbow_extension", 120, "Elbow extension")]
    elif tid == "T1" and sid in {"T1-S2", "T1-S3"}:
        criteria = [criterion("elbow_extension", 110, "Elbow extension"), criterion("arm_elevation", 50, "Arm elevation")]
    elif tid == "T2" and sid.endswith(("S2", "S3")):
        criteria = [criterion("elbow_extension", 150, "Elbow extension"), criterion("arm_elevation", 100, "Arm elevation")]
    elif tid in {"T2", "T3"} and sid.endswith("S1"):
        criteria = [criterion("arm_elevation", 30, "Arm elevation")]
    elif target == "MOUTH":
        criteria = [criterion("elbow_flexion", 110, "Elbow bend")]
    elif target in {"HAND_OPEN", "OBJECT_RELEASED", "PINCH_RELEASED"} or sid == "H6-S2":
        criteria = [criterion("hand_open", .8, "Hand opening", "ratio")]
    elif target in {"HAND_CLOSED", "OBJECT_GRASPED", "OBJECT_LIFTED", "OBJECT_TRANSPORTED"} or sid in {"H2-S2", "H5-S2"}:
        criteria = [criterion("hand_closed", .75, "Grasp closure", "ratio")]
    elif target == "PINCH":
        criteria = [criterion("pinch", .8, "Thumb-index pinch", "ratio")]
    elif sid == "H4-S2":
        criteria = [criterion("open_close_cycle", 1, "Close then reopen", "ratio")]
    elif sid == "H7-S2":
        criteria = [criterion("wrist_extension_change", 15, "Wrist movement from rest")]
    elif target in {"KNEE_EXTENDED", "KNEE_EXTENDED_STABLE"}:
        criteria = [criterion("knee_extension", 150, "Knee extension")]
    elif target == "TOES_LIFTED":
        criteria = [criterion("ankle_change", 10, "Ankle movement from rest")]
    elif target in {"HIP_RISEN", "STAND_UPRIGHT", "SUPPORTED_STAND_STABLE", "STEP_STANCE_STABLE"}:
        criteria = [criterion("knee_extension", 155, "Knee extension"), criterion("hip_extension", 155, "Hip extension")]
    elif target in {"AFFECTED_KNEE_LIFTED", "UNAFFECTED_KNEE_LIFTED"}:
        criteria = [criterion("other_hip_flexion" if target.startswith("UNAFFECTED") else "hip_flexion", 25, "Hip flexion")]
    elif target == "AFFECTED_FOOT_FORWARD":
        criteria = [criterion("step_distance", .15, "Step length / leg length", "ratio")]
    elif target == "WALK_ACROSS":
        criteria = [criterion("gait_alternations", 2, "Alternating steps", "count")]
    # Positioning, return and stable-hold steps use observed target accuracy AND
    # calibrated postural control, rather than a tap or timer alone.
    if not criteria:
        criteria = [criterion("target_control", .8, "Target control", "ratio")]
    comps = ["trunk_lean"]
    if task_domain(tid) != "lower_limb":
        comps.append("shoulder_hike")
    if task_domain(tid) == "hand" and tid != "H7":
        comps.append("wrist_bend")
    if tid == "T3":
        comps.append("head_drop")
    if tid in {"L4", "L5", "L6", "B5"}:
        comps.append("hip_hike")
    # These tasks intentionally ask for trunk/pelvis motion. Do not flag it.
    if tid in {"L3", "B2", "B4"}:
        comps = [c for c in comps if c != "trunk_lean"]
    return {"id": sid, "label": step.get("caption", sid), "criteria": criteria, "compensations": comps}


def build_rubrics(tasks):
    return {task["id"]: {"id": task["id"], "label": task["title"], "domain": task_domain(task["id"]),
                          "steps": [step_rubric(task, step) for step in task["steps"]]} for task in tasks}


def value(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)


def number(raw):
    return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) and isfinite(raw) else None


def score_step(step, rubric):
    if rubric.get("scoring_method") == "target_completion":
        completed = bool(value(step, "completed", False))
        score = 100 if completed else 0 if step is not None else None
        return {"step_id": rubric["id"], "label": rubric["label"], "completed": completed,
                "duration_ms": value(step, "duration_ms", 0), "score": score,
                "criteria": [], "compensations": [], "scoring_method": "target_completion",
                "status": "measured" if score is not None else "not_measured"}
    evidence = (value(step, "metrics", {}) or {}).get("quality") or {}
    current = evidence.get("version") == VERSION
    measures = evidence.get("measurements") or {}
    rows = []
    for rule in rubric["criteria"]:
        raw = measures.get(rule["metric"]) or {}
        observed = number(raw.get("value")) if current and (number(raw.get("samples")) or 0) >= MIN_SAMPLES else None
        rows.append({**rule, "observed": observed, "attainment": min(1, max(0, observed / rule["target"])) if observed is not None else None})
    checks = []
    for cid in rubric["compensations"]:
        raw = (evidence.get("compensations") or {}).get(cid) or {}
        available = current and (number(raw.get("eligible_ms")) or 0) >= 500
        comparison_lean = (cid == "trunk_lean" and rubric["id"].startswith("T1-S")
                           and raw.get("method") == "pelvis_normalized_shoulder_or_face_v1")
        # Confirm one cue held its own threshold for 0.5 s. Alternating short
        # shoulder and face hits must not add up to a sustained trunk cue.
        cue_evidence = raw.get("cue_evidence") if comparison_lean and isinstance(raw.get("cue_evidence"), Mapping) else None
        verified_cues = {}
        if cue_evidence is not None:
            for cue, threshold, inclusive in (("shoulder", 12, False), ("face", 7, True)):
                evidence_row = cue_evidence.get(cue)
                evidence_row = evidence_row if isinstance(evidence_row, Mapping) else {}
                duration = number(evidence_row.get("duration_ms")) or 0
                peak = number(evidence_row.get("peak"))
                if duration >= 500 and peak is not None and (peak >= threshold if inclusive else peak > threshold):
                    verified_cues[cue] = {"duration_ms": duration, "peak": peak, "threshold": threshold}
        sustained = bool(verified_cues) if cue_evidence is not None else (number(raw.get("max_streak_ms")) or 0) >= 500
        confirmed = available and sustained and (comparison_lean or (number(raw.get("max_value")) or 0) > COMPENSATIONS[cid]["threshold"])
        check = {"id": cid, **COMPENSATIONS[cid], "status": "detected" if confirmed else "not_detected" if available else "not_measured"}
        if rubric["id"].startswith("T3-"):
            check.update({"unit": "proxy" if cid == "head_drop" else "deg",
                          "max_value": number(raw.get("max_value")) if current else None,
                          "max_streak_ms": number(raw.get("max_streak_ms")) if current else None,
                          "eligible_ms": number(raw.get("eligible_ms")) if current else None})
        if comparison_lean:
            check.update({"method": raw["method"], "face_threshold": 7,
                          "shoulder_peak": number(raw.get("shoulder_peak")),
                          "face_peak": number(raw.get("face_peak")),
                          "confirmed_cues": verified_cues})
        checks.append(check)
    measured = current and bool(rows) and all(row["observed"] is not None for row in rows)
    # At least one posture check must be observed if the step requires them.
    measured = measured and (not checks or any(c["status"] != "not_measured" for c in checks))
    score = None
    if measured:
        rom = sum(row["attainment"] for row in rows) / len(rows)
        penalties = sum(c["status"] == "detected" for c in checks)
        score = round((20 * bool(value(step, "completed", False)) + 80 * rom) * max(.4, 1 - .2 * penalties), 1)
    return {"step_id": rubric["id"], "label": rubric["label"], "completed": bool(value(step, "completed", False)),
            "duration_ms": value(step, "duration_ms", 0), "score": score, "criteria": rows, "compensations": checks,
            "status": "not_measured" if score is None else "limited_view" if any(c["status"] == "not_measured" for c in checks) else "measured"}


def scored_gait_task(task, rubric):
    analysis = (value(task, "metrics", {}) or {}).get("gait_analysis") or {}
    gait_score = number(analysis.get("score")) if analysis.get("status") == "scored" else None
    if gait_score is None or not 0 <= gait_score <= 100:
        return None
    labels = {
        "step_length_proxy": "2D step-length proxy",
        "step_length_proxy_symmetry": "2D step-length symmetry",
        "step_time_symmetry": "Step-time symmetry",
        "rhythm_regularity": "Walking rhythm",
        "swing_clearance_proxy": "Swing-clearance proxy",
        "trunk_stability": "Trunk stability",
    }
    criteria = []
    for metric, component in (analysis.get("components") or {}).items():
        component_score = number(component.get("score")) if isinstance(component, Mapping) else None
        if component_score is None:
            continue
        criteria.append({
            "metric": metric,
            "target": 100,
            "label": labels.get(metric, metric.replace("_", " ").title()),
            "unit": "score",
            "observed": component_score,
            "attainment": min(1, max(0, component_score / 100)),
            "weight": component.get("weight"),
        })
    duration_ms = value(task, "duration_ms", 0)
    row = {
        "step_id": "L6-GAIT",
        "label": "Walking video analysis",
        "completed": True,
        "duration_ms": duration_ms,
        "score": round(gait_score, 1),
        "criteria": criteria,
        "compensations": [],
        "status": "measured",
    }
    return {
        "task_id": "L6",
        "label": rubric["label"],
        "domain": rubric["domain"],
        "score": round(gait_score, 1),
        "earned_score": round(gait_score, 1),
        "assisted": (value(task, "metrics", {}) or {}).get("assisted") is True,
        "measured_steps": 1,
        "total_steps": 1,
        "steps": [row],
        "gait_analysis_version": analysis.get("version"),
    }


def score_assessment(task_results, rubrics, assigned_task_ids=None):
    submitted = {value(task, "task_id"): task for task in task_results}
    expected = list(dict.fromkeys(assigned_task_ids if assigned_task_ids is not None else submitted))
    tasks = []
    for tid in expected:
        if tid not in rubrics:
            continue
        rubric = rubrics[tid]
        task = submitted.get(tid)
        if tid == "L6" and task is not None:
            gait_task = scored_gait_task(task, rubric)
            if gait_task is not None:
                tasks.append(gait_task)
                continue
        steps = {value(step, "step_id"): step for step in value(task, "steps", []) or []}
        rows = [score_step(steps.get(rule["id"]), rule) for rule in rubric["steps"]]
        measured = [row["score"] for row in rows if row["score"] is not None]
        # Keep every assigned step's share of the points. Partial evidence earns
        # only its observed points; it does not become a complete movement score.
        earned_score = round(sum(measured) / len(rows), 1) if measured else None
        assisted = (value(task, "metrics", {}) or {}).get("assisted") is True
        if assisted and earned_score is not None:
            earned_score = round(earned_score * .5, 1)
        score = earned_score if len(measured) == len(rows) and rows else None
        tasks.append({"task_id": tid, "label": rubric["label"], "domain": rubric["domain"], "score": score,
                      "earned_score": earned_score,
                      "assisted": assisted, "measured_steps": len(measured), "total_steps": len(rows), "steps": rows})
    modules = {}
    for domain in ("upper_limb", "hand", "lower_limb"):
        selected = [task for task in tasks if task["domain"] == domain]
        weight = 100 / len(selected) if selected else 0
        for task in selected:
            task["module_weight"] = round(weight, 2)
            task["earned_module_points"] = round(task["earned_score"] / 100 * weight, 2) if task["earned_score"] is not None else None
        complete = bool(selected) and all(task["score"] is not None for task in selected)
        measured_steps = sum(task["measured_steps"] for task in selected)
        earned_score = round(sum(task["earned_score"] or 0 for task in selected) / len(selected), 1) if measured_steps else None
        modules[domain] = {"score": earned_score if complete else None, "earned_score": earned_score,
                           "measured_steps": measured_steps, "total_steps": sum(task["total_steps"] for task in selected),
                           "maximum": 100, "task_count": len(selected), "measured_tasks": sum(task["score"] is not None for task in selected)}
    return {"version": VERSION, "modules": modules, "tasks": tasks, "clinical_measure": False}


TEST_METRICS = {
    "arm_elevation": ("Shoulder elevation", "deg"),
    "elbow_extension": ("Elbow extension", "deg"),
    "elbow_flexion": ("Elbow bend", "deg"),
    "reach_ratio": ("Reach distance / arm length", "ratio"),
    "trunk_lean": ("Trunk lean from starting posture", "deg"),
    "shoulder_hike": ("Excess shoulder lift", "deg"),
    "wrist_bend": ("Wrist alignment change", "deg"),
    "target_control": ("Samples inside target", "ratio"),
}


def testing_task_report(task, rubrics):
    """Calculate an ephemeral diagnostic report using the patient rubric.

    This function has no account, storage, rewards or clinical-plan side effects.
    Supplemental observations do not change the score.
    """
    tid = value(task, "task_id")
    quality = score_assessment([task], rubrics, [tid])
    result = quality["tasks"][0]
    submitted = {value(step, "step_id"): step for step in value(task, "steps", [])}
    for step, rule in zip(result["steps"], rubrics[tid]["steps"]):
        raw = submitted.get(step["step_id"])
        evidence = (value(raw, "metrics", {}) or {}).get("quality") or {}
        current = evidence.get("version") == VERSION
        observations = evidence.get("observations") or {}
        definitions = {rule["metric"]: (rule["label"], rule["unit"]) for rule in step["criteria"]}
        if tid.startswith("T"):
            definitions.update(TEST_METRICS)
        else:
            definitions.update({cid: (COMPENSATIONS[cid]["label"], "deg") for cid in rule["compensations"]})
        metrics = []
        for key, (label, unit) in definitions.items():
            observation = observations.get(key) or {}
            count = int(number(observation.get("samples")) or 0) if current else 0
            available = current and count >= MIN_SAMPLES
            metrics.append({"metric": key, "label": label, "unit": unit, "samples": count,
                            "median": number(observation.get("target_fraction" if key == "target_control" else "median")) if available else None,
                            "endpoint": number(observation.get("endpoint")) if available else None,
                            "min": number(observation.get("min")) if available else None,
                            "max": number(observation.get("max")) if available else None})
        step["measurements"] = metrics
        for criterion in step["criteria"]:
            measurement = (evidence.get("measurements") or {}).get(criterion["metric"]) or {}
            raw_series = measurement.get("series") if current else []
            series = []
            if isinstance(raw_series, list):
                for point in raw_series[:240]:
                    if not isinstance(point, Mapping):
                        continue
                    elapsed_ms, sample_value = number(point.get("elapsed_ms")), number(point.get("value"))
                    if elapsed_ms is None or sample_value is None or elapsed_ms < 0:
                        continue
                    series.append({"elapsed_ms": round(elapsed_ms), "value": sample_value,
                                   "in_target": bool(point.get("in_target", False))})
            criterion["series"] = series
            source = measurement.get("statistic_source")
            criterion["statistic_source"] = source if source in {"target_median", "movement_median", "sample_proportion", "movement_maximum"} else None
            criterion["statistic_samples"] = number(measurement.get("statistic_samples")) if current else None
            criterion["peak_elapsed_ms"] = number(measurement.get("peak_elapsed_ms")) if current and source == "movement_maximum" else None
        completion_only = step.get("scoring_method") == "target_completion"
        if completion_only:
            step["calculation"] = {"completion_points": step["score"], "range_points": 0,
                                   "detected_compensations": 0, "form_factor": 1}
        else:
            detections = sum(check["status"] == "detected" for check in step["compensations"])
            rom = (sum(rule["attainment"] for rule in step["criteria"]) / len(step["criteria"])
                   if all(rule["attainment"] is not None for rule in step["criteria"]) else None)
            step["calculation"] = {"completion_points": 20 if step["completed"] else 0,
                                   "range_points": round(80 * rom, 3) if rom is not None else None,
                                   "detected_compensations": detections, "form_factor": max(.4, 1 - .2 * detections)}
        adaptation = (value(raw, "metrics", {}) or {}).get("testing_reach")
        if tid == "T1" and isinstance(adaptation, Mapping) and adaptation.get("version") == "testing-reach-adaptation-1":
            # Testing-only engineering rubric. Keep original angle benchmarks;
            # easier targets must not redefine full independent movement credit.
            levels = [1, .85, .70, .55, .40]
            level = adaptation.get("final_level")
            valid = isinstance(level, int) and not isinstance(level, bool) and 0 <= level < len(levels)
            difficulty = (1 if step["step_id"] == "T1-S4" else levels[level]) if valid else None
            assisted = adaptation.get("assisted")
            valid = valid and isinstance(assisted, bool)
            assistance_factor = .5 if assisted is True else 1
            c = step["calculation"]
            raw_range = c["range_points"]
            c.update({"raw_range_points": raw_range, "difficulty_factor": difficulty,
                      "assistance_factor": assistance_factor})
            c["range_points"] = (0 if completion_only else round(raw_range * difficulty, 3)
                                  if valid and raw_range is not None else None)
            c["assistance_factor"] = 1 if completion_only else assistance_factor
            if not completion_only:
                step["score"] = (round((c["completion_points"] + c["range_points"]) * c["form_factor"] * assistance_factor, 1)
                                 if valid and step["score"] is not None and c["range_points"] is not None else None)
            step["adaptation"] = {"difficulty": difficulty, "assisted": assisted is True,
                                  "reduction_count": len(adaptation.get("reductions") or []),
                                  "support_available": adaptation.get("support_available") is True,
                                  "axis": "distance" if step["step_id"] == "T1-S1" else "height",
                                  "inherited": step["step_id"] == "T1-S3" and (adaptation.get("initial_level") or 0) > 0,
                                  "learning": adaptation.get("learning"),
                                  "learning_history": adaptation.get("learning_history") or []}
            if not valid and not completion_only:
                step["status"] = "not_measured"
    if any("adaptation" in step for step in result["steps"]):
        measured = [step["score"] for step in result["steps"] if step["score"] is not None]
        result["adaptation_applied"] = True
        result["assisted"] = any(step.get("adaptation", {}).get("assisted") for step in result["steps"])
        result["earned_score"] = round(sum(measured) / len(result["steps"]), 1) if measured else None
        result["score"] = result["earned_score"] if len(measured) == len(result["steps"]) else None
        result["measured_steps"] = len(measured)
    return {"version": VERSION, "task": result,
            "duration_ms": sum(step["duration_ms"] for step in result["steps"]),
            "completed_steps": sum(step["completed"] for step in result["steps"]),
            "recorded": False, "clinical_measure": False}
