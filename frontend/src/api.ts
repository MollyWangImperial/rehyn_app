import { authedFetch } from "@/src/auth";
import { API_BASE as BASE } from "@/src/config";

export type TaskStep = {
  id: string;
  voice: string;
  caption: string;
  target: { x: number; y: number; r: number; landmark: string };
  hold_ms: number;
  measure?: string[];
  movement_required?: boolean;
  failure_phenotype?: {
    code: string;
    domain: string;
    label: string;
    description: string;
    severity: string;
    source: string;
    rehab_code: string;
  };
};

export type Task = {
  id: string;
  title: string;
  view: string;
  focus: string;
  steps: TaskStep[];
  advanced_marker_required?: boolean;
  advanced_label?: string;
  recommended_objects?: string[];
  safety_tier?: "seated" | "spotter_required";
  safety_note?: string;
};

export type AssessmentPackageId = "initial" | "upper_limb" | "hand" | "lower_limb" | "balance";

export type AssessmentPackage = {
  id: AssessmentPackageId;
  title: string;
  subtitle: string;
  task_count: number;
};

export type TestingAssessmentTask = {
  id: string;
  title: string;
  view: string;
  focus: string;
  step_count: number;
  safety_tier: "seated" | "spotter_required";
  safety_note?: string | null;
};

export type TestingAssessmentPackage = {
  id: Exclude<AssessmentPackageId, "initial">;
  title: string;
  subtitle: string;
  tasks: TestingAssessmentTask[];
};

export type TestingExercise = {
  id: string;
  name: string;
  description: string;
  sets: number;
  reps: number;
  guided_reps: number;
  frequency: string;
  targets_issue: string;
  source: string;
  safety_note?: string | null;
  pose_mode: "body" | "tap" | "guided";
  support_required: boolean;
};

export type TestingLibrary = {
  assessment_task_count: number;
  exercise_count: number;
  assessment_packages: TestingAssessmentPackage[];
  exercises: TestingExercise[];
  test_runs_are_recorded: false;
};

export type TaskVideo = {
  id: string;
  user_id: string;
  package_id: AssessmentPackageId;
  task_id: string;
  duration_ms: number;
  content_type: string;
  created_at: string;
  filename: string;
  size_bytes: number;
  storage: "gridfs" | "local" | "r2";
};

export type PatientInsightActivation = {
  task_id: string;
  domain: "upper_limb" | "hand" | "lower_limb";
  muscle: string;
  label: string;
  mean: number;
  peak: number;
  template_mean: number | null;
  delta_mean: number | null;
};

export type PatientInsights = {
  version: string;
  status: "processing" | "research_ready" | "validated" | "needs_review";
  badge: string;
  headline: string;
  summary: string;
  domain_metrics: {
    domain: "upper_limb" | "hand" | "lower_limb";
    label: string;
    completion_percent: number;
    findings_count: number;
    status: string;
  }[];
  activation_profile: PatientInsightActivation[];
  modeled_domains: string[];
  observations: { title: string; detail: string }[];
  analysis_order: string[];
  reporting_rule: string;
};

export type MovementSnapshotDecision = {
  version: string;
  status: string;
  affected_side: "left" | "right";
  step_outcomes: {
    task_id: string;
    step_id: string;
    completed: boolean;
    failure_code?: string | null;
  }[];
  triggered_thresholds: {
    task_id: string;
    source: string;
    metric: string;
    observed: number | boolean;
    operator: string;
    threshold: number | boolean;
    finding_code: string;
    severity_rule?: string;
  }[];
  functional_findings: {
    code: string;
    label: string;
    severity: string;
    related_task: string;
    related_step?: string | null;
    phenotype_domain?: string | null;
    category: "upper_limb" | "hand" | "lower_limb" | "other";
  }[];
  body_function_domains: {
    domain: string;
    status: string;
    findings_count: number;
    step_completion_percent: number;
  }[];
  model_status: {
    overall: string;
    gpu_stage: Record<string, unknown>;
    musculoskeletal_stage: Record<string, unknown>;
  };
  primary_finding?: {
    code: string;
    label: string;
    category: "upper_limb" | "hand" | "lower_limb" | "other";
  } | null;
  selection_rule: {
    strategy: string;
    candidate_issue_codes: string[];
    selected_issue_code?: string | null;
    no_issue_sentinel_excluded: boolean;
  };
  presentation: {
    eyebrow: string;
    title: string;
    summary: string;
    tone: "attention" | "pending" | "well";
  };
  anatomy_marker: {
    visible: boolean;
    region?: string | null;
    side?: "left" | "right" | null;
    source_issue_code?: string | null;
    coordinate_source?: string | null;
    reason: string;
  };
};

