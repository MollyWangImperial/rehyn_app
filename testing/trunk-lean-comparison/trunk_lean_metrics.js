(function attachTrunkLeanMetrics(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TrunkLeanMetrics = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function buildTrunkLeanMetrics() {
  "use strict";

  const LANDMARK = Object.freeze({
    nose: 0,
    leftEar: 7,
    rightEar: 8,
    leftShoulder: 11,
    rightShoulder: 12,
    leftHip: 23,
    rightHip: 24,
  });
  const NUMERIC_BASELINE_FIELDS = Object.freeze([
    "shoulder_width",
    "shoulder_line_delta",
    "ear_width",
    "trunk_depth_tilt",
    "torso_length",
    "shoulder_width_corrected",
    "shoulder_span_pelvis_axis",
    "hip_width_corrected",
    "torso_length_corrected",
    "torso_area_corrected",
  ]);

  function clamp(value, low, high) {
    return Math.min(high, Math.max(low, value));
  }

  function radToDeg(value) {
    return value * 180 / Math.PI;
  }

  function median(values) {
    const sorted = values.filter(Number.isFinite).slice().sort((a, b) => a - b);
    if (!sorted.length) return NaN;
    const middle = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
  }

  function pointUsable(point, minimumVisibility) {
    if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y)) return false;
    const threshold = Number.isFinite(minimumVisibility) ? minimumVisibility : 0.45;
    const confidence = Number.isFinite(point.visibility)
      ? point.visibility
      : (Number.isFinite(point.presence) ? point.presence : 1);
    return confidence >= threshold;
  }

  function midpoint(first, second) {
    return {
      x: (first.x + second.x) / 2,
      y: (first.y + second.y) / 2,
      z: ((Number(first.z) || 0) + (Number(second.z) || 0)) / 2,
    };
  }

  function distance2d(first, second, aspect) {
    const xScale = Number.isFinite(aspect) && aspect > 0 ? aspect : 1;
    return Math.hypot((first.x - second.x) * xScale, first.y - second.y);
  }

  function polygonArea(points, aspect) {
    const xScale = Number.isFinite(aspect) && aspect > 0 ? aspect : 1;
    let twiceArea = 0;
    for (let index = 0; index < points.length; index += 1) {
      const current = points[index];
      const next = points[(index + 1) % points.length];
      twiceArea += current.x * xScale * next.y - next.x * xScale * current.y;
    }
    return Math.abs(twiceArea) / 2;
  }

  function clonePoint(point) {
    return {x: point.x, y: point.y, z: Number(point.z) || 0};
  }

  function metricsFromLandmarks(landmarks, aspect) {
    if (!Array.isArray(landmarks)) return {valid: false, reason: "No pose was detected."};
    const core = {
      nose: landmarks[LANDMARK.nose],
      leftShoulder: landmarks[LANDMARK.leftShoulder],
      rightShoulder: landmarks[LANDMARK.rightShoulder],
      leftHip: landmarks[LANDMARK.leftHip],
      rightHip: landmarks[LANDMARK.rightHip],
    };
    const torsoPoints = [core.leftShoulder, core.rightShoulder, core.leftHip, core.rightHip];
    if (!torsoPoints.every(point => pointUsable(point))) {
      return {valid: false, reason: "Keep both shoulders and both hips in view."};
    }

    const midShoulder = midpoint(core.leftShoulder, core.rightShoulder);
    const midHip = midpoint(core.leftHip, core.rightHip);
    const shoulderWidth = Math.max(
      0.03,
      Math.hypot(core.leftShoulder.x - core.rightShoulder.x, core.leftShoulder.y - core.rightShoulder.y),
    );
    const torsoLength = Math.max(0.04, Math.hypot(midShoulder.x - midHip.x, midShoulder.y - midHip.y));
    const correctedShoulderWidth = Math.max(0.03, distance2d(core.leftShoulder, core.rightShoulder, aspect));
    const correctedHipWidth = Math.max(0.03, distance2d(core.leftHip, core.rightHip, aspect));
    const xScale = Number.isFinite(aspect) && aspect > 0 ? aspect : 1;
    const hipX = (core.rightHip.x - core.leftHip.x) * xScale;
    const hipY = core.rightHip.y - core.leftHip.y;
    const hipSpan = Math.hypot(hipX, hipY);
    // A raised shoulder lengthens the diagonal shoulder-to-shoulder line.
    // Its projection onto the pelvis axis removes that elevation component.
    const shoulderSpanPelvisAxis = hipSpan >= 0.03 ? Math.abs(
      ((core.rightShoulder.x - core.leftShoulder.x) * xScale * hipX
        + (core.rightShoulder.y - core.leftShoulder.y) * hipY) / hipSpan,
    ) : NaN;
    const correctedTorsoLength = Math.max(0.04, distance2d(midShoulder, midHip, aspect));
    const earLeft = landmarks[LANDMARK.leftEar];
    const earRight = landmarks[LANDMARK.rightEar];
    const earWidth = pointUsable(earLeft) && pointUsable(earRight)
      ? Math.max(0.01, Math.hypot(earLeft.x - earRight.x, earLeft.y - earRight.y))
      : NaN;
    const hasPredictedDepth = torsoPoints.every(point => Number.isFinite(point.z));
    const depthDifference = midHip.z - midShoulder.z;
    const confidences = torsoPoints.concat(pointUsable(core.nose) ? [core.nose] : []).map(point => (
      Number.isFinite(point.visibility) ? point.visibility : (Number.isFinite(point.presence) ? point.presence : 1)
    ));

    return {
      valid: true,
      quality: Math.min(...confidences),
      trunk_projection_visible: true,
      shoulder_width: shoulderWidth,
      shoulder_line_delta: core.rightShoulder.y - core.leftShoulder.y,
      ear_width: earWidth,
      torso_length: torsoLength,
      trunk_depth_tilt: hasPredictedDepth
        ? radToDeg(Math.asin(clamp(depthDifference / Math.max(0.05, torsoLength), -1, 1)))
        : NaN,
      shoulder_width_corrected: correctedShoulderWidth,
      shoulder_span_pelvis_axis: shoulderSpanPelvisAxis >= 0.03 ? shoulderSpanPelvisAxis : NaN,
      hip_width_corrected: correctedHipWidth,
      torso_length_corrected: correctedTorsoLength,
      torso_area_corrected: polygonArea(
        [core.leftShoulder, core.rightShoulder, core.rightHip, core.leftHip],
        aspect,
      ),
      points: {
        nose: pointUsable(core.nose) ? clonePoint(core.nose) : null,
        leftShoulder: clonePoint(core.leftShoulder),
        rightShoulder: clonePoint(core.rightShoulder),
        leftHip: clonePoint(core.leftHip),
        rightHip: clonePoint(core.rightHip),
        midShoulder: clonePoint(midShoulder),
        midHip: clonePoint(midHip),
      },
      aspect: Number.isFinite(aspect) && aspect > 0 ? aspect : 1,
    };
  }

  function baselineFromSamples(samples) {
    const validSamples = (samples || []).filter(sample => sample && sample.valid);
    if (!validSamples.length) return null;
    const baseline = {valid: true, trunk_projection_visible: true};
    for (const field of NUMERIC_BASELINE_FIELDS) {
      baseline[field] = median(validSamples.map(sample => Number(sample[field])));
    }
    const pointNames = ["nose", "leftShoulder", "rightShoulder", "leftHip", "rightHip", "midShoulder", "midHip"];
    baseline.points = {};
    for (const name of pointNames) {
      const points = validSamples.map(sample => sample.points && sample.points[name]).filter(Boolean);
      baseline.points[name] = points.length ? {
        x: median(points.map(point => point.x)),
        y: median(points.map(point => point.y)),
        z: median(points.map(point => Number(point.z) || 0)),
      } : null;
    }
    baseline.aspect = median(validSamples.map(sample => Number(sample.aspect)));
    return baseline;
  }

  function horizontalShoulderSpan(metrics) {
    const width = Number(metrics && metrics.shoulder_width);
    const rise = Number(metrics && metrics.shoulder_line_delta);
    return Number.isFinite(width) && Number.isFinite(rise) && width > Math.abs(rise)
      ? Math.sqrt(width * width - rise * rise)
      : NaN;
  }

  // Preserved as the original Graded Forward Reach calculation for reference.
  function legacyForwardLeanDegrees(raw, baseline) {
    if (!raw || !baseline || raw.trunk_projection_visible === false) return NaN;
    const width0 = horizontalShoulderSpan(baseline);
    const width = horizontalShoulderSpan(raw);
    if (!Number.isFinite(width0) || !Number.isFinite(width) || width0 < 0.03 || width < 0.03) return NaN;
    const evidence = [];
    const ear0 = Number(baseline.ear_width);
    const ear = Number(raw.ear_width);
    if (Number.isFinite(ear0) && Number.isFinite(ear) && ear0 > 0.01 && ear > 0.01) {
      const shoulderApproach = radToDeg(Math.asin(clamp(2 * (1 - width0 / width), 0, 1)));
      const faceApproach = radToDeg(Math.asin(clamp(1.5 * (1 - ear0 / ear), 0, 1)));
      evidence.push(Math.min(shoulderApproach, faceApproach));
    }
    const depth0 = Number(baseline.trunk_depth_tilt);
    const depth = Number(raw.trunk_depth_tilt);
    const torso0 = Number(baseline.torso_length);
    const torso = Number(raw.torso_length);
    if (Number.isFinite(depth0) && Number.isFinite(depth) && torso0 > 0 && torso > 0) {
      const shortening = radToDeg(Math.acos(clamp((torso / width) / (torso0 / width0), 0, 1)));
      evidence.push(Math.min(Math.max(0, depth - depth0), shortening));
    }
    return evidence.length ? Math.max(...evidence) : NaN;
  }

  function approachAngle(scale, distanceFactor) {
    if (!Number.isFinite(scale) || scale <= 0) return NaN;
    return radToDeg(Math.asin(clamp(distanceFactor * (1 - 1 / scale), 0, 1)));
  }

  function newForwardLeanEvidence(raw, baseline, {separateShoulderHike = false} = {}) {
    if (!raw || !baseline || !raw.valid || !baseline.valid) {
      return {degrees: NaN, detected: false, supported: false, reason: "A calibrated torso pose is required."};
    }
    const widthField = separateShoulderHike ? "shoulder_span_pelvis_axis" : "shoulder_width_corrected";
    const width0 = Number(baseline[widthField]);
    const width = Number(raw[widthField]);
    const hip0 = Number(baseline.hip_width_corrected);
    const hip = Number(raw.hip_width_corrected);
    const pelvisScale = hip0 > 0 && hip > 0 ? hip / hip0 : NaN;
    const upperScale = width0 > 0 && width > 0 && Number.isFinite(pelvisScale) && pelvisScale > 0
      ? (width / width0) / pelvisScale
      : NaN;
    const face0 = Number(baseline.ear_width);
    const face = Number(raw.ear_width);
    const faceScale = face0 > 0 && face > 0 && Number.isFinite(pelvisScale) && pelvisScale > 0
      ? (face / face0) / pelvisScale
      : NaN;
    const cues = {
      pelvisNormalizedShoulderScale: approachAngle(upperScale, 2),
      pelvisNormalizedFaceScale: approachAngle(faceScale, 1.5),
    };
    const availableCues = Object.values(cues).filter(Number.isFinite);
    const degrees = availableCues.length ? Math.max(...availableCues) : NaN;
    const shoulderDetected = Number.isFinite(cues.pelvisNormalizedShoulderScale)
      && cues.pelvisNormalizedShoulderScale > 12;
    const faceDetected = Number.isFinite(cues.pelvisNormalizedFaceScale)
      && cues.pelvisNormalizedFaceScale + 1e-9 >= 7;
    const detected = shoulderDetected || faceDetected;
    const supported = availableCues.length > 0;
    let supportReason = "Waiting for a measurable shoulder or face cue.";
    if (shoulderDetected && faceDetected) supportReason = "Shoulder is above 12 degrees and face is at least 7 degrees.";
    else if (shoulderDetected) supportReason = "Shoulder cue is above 12 degrees.";
    else if (faceDetected) supportReason = "Face cue is at least 7 degrees.";
    else if (supported) supportReason = "Shoulder is within 12 degrees and face is below 7 degrees.";

    return {
      method: separateShoulderHike ? "pelvis_axis_shoulder_or_face_v2" : "pelvis_normalized_shoulder_or_face_v1",
      degrees,
      detected,
      supported,
      supportReason,
      cues,
      pelvisScale,
      upperScale,
      faceScale,
    };
  }

  function predictedDepthEvidence(raw, baseline) {
    const current = raw && raw.valid ? raw.trunk_depth_tilt : NaN;
    const upright = baseline && baseline.valid ? baseline.trunk_depth_tilt : NaN;
    const available = Number.isFinite(current) && Number.isFinite(upright);
    const degrees = available ? Math.max(0, current - upright) : NaN;
    return {
      degrees,
      current,
      upright,
      detected: Number.isFinite(degrees) && degrees > 12,
      supportReason: !available
        ? "Predicted shoulder and hip depth is unavailable."
        : degrees > 12
          ? "Predicted depth change is above 12 degrees."
          : "Predicted depth change is within 12 degrees.",
    };
  }

  function createTemporalState() {
    return {trackedFrames: 0, aboveFrames: 0, consecutiveFrames: 0, confirmed: false, ratio: 0};
  }

  function updateTemporalState(state, degrees, options) {
    const target = state || createTemporalState();
    const config = Object.assign({threshold: 12, minFrames: 8, minRatio: 0.35}, options || {});
    if (!Number.isFinite(degrees)) return target;
    target.trackedFrames += 1;
    if (degrees > config.threshold) {
      target.aboveFrames += 1;
      target.consecutiveFrames += 1;
    } else {
      target.consecutiveFrames = 0;
    }
    target.ratio = target.aboveFrames / Math.max(1, target.trackedFrames);
    target.confirmed = target.confirmed
      || (target.aboveFrames >= config.minFrames && target.ratio >= config.minRatio);
    return target;
  }

  return Object.freeze({
    LANDMARK,
    baselineFromSamples,
    createTemporalState,
    legacyForwardLeanDegrees,
    metricsFromLandmarks,
    newForwardLeanEvidence,
    predictedDepthEvidence,
    updateTemporalState,
  });
});
