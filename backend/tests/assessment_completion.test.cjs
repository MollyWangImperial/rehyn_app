const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {create}=require('../assessment_completion.js');

function fixture(options={}){
  class Element{
    constructor(){this.dataset={};this.children=[];this.attributes={};this.hidden=true;this.classList={add:()=>{}};}
    append(...items){this.children.push(...items);}
    replaceChildren(...items){this.children=items;}
    setAttribute(k,v){this.attributes[k]=v;}
    focus(){}
  }
  const nodes={};
  const document={body:new Element(),getElementById:id=>nodes[id] ||= new Element(),createElement:()=>new Element()};
  const screen=create({document,minimumMs:0,timeoutMs:100,slowMs:50,...options});
  return {screen,nodes};
}
const data={preview_only:true,task_results:[{task_id:'L6',metrics:{walking_skipped:true}}],metrics:{task_quality:{tasks:[
  {task_id:'T1',label:'Seated Forward Reach',score:85},
  {task_id:'T3',label:'Hand to Mouth',score:null},
  {task_id:'L6',label:'Walking',score:null}
]}}};

test('pending analysis stays visible and only response data becomes results',async()=>{
  const {screen,nodes}=fixture();
  let resolve, calls=0, completed=0;
  const work=()=>{calls++;return new Promise(r=>resolve=r)};
  const pending=screen.run(work,()=>completed++);
  assert.equal(nodes.assessmentCompletion.dataset.state,'analyzing');
  assert.equal(nodes.analysisResults.hidden,true);
  await screen.run(work);
  assert.equal(calls,1);
  resolve(data); await pending;
  assert.equal(nodes.assessmentCompletion.dataset.state,'ready');
  assert.equal(completed,1);
  assert.equal(nodes.analysisPreviewNote.hidden,false);
  const rows=nodes.analysisResults.children;
  assert.equal(rows[0].children[1].textContent,'85 / 100');
  assert.equal(rows[1].children[1].textContent,'—');
  assert.equal(rows[2].children[1].textContent,'Not assessed');
  await screen.run(work);
  assert.equal(calls,1);
});

test('failed request stops animation and retry reuses the existing movement data',async()=>{
  const {screen,nodes}=fixture();
  let attempts=0,completed=0;
  const work=async()=>{if(++attempts===1)throw Error('Offline');return data;};
  await screen.run(work,()=>completed++);
  assert.equal(nodes.assessmentCompletion.dataset.state,'error');
  assert.equal(nodes.analysisRetry.hidden,false);
  assert.equal(completed,0);
  await nodes.analysisRetry.onclick();
  assert.equal(attempts,2);
  assert.equal(completed,1);
  assert.equal(nodes.assessmentCompletion.dataset.state,'ready');
});

test('a hung request is aborted and cannot replace the error with a late response',async()=>{
  const {screen,nodes}=fixture({timeoutMs:10,slowMs:3});
  let signal,resolve,completed=false;
  await screen.run(s=>{signal=s;return new Promise(r=>resolve=r)},()=>completed=true);
  assert.equal(signal.aborted,true);
  assert.equal(nodes.assessmentCompletion.dataset.state,'error');
  resolve(data); await new Promise(r=>setTimeout(r,5));
  assert.equal(completed,false);
  assert.equal(nodes.assessmentCompletion.dataset.state,'error');
});

test('a disconnected host does not turn finished results into a second submission',async()=>{
  const {screen,nodes}=fixture();
  let calls=0;
  const work=async()=>{calls++;return data;};
  await screen.run(work,()=>{throw Error('Host closed');});
  assert.equal(nodes.assessmentCompletion.dataset.state,'ready');
  await screen.run(work);
  assert.equal(calls,1);
});

const source=fs.readFileSync(require.resolve('../server.py'),'utf8');
const finish=source.slice(source.indexOf('let completionScreen = null;'),source.indexOf('let lastTestingReachPoseVideoTime'));
for(const preview of [true,false])test(`runner finish uses ${preview?'stateless preview':'normal saved assessment'} and releases the camera`,async()=>{
  let stopped=false,cleared=false,posted,request;
  const video={videoWidth:640,videoHeight:480,srcObject:{getTracks:()=>[{stop:()=>{stopped=true;video.videoWidth=0;video.videoHeight=0;}}]}};
  const context={
    document:{body:{classList:{remove(){}}}},setInstructionsOpen(){},running:true,audioEl:{pause(){}},
    LOCAL_PREVIEW_MODE:preview,LIBRARY_TEST_MODE:false,video,canvas:{width:640,height:480},ctx:{clearRect(){cleared=true}},
    cameraFrame:{clientWidth:920,clientHeight:668},CAMERA_FIT_MODE:'contain',CAMERA_DEVICE_CLASS:'desktop',
    pendingTaskVideoSaves:new Set(),pendingTaskProgressSaves:new Set(),API_BASE:'http://127.0.0.1:8001/api',
    CURRENT_USER_ID:preview?'':'user',ACCOUNT_HEADERS:{'x-user-id':'user'},taskResults:data.task_results,
    tasks:[{id:'L6'}],AFFECTED_SIDE:'right',ASSESSMENT_PACKAGE:'initial',MOTION_SAMPLE_INTERVAL_MS:100,MAX_MOTION_FRAMES:500,motionFrames:[{}],
    window:{RehynAssessmentCompletion:{create:()=>({run:async(work,done)=>done(await work(new AbortController().signal))})}},
    fetch:async(url,options)=>{request={url,...options};return {ok:true,json:async()=>({...data,id:preview?undefined:'saved'})}},
    postRN:m=>posted=m,
  };
  vm.runInNewContext(finish+';this.finishAssessment=finishAssessment;',context);
  await context.finishAssessment();
  assert.ok(stopped && cleared);
  const payload=JSON.parse(request.body);
  assert.equal(posted.type,preview?'assessment_preview_complete':'assessment_complete');
  assert.equal(posted.results_in_runner,true);
  if(preview){assert.match(request.url,/preview-results\?local_preview=1/);assert.equal(payload.motion_data,undefined);}
  else{assert.match(request.url,/assessment\/submit$/);assert.equal(payload.motion_data.camera_projection.source_width,640);}
});
