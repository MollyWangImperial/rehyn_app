# Hand to Mouth (T3) Testing

Direct local entry: `/testing/hand-to-mouth?affected_side=right` (or `left`).
Uses the existing signed-in account and Testing score endpoint. No patient
assessment history, care-plan or reward writes occur. This is an engineering
camera task score, not a validated clinical scale.

## Shared normal-assessment task

Testing selects the existing T3 definition from `TASKS_DATA`; it does not copy
or replace its steps. Both modes use the same chest target at x=0.5, y=0.48,
the same calibrated mouth and lap locations, and the normal body-scaled radii.
The Testing-only chest anchor and custom chest-radius function were removed.
Both modes call the same contact, renderer and hold controller, including
transparent previews during speech and removal of completed circles. The mouth
hold reminder precedes mouth contact in both modes so the patient keeps the
hand raised. Testing adds the left measurements, angle arcs, calibration-quality
diagnostics and a separate test report rather than writing assessment history.

## Scoring rubric

| Step | Target / hold | Diagnostic metric | Reference | Statistic |
|---|---|---|---|---|
| T3-S1: lift | Standard assessment chest target / 1 s | Arm elevation | 30° | Target median |
| T3-S2: transport | Calibrated mouth / 1.5 s | Elbow bend | 110° | Target median |
| T3-S3: hold | Same mouth / 1.5 s | Elbow bend | 110° | Target median |
| T3-S4: return | Locked lap / 1.5 s | Target completion only | Completed = 100; unfinished = 0 | No angle or occupancy statistic |

Angle diagnostics use the lower middle order statistic implemented by the existing
tracker, taking up to 120 recent valid in-target samples; below five target
samples it uses up to 600 recent valid movement samples. At least five valid
samples are necessary to report each angle statistic in steps 1–3; angle
availability no longer gates the Testing total. T1's peak-angle rule does not apply.
The reported sample count identifies the window actually used. Step 4 earns
100 points on lap-target completion, without an angle, form or assistance deduction.

Arm elevation is the model-world hip–shoulder–elbow angle. Its cyan 2D arc is
labelled separately because projection changes the angle. Elbow extension is
the aspect-corrected image shoulder–elbow–wrist internal angle. Bend = 180° minus
extension. The yellow bend arc is between the continuation of the upper arm and
the forearm, and uses exactly this same 2D geometry.

Testing uses `completion_compensation_v1` for the whole attempt:

- All four targets and holds completed, with all posture checks observed and
  no confirmed compensation: **100/100**.
- Any confirmed compensation in any of the four active steps: **15/100**.
- An incomplete attempt (including a skipped or absent step): **15/100**.
- A completed attempt with no confirmed compensation but missing required
  posture evidence: unavailable, never an assumed 100.

Angles and assistance remain diagnostic and cause no separate deductions.
Existing angle-based step values remain in the API for inspection; they are
not averaged into this final total or presented as contributing points.
Completing the lap still earns 100 lap-completion points, while compensation
during that return affects the whole-test result. Normal-assessment grading
is unchanged. Each required posture check needs at least 500 ms of evidence.

A red sticky alert in the left diagnostics panel names confirmed trunk lean,
shoulder hiking or head movement and shows the whole-test score of 15/100.
It distinguishes a current pattern from one recorded earlier and retains the
finding across step changes and missing frames. It stays outside the camera
and targets. Brief threshold crossings, calibration and control use do not
create a flag; the alert uses the same sustained evidence as the score service.

## Compensation measurements

All checks compare with the stable two-second upright calibration. A gap over
200 ms breaks a compensation run; each check needs 500 ms of valid observations
and a continuous 500 ms meeting its threshold. Evidence is collected only
during an active reach/hold, never while listening or using controls.

* Trunk uses shoulder-span growth alone (`image_shoulder_expansion_v3`).
  S = current shoulder span along the fixed calibration pelvis axis / baseline span (aspect-corrected).
  Cue = max(0, S - 1) * 100, threshold >12% for 500 ms. Neither face growth
  nor hip-width changes can trigger this detector. A vertical shrug does not
  enlarge horizontal shoulder span. This is a forward-approach size proxy;
  side bending/rotation can change the projected span and are not separately
  classified. Keep the camera fixed. T1 retains its previous detector.
* Shoulder lift uses shoulder rise alone (`world_shoulder_rise_v2`): rise is the positive change in
  affected-versus-opposite shoulder height difference divided by shoulder width.
  Ear-to-shoulder shortening is not required. Cue = `max(0, degrees(atan2(rise, 0.5))
  - 0.12 * arm elevation)`; threshold >12. This world-space cue requires
  image landmark visibility >=0.65 and projected shoulder span within 30% of
  baseline. The scale guard uses the pelvis-axis projection too, so a hike
  does not invalidate its own visibility check by lengthening the diagonal.
  The 500 ms confirmation and arm-elevation allowance are unchanged. T3 Testing
  calibration no longer requires a world-space ear-to-shoulder gap; image face
  landmarks are still needed for the separate head and face checks. Earlier
  attempts and other tasks keep their original rise-plus-shortening method.
