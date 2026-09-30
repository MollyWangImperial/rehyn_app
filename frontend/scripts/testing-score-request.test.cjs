const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const ts=require('typescript');
const compiled=ts.transpileModule(fs.readFileSync(path.join(__dirname,'../src/testingScoreRequest.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const context={exports:{},AbortController,setTimeout,clearTimeout};vm.runInNewContext(compiled,context);
const {requestTestingScore}=context.exports;
const fast={retryDelayMs:0,timeoutMs:100};
test('the same completed calculation recovers from network and temporary service failures',async()=>{
  let calls=0;
  const result=await requestTestingScore(async()=>{calls++;if(calls===1)throw new TypeError('NetworkError');if(calls===2)return new Response('',{status:503});return Response.json({score:80});},fast);
  assert.equal(result.score,80);assert.equal(calls,3);
});
test('permanent validation and authorization errors are not repeatedly submitted',async()=>{
  for(const status of [401,403,409,422]){let calls=0;
    await assert.rejects(requestTestingScore(async()=>{calls++;return new Response('',{status});},fast));assert.equal(calls,1);
  }
});
test('a failed calculation preserves the retry instruction rather than asking to repeat movement',async()=>{
  let calls=0;await assert.rejects(requestTestingScore(async()=>{calls++;throw new TypeError('NetworkError');},fast),/movement evidence is still on this page.*do not need to repeat/);
  assert.equal(calls,3);
});
test('stalled requests are aborted and retry is bounded',async()=>{
  let calls=0;await assert.rejects(requestTestingScore(signal=>{calls++;return new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('aborted'))));},{...fast,timeoutMs:5}));assert.equal(calls,3);
});
test('an interrupted JSON response is retried',async()=>{
  let calls=0;const result=await requestTestingScore(async()=>++calls===1?new Response('{'):Response.json({score:90}),fast);
  assert.equal(result.score,90);assert.equal(calls,2);
});
