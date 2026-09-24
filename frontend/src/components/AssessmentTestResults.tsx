import { ActivityIndicator, Linking, Pressable, ScrollView, StyleSheet, Text, useWindowDimensions, View } from "react-native";
import Svg, { Circle, Line, Polyline, Text as SvgText } from "react-native-svg";
import { LocalAssessmentRecording, TestingAssessmentReport } from "@/src/api";
import { useDisplayPreferences } from "@/src/displayPreferences";

type Props = {
  report: TestingAssessmentReport | null;
  recording?: LocalAssessmentRecording | null;
  loading: boolean;
  error: string | null;
  onRetryScore: () => void;
  onBack: () => void;
  onTryAgain: () => void;
};

type ScoredStep = NonNullable<TestingAssessmentReport["task"]>["steps"][number];
type Criterion = ScoredStep["criteria"][number];
type Compensation = ScoredStep["compensations"][number];

function measure(value: number | null | undefined, unit: string) {
  if (value == null || !Number.isFinite(value)) return "not measured";
  const scaled = unit === "ratio" ? value * 100 : value;
  return `${Number(scaled.toFixed(1))}${unit === "ratio" ? "%" : unit === "deg" ? "°" : ""}`;
}

function list(items: string[]) {
  if (!items.length) return "none";
  if (items.length === 1) return items[0];
  return `${items.slice(0, -1).join(", ")} and ${items.at(-1)}`;
}

function alternatives(items: string[]) {
  if (items.length <= 1) return items[0] || "the configured threshold is exceeded";
  return `${items.slice(0, -1).join(", ")} or ${items.at(-1)}`;
}

function metricMeaning(rule: Criterion, stepIndex: number, taskId?: string) {
  if (taskId === "T3" && rule.metric === "arm_elevation") return `Arm elevation is the model-estimated 3D angle between the shoulder-to-hip and shoulder-to-elbow vectors; 0° places the upper arm beside the torso. The ${measure(rule.target, rule.unit)} reference is the existing engineering benchmark for initiating the hand lift toward the chest. The cyan camera arc is a separate 2D projection and can differ from this scoring value.`;
  if (rule.metric === "elbow_flexion") return `Elbow bend is 180° minus the 2D shoulder–elbow–wrist internal angle, with image proportions corrected for camera aspect ratio. A straight elbow has 0° bend; a larger value means more bending. The ${measure(rule.target, rule.unit)} reference is the existing hand-to-mouth bending benchmark (equivalent to ${measure(180-rule.target, "deg")} internal extension). It represents bending the hand toward the mouth, not straightening the elbow. Reaching the mouth and maintaining the required hold are checked separately.`;
  if (rule.metric === "arm_elevation") {
    const phase = stepIndex === 0 ? "the early phase of a meaningful forward arm lift" : "a meaningful forward reach without requiring the arm to be lifted overhead";
    return `Arm elevation is the angle of the upper arm relative to the torso, where 0° is beside the body. The ${measure(rule.target, rule.unit)} reference represents ${phase}.`;
  }
  if (rule.metric === "elbow_extension") {
    const phase = stepIndex === 0 ? "a meaningful initial straightening of the elbow as the hand moves toward the first target" : "elbow straightening during the forward reach while allowing a comfortable bend";
    return `Elbow extension is the 2D image-plane angle formed by the shoulder, elbow and wrist, where 180° is a straight elbow in the camera view. The ${measure(rule.target, rule.unit)} reference represents ${phase}.`;
  }
  if (rule.metric === "target_control") {
    return `Target control is the proportion of valid camera samples in which the hand remains inside the target. The ${measure(rule.target, rule.unit)} reference allows brief tracking fluctuations while requiring the hand to remain at the target for most of the step.`;
  }
  return `${rule.label} is compared with a ${measure(rule.target, rule.unit)} reference for this phase of the movement.`;
}