export type PatientAssessmentSummary = {
  id: string;
  created_at: string;
  assessment_package: AssessmentPackageId;
  collection: {
    tasks_collected: number;
    tasks_expected: number;
    completed_steps: number;
    total_steps: number;
    completion_percent: number;
    duration_ms: number;
    domains: { domain: string; label: string }[];
  };
  body_function_summary: {
    version: string;
    overall_status: "analysis_pending" | "review_recommended" | "no_observable_difficulty";
    model_analysis_complete: boolean;
    domains: BodyFunctionDomainSummary[];
    reporting_rule: string;
  };
  movement_snapshot_decision?: MovementSnapshotDecision;
  functional_metrics?: FunctionalMetrics;
  rehab_plan_ready: boolean;
  clinical_review_gate: ClinicalReviewGate;
  insights: PatientInsights;
};

export type FunctionalMetrics = {
  task_quality?: AssessmentTaskQuality;
  shoulder_flexion_deg?: number | null;
  trunk_lean_deg?: number | null;
  reach_completion?: number | null;
  bilateral_symmetry?: number | null;
  pinch_grip?: number | null;
  hand_opening?: number | null;
  walking_skipped?: boolean;
  domains?: {
    upper_limb?: {
      survey_answered?: boolean;
      survey_affected?: boolean | null;
      survey_affected_sides?: ("left" | "right")[];
      observed?: boolean;
      step_completion_percent?: number | null;
      shoulder_elevation_deg?: number | null;
      trunk_lean_deg?: number | null;
      shoulder_hike_detected?: boolean;
    };
    hand?: {
      survey_answered?: boolean;
      survey_affected?: boolean | null;
      survey_affected_sides?: ("left" | "right")[];
      observed?: boolean;
      step_completion_percent?: number | null;
      hand_opening_percent?: number | null;
      pinch_control_percent?: number | null;
    };
    lower_limb?: {
      survey_answered?: boolean;
      survey_affected?: boolean | null;
      survey_affected_sides?: ("left" | "right")[];
      observed?: boolean;
      reported?: boolean;
      skipped?: boolean;
      result_source?: "survey" | string | null;
      survey_score?: number | null;
      reported_assistance_level?: string | null;
      walking_video_uploaded?: boolean;
      step_completion_percent?: number | null;
      bilateral_motion_symmetry_percent?: number | null;
      full_body_visibility_percent?: number | null;
      video_duration_seconds?: number | null;
    };
  };
};

export type AssessmentTaskQuality = {
  version: string;
  modules: Record<"upper_limb" | "hand" | "lower_limb", {
    score: number | null; earned_score?: number | null; maximum: number; task_count: number; measured_tasks: number;
    measured_steps?: number; total_steps?: number;
    score_source?: "survey" | "camera" | string;
    reported_assistance_level?: string | null;
    survey_response?: string | null;
  }>;
  tasks: {
    task_id: string; label: string; domain: string; score: number | null; module_weight: number; earned_module_points: number | null;
    earned_score?: number | null; assisted: boolean; measured_steps: number; total_steps: number;
    steps: {
      step_id: string; label: string; completed: boolean; duration_ms: number; score: number | null; status: string;
      criteria: {metric: string; label: string; target: number; observed: number | null; unit: string}[];
      compensations: {id: string; label: string; cue: string; status: string}[];
    }[];
  }[];
};

export type TestingTaskResult = {
  task_id: string;
  completed_steps: number;
  total_steps: number;
  duration_ms: number;
  steps: { step_id: string; completed: boolean; duration_ms: number; metrics: Record<string, unknown> }[];
  metrics: Record<string, unknown>;
};

