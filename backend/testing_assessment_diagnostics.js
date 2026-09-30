/* Read-only Testing overlays for assessment tasks other than T1 and T3. */
(function(root) {
  const TASKS = new Set(["T2", "H1", "H3", "H4"]);
  const FINGERS = [
    ["Index", 5, 6, 7, 8], ["Middle", 9, 10, 11, 12],
    ["Ring", 13, 14, 15, 16], ["Little", 17, 18, 19, 20],
  ];
  const finite = Number.isFinite;
  const distance2D = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  function angle3D(a, b, c) {
    if (![a, b, c].every(p => p && [p.x, p.y, p.z ?? 0].every(finite))) return null;
    const u = [a.x-b.x, a.y-b.y, (a.z ?? 0)-(b.z ?? 0)];
    const v = [c.x-b.x, c.y-b.y, (c.z ?? 0)-(b.z ?? 0)];
    const norms = Math.hypot(...u)*Math.hypot(...v);
    if (norms < .00001) return null;
    return Math.acos(Math.max(-1, Math.min(1, u.reduce((sum, x, i) => sum+x*v[i], 0)/norms))) * 180/Math.PI;
  }
  function handReadout(hand) {
    if (!hand || hand.length < 21 || !hand.every(p => p && finite(p.x) && finite(p.y))) return null;
    const fingers = FINGERS.map(([name, mcp, pip, dip, tip]) => ({
      name, pip: angle3D(hand[mcp], hand[pip], hand[dip]),
      dip: angle3D(hand[pip], hand[dip], hand[tip]),
      joints: [mcp, pip, dip, tip],
    }));
    const palmWidth = Math.max(.01, distance2D(hand[5], hand[17]));
    return {fingers, pinchGap: distance2D(hand[4], hand[8])/palmWidth};
  }
  function readout(taskId, rubric, sample, hand) {
    if (!TASKS.has(taskId)) return null;
    const value = key => finite(sample?.[key]) ? sample[key] : null;
    return {
      criteria: (rubric?.criteria || []).map(rule => ({label: rule.label, metric: rule.metric,
        target: rule.target, unit: rule.unit, value: value(rule.metric)})),
      posture: (rubric?.compensations || []).map(id => ({id, value: value(id)})),
      armElevation: value("arm_elevation"), elbowExtension: value("elbow_extension"),
      handOpen: value("hand_open"), handClosed: value("hand_closed"),
      pinch: value("pinch"), cycle: value("open_close_cycle"),
      hand: handReadout(hand),
    };
  }
  function drawHandGuides(ctx, hand, width, height, taskId, arcApi) {
    if (!handReadout(hand) || !["H1", "H3", "H4"].includes(taskId)) return;
    ctx.save();
    ctx.lineWidth = Math.max(2, width/300); ctx.lineCap = "round";
    if (taskId === "H3") {
      ctx.strokeStyle = "#fb7185";
      ctx.beginPath(); ctx.moveTo(hand[4].x*width, hand[4].y*height);
      ctx.lineTo(hand[8].x*width, hand[8].y*height); ctx.stroke();
    } else if (arcApi?.jointArc) {
      for (const [, mcp, pip, dip, tip] of FINGERS) {
        for (const [a, b, c, color] of [[mcp, pip, dip, "#67e8f9"], [pip, dip, tip, "#facc15"]]) {
          const arc = arcApi.jointArc(hand[a], hand[b], hand[c], width/height);
          if (!arc) continue;
          const x = hand[b].x*width, y = hand[b].y*height;
          const radius = Math.max(5, Math.min(16, Math.hypot(hand[a].x-hand[b].x, hand[a].y-hand[b].y)*height*.38));
          ctx.strokeStyle = color;
          ctx.beginPath(); ctx.arc(x, y, radius, arc.start, arc.start+arc.sweep, arc.sweep < 0); ctx.stroke();
        }
      }
    }
    ctx.restore();
  }
  const api = {TASKS, angle3D, handReadout, readout, drawHandGuides};
  root.RehynTestingAssessmentDiagnostics = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(globalThis);