function statisticExplanation(rule: Criterion) {
  if (rule.statistic_source === "movement_maximum") return "We use the maximum valid angle reached anywhere during this step. Reaching the reference once earns full angle attainment, even if you lower your arm or bend your elbow afterward. Only clearly tracked samples count; at least five valid samples are required to score the step. A single accepted peak is used, so camera tracking errors can still affect it.";
  if (rule.statistic_source === "sample_proportion") return "We use the proportion of all valid samples recorded inside the target.";
  if (rule.statistic_source === "movement_median") return "Fewer than five valid samples were recorded at the target, so we use the median of all valid movement samples.";
  return "We use the median of the valid samples recorded while the hand is at the target, which reduces the effect of an individual noisy camera frame.";
}

function trunkLeanAttempt(check: Compensation) {
  const cues = Object.entries(check.confirmed_cues || {}).filter(([, evidence]) => evidence.duration_ms >= 500);
  if (cues.length) {
    const reasons = cues.map(([name, evidence]) =>
      `the ${name} cue passed its ${measure(evidence.threshold, "deg")} threshold for ${(evidence.duration_ms / 1000).toFixed(2)} seconds (peak ${measure(evidence.peak, "deg")} during that interval)`);
    return `trunk lean was identified because ${list(reasons)}. The highest estimated shoulder and face cues across the step were ${measure(check.shoulder_peak, "deg")} and ${measure(check.face_peak, "deg")}, respectively.`;
  }
  return "trunk lean was identified, but this result does not contain separate cue durations to explain which cue triggered it.";
}

function TimeSeriesChart({ rule, brand, text, muted, border, surface }: {
  rule: Criterion; brand: string; text: string; muted: string; border: string; surface: string;
}) {
  const { width: viewportWidth } = useWindowDimensions();
  const series = (rule.series || []).filter(point => Number.isFinite(point.elapsed_ms) && Number.isFinite(point.value));
  if (!series.length) return <Text style={[styles.small, { color: muted }]}>No valid time-series samples were available for this metric.</Text>;
  const width = Math.min(720, Math.max(210, viewportWidth - 104));
  const height = viewportWidth <= 480 ? 175 : 190;
  const labelSize = viewportWidth <= 480 ? 9 : 12;
  const left = viewportWidth <= 480 ? 37 : 48, right = 8, top = 18, bottom = 34;
  const values = [...series.map(point => point.value), rule.target, ...(rule.observed == null ? [] : [rule.observed])];
  const rawMin = rule.unit === "ratio" ? 0 : Math.min(...values);
  const rawMax = rule.unit === "ratio" ? 1 : Math.max(...values);
  const padding = Math.max((rawMax - rawMin) * .12, rule.unit === "deg" ? 4 : .05);
  const yMin = rule.unit === "ratio" ? 0 : Math.max(0, rawMin - padding);
  const yMax = rule.unit === "ratio" ? 1 : rawMax + padding;
  const maxTime = Math.max(1, ...series.map(point => point.elapsed_ms));
  const x = (elapsed: number) => left + elapsed / maxTime * (width - left - right);
  const y = (value: number) => top + (yMax - value) / Math.max(.001, yMax - yMin) * (height - top - bottom);
  const points = series.map(point => `${x(point.elapsed_ms)},${y(point.value)}`).join(" ");
  const targetY = y(rule.target);
  return <View style={[styles.chart, { borderColor: border, backgroundColor: surface }]} testID={`testing-time-series-${rule.metric}`}>
    <Text style={[styles.chartTitle, { color: text }]}>{rule.label} over time</Text>
    <Svg width="100%" height={height} viewBox={`0 0 ${width} ${height}`} accessibilityLabel={`${rule.label} time series`}>
      <Line x1={left} x2={width - right} y1={height - bottom} y2={height - bottom} stroke={border} strokeWidth={1} />
      <Line x1={left} x2={left} y1={top} y2={height - bottom} stroke={border} strokeWidth={1} />
      <Line x1={left} x2={width - right} y1={targetY} y2={targetY} stroke="#B06737" strokeWidth={2} strokeDasharray="7 6" />
      {rule.observed != null && <Line x1={left} x2={width - right} y1={y(rule.observed)} y2={y(rule.observed)} stroke={muted} strokeWidth={1.5} strokeDasharray="3 5" />}
      <Polyline points={points} fill="none" stroke={brand} strokeWidth={3} strokeLinecap="round" strokeLinejoin="round" />
      {rule.statistic_source === "movement_maximum" && rule.peak_elapsed_ms != null && rule.observed != null && <Circle cx={x(rule.peak_elapsed_ms)} cy={y(rule.observed)} r={5} fill={surface} stroke={brand} strokeWidth={3} />}
      {series.map((point, index) => point.in_target && (index % Math.max(1, Math.ceil(series.length / 70)) === 0)
        ? <Circle key={`${point.elapsed_ms}-${index}`} cx={x(point.elapsed_ms)} cy={y(point.value)} r={3.4} fill="#B06737" /> : null)}
      <SvgText x={left - 5} y={top + 4} fill={muted} fontSize={labelSize} textAnchor="end">{measure(yMax, rule.unit)}</SvgText>
      <SvgText x={left - 5} y={height - bottom + 4} fill={muted} fontSize={labelSize} textAnchor="end">{measure(yMin, rule.unit)}</SvgText>
      <SvgText x={left} y={height - 9} fill={muted} fontSize={labelSize}>0 s</SvgText>
      <SvgText x={width - right} y={height - 9} fill={muted} fontSize={labelSize} textAnchor="end">{(maxTime / 1000).toFixed(1)} s</SvgText>
      <SvgText x={width - right - 3} y={Math.max(13, targetY - 7)} fill="#8A4C28" fontSize={labelSize} textAnchor="end">Reference {measure(rule.target, rule.unit)}</SvgText>
      {rule.observed != null && <SvgText x={left + 4} y={Math.max(13, y(rule.observed) - 6)} fill={muted} fontSize={labelSize}>{rule.statistic_source === "movement_maximum" ? "Maximum" : "Scoring value"} {measure(rule.observed, rule.unit)}</SvgText>}
    </Svg>
    <Text style={[styles.chartNote, { color: muted }]}>Green line: valid measurements · Brown dots: samples recorded at the target · Brown dashed line: reference · Grey dotted line: scoring statistic{rule.statistic_source === "movement_maximum" ? ` · Outlined point: maximum at ${((rule.peak_elapsed_ms ?? 0) / 1000).toFixed(2)} s` : ""}</Text>
  </View>;
}