export type LocalAssessmentRecording = {
  status: "saved" | "video_saved" | "download_requested" | "error";
  id?: string; filename?: string; path?: string; evidence_path?: string; playback_url?: string;
  duration_ms?: number; size_bytes?: number; error?: string; warning?: string | null;
};

export type TestingAssessmentReport = {
  version: string;
  completed_steps: number;
  duration_ms: number;
  recorded: false;
  clinical_measure: false;
  task: Omit<AssessmentTaskQuality["tasks"][number], "steps"> & {
    adaptation_applied?: boolean;
    steps: (Omit<AssessmentTaskQuality["tasks"][number]["steps"][number], "criteria" | "compensations"> & {
      scoring_method?: "target_completion";
      criteria: {
        metric: string; label: string; target: number; observed: number | null; unit: string; attainment: number | null;
        statistic_source?: "target_median" | "movement_median" | "sample_proportion" | "movement_maximum" | null;
        statistic_samples?: number | null;
        peak_elapsed_ms?: number | null;
        series?: { elapsed_ms: number; value: number; in_target: boolean }[];
      }[];
      compensations: { id: string; label: string; cue: string; status: string; threshold: number;
        unit?: string; max_value?: number | null; max_streak_ms?: number | null; eligible_ms?: number | null;
        method?: string; face_threshold?: number; shoulder_peak?: number | null; face_peak?: number | null;
        confirmed_cues?: Record<string, { duration_ms: number; peak: number; threshold: number }> }[];
      measurements: { metric: string; label: string; unit: string; samples: number; median: number | null; endpoint: number | null; min: number | null; max: number | null }[];
      calculation: { completion_points: number; range_points: number | null; detected_compensations: number; form_factor: number; raw_range_points?: number | null; difficulty_factor?: number | null; assistance_factor?: number };
      adaptation?: { difficulty: number | null; assisted: boolean; reduction_count: number; support_available: boolean; axis: string; inherited: boolean; learning?: TestingReachLearning | null; learning_history?: TestingReachLearning[] };
    })[];
  };
};

type TestingReachLearning = { success: boolean; terminal_reward: number; reductions: number; updates: { state: string; action: number; probabilities: number[]; return: number; advantage: number }[] };

export async function scoreTestingAssessment(result: TestingTaskResult): Promise<TestingAssessmentReport> {
  const response = await authedFetch("/api/testing/assessment-score", {
    method: "POST", body: JSON.stringify(result),
  });
  if (!response.ok) throw new Error("Could not calculate the test results. Please retry.");
  return response.json();
}

export type BodyFunctionDomainSummary = {
  domain: "upper_limb" | "hand" | "lower_limb";
  label: string;
  status: "analysis_pending" | "review_recommended" | "no_observable_difficulty" | "not_observed" | "survey_reported" | "survey_reported_affected" | "survey_reported_unaffected" | "survey_unsure";
  score_source?: "survey" | string;
  survey_score?: number | null;
  survey_answered?: boolean;
  survey_affected?: boolean | null;
  survey_affected_sides?: ("left" | "right")[];
  tasks_completed: number;
  tasks_observed: number;
  step_completion_percent: number;
  average_task_duration_ms: number;
  findings_count: number;
  summary: string;
};

export type ClinicalReviewGate = {
  version?: string;
  status: "clear" | "awaiting_model_analysis" | "therapist_confirmation_required" | "no_rehab_needed";
  rehab_access: "allowed" | "interim" | "blocked" | "not_needed";
  rehab_plan_source?: "survey_reported_problems" | string;
  interim_plan_available?: boolean;
  reason_code?: string;
  therapist_confirmation_required?: boolean;
  patient_title?: string;
  patient_message?: string;
  next_step?: string;
  reported_domains?: string[];
  objective_evidence?: {
    task_collection_complete: boolean;
    validated_model_complete: boolean;
    camera_findings_count: number;
    model_findings_count: number;
  };
  reporting_rule?: string;
};

