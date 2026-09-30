/* Screen coordinates throughout. Shared by T1 and the isolated RL experiment. */
(function (root) {
  const usable = p => p && [p.x, p.y].every(Number.isFinite)
    && p.x >= 0 && p.x <= 1 && p.y >= 0 && p.y <= 1;
  function screenDistance(a, b, aspect = 1) {
    if (!usable(a) || !usable(b) || !Number.isFinite(aspect) || aspect <= 0) return Infinity;
    return Math.hypot((a.x - b.x) * Math.max(aspect, 1), (a.y - b.y) * Math.max(1 / aspect, 1));
  }
  // Both inputs are raw camera coordinates; the shared canvas mirrors once.
  // Only the affected Pose wrist activates T1, never fingers or the other arm.
  function wristContact({landmarks, side, target, aspect = 1}) {
    const wrist = landmarks?.[side === 'left' ? 15 : 16];
    const point = usable(wrist) && (wrist.visibility ?? 0) >= 0.5
      && (wrist.presence === undefined || wrist.presence >= 0.5) ? wrist : null;
    return {point, distance: screenDistance(point, target, aspect), source: point ? 'wrist' : 'none'};
  }
  function contains(distance, radius) {
    return Number.isFinite(distance) && Number.isFinite(radius) && radius > 0 && distance <= radius + 1e-9;
  }
  const completionDurationMs = 800;
  // Decorative light breathes around a fixed hit boundary. No pose or target
  // geometry is animated, so a stationary hand never gains/loses contact.
  function drawBreathingHalo(ctx, {x, y, radius, now = 0, contact = false, muted = false, reducedMotion = false}) {
    const breath = reducedMotion ? .5 : (1 - Math.cos(now * Math.PI * 2 / 2800)) / 2;
    const rgb = contact ? '127,229,163' : '225,142,109';
    const strength = muted ? .55 : 1;
    ctx.save();ctx.setLineDash([]);
    ctx.beginPath();ctx.arc(x,y,radius*(.98+.08*breath),0,Math.PI*2);
    ctx.lineWidth=5+5*breath;
    ctx.strokeStyle=`rgba(${rgb},${strength*(.09+.13*breath)})`;
    ctx.shadowColor=`rgba(${rgb},${strength*.6})`;ctx.shadowBlur=8+10*breath;
    ctx.stroke();ctx.shadowBlur=0;
    ctx.beginPath();ctx.arc(x,y,radius*(.72+.14*breath),0,Math.PI*2);
    ctx.fillStyle=`rgba(${rgb},${strength*(.025+.035*breath)})`;ctx.fill();
    ctx.restore();
  }
  function drawTargetCompletion(ctx, {x, y, radius, elapsed, reducedMotion = false, mirrored = false}) {
    if(elapsed < 0 || elapsed >= completionDurationMs) return;
    const t = elapsed / completionDurationMs;
    const ease = 1-Math.pow(1-t,3);
    const fade = reducedMotion ? 1 : Math.min(1,(1-t)/.25);
    ctx.save();ctx.setLineDash([]);ctx.globalAlpha=fade;
    ctx.lineCap='round';ctx.lineJoin='round';
    ctx.beginPath();ctx.arc(x,y,radius,0,Math.PI*2);
    ctx.fillStyle='rgba(63,168,110,0.22)';ctx.fill();
    ctx.strokeStyle='#7FE5A3';ctx.lineWidth=6;
    ctx.shadowColor='#7FE5A3';ctx.shadowBlur=reducedMotion?0:18*(1-t);ctx.stroke();ctx.shadowBlur=0;
    if(!reducedMotion){
      // Two soft ripples release outward as the completed ring settles.
      for(const lag of [0,.18]){
        const p=Math.max(0,Math.min(1,(t-lag)/(1-lag)));
        if(t<lag)continue;
        ctx.beginPath();ctx.arc(x,y,radius*(1+.28*(1-Math.pow(1-p,2))),0,Math.PI*2);
        ctx.strokeStyle=`rgba(127,229,163,${.5*(1-p)})`;ctx.lineWidth=3*(1-p)+1;ctx.stroke();
      }
    }
    // Draw one check in screen orientation, including on the mirrored camera.
    ctx.translate(x,y);if(mirrored)ctx.scale(-1,1);
    const scale=reducedMotion?1:.82+.18*ease;ctx.scale(scale,scale);
    const points=[[-.30,0],[-.07,.23],[.34,-.25]];
    const reveal=reducedMotion?1:Math.min(1,elapsed/240);
    ctx.beginPath();ctx.moveTo(points[0][0]*radius,points[0][1]*radius);
    for(let i=1;i<points.length;i++){
      const part=Math.max(0,Math.min(1,reveal*2-(i-1)));
      ctx.lineTo((points[i-1][0]+(points[i][0]-points[i-1][0])*part)*radius,
        (points[i-1][1]+(points[i][1]-points[i-1][1])*part)*radius);
    }
    ctx.strokeStyle='#E2FFEA';ctx.lineWidth=Math.max(4,radius*.085);ctx.stroke();ctx.restore();
  }
  // One visual contract for both seated Testing tasks. The outer boundary is
  // fixed at the contact radius; progress stays inside it and never enlarges it.
  function drawTestingTarget(ctx, {x, y, radius, armed, contact, progress = 0, now = 0, reducedMotion = false}) {
    ctx.save();
    ctx.setLineDash(armed ? [] : [10, 8]);
    ctx.beginPath();ctx.arc(x,y,radius,0,Math.PI*2);
    ctx.lineWidth=6;
    ctx.strokeStyle=armed ? contact ? '#7FE5A3' : '#E18E6D' : 'rgba(225,142,109,0.45)';
    ctx.stroke();ctx.setLineDash([]);
    if(armed){
      ctx.beginPath();ctx.arc(x,y,radius*.55,0,Math.PI*2);
      ctx.fillStyle=contact ? 'rgba(127,229,163,0.4)' : 'rgba(225,142,109,0.4)';ctx.fill();
    }
    ctx.beginPath();ctx.arc(x,y,Math.max(5,Math.min(10,radius*.16)),0,Math.PI*2);
    ctx.fillStyle=armed ? '#fff' : 'rgba(255,255,255,0.8)';ctx.fill();
    if(armed && progress>0){
      ctx.beginPath();ctx.arc(x,y,radius*.82,-Math.PI/2,-Math.PI/2+Math.min(1,progress)*Math.PI*2);
      ctx.strokeStyle='#3C8255';ctx.lineWidth=8;ctx.stroke();
    }
    ctx.restore();
    drawBreathingHalo(ctx,{x,y,radius,now,contact,muted:!armed,reducedMotion});
  }
  function placeRightOfHand({ hand, shoulder, shoulderWidth, radius, aspect = 1, rightEdge = 1, clearanceShoulderWidths = 0.22 }) {
    if (!usable(hand) || !usable(shoulder) || ![shoulderWidth, radius, aspect].every(Number.isFinite)
      || shoulderWidth <= 0 || radius <= 0 || aspect <= 0 || !Number.isFinite(rightEdge) || rightEdge <= 0 || rightEdge > 1
      || !Number.isFinite(clearanceShoulderWidths) || clearanceShoulderWidths < 0) return { ready: false, reason: 'missing_anchors' };
    const radiusX = radius / Math.max(aspect, 1);
    const gap = Math.max(0.035, shoulderWidth * clearanceShoulderWidths);
    // Even the near edge of the circle clears both starting anchors. Include
    // the runner's 8% ring pulse and a frame margin when checking room.
    const x = Math.max(hand.x, shoulder.x) + radiusX * 1.08 + gap;
    const maxX = rightEdge - radiusX * 1.08 - 0.025;
    if (x > maxX) return { ready: false, reason: 'need_room_right',
      guidance: 'Move slightly to the left in the camera view, with your hand resting on your lap, so there is room for the circle on your right.' };
    return { ready: true, x, radius, radiusX, gap };
  }
  function placeSeatedForwardReachTargets({ lap, shoulder, topEdge = 0, bottomEdge = 1, ...geometry }) {
    // Separate anatomical anchors, including height. Do not use max(lap.x,
    // shoulder.x) for both steps or fixed viewport y values. Reserve enough
    // lateral clearance that the near edge is visibly away from each anchor.
    const beside = anchor => placeRightOfHand({ ...geometry, hand: anchor, shoulder: anchor, clearanceShoulderWidths: 0.45 });
    const start = beside(lap), raised = beside(shoulder);
    if (!start.ready) return { ...start, anchor: 'lap' };
    if (!raised.ready) return { ...raised, anchor: 'shoulder' };
    const radiusY = geometry.radius / Math.max(1 / (geometry.aspect || 1), 1);
    if (![topEdge, bottomEdge].every(Number.isFinite) || topEdge < 0 || bottomEdge > 1 || topEdge >= bottomEdge
      || [lap.y, shoulder.y].some(y => y - radiusY * 1.08 < topEdge + 0.025 || y + radiusY * 1.08 > bottomEdge - 0.025)) {
      return { ready: false, reason: 'need_vertical_room',
        guidance: 'Tilt the camera so there is room above your shoulder and below your resting hand. Move it back only if both cannot fit.' };
    }
    return { ready: true, start: { x: start.x, y: lap.y }, raised: { x: raised.x, y: shoulder.y },
      radius: geometry.radius, radiusX: start.radiusX, radiusY, gap: start.gap };
  }
  function fitSeatedForwardReachTargets(options, minRadius = 0.065) {
    const placementAt = radius => {
      const placement = placeSeatedForwardReachTargets({ ...options, radius });
      if (!placement.ready) return placement;
      const { lap, aspect = 1, leftEdge = 0, rightEdge = 1, topEdge = 0, bottomEdge = 1 } = options;
      const radiusX = radius / Math.max(aspect, 1);
      const radiusY = radius / Math.max(1 / aspect, 1);
      if (lap.x - radiusX * 1.08 < leftEdge + 0.025 || lap.x + radiusX * 1.08 > rightEdge - 0.025
        || lap.y - radiusY * 1.08 < topEdge + 0.025 || lap.y + radiusY * 1.08 > bottomEdge - 0.025)
        return { ready: false, reason: 'need_room_lap',
          guidance: 'Tilt or reposition the camera until your resting hand is comfortably inside the view.' };
      return placement;
    };
    const preferred = placementAt(options.radius);
    if (preferred.ready || !Number.isFinite(options?.radius) || !Number.isFinite(minRadius)
      || minRadius <= 0 || minRadius >= options.radius) return preferred;
    const smallest = placementAt(minRadius);
    // First find the largest circle that fits without moving either forward
    // target. This preserves the exact lap/shoulder heights whenever possible.
    const largestFit = (at, first, ceiling = options.radius) => {
      if (!first?.ready) return null;
      let low = minRadius, high = ceiling, best = first;
      for (let i = 0; i < 16; i++) {
        const radius = (low + high) / 2;
        const candidate = at(radius);
        if (candidate.ready) { low = radius; best = candidate; }
        else high = radius;
      }
      return { ...best, fitted: true, preferredRadius: options.radius };
    };
    const direct = largestFit(placementAt, smallest);

    // A low but visible lap need not force every forward circle to be tiny.
    // Fit the return ring independently around the *actual* hand position and
    // permit at most a small upward shift of the first forward circle.
    const {lap, shoulder, aspect = 1, leftEdge = 0, rightEdge = 1,
      topEdge = 0, bottomEdge = 1} = options;
    if (!usable(lap) || !usable(shoulder) || ![aspect,leftEdge,rightEdge,topEdge,bottomEdge].every(Number.isFinite)
      || aspect <= 0 || topEdge >= bottomEdge || leftEdge >= rightEdge) return direct || smallest;
    const adjustedAt = radius => {
      const radiusY = radius / Math.max(1 / aspect, 1);
      const minY = topEdge + 0.025 + radiusY * 1.08;
      const maxY = bottomEdge - 0.025 - radiusY * 1.08;
      if (minY > maxY) return {ready:false};
      const clampY = y => Math.min(maxY, Math.max(minY, y));
      const startY = clampY(lap.y), raisedY = clampY(shoulder.y);
      if (Math.abs(startY - lap.y) > 0.08 || Math.abs(raisedY - shoulder.y) > 0.08)
        return {ready:false};
      const lapMargin = 0.01;
      const lapRadius = Math.min(radius,
        (lap.x - leftEdge - lapMargin) * Math.max(aspect, 1) / 1.08,
        (rightEdge - lap.x - lapMargin) * Math.max(aspect, 1) / 1.08,
        (lap.y - topEdge - lapMargin) * Math.max(1 / aspect, 1) / 1.08,
        (bottomEdge - lap.y - lapMargin) * Math.max(1 / aspect, 1) / 1.08);
      if (lapRadius < 0.025) return {ready:false};
      const placement = placeSeatedForwardReachTargets({...options,
        lap:{...lap,y:startY},shoulder:{...shoulder,y:raisedY},radius});
      return placement.ready ? {...placement,lapRadius,
        verticallyAdjusted:startY !== lap.y || raisedY !== shoulder.y} : placement;
    };
    // A roughly 11%-of-frame forward radius is already easy to see. Avoid
    // making a larger hit area merely because the return ring sits low.
    const adjusted = largestFit(adjustedAt, adjustedAt(minRadius), Math.min(options.radius, 0.11));
    return adjusted && (!direct || adjusted.radius > direct.radius + 0.005)
      ? adjusted : direct || smallest;
  }
  const api = { placeRightOfHand, placeSeatedForwardReachTargets, fitSeatedForwardReachTargets, screenDistance, wristContact, contains, drawTestingTarget,
    drawBreathingHalo, drawTargetCompletion, completionDurationMs };
  root.RehynReachTarget = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(globalThis);