function ReachStepExplanation({ step, index, palette, taskId }: {
  step: ScoredStep; index: number; taskId?: string; palette: ReturnType<typeof useDisplayPreferences>["palette"];
}) {
  if (step.scoring_method === "target_completion") return <View style={[styles.card, { backgroundColor: palette.surface, borderColor: palette.border }]} testID={`testing-step-${step.step_id}`}>
    <Text style={[styles.stepTitle, { color: palette.text }]}>{index + 1}. {step.label}</Text>
    <Text style={[styles.body, { color: palette.text }]}>This step is scored only by completing the calibrated lap target. Reaching and holding that target earns the full 100 points. Angle measurements, form checks, target difficulty and recorded hands-on help do not reduce this step score.</Text>
    <Text style={[styles.body, { color: palette.text }]}>{step.score === null ? "This step was not recorded, so no score was assigned." : step.completed ? "In this attempt, the lap target was completed." : "In this attempt, the lap target was not completed."}</Text>
    <Text style={[styles.equation, { color: palette.brand }]}>{step.score === null ? "Score unavailable" : `Lap target ${step.completed ? "completed" : "not completed"} = ${step.score} points`}</Text>
  </View>;
  const c = step.calculation;
  const detected = step.compensations.filter(check => check.status === "detected");
  const notMeasured = step.compensations.filter(check => check.status === "not_measured");
  const criterionResults = step.criteria.map(rule => rule.observed == null
    ? `${rule.label.toLowerCase()} could not be measured`
    : `${rule.statistic_source === "movement_maximum" ? "maximum " : ""}${rule.label.toLowerCase()} was ${measure(rule.observed, rule.unit)} against the ${measure(rule.target, rule.unit)} reference (${measure(rule.attainment, "ratio")} achieved)`).join("; ");
  const compensationNames = list(step.compensations.map(check => check.label.toLowerCase()));
  const comparisonLean = step.compensations.find(check => check.id === "trunk_lean" && check.method === "pelvis_normalized_shoulder_or_face_v1");
  const thresholds = step.compensations.map(check => check.method === "pelvis_normalized_shoulder_or_face_v1"
    ? `the calibrated shoulder cue exceeds ${measure(check.threshold, "deg")} or the face cue reaches ${measure(check.face_threshold, "deg")} for at least 0.5 seconds`
    : `${check.label.toLowerCase()} exceeds ${measure(check.threshold, check.unit || "deg")}${check.unit === "proxy" ? " proxy units" : ""} for at least 0.5 seconds`);
  const statisticText = [...new Set(step.criteria.map(statisticExplanation))].join(" ");
  const pointSummary = step.score == null ? "This step could not be scored because there was not enough valid movement or posture evidence."
    : `${step.completed ? "The target was reached, adding 20 target points" : "The target was not reached, so no target points were added"}. The measured movement contributed ${Number((c.range_points ?? 0).toFixed(1))} of 80 movement points. ${detected.length ? `${list(detected.map(check => check.label))} ${detected.length === 1 ? "was" : "were"} detected, so the form factor was reduced to ${Number(c.form_factor.toFixed(1))}.` : notMeasured.length ? `${list(notMeasured.map(check => check.label))} could not be measured, so the available posture evidence was used.` : "No compensatory movement was detected, so no form penalty was applied."}`;
  return <View style={[styles.card, { backgroundColor: palette.surface, borderColor: palette.border }]} testID={`testing-step-${step.step_id}`}>
    <Text style={[styles.stepTitle, { color: palette.text }]}>{index + 1}. {step.label}</Text>
    {step.criteria.map(rule => <Text key={`${rule.metric}-meaning`} style={[styles.body, { color: palette.text }]}>
      <Text style={styles.bold}>Movement metric: {rule.label}{"\n"}</Text>
      {metricMeaning(rule, index, taskId)} Reaching or exceeding the reference gives 100% attainment for this metric; a lower measurement gives proportional attainment. When a step uses more than one metric, their attainment percentages are averaged to award the 80 movement points.
    </Text>)}
    <Text style={[styles.body, { color: palette.text }]}>{statisticText} We also check for <Text style={styles.bold}>{compensationNames}</Text>. A compensation is identified when {alternatives(thresholds)}. {comparisonLean ? "The trunk cues compare face and shoulder size with the upright calibration, adjusted by hip size to reduce the effect of moving uniformly toward the camera. They are camera estimates, not anatomical angles." : ""}</Text>
    {taskId === "T3" && <Text style={[styles.body, { color: palette.text }]}>For angle statistics, the tracker keeps up to 120 recent valid target samples or, for the fallback, 600 recent valid movement samples. At least five valid samples are required. Target control uses all valid samples in the step. {step.criteria.map(rule => `${rule.label}: ${rule.statistic_samples ?? "unknown"} samples contributed to the statistic.`).join(" ")}</Text>}
    {taskId === "T3" && step.compensations.map(check => <Text key={check.id} style={[styles.small, { color: palette.muted }]}>{check.label}: {check.status === "detected" ? "identified" : check.status === "not_measured" ? "not measured" : "not detected"}. Highest measured value: {measure(check.max_value, check.unit || "deg")}{check.unit === "proxy" && check.max_value != null ? " proxy units" : ""}; longest continuous time above threshold: {check.max_streak_ms == null ? "not measured" : `${(check.max_streak_ms / 1000).toFixed(2)} seconds`}. Valid observation time: {check.eligible_ms == null ? "not measured" : `${(check.eligible_ms / 1000).toFixed(2)} seconds`}.</Text>)}
    <Text style={[styles.body, { color: palette.text }]}>In this attempt, {criterionResults || "the required movement statistic was not available"}. {pointSummary}</Text>
    {step.adaptation && <Text style={[styles.body, { color: palette.text }]}>
      The angle references stay fixed when the target is made easier. In this attempt, target difficulty was {measure(step.adaptation.difficulty, "ratio")}{step.adaptation.inherited ? ", carried over from the preceding reach" : ""}. {step.adaptation.reduction_count} target {step.adaptation.axis === "distance" ? "distance" : "height"} reductions occurred in this step. The {Number((c.raw_range_points ?? 0).toFixed(1))} movement points from the measured criteria were multiplied by {c.difficulty_factor ?? "an unavailable factor"}, giving {Number((c.range_points ?? 0).toFixed(1))} movement points. {step.adaptation.assisted ? "Hands-on help was confirmed, so the complete step score is multiplied by 0.5. The camera cannot separate the patient’s contribution from the helper's." : "No hands-on help was reported; the assistance factor is 1. Having someone beside you alone does not reduce the score."}
    </Text>}
    <Text style={[styles.equation, { color: palette.brand }]}>{step.score === null ? "Score unavailable" : `(${c.completion_points} target points + ${Number((c.range_points ?? 0).toFixed(1))} movement points) × ${Number(c.form_factor.toFixed(1))} form${step.adaptation ? ` × ${c.assistance_factor} assistance` : ""} = ${step.score} points`}</Text>
    <Text style={[styles.subheading, { color: palette.text }]}>Movement evidence over time</Text>
    {step.criteria.map(rule => <TimeSeriesChart key={rule.metric} rule={rule} brand={palette.brand} text={palette.text} muted={palette.muted} border={palette.border} surface={palette.page} />)}
  </View>;
}