export type FunctionalIssue = {
  code: string;
  label: string;
  description: string;
  source: string;
  severity: string;
  related_task: string;
  related_step?: string | null;
  phenotype_domain?: string | null;
};

export type RehabExercise = {
  id: string;
  name: string;
  description: string;
  sets: number;
  reps: number;
  frequency: string;
  targets_issue: string;
  source: string;
  selection_reason?: string | null;
  linked_goal?: string | null;
  safety_note?: string | null;
  requires_clinician_confirmation?: boolean;
};

export type Assessment = {
  id: string;
  created_at: string;
  affected_side: string;
  assessment_package?: AssessmentPackageId;
  testing_shortcut?: boolean;
  result_provenance?: "observed_assessment" | "generated_testing_sample" | string;
  assigned_task_ids?: string[];
  metrics?: FunctionalMetrics;
  patient_parameters?: { age_band?: string; [key: string]: unknown };
  functional_issues: FunctionalIssue[];
  rehab_plan: RehabExercise[];
  domain_assessments: {
    domain: string;
    label: string;
    task_count: number;
    completed_steps: number;
    total_steps: number;
    completion_percent: number;
    interpretation: string;
    method: string;
    clinical_status: string;
  }[];
  clinician_measures: {
    code: string;
    value: unknown;
    method: string;
    provenance: string;
    clinical_status: string;
  }[];
  biomechanical_estimates: {
    code: string;
    label: string;
    value: unknown;
    unit?: string | null;
    method: string;
    provenance: string;
    confidence: string;
    interpretation: string;
  }[];
  measurement_form?: {
    version: string;
    reporting_rule: string;
    summary: {
      auto_filled: number;
      model_filled: number;
      clinician_filled: number;
      tool_filled: number;
      pending: number;
    };
    domains: {
      domain: string;
      label: string;
      auto_filled: number;
      model_filled: number;
      clinician_filled: number;
      tool_filled: number;
      pending: number;
      rows: {
        code: string;
        label: string;
        scale: string;
        source_type: string;
        source_label: string;
        status: string;
        value: unknown;
        unit?: string | null;
        method: string;
        confidence: string;
        requirement: string;
        applicable_to_submitted_tasks: boolean;
      }[];
    }[];
    scale_readiness: {
      scale: string;
      status: string;
      formal_score: unknown;
      reason: string;
    }[];
  };
  muscle_activation_diagnosis?: MuscleActivationDiagnosis;
  survey_consistency?: SurveyConsistency;
  analysis_pipeline?: AnalysisPipeline;
  clinical_review_gate?: ClinicalReviewGate;
  patient_insights?: PatientInsights;
  movement_snapshot_decision?: MovementSnapshotDecision;
  rehabilitation_goals?: {
    version: string;
    method: string;
    status: string;
    short_term: RehabilitationGoal[];
    long_term: RehabilitationGoal[];
    missing_information: string[];
    safety_rule: string;
    generation_rule: string;
    measurement_form_version?: string | null;
  };
};

export type SurveyConsistency = {
  version: string;
  method: string;
  overall_status: string;
  counts: Record<"consistent" | "discordant" | "not_addressed" | "survey_only", number>;
  reported_domains: string[];
  survey_evidence: string[];
  findings: {
    issue_code?: string | null;
    issue_label: string;
    domain: string;
    domain_label: string;
    status: "consistent" | "discordant" | "not_addressed" | "survey_only";
    interpretation: string;
    action: string;
  }[];
  reporting_rule: string;
};

export type AnalysisPipeline = {
  version: string;
  overall_status: string;
  stages: {
    id: string;
    label: string;
    status: "complete" | "insufficient_data" | "not_run" | "screening_only";
    method: string;
    evidence?: Record<string, unknown>;
    limitation?: string;
    reason?: string | null;
  }[];
  model_outputs: {
    activation_available: boolean;
    muscle_force_available: boolean;
    confidence: string;
  };
  reporting_rule: string;
};