* Head-forward movement uses face-span growth (`image_face_expansion_v4`).
  F = current aspect-corrected 2D ear-to-ear distance / baseline distance.
  H = F / max(1, S). Cue = max(0, H - 1) * 100, threshold >10% for 500 ms.
  This requires actual face growth beyond shared shoulder-span growth; shrinking
  shoulders cannot create a head warning. Correcting aspect before measuring
  the ear distance avoids false growth when the head rolls in a wide image.
  The existing MediaPipe Pose nose/ear/shoulder points are used, with visibility
  >=0.65; no extra model is loaded. Missing face/shoulder span abstains. Head
  nodding and head-to-shoulder lowering are no longer scoring inputs. Active
  reach/hold and raised-hand proximity timing still apply; the latter uses the
  new 10% threshold. The 12% and 10% thresholds are initial engineering choices,
  not clinically validated limits. Camera zoom/movement and changes in head/body
  orientation can still affect apparent size. Older records retain their methods.

The report records each method so older torso-vector/head-pitch attempts retain
accurate explanations. Current Testing results explain the whole-attempt
100/15 rule and show angle evidence without proportional point deductions.

## Calibration and live panel

T1 and T3 now use the same two-second stable-wrist lap calibration and the
same independent camera checks: exposure, clothing/background contrast,
landmark confidence, available grading angles, two-second stability, and
upright posture reference. T3 additionally requires the nose/head measurement
for its head-to-hand check and locks its mouth target before the hand covers it.
Both baseline implementations use that same clear window (at least eight
fresh frames spanning two seconds), rather than waiting for an extra fixed
number of frames. A saved lap point survives an unrelated failed image check.

The desktop panel is to the left, outside the camera. On narrow screens it is
below the camera. It matches T1’s cyan/yellow angle cards and pink compensation
cards, with separate shoulder/face cues and Sface. It shows arm elevation, elbow bend and internal extension,
the current step's reference and scoring statistic/sample count, and each
compensation's live value, threshold, current duration and longest duration.
Readings remain live during setup and instructions, but only eligible movement
frames enter the score. Missing measurements are labelled, not shown as zero.

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
lap is stable and is retained through the task. Pose Lite continues tracking the
body. One MediaPipe Hand Landmarker instance is loaded during setup (up to two
hands), and runs only during the mouth steps, at most 10 times per second on
new video frames. Select the hand by its proximity to the affected pose wrist;
reject a detection closer to the opposite wrist. Mouth contact accepts any of
its five fingertips or palm centre, plus confident affected-side Pose hand
points as fallback. Fine-hand points expire after 220 ms; chest and lap steps
still use the affected wrist. The visible fine-hand skeleton is the hand used
for contact. Coordinates share
the camera projection and mirror transform; T3 hit distances use the same
pixel aspect and radius as the drawn circles. Low-confidence or stale frames
do not count toward holding. The hold instruction is spoken before the mouth
target activates, and is not repeated at the transition to the hold step.

Posture and angle evidence is collected only during an active reach/hold. After
the full instruction, affected-wrist motion relative to the affected shoulder
must progress toward the circle (normally 2.5% of the camera short side), or the
affected hand must already be touching the target. Whole
body movement with a resting arm does not start this window. During mouth
steps, a head cue above 15 with the affected hand raised near shoulder height
and within 20% of the camera short side of the nose also opens the window.
This captures bringing the head to a stationary raised hand; it excludes a
hand down at the lap or mouse. It remains disabled during narration/controls. The hold step
continues at the mouth without requiring another reach. Evidence stops when
the step completes, during speech/tracking interruptions and when interacting
with controls. Resting outside the target for 1.5 seconds closes the window;
another approach reopens it. Pauses break compensation streaks without erasing
previously confirmed evidence or recalibrating the upright posture.

The first/chest circle uses the normal assessment radius:
`min(max(0.11, shoulderWidth * 0.70), 0.18)` in camera short-side units.
Mouth uses `min(max(0.10, shoulderWidth * 0.48), 0.14)`. The lap radius
is locked during calibration. The drawn and hit radii agree, with no enlarged
pulsing border. The affected wrist must remain in the chest target for the
standard one-second hold; mouth and lap holds remain 1.5 seconds each.
Moving within a circle does not restart its hold. Both modes tolerate a brief
departure of up to 350 ms before reset; missing tracking pauses the hold.
T1 and T3 clear a completed circle on the next render. The next circle previews
transparently with a dashed edge during instructions; contact and progress are
disabled until the entire instruction finishes plus the shared 350 ms delay. T3 now shares the green
contact feedback, visible centre and inset progress ring of forward reach.
The mouth reach and subsequent mouth hold remain separate scoring steps at
the same anatomical location, separated by the completion transition.

