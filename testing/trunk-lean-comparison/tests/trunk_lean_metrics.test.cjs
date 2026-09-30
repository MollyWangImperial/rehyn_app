const {test} = require('node:test');
const assert = require('node:assert/strict');
const {
  legacyForwardLeanDegrees,
  metricsFromLandmarks,
  newForwardLeanEvidence,
  predictedDepthEvidence,
  createTemporalState,
  updateTemporalState,
} = require('../trunk_lean_metrics.js');

function sample(overrides = {}) {
  return {
    valid: true,
    trunk_projection_visible: true,
    shoulder_width: 0.20,
    shoulder_line_delta: 0,
    ear_width: 0.08,
    trunk_depth_tilt: 5,
    torso_length: 0.40,
    shoulder_width_corrected: 0.30,
    hip_width_corrected: 0.28,
    torso_length_corrected: 0.40,
    torso_area_corrected: 0.10,
    aspect: 1.5,
    points: {},
    ...overrides,
  };
}

test('legacy detector preserves the current Graded Forward Reach calculation', () => {
  const baseline = sample();
  const current = sample({
    shoulder_width: 0.24,
    ear_width: 0.096,
    trunk_depth_tilt: 22,
    torso_length: 0.36,
  });
  const shoulder = Math.asin(2 * (1 - 0.20 / 0.24)) * 180 / Math.PI;
  const face = Math.asin(1.5 * (1 - 0.08 / 0.096)) * 180 / Math.PI;
  const shortening = Math.acos((0.36 / 0.24) / (0.40 / 0.20)) * 180 / Math.PI;
  const expected = Math.max(Math.min(shoulder, face), Math.min(17, shortening));
  assert.ok(Math.abs(legacyForwardLeanDegrees(current, baseline) - expected) < 1e-10);
});

test('new detector cancels uniform camera approach using hip normalization', () => {
  const baseline = sample();
  const zoomed = sample({
    shoulder_width: 0.24,
    ear_width: 0.096,
    torso_length: 0.48,
    shoulder_width_corrected: 0.36,
    hip_width_corrected: 0.336,
    torso_length_corrected: 0.48,
  });
  assert.ok(legacyForwardLeanDegrees(zoomed, baseline) > 12);
  const result = newForwardLeanEvidence(zoomed, baseline);
  assert.equal(result.detected, false);
  assert.ok(result.degrees < 0.001);
  assert.ok(Math.abs(result.pelvisScale - 1.2) < 1e-10);
});

test('new detector identifies trunk lean when the shoulder cue alone exceeds 12 degrees', () => {
  const baseline = sample();
  const lean = sample({
    shoulder_width_corrected: 0.345,
    hip_width_corrected: 0.28,
  });
  const result = newForwardLeanEvidence(lean, baseline);
  assert.equal(result.supported, true);
  assert.equal(result.detected, true);
  assert.ok(result.degrees > 12);
  assert.match(result.supportReason, /Shoulder cue/);
});

test('new detector identifies trunk lean when the face cue alone reaches 7 degrees', () => {
  const baseline = sample();
  const earWidthAtSevenDegrees = baseline.ear_width / (1 - Math.sin(7 * Math.PI / 180) / 1.5);
  const lean = sample({ear_width: earWidthAtSevenDegrees});
  const result = newForwardLeanEvidence(lean, baseline);
  assert.equal(result.detected, true);
  assert.ok(Math.abs(result.cues.pelvisNormalizedFaceScale - 7) < 1e-10);
  assert.ok(result.cues.pelvisNormalizedShoulderScale < 0.001);
  assert.match(result.supportReason, /Face cue/);
});

test('faceScale exposes ear-width change divided by hip-width change', () => {
  const baseline = sample();
  const current = sample({ear_width: baseline.ear_width * 1.10,
    hip_width_corrected: baseline.hip_width_corrected * 1.05});
  const result = newForwardLeanEvidence(current, baseline);
  assert.ok(Math.abs(result.faceScale - 1.10 / 1.05) < 1e-10);
  assert.ok(result.cues.pelvisNormalizedFaceScale > 0);
});

test('new detector does not flag the face cue just below 7 degrees', () => {
  const baseline = sample();
  const earWidthBelowSeven = baseline.ear_width / (1 - Math.sin(6.9 * Math.PI / 180) / 1.5);
  const result = newForwardLeanEvidence(sample({ear_width: earWidthBelowSeven}), baseline);
  assert.equal(result.detected, false);
  assert.ok(result.cues.pelvisNormalizedFaceScale < 7);
});