export type MuscleActivationFinding = {
  code: string;
  package: string;
  anomaly_type: "hypoactivation" | "hyperactivation" | "timing_disorder" | "co_contraction";
  anomaly_label: string;
  label: string;
  muscles: string;
  pipeline_route: string;
  pipeline_route_label: string;
  severity: string;
  interpretation: string;
  evidence_metrics: Record<string, unknown>;
  related_tasks: string[];
  citation: string;
};

export type MuscleActivationDiagnosis = {
  version: string;
  method: string;
  reporting_rule: string;
  anomaly_taxonomy: Record<string, string>;
  packages_evaluated: string[];
  findings: MuscleActivationFinding[];
};

export type RehabilitationGoal = {
  id: string;
  horizon: "short_term" | "long_term";
  domain: string;
  domain_label: string;
  statement: string;
  timeframe: string;
  baseline: string;
  target: string;
  outcome_measure: string;
  review_schedule: string;
  linked_task_ids: string[];
  linked_issue_codes: string[];
  patient_priority?: string | null;
  patient_agreement_required: boolean;
  clinician_confirmation_required: boolean;
  status: string;
  evidence: {
    id: string;
    source: string;
    title: string;
    organization: string;
    year: number;
    url: string;
    rules: string[];
    retrieval_score: number;
    matched_tags: string[];
  }[];
};

export async function fetchTasks(packageId: AssessmentPackageId = "upper_limb", assignedTaskIds?: string[]): Promise<{
  tasks: Task[];
  voice_id: string;
  package_id: AssessmentPackageId;
  package_title: string;
  package_subtitle: string;
  assigned_task_ids: string[];
  packages: AssessmentPackage[];
}> {
  const query = new URLSearchParams({ package: packageId });
  if (assignedTaskIds) query.set("task_ids", assignedTaskIds.join(","));
  const res = await authedFetch(`/api/assessment/tasks?${query.toString()}`);
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail || "Could not load assessment tasks");
  }
  return res.json();
}

export async function fetchTestingLibrary(): Promise<TestingLibrary> {
  const res = await authedFetch("/api/testing/library?patient_tasks_only=true");
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail || "Could not load the testing library");
  }
  return res.json();
}

export async function fetchHistory(): Promise<Assessment[]> {
  const res = await authedFetch("/api/assessment/history");
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail || "Could not load assessment history");
  }
  return res.json();
}

export async function completeInitialAssessmentForTesting(): Promise<Assessment> {
  const res = await authedFetch("/api/assessment/complete-for-testing", { method: "POST" });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error(body?.detail || "Could not finish the assessment for testing");
  }
  return body as Assessment;
}

export async function fetchTaskVideos(packageId: AssessmentPackageId = "initial"): Promise<TaskVideo[]> {
  const res = await authedFetch(`/api/assessment/task-videos?package=${encodeURIComponent(packageId)}`);
  if (!res.ok) return [];
  const data = await res.json();
  return Array.isArray(data?.videos) ? data.videos : [];
}

export async function fetchTaskProgress(packageId: AssessmentPackageId = "initial"): Promise<string[]> {
  const res = await authedFetch(`/api/assessment/task-progress?package=${encodeURIComponent(packageId)}`);
  if (!res.ok) throw new Error(`Could not load task progress (${res.status})`);
  const data = await res.json();
  return Array.isArray(data?.completed_task_ids) ? data.completed_task_ids.map(String) : [];
}

export async function resetTaskProgress(packageId: AssessmentPackageId = "initial"): Promise<void> {
  const res = await authedFetch(`/api/assessment/task-progress?package=${encodeURIComponent(packageId)}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`Could not reset task progress (${res.status})`);
}

export async function fetchAssessment(id: string): Promise<Assessment> {
  const res = await authedFetch(`/api/assessment/${id}`);
  if (!res.ok) throw new Error(`Could not load assessment (${res.status})`);
  return res.json();
}

export async function fetchPatientAssessmentSummary(id: string): Promise<PatientAssessmentSummary> {
  const res = await authedFetch(`/api/assessment/${id}/patient-summary`);
  if (!res.ok) throw new Error(`Could not load assessment summary (${res.status})`);
  return res.json();
}

export const POSE_RUNNER_URL = `${BASE}/api/pose/runner`;
