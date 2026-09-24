# Hand to Mouth (T3) Testing

Direct local entry: `/testing/hand-to-mouth?affected_side=right` (or `left`).
Uses the existing signed-in account and Testing score endpoint. No patient
assessment history, care-plan or reward writes occur. This is an engineering
camera task score, not a validated clinical scale.

## Rubric retained

| Step | Target / hold | Scoring metric | Reference | Statistic |
|---|---|---|---|---|
| T3-S1: lift | Calibrated chest / 1 s | Arm elevation | 30° | Target median |
| T3-S2: transport | Calibrated mouth / 1.5 s | Elbow bend | 110° | Target median |
| T3-S3: hold | Same mouth / 1.5 s | Elbow bend | 110° | Target median |
| T3-S4: return | Locked lap / 1.5 s | Target occupancy | 80% | All valid sample proportion |

Angle scoring uses the lower middle order statistic implemented by the existing
tracker, taking up to 120 recent valid in-target samples; below five target
samples it uses up to 600 recent valid movement samples. At least five valid
samples are necessary. T1's peak-angle and completion-only return exceptions do
not apply. The reported sample count identifies the window actually used.

Arm elevation is the model-world hip–shoulder–elbow angle. Its cyan 2D arc is
labelled separately because projection changes the angle. Elbow extension is
the aspect-corrected image shoulder–elbow–wrist internal angle. Bend = 180° minus
extension. The yellow bend arc is between the continuation of the upper arm and
the forearm, and uses exactly this same 2D geometry.

Each step: `(20 × target completed + 80 × min(1, value/reference)) × form`.
Form is `max(0.4, 1 − 0.2 × detected compensation count)`. Final score is the
equally weighted average; explicitly confirmed physical assistance halves the
task score. Merely having a helper present is not assistance. A missing required
metric, or no measured posture checks, leaves the step unscored; any missing
step leaves the final score unavailable. Individual unavailable checks are
labelled, not inferred as normal.

## Compensation measurements

All checks compare with the stable upright calibration. Valid landmark
visibility must be at least 0.65. Posture measurements also require current
screen shoulder width within 30% of baseline. A gap over 200 ms breaks a run.
Each check needs 500 ms eligible data, and a continuous 500 ms over threshold.

* Trunk: angle between current and baseline model-world torso vectors, where
  torso = shoulder midpoint minus hip midpoint; threshold >12°.
* Shoulder lift: rise is the positive change in the affected-versus-opposite
  shoulder height difference normalized by current shoulder width. Shortening
  is the positive loss of affected ear–shoulder distance divided by baseline
  shoulder width. Cue = `max(0, degrees(atan2(min(rise, shortening), 0.5))
  − 0.12 × arm elevation)`; threshold >12°.
* Head: `headPitch = (nose.y − earMidpoint.y) / max(0.04, earWidth)` in model
  world coordinates. Cue = `max(0, currentPitch − baselinePitch) × 60`;
  threshold >15 proxy units. It is not an anatomical head angle.

T1's pelvis-normalized face/shoulder-width detector is deliberately not enabled
for T3: it is a different method, and its frontal width ratios are not verified
for T3's side-view geometry. The diagnostics show actual T3 cues instead.

## Runtime and evidence

* `backend/testing_mouth.js`: pure diagnostic projection and bend-arc geometry.
* `backend/testing_mouth_flow.js` and `testing_mouth_ui.html`: queued Molly
  speech and live panel outside the camera area; assistance control.
* `backend/server.py`: shared calibration, stabilized mouth target, affected
  hand hit detection, hold progression and completion message.
* `backend/assessment_quality.js` / `.py`: measurement and grading authorities.
* `frontend/src/components/AssessmentTestResults.tsx`: metric definitions,
  statistics, actual compensation evidence, equations and time series.

The lap anchor is locked for the assessment; mouth calibration begins once the
lap is stable and is retained through the task. Mouth contact accepts affected
fingertips/palm, with affected-side pose points as fallback. Coordinates share
the camera projection and mirror transform; T3 hit distances use the same
pixel aspect and radius as the drawn circles. Low-confidence or stale frames
do not count toward holding. The hold instruction is spoken before the mouth
target activates, and is not repeated at the transition to the hold step.

## Private Molly audio

`backend/testing_mouth_voice_lines.py` contains nine authored cues. Generate:

```
python -m backend.build_testing_mouth_voice_bundle --reference <private-Molly-WAV>
```

The ignored `backend/voice_samples/mouth-molly-bundle.json` contains the cloned
clips. `/api/testing/mouth/voice/health` verifies complete coverage. Serving
clips requires a signed-in user and exact allowlisted text; unavailable audio
continues with clearly labelled captions. No preset voice substitution occurs.
T1's existing voice bundle and endpoint stay independent.

On localhost the existing review recorder saves video plus JSON evidence under
`backend/.local_state/assessment-recordings/`. The result shows the absolute
path. The video includes camera and angle arcs, plus the metric's reference
and scoring statistic; no microphone. Hosted Testing recording remains disabled.

## Verification

Focused tests cover both sides and aspect ratios, arc/scoring agreement,
occlusion, target-median fallback, long-run target occupancy, compensation
duration, exact score arithmetic, missing evidence, assistance, direct routing,
private voice authorization and complete cue coverage. Browser flow checks
use synthetic landmarks and a generated camera stream; these verify UI and
integration, not physical camera measurement accuracy.

Local verification on 24 September 2026: 39 focused Python tests and 27 Node
tests passed, plus the web export and results-component lint. Desktop/right-arm
and phone/left-arm browser flows completed all four steps, displayed all four
charts, returned 100/100 for the full-credit fixture and saved video/evidence
files. The real MediaPipe runtime also loaded without errors and correctly
kept calibration pending for a blank camera stream. All nine private Molly
clips played to their ends and were checked against the authored transcripts.