## Private Molly audio

`backend/testing_mouth_voice_lines.py` contains nine authored cues. Generate:

```
python -m backend.build_testing_mouth_voice_bundle --reference <private-Molly-WAV>
```

The ignored `backend/voice_samples/mouth-molly-bundle.json` contains the cloned
clips. `/api/testing/mouth/voice/health` verifies complete coverage. Serving
clips requires a signed-in user and exact allowlisted text. A failed cue is
retried once with the same text. Repeated failure holds the instruction queue
and target timing, keeps voice enabled, and offers **Resume Molly voice** to
unlock playback from a browser gesture. **Replay instruction** also lets the
patient repeat the last cue. Captions-only continuation requires explicitly
turning voice off; one failed clip never silently disables all later speech.
Exiting cancels pending recovery and queued cues. No preset voice substitution occurs.
T1's existing voice bundle and endpoint stay independent.

Testing uses live camera measurements without starting a video recorder or
automatically saving a video or companion JSON file, including on localhost.
Results show the movement score and evidence charts without a saved-video panel.

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

Voice recovery regression (26 September 2026):
`frontend/scripts/verify-mouth-voice.cjs` simulates browser playback rejection,
resumes via the visible control, interrupts each of the nine actual Molly clips
midway, and verifies same-clip resumption through the end. Targets and Skip wait
until all queued instructions finish, including the reach/hold pair. It checks
prefetch/cache reuse, dark-light and missing-nose calibration failures, the
shared two-second window, left-side desktop layout, camera-clear mobile layout,
all four target completions and the real results page on desktop and phone.
The current nine local clips were also transcribed to verify that their audio
contains the full authored sentences, including each sentence ending.

T3 additionally monitors actual media-time progress. If the playhead stops for
2.5 seconds without a pause/error event, it reloads the cached clip and resumes
0.15 seconds before the last observed position. It permits two reload attempts
before returning to the existing retry/resume flow. Ending or cancelling a cue
clears the monitor; an unfinished clip never unlocks the target. Voice status
and Replay/Resume are near the top of the left panel.

`frontend/scripts/verify-mouth-audio-runtime.cjs` runs the real MediaPipe model
with a generated camera stream and plays all nine real Molly clips at normal
speed. `QA_BROWSER=firefox` selects Firefox; Edge is the default. With
`QA_SILENT_STALL=1`, every cue is halted after 2.5 seconds while suppressing its
pause event, to verify progress-based recovery independently of pause handling.
Set `PLAYWRIGHT_MODULE` if Playwright is installed outside the frontend; the
script only accepts localhost URLs and uses an existing local trial account.

Earlier movement-window regression (26 September 2026): desktop/right and phone/left
flows excluded a simulated resting-arm mouse lean, rejected a point outside
the smaller chest circle, rejected continued motion inside it, and completed
all four steps with results after a steady hold. No HandLandmarker was created.
All nine Molly cues completed, including playback interruption/recovery.
The real Pose Lite runtime also completed all nine clips at normal speed.
The seated-forward-reach browser regression passed unchanged. These are
automated integration checks, not a validation of camera-angle accuracy.

Results/target-transition regression (26 September 2026): the results client
retries the same completed payload up to three times for network failures,
timeouts and temporary server errors. Each request has a 12-second timeout;
validation/auth errors are not repeatedly submitted. Exhausted retries retain
the evidence in the existing results page and explain how to retry calculation
without repeating the exercise. The web bridge dispatches completion on the
parent event loop before the camera iframe is unmounted.

`QA_BROWSER=firefox QA_SCORE_RECOVERY=1` with `verify-mouth-voice.cjs` injects a
failed request and a 503 response, then verifies the real score API and results
using the identical payload. It also inspects canvas calls: each step displays
one circle, completion draws none, and instruction playback draws none.

Target consistency update: T1 and T3 now use `RehynReachTarget.drawTestingTarget`
for the same fixed coral boundary, green contact feedback, white centre and
inset green progress arc. Contact and rendering share one readiness state.
The circle interior and exact edge both count. T3 uses the affected wrist at
the chest/lap and affected Pose hand points at the mouth. The common hold timer
allows 350 ms of brief boundary jitter, with no extra T3 wrist-drift reset.
Targets remain hidden during narration and disappear immediately on completion.
Task-specific locations and prescribed hold durations are retained. Posture
scoring still excludes idle mouse reaches outside the target.