export function AssessmentTestResults({ report, recording, loading, error, onRetryScore, onBack, onTryAgain }: Props) {
  const { palette } = useDisplayPreferences();
  const task = report?.task;
  const card = { backgroundColor: palette.surface, borderColor: palette.border };
  const isForwardReach = task?.task_id === "T1";
  const isHandToMouth = task?.task_id === "T3";
  return (
    <ScrollView style={[styles.screen, { backgroundColor: palette.page }]} contentContainerStyle={styles.scroll} testID="assessment-library-test-complete">
      <View style={styles.content}>
        <Text style={[styles.eyebrow, { color: palette.brand }]}>TASK TEST COMPLETE</Text>
        <Text accessibilityRole="header" style={[styles.title, { color: palette.text }]}>{task?.label || "Movement results"}</Text>
        {recording && <View style={[styles.card, card]} testID="assessment-local-recording">
          <Text style={[styles.heading, { color: palette.text }]}>{recording.status === "saved" ? "Assessment video saved locally" : "Assessment video"}</Text>
          {recording.path && <Text selectable style={[styles.body, { color: palette.text }]} testID="assessment-video-path">{recording.path}</Text>}
          {recording.evidence_path && <Text selectable style={[styles.small, { color: palette.muted }]}>Angle evidence and video timing: {recording.evidence_path}</Text>}
          {recording.playback_url && <Pressable accessibilityRole="link" testID="assessment-video-playback" onPress={() => void Linking.openURL(recording.playback_url!)} style={styles.link}><Text style={[styles.linkText, { color: palette.brand }]}>Open recorded video</Text></Pressable>}
          {recording.status === "saved" && <Text style={[styles.small, { color: palette.muted }]}>Includes the camera view from the start of this attempt through completion, including calibration when used, with the visible angle overlay and reference values. No microphone audio is recorded.</Text>}
          {recording.status === "download_requested" && <Text selectable style={[styles.body, { color: palette.text }]}>Browser download requested: {recording.filename}. Check the browser’s download location; a local server file was not confirmed.</Text>}
          {(recording.error || recording.warning) && <Text style={[styles.body, { color: palette.text }]}>{recording.error || recording.warning}</Text>}
        </View>}
        {loading && <View style={styles.loading}><ActivityIndicator color={palette.brand} /><Text style={[styles.body, { color: palette.text }]}>Calculating your movement results…</Text></View>}
        {error && <View accessibilityRole="alert" style={[styles.card, card]}>
          <Text style={[styles.body, { color: palette.text }]}>{error}</Text>
          <Pressable onPress={onRetryScore} accessibilityRole="button" style={styles.link}><Text style={[styles.linkText, { color: palette.brand }]}>Retry calculation</Text></Pressable>
        </View>}
        {report && task && <>
          <View style={[styles.card, styles.scoreCard, card]}>
            <Text style={[styles.heading, { color: palette.text }]}>Movement score</Text>
            <Text style={[styles.score, { color: palette.brand }]} testID="assessment-test-score">{task.score === null ? "Not available" : `${task.score} / 100`}</Text>
          </View>
          <View style={[styles.card, card]} testID="assessment-test-formula">
            <Text style={[styles.heading, { color: palette.text }]}>How the score is calculated</Text>
            {isForwardReach ? <>
              <Text style={[styles.body, { color: palette.text }]}>The <Text style={styles.bold}>Seated Forward Reach</Text> exercise is divided into four steps: <Text style={styles.bold}>{task.steps.map((step, index) => `(${index + 1}) ${step.label.toLowerCase()}`).join(", ")}</Text>. Each step is scored out of <Text style={styles.bold}>100 points</Text>, and all four steps have equal weight in the final score.</Text>
              <Text style={[styles.body, { color: palette.text }]}>For steps 1–3, the score is calculated as <Text style={styles.bold}>{task.adaptation_applied ? "(Target points + Angle points × Target difficulty) × Form factor × Assistance factor" : "(Target points + Movement points) × Form factor"}</Text>. You receive <Text style={styles.bold}>20 target points</Text> when the target is reached. The remaining <Text style={styles.bold}>80 movement points</Text> compare the measured angles with the references. Meeting or exceeding a reference receives full credit; a lower result receives a proportional score. When a step uses two metrics, their attainment percentages are averaged. We also check for trunk lean and excess shoulder lift. Each detected compensation reduces that step’s pre-penalty score by 20%. <Text style={styles.bold}>Step 4, return to lap, is completion-only:</Text> completing the calibrated lap target earns 100 points, otherwise 0. Its angle, form and assistance measurements do not reduce that step score.</Text>
              {task.steps.some(step => step.compensations.some(check => check.id === "trunk_lean" && check.method === "pelvis_normalized_shoulder_or_face_v1")) &&
                <Text style={[styles.body, { color: palette.text }]}>For trunk lean, the camera first records an upright reference from 45 valid frames. It divides the current shoulder width by its reference width and then divides that result by the corresponding hip-width change. The face cue uses ear-to-ear width in the same way. The shoulder ratio becomes arcsin(2 × (1 − 1 ÷ ratio)); the face ratio becomes arcsin(1.5 × (1 − 1 ÷ ratio)). The arcsin input is limited to 0–1, and each result is converted to degrees. These are estimated camera cues, not anatomical joint angles. Trunk lean is identified only when the shoulder cue exceeds 12° or the face cue reaches 7° continuously for at least 0.5 seconds. For any step where it was identified, the measured cue and duration appear below.</Text>}
              {task.steps.flatMap((step, index) => step.compensations.filter(check => check.id === "trunk_lean" && check.method === "pelvis_normalized_shoulder_or_face_v1" && check.status === "detected").map(check =>
                <Text key={`${step.step_id}-trunk-evidence`} style={[styles.body, { color: palette.text }]}>For step {index + 1}, {step.label.toLowerCase()}, {trunkLeanAttempt(check)} This reduced the step’s form factor by 0.2.</Text>))}
              {task.adaptation_applied && <>
                <Text style={[styles.body, { color: palette.text }]}>For steps 1–3, target difficulty starts at 100% and can fall to 85%, 70%, 55% or 40%. It scales the movement points, while the angle references stay fixed. A supported reach can still earn target points. Confirmed hands-on assistance multiplies those step scores by 0.5. The return-to-lap target does not move or receive these deductions. These are experimental Testing rules, not a clinical scale or a comparison with a “normal” person.</Text>
                <Text style={[styles.body, { color: palette.text }]}>The agent chooses a reduction of one or two levels only after an 8-second tracked attempt, spoken encouragement and a further 6-second attempt. A successful step gives a reward of 2 × remaining difficulty × assistance factor; an unfinished step gives −1. Each reduction costs 0.04, and future reward is discounted by 0.95. Speech, poor tracking and pauses do not count against the patient. Policy-gradient updates learn the reduction size; the first target always starts at full difficulty.</Text>
                {task.steps.flatMap(step => (step.adaptation?.learning_history?.length ? step.adaptation.learning_history : step.adaptation?.learning ? [step.adaptation.learning] : []).map((learning, i) => <Text key={`${step.step_id}-policy-${i}`} style={[styles.small, { color: palette.muted }]}>
                  {step.label}, {learning.success ? "completed" : "unfinished"} policy episode: agent terminal reward {learning.terminal_reward.toFixed(2)} after {learning.reductions} reductions. {learning.updates.map(update => `Chosen reduction: ${update.action} level(s); probabilities ${update.probabilities.map(p => `${(p * 100).toFixed(1)}%`).join(" / ")}; discounted return ${update.return.toFixed(3)}, advantage ${update.advantage.toFixed(3)}.`).join(" ")}
                </Text>))}
              </>}
              <Text style={[styles.body, { color: palette.text }]}>The final movement score is the <Text style={styles.bold}>average of the four step scores</Text>. In this assessment, the score is</Text>
              <Text style={[styles.equation, { color: palette.brand }]}>{task.score === null ? "A final score needs valid evidence for all four steps." : `(${task.steps.map(step => step.score).join(" + ")}) ÷ 4${task.assisted && !task.adaptation_applied ? " × 0.5 for recorded assistance" : ""} = ${task.score} / 100`}</Text>
            </> : isHandToMouth ? <>
              <Text style={[styles.body, { color: palette.text }]}>The <Text style={styles.bold}>Hand to Mouth</Text> task has four equally weighted steps: {task.steps.map((step, i) => `(${i+1}) ${step.label.toLowerCase()}`).join(", ")}. Each step is scored out of 100.</Text>
              <Text style={[styles.body, { color: palette.text }]}>Each step uses <Text style={styles.bold}>(Target points + Movement points) × Form factor</Text>. Completing the target and its hold earns 20 target points. Movement points are measured value ÷ reference × 80, capped at 80. Reaching or exceeding the reference earns all 80. Return-to-lap uses the proportion of valid samples inside the target against its 80% reference; it is not completion-only. The references are engineering benchmarks, not clinical norms or instructions to force movement.</Text>
              <Text style={[styles.body, { color: palette.text }]}>We check trunk lean, excess shoulder lift and head movement toward the hand. Each detected pattern reduces the form factor by 0.2, down to a minimum of 0.4: one pattern gives ×0.8, two ×0.6, and three ×0.4. These are proportional reductions, not fixed point deductions. Each check needs at least 0.5 seconds of valid observations and a continuous 0.5 seconds above its threshold. Missing evidence is labelled not measured; a final score needs valid scores for all four steps.</Text>
              <Text style={[styles.body, { color: palette.text }]}>Trunk lean is the angle between the current model-estimated 3D torso vector (hip midpoint to shoulder midpoint) and its upright calibration vector. It is identified above 12°. T3 uses this torso method, not T1’s face/shoulder-width ratio method. Excess shoulder lift uses the smaller of the affected shoulder’s rise relative to the other shoulder and the shortening of the ear-to-shoulder gap, both normalized by shoulder width. It converts that value with atan2(value, 0.5) to degrees, subtracts 0.12 × arm elevation to allow normal lifting, and clamps below at zero. Its threshold is above 12°.</Text>
              <Text style={[styles.body, { color: palette.text }]}>The head cue is a proxy: 60 × the increase from baseline in (nose height minus ear-midpoint height) ÷ ear-to-ear width, clamped below at zero. The denominator is at least 0.04 model-world units. A value above 15 for 0.5 seconds identifies the pattern. These are proxy units, not 15° of anatomical head tilt. Occluded landmarks or an unavailable baseline cannot establish this check.</Text>
              <Text style={[styles.body, { color: palette.text }]}>The final score is the average of the four step scores{task.assisted ? ", multiplied by 0.5 because hands-on assistance was explicitly recorded" : ". Someone merely being present does not count as hands-on assistance"}. In this attempt:</Text>
              <Text style={[styles.equation, { color: palette.brand }]}>{task.score === null ? "A final score needs valid evidence for all four steps." : `(${task.steps.map(step => step.score).join(" + ")}) ÷ 4${task.assisted ? " × 0.5 for assistance" : ""} = ${task.score} / 100`}</Text>
            </> : <>
              <Text style={[styles.body, { color: palette.text }]}>Each step is scored as <Text style={styles.bold}>(Target points + Movement points) × Form factor</Text>. Reaching the target contributes 20 points, and movement compared with the step reference contributes up to 80 points.</Text>
              <Text style={[styles.body, { color: palette.text }]}>The final movement score is the average of the equally weighted step scores.</Text>
            </>}
          </View>
          <Text style={[styles.heading, { color: palette.text }]}>Step-by-step breakdown</Text>
          {isForwardReach || isHandToMouth ? task.steps.map((step, index) => <ReachStepExplanation key={step.step_id} step={step} index={index} taskId={task.task_id} palette={palette} />)
            : task.steps.map((step, index) => <View key={step.step_id} style={[styles.card, card]} testID={`testing-step-${step.step_id}`}>
              <Text style={[styles.stepTitle, { color: palette.text }]}>{index + 1}. {step.label}</Text>
              <Text style={[styles.body, { color: palette.text }]}>{step.score === null ? "Not enough evidence to score this step." : `Movement score: ${step.score} / 100.`}</Text>
              {step.criteria.map(rule => <TimeSeriesChart key={rule.metric} rule={rule} brand={palette.brand} text={palette.text} muted={palette.muted} border={palette.border} surface={palette.page} />)}
            </View>)}
        </>}
        <View style={styles.actions}>
          <Pressable onPress={onTryAgain} accessibilityRole="button" style={[styles.primary, { backgroundColor: palette.brand }]} testID="assessment-library-try-again"><Text style={styles.primaryText}>Test task again</Text></Pressable>
          <Pressable onPress={onBack} accessibilityRole="button" style={[styles.secondary, { borderColor: palette.border }]} testID="assessment-library-back"><Text style={[styles.linkText, { color: palette.brand }]}>Back to Testing</Text></Pressable>
        </View>
      </View>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  screen: { ...StyleSheet.absoluteFillObject, zIndex: 10 },
  scroll: { flexGrow: 1, padding: 20, paddingBottom: 80 },
  content: { width: "100%", maxWidth: 1040, alignSelf: "center", gap: 18 },
  eyebrow: { fontSize: 13, fontWeight: "800", letterSpacing: 1, marginTop: 14 },
  title: { fontSize: 30, lineHeight: 38, fontWeight: "800" },
  heading: { fontSize: 21, lineHeight: 28, fontWeight: "700" },
  subheading: { fontSize: 17, lineHeight: 24, fontWeight: "700", marginTop: 4 },
  body: { fontSize: 16, lineHeight: 25 },
  small: { fontSize: 14, lineHeight: 21 },
  bold: { fontWeight: "700" },
  loading: { padding: 28, flexDirection: "row", gap: 12, alignItems: "center" },
  card: { padding: 20, borderRadius: 18, borderWidth: 1, gap: 14 },
  scoreCard: { gap: 8 },
  score: { fontSize: 38, lineHeight: 46, fontWeight: "800" },
  stepTitle: { fontSize: 19, fontWeight: "700", lineHeight: 27 },
  equation: { fontSize: 16, lineHeight: 25, fontWeight: "700" },
  chart: { borderWidth: 1, borderRadius: 14, padding: 12, overflow: "hidden" },
  chartTitle: { fontSize: 15, lineHeight: 22, fontWeight: "700", marginBottom: 4 },
  chartNote: { fontSize: 12, lineHeight: 18, marginTop: 2 },
  link: { minHeight: 48, justifyContent: "center" },
  linkText: { fontSize: 16, fontWeight: "700", flexShrink: 1 },
  actions: { flexDirection: "row", flexWrap: "wrap", gap: 12 },
  primary: { minHeight: 54, paddingHorizontal: 24, paddingVertical: 16, borderRadius: 14, alignItems: "center", flexGrow: 1 },
  primaryText: { color: "#FFFFFF", fontWeight: "700", fontSize: 16 },
  secondary: { minHeight: 54, borderWidth: 1, borderRadius: 14, paddingHorizontal: 24, paddingVertical: 16, alignItems: "center", flexGrow: 1 },
});
