/* Seated Testing camera-quality screening, not a guarantee of anatomical accuracy.
 * Pose Landmarker Lite supplies landmarks and a person/background mask. Colour
 * and exposure checks are deterministic, local pixel measurements, not a chair
 * classifier. Never infer muscle weakness from a failed camera check. */
(function(root) {
  const CONFIG=Object.freeze({visibility:.75,clearMs:2000,maxGapMs:450,minFrames:8,
    darkLuma:45,brightLuma:235,similarDeltaE:12,minRegionPixels:24});
  const MESSAGES=Object.freeze({
    waiting:'Checking camera lighting, clothing contrast and grading angles. Keep your hand resting on your lap.',
    pixels:'The camera image could not be checked. Restart the camera and allow camera access before beginning.',
    dark:'Your arm is too dark for a clear measurement. Turn on a light in front of you or face a brighter light. Avoid a bright window behind you.',
    bright:'There is too much glare on your upper body. Reduce the light shining directly at the camera or move out of direct sunlight.',
    backlit:'The background is much brighter than your upper body. Put a light in front of you or turn away from the bright window.',
    contrast:'Your top appears to blend into the chair or background near your shoulders. Try a contrasting clothing colour, or use a contrasting chair cover or background, then sit still again.',
    segmentation:'I cannot clearly separate your shoulders from the background. Use even front lighting and a contrasting top or chair cover, with your shoulders in view.',
    landmarks:'Keep your face, both shoulders, affected elbow and hand, and both hips in view. If they are already visible, improve the lighting so the camera can track them clearly.',
    angles:'The grading angles are not clear enough yet. Keep your affected arm visible, away from objects that hide it, and improve the front lighting.',
    unstable:'The shoulder or elbow tracking is moving around. Rest your hand on your lap, keep still and use even front lighting with a contrasting background.',
    baseline:'Keep sitting upright with your hand on your lap while the shoulder and face references are recorded.',
    settling:'The view is clear. Keep still for two seconds while the grading angles are checked.',
    ready:'Lighting, clothing contrast and grading angles checked.',
    stale:'Waiting for a fresh camera frame. Movement scoring and target timing are paused.'
  });
  const median=a=>a.length?[...a].sort((x,y)=>x-y)[Math.floor(a.length/2)]:NaN;
  const number=(v,digits=2)=>Number.isFinite(v)?v.toFixed(digits):'unavailable';
  const luma=rgb=>.2126*rgb[0]+.7152*rgb[1]+.0722*rgb[2];
  function lab(rgb){
    const [r,g,b]=rgb.map(v=>{v/=255;return v>.04045?((v+.055)/1.055)**2.4:v/12.92;});
    const f=v=>v>.008856?Math.cbrt(v):7.787*v+16/116;
    const x=f((r*.4124564+g*.3575761+b*.1804375)/.95047);
    const y=f(r*.2126729+g*.7151522+b*.0721750);
    const z=f((r*.0193339+g*.1191920+b*.9503041)/1.08883);
    return [116*y-16,500*(x-y),200*(y-z)];
  }
  function regionSummary(colors,minPixels=CONFIG.minRegionPixels){
    if(colors.length<minPixels)return null;
    const rgb=[0,1,2].map(i=>median(colors.map(c=>c[i]))), ys=colors.map(luma);
    return {rgb,lab:lab(rgb),luma:median(ys),dark:ys.filter(v=>v<30).length/ys.length,
      clipped:ys.filter(v=>v>245).length/ys.length,count:colors.length};
  }
  // Work in the unmirrored camera coordinates used by Pose Landmarker.
  function inspectImage(image,pose,mask){
    if(!image?.data || !image.width || !image.height)return {issue:'pixels'};
    const {data,width,height}=image;
    if(data.length<width*height*4)return {issue:'pixels'};
    const shoulders=[pose?.[11],pose?.[12]];
    const haveShoulders=shoulders.every(p=>p && Number.isFinite(p.x) && Number.isFinite(p.y)
      && p.x>0 && p.x<1 && p.y>0 && p.y<1 && (p.visibility??0)>=.35);
    const validMask=mask?.data && mask.width>0 && mask.height>0 && mask.data.length>=mask.width*mask.height;
    const mid=haveShoulders?{x:(shoulders[0].x+shoulders[1].x)/2,y:(shoulders[0].y+shoulders[1].y)/2}:{x:.5,y:.35};
    const span=haveShoulders?Math.abs(shoulders[0].x-shoulders[1].x):.35;
    const foreground=[],background=[],central=[],face=[],sides=shoulders.map(()=>({inside:[],outside:[]}));
    const nose=pose?.[0],faceWidth=Math.abs((pose?.[7]?.x??0)-(pose?.[8]?.x??0));
    for(let y=0;y<height;y+=2)for(let x=0;x<width;x+=2){
      const nx=x/width,ny=y/height;
      if(Math.abs(nx-mid.x)>span*1.1 || ny<mid.y-.30 || ny>mid.y+.23)continue;
      const i=(y*width+x)*4,c=[data[i],data[i+1],data[i+2]];
      const prob=validMask?mask.data[Math.min(mask.height-1,Math.floor(ny*mask.height))*mask.width+Math.min(mask.width-1,Math.floor(nx*mask.width))]:NaN;
      if(Math.abs(nx-mid.x)<span*.32 && ny>=mid.y && ny<mid.y+.2)central.push(c);
      if(prob>=.8)foreground.push(c);
      if(prob<=.2)background.push(c);
      if(nose && (nose.visibility??0)>=.5 && faceWidth>.025 && Math.abs(nx-nose.x)<faceWidth*.45
        && Math.abs(ny-nose.y)<faceWidth*.45 && prob>=.8)face.push(c);
      if(!haveShoulders)continue;
      shoulders.forEach((s,n)=>{
        const sign=s.x>=mid.x?1:-1,offset=(nx-s.x)*sign/span;
        // Sleeve/upper-torso patches below each shoulder. Background is sampled
        // beyond the sleeve, never the opposite half of the shirt.
        if(ny<s.y+.025 || ny>s.y+.15)return;
        if(offset>=-.23 && offset<=.08 && prob>=.8)sides[n].inside.push(c);
        if(offset>=.16 && offset<=.55 && prob<=.2)sides[n].outside.push(c);
      });
    }
    // Prefer facial exposure so a well-lit black or white shirt is not itself
    // mistaken for darkness or glare. This remains a conservative image check.
    const facialExposure=regionSummary(face,6);
    const body=facialExposure||regionSummary(foreground)||regionSummary(central),bg=regionSummary(background);
    if(!body)return {issue:'pixels'};
    const result={bodyLuma:body.luma,backgroundLuma:bg?.luma??null,contrast:[],issue:null};
    if(facialExposure){
      if(body.luma<CONFIG.darkLuma || body.dark>.55)result.issue='dark';
      else if(body.luma>CONFIG.brightLuma || body.clipped>.35)result.issue='bright';
      else if(bg && bg.luma>170 && body.luma<85 && bg.luma/body.luma>2.5)result.issue='backlit';
    }else{
      // Without a readable face, only diagnose extreme scene exposure. A dark
      // shirt against a lit room must not create a spurious lighting warning.
      const levels=[...central,...background].map(luma).sort((a,b)=>a-b);
      if(levels.length && levels[Math.floor(levels.length*.9)]<30)result.issue='dark';
      else if(levels.length && levels[Math.floor(levels.length*.1)]>245)result.issue='bright';
    }
    const lightingIssue=result.issue;
    let contrastIssue=null;
    for(const side of sides){
      const a=regionSummary(side.inside),b=regionSummary(side.outside);
      result.contrast.push(a&&b?Math.hypot(...a.lab.map((v,i)=>v-b.lab[i])):null);
    }
    if(!haveShoulders)contrastIssue='landmarks';
    else if(!validMask || result.contrast.some(v=>v===null))contrastIssue='segmentation';
    else if(result.contrast.some(v=>v<CONFIG.similarDeltaE))contrastIssue='contrast';
    result.checks={
      lighting:{ready:!lightingIssue,detail:lightingIssue?MESSAGES[lightingIssue]
        :facialExposure?`Face brightness ${number(body.luma,0)}/255; range 45–235. No excessive glare or backlighting.`
        :'Scene exposure checked; a readable face sample was unavailable.'},
      contrast:{ready:!contrastIssue,detail:contrastIssue==='landmarks'?'Shoulder positions are missing; colour contrast cannot be checked.'
        :contrastIssue==='segmentation'?'Clothing/background samples are unclear near a shoulder; colour contrast cannot be measured.'
        :`Colour difference: left ${number(result.contrast[0],1)}, right ${number(result.contrast[1],1)}; each needs at least 12.`}
    };
    result.issue=lightingIssue||contrastIssue;
    return result;
  }
  function gradingFrame(pose,world,metrics,side='right',checkHead=false){
    const ids=side==='left'?[7,8,11,12,13,15,23,24]:[7,8,11,12,14,16,23,24];
    if(checkHead)ids.push(0);
    const names={0:'Nose',7:'Left ear',8:'Right ear',11:'Left shoulder',12:'Right shoulder',13:'Left elbow',14:'Right elbow',15:'Left wrist',16:'Right wrist',23:'Left hip',24:'Right hip'};
    const failures=ids.flatMap(i=>{
      const p=pose?.[i],w=world?.[i];
      if(!p)return [`${names[i]}: not detected`];
      if(!(p.x>.01 && p.x<.99 && p.y>.01 && p.y<.99))return [`${names[i]}: outside the usable camera view`];
      if(!((p.visibility??0)>=CONFIG.visibility))return [`${names[i]} visibility ${number(p.visibility??0)} < ${CONFIG.visibility}`];
      if(p.presence!==undefined && !(p.presence>=CONFIG.visibility))return [`${names[i]} presence ${number(p.presence)} < ${CONFIG.visibility}`];
      // T3 head/face checks use image points. Its shoulder-rise check no
      // longer needs the model's 3D ear position or ear-to-shoulder distance.
      if(!(checkHead && [0,7,8].includes(i)) && (!w || ![w.x,w.y,w.z].every(Number.isFinite)))return [`${names[i]}: model coordinates unavailable`];
      return [];
    });
    const keys=['elbow_extension','arm_elevation','width','screenWidth','shoulderLine'];
    if(!checkHead)keys.push('neckGap');
    if(checkHead)keys.push('headPitch','elbow_flexion');
    const metricNames={elbow_extension:'Elbow extension',arm_elevation:'Arm elevation',width:'Model shoulder width',screenWidth:'Visible shoulder width',shoulderLine:'Shoulder alignment',neckGap:'Shoulder-to-ear distance'};
    Object.assign(metricNames,{headPitch:'Head position',elbow_flexion:'Elbow bend'});
    const missing=keys.filter(k=>!Number.isFinite(metrics?.[k]));
    let angleDetail=missing.length?missing.map(k=>metricNames[k]).join(', ')+': measurement unavailable.':'';
    if(!angleDetail && (metrics.width<.05 || metrics.screenWidth<.08))angleDetail=`Shoulder width too small to measure: model ${number(metrics.width)} (minimum 0.05), image ${number(metrics.screenWidth)} (minimum 0.08).`;
    if(!angleDetail && !metrics.torso?.every(Number.isFinite))angleDetail='Torso reference coordinates unavailable.';
    const a=side==='left'?{s:11,e:13,w:15}:{s:12,e:14,w:16};
    // Vanishing projected segments make the 2D elbow angle ill-conditioned.
    const segments=[[a.s,a.e,'Upper arm'],[a.e,a.w,'Forearm']];
    for(const [i,j,name] of segments){
      if(angleDetail)break;
      if(!pose?.[i] || !pose?.[j]){angleDetail=`${name} points are missing.`;break;}
      const size=Math.hypot(pose[i].x-pose[j].x,pose[i].y-pose[j].y);
      if(!Number.isFinite(size) || size<.025)angleDetail=`${name} projected length ${number(size,3)} < 0.025; its points overlap in the image.`;
    }
    const checks={landmarks:{ready:!failures.length,detail:failures.length?failures.join('; ')+'.':`${checkHead?'Nose, both ears':'Both ears'}, shoulders and hips, and the affected elbow/wrist meet 0.75 confidence.`},
      angles:{ready:!angleDetail,detail:angleDetail||`Arm elevation ${number(metrics.arm_elevation,1)}°; elbow extension ${number(metrics.elbow_extension,1)}°. All grading inputs are available.`}};
    if(failures.length || angleDetail)return {issue:failures.length?'landmarks':'angles',detail:failures.length?checks.landmarks.detail:angleDetail,checks};
    return {issue:null,checks,signature:[pose[11].x,pose[11].y,pose[12].x,pose[12].y],angles:[metrics.arm_elevation,metrics.elbow_extension]};
  }
  function runtimeStatus({now,measuredAt,image,frame}){
    // Similar colours are a risk cue, not proof that currently confident
    // landmarks are wrong. Keep the initial setup check; once it passed, a
    // colour-only warning must not cancel an otherwise measurable hand hit.
    const advisory=image?.issue==='contrast'?'contrast':null;
    const issue=now-measuredAt>CONFIG.maxGapMs?'stale':frame?.issue
      || (advisory?null:image?.issue) || (!image||!frame?'waiting':null);
    return {ready:!issue,issue:issue||'ready',advisory,message:MESSAGES[issue||advisory||'ready']};
  }
  class Gate {
    constructor(){this.reset();}
    reset(){this.lastAt=null;this.goodSince=null;this.frames=[];this.issue='waiting';this.ready=false;this.clearReady=false;this.evidence=null;this.frame=null;this.detail='';this.lastReset=null;this.resetCount=0;this.baselineReady=false;this.baselineDetail='';}
    update({now,image,frame,baselineReady}){
      if(!Number.isFinite(now) || (this.lastAt!==null && now<=this.lastAt))return this.status(now);
      const elapsed=this.lastAt===null?0:now-this.lastAt;
      const gap=this.lastAt!==null && elapsed>CONFIG.maxGapMs;
      const hadProgress=this.goodSince!==null;
      this.lastAt=now;this.evidence=image;this.frame=frame;this.baselineReady=!!baselineReady;
      let issue=image?.issue || frame?.issue;
      let detail=image?.issue?MESSAGES[image.issue]:frame?.detail||'';
      if(image?.issue==='contrast')detail=`Shoulder colour difference: left ${number(image.contrast?.[0],1)}, right ${number(image.contrast?.[1],1)}; each needs at least 12.`;
      if(!image || !frame)issue='waiting';
      if(issue || gap){this.goodSince=null;this.frames=[];this.ready=false;this.clearReady=false;}
      if(!issue){
        this.frames.push({now,...frame});
        while(this.frames.length>CONFIG.minFrames && this.frames[1].now<=now-CONFIG.clearMs)this.frames.shift();
        if(this.frames.length>=4){
          const spread=i=>Math.max(...this.frames.map(f=>f.angles[i]))-Math.min(...this.frames.map(f=>f.angles[i]));
          const jump=frame.signature.some((v,i)=>Math.abs(v-median(this.frames.map(f=>f.signature[i])))>.025);
          if(jump || spread(0)>12 || spread(1)>18){
            issue='unstable';
            const reasons=[];
            if(jump)reasons.push('shoulder position changed by more than 2.5% of the image dimension');
            if(spread(0)>12)reasons.push(`arm elevation varied ${number(spread(0),1)}° (limit 12°)`);
            if(spread(1)>18)reasons.push(`elbow extension varied ${number(spread(1),1)}° (limit 18°)`);
            detail=reasons.join('; ')+'.';
          }
        }
        if(issue){this.goodSince=null;this.frames=[];this.ready=false;this.clearReady=false;}
        else {
          if(this.goodSince===null)this.goodSince=now;
          this.clearReady=now-this.goodSince>=CONFIG.clearMs && this.frames.length>=CONFIG.minFrames;
          this.ready=this.clearReady && !!baselineReady;
        }
      }
      // Keep the failure visible after tracking recovers; otherwise a short
      // confidence dip vanishes before the patient can read why time reset.
      if(hadProgress && (issue || gap)){
        this.resetCount++;
        this.lastReset={at:now,issue:issue||'stale',detail:issue?(detail||MESSAGES[issue]):`Tracking gap ${number(elapsed,0)} ms exceeded the 450 ms limit.`};
      }
      this.issue=issue || (this.ready?'ready':this.clearReady?'baseline':'settling');
      this.detail=detail;
      return this.status(now);
    }
    status(now){
      const fresh=this.lastAt!==null && now>=this.lastAt && now-this.lastAt<=CONFIG.maxGapMs;
      const issue=fresh?this.issue:'stale';
      const clearMs=this.goodSince===null?0:Math.max(0,now-this.goodSince);
      const pending={ready:null,detail:'Waiting for a fresh camera measurement.'};
      const checks={};
      for(const key of ['lighting','contrast'])checks[key]=fresh?(this.evidence?.checks?.[key]||{ready:null,detail:MESSAGES[this.evidence?.issue]||pending.detail}):pending;
      for(const key of ['landmarks','angles'])checks[key]=fresh?(this.frame?.checks?.[key]||{ready:null,detail:MESSAGES[this.frame?.issue]||pending.detail}):pending;
      checks.stability={ready:fresh&&this.clearReady?true:issue==='unstable'?false:null,
        detail:!fresh?'Waiting for fresh tracking (maximum gap 450 ms).':issue==='unstable'?this.detail
        :`${number(Math.min(clearMs,CONFIG.clearMs)/1000,1)} of 2 seconds; ${this.frames.length} fresh frames (minimum 8).`};
      checks.baseline={ready:fresh&&this.clearReady&&this.baselineReady?true:null,
        detail:fresh&&this.clearReady&&this.baselineReady?'Shoulder, face and torso references recorded.'
        :this.baselineDetail||'Waiting for stable, clear measurements with the hand on the lap.'};
      return {ready:fresh&&this.ready,clearReady:fresh&&this.clearReady,issue,message:MESSAGES[issue],evidence:this.evidence,
        checks,
        detail:fresh?this.detail:`No fresh pose measurement for ${this.lastAt===null?'an unknown time':number(now-this.lastAt,0)+' ms'} (limit 450 ms).`,
        lastReset:this.lastReset,resetCount:this.resetCount,
        clearMs};
    }
  }
  const api={CONFIG,MESSAGES,inspectImage,gradingFrame,runtimeStatus,Gate,lab};
  root.RehynReachCameraQuality=api;
  if(typeof module!=='undefined')module.exports=api;
})(globalThis);