test('pelvis-axis shoulder cue abstains when the span is unavailable, retaining independent face evidence',()=>{
  const baseline=sample({shoulder_span_pelvis_axis:.3});
  const missing=sample({shoulder_width_corrected:.6,shoulder_span_pelvis_axis:NaN});
  const result=newForwardLeanEvidence(missing,baseline,{separateShoulderHike:true});
  assert.equal(result.detected,false);
  assert.ok(Number.isNaN(result.cues.pelvisNormalizedShoulderScale));
  assert.equal(newForwardLeanEvidence({...missing,ear_width:.1},baseline,{separateShoulderHike:true}).detected,true);
});

test('new detector does not require or use depth and torso-shortening cues', () => {
  const baseline = sample();
  const depthOnly = sample({trunk_depth_tilt: 45, torso_length_corrected: 0.25});
  const result = newForwardLeanEvidence(depthOnly, baseline);
  assert.equal(result.detected, false);
  assert.deepEqual(Object.keys(result.cues).sort(), [
    'pelvisNormalizedFaceScale',
    'pelvisNormalizedShoulderScale',
  ]);
});

test('predicted depth detects a depth-only lean on the same frame', () => {
  const baseline = sample();
  const depthOnly = sample({trunk_depth_tilt: 22});
  assert.equal(newForwardLeanEvidence(depthOnly, baseline).detected, false);
  const depth = predictedDepthEvidence(depthOnly, baseline);
  assert.equal(depth.degrees, 17);
  assert.equal(depth.detected, true);

  const shoulderOnly = sample({shoulder_width_corrected: 0.345});
  assert.equal(newForwardLeanEvidence(shoulderOnly, baseline).detected, true);
  assert.equal(predictedDepthEvidence(shoulderOnly, baseline).detected, false);
});

test('predicted depth requires a change strictly above 12 degrees', () => {
  const baseline = sample();
  assert.equal(predictedDepthEvidence(sample({trunk_depth_tilt: 17}), baseline).detected, false);
  assert.equal(predictedDepthEvidence(sample({trunk_depth_tilt: 17.1}), baseline).detected, true);
  assert.equal(predictedDepthEvidence(sample({trunk_depth_tilt: -10}), baseline).degrees, 0);
});

test('predicted depth remains unavailable when MediaPipe provides no z coordinates', () => {
  const landmarks = Array.from({length: 25}, () => ({x: 0.5, y: 0.5, visibility: 1}));
  landmarks[11] = {x: 0.4, y: 0.3, visibility: 1};
  landmarks[12] = {x: 0.6, y: 0.3, visibility: 1};
  landmarks[23] = {x: 0.45, y: 0.7, visibility: 1};
  landmarks[24] = {x: 0.55, y: 0.7, visibility: 1};
  const frame = metricsFromLandmarks(landmarks, 1.5);
  assert.equal(frame.valid, true);
  assert.equal(Number.isNaN(frame.trunk_depth_tilt), true);
  const depth = predictedDepthEvidence(frame, sample());
  assert.equal(depth.detected, false);
  assert.equal(Number.isNaN(depth.degrees), true);
});

test('predicted depth angle follows the model z difference between shoulders and hips', () => {
  const landmarks = Array.from({length: 25}, () => ({x: 0.5, y: 0.5, z: 0, visibility: 1}));
  landmarks[11] = {x: 0.4, y: 0.3, z: -0.1, visibility: 1};
  landmarks[12] = {x: 0.6, y: 0.3, z: -0.1, visibility: 1};
  landmarks[23] = {x: 0.45, y: 0.7, z: 0, visibility: 1};
  landmarks[24] = {x: 0.55, y: 0.7, z: 0, visibility: 1};
  const frame = metricsFromLandmarks(landmarks, 1.5);
  const depth = predictedDepthEvidence(frame, sample({trunk_depth_tilt: 0}));
  assert.ok(Math.abs(depth.degrees - Math.asin(0.1 / 0.4) * 180 / Math.PI) < 1e-10);
  assert.equal(depth.detected, true);
});

test('legacy detector keeps the production 8-frame and 35-percent confirmation rule', () => {
  const state = createTemporalState();
  for (let index = 0; index < 7; index += 1) updateTemporalState(state, 15);
  assert.equal(state.confirmed, false);
  updateTemporalState(state, 15);
  assert.equal(state.confirmed, true);
  assert.equal(state.aboveFrames, 8);
  assert.equal(state.ratio, 1);
  for (let index = 0; index < 30; index += 1) updateTemporalState(state, 0);
  assert.equal(state.confirmed, true, 'the result remains visible after the patient returns upright');
});
