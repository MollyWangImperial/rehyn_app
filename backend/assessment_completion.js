/* Completion presentation shared by the standalone and embedded assessment runner. */
(function(root){
  "use strict";
  function create({document:doc=root.document, onExit, minimumMs=1800, timeoutMs=90000, slowMs=15000}={}){
    const byId=id=>doc.getElementById(id);
    const panel=byId("assessmentCompletion"), title=byId("analysisTitle"), message=byId("analysisMessage");
    const results=byId("analysisResults"), actions=byId("analysisActions"), retry=byId("analysisRetry"), exit=byId("analysisExit");
    let busy=false, finished=false;
    exit.onclick=()=>onExit?.();
    function showResults(data){
      const quality=data.metrics?.task_quality;
      const skipped=new Set((data.task_results || []).filter(t=>t.metrics?.walking_skipped).map(t=>t.task_id));
      results.replaceChildren();
      for(const task of quality?.tasks || []){
        const row=doc.createElement("li"), label=doc.createElement("span"), value=doc.createElement("strong");
        row.className="analysisResult";
        label.textContent=task.label;
        if(skipped.has(task.task_id)) value.textContent="Not assessed";
        else if(typeof task.score==="number" && Number.isFinite(task.score)) value.textContent=`${Math.round(task.score)} / 100`;
        else{
          value.textContent="—";
          const note=doc.createElement("small");
          note.textContent="Not enough measured movement to give a score.";
          label.append(note);
        }
        row.append(label,value); results.append(row);
      }
      results.hidden=!results.children.length;
      title.textContent="Your movement results";
      message.textContent=results.children.length ? "Here’s what we measured in this movement check." : "Your movement check is saved. You can return to your assessment results.";
      byId("analysisPreviewNote").hidden=!data.preview_only;
      panel.dataset.state="ready"; panel.setAttribute("aria-busy","false");
      actions.hidden=false; retry.hidden=true; exit.textContent="Done";
      title.focus({preventScroll:true});
    }
    async function run(work,onComplete){
      if(busy || finished) return;
      busy=true;
      doc.body.classList.add("assessment-finishing");
      panel.hidden=false; panel.dataset.state="analyzing"; panel.setAttribute("aria-busy","true");
      title.textContent="Analyzing your movement";
      message.textContent="Your movement check is complete. We’re bringing your results together.";
      actions.hidden=true; results.hidden=true; retry.hidden=true;
      byId("analysisPreviewNote").hidden=true;
      title.focus({preventScroll:true});
      const controller=new AbortController();
      let timeout;
      const slow=setTimeout(()=>{message.textContent="This is taking a little longer. Please keep this page open.";},slowMs);
      try{
        // The motion conveys activity, never an invented analysis percentage.
        const result=await Promise.race([
          Promise.all([work(controller.signal),new Promise(resolve=>setTimeout(resolve,minimumMs))]).then(([data])=>data),
          new Promise((_,reject)=>{timeout=setTimeout(()=>{controller.abort();reject(new Error("Analysis timed out"));},timeoutMs);})
        ]);
        clearTimeout(slow); clearTimeout(timeout);
        showResults(result);
        finished=true;
        // A closed host must not turn successful analysis into a retryable save.
        try{onComplete?.(result);}catch{}
      }catch(error){
        clearTimeout(slow); clearTimeout(timeout);
        panel.dataset.state="error"; panel.setAttribute("aria-busy","false");
        title.textContent="We couldn’t finish the analysis";
        message.textContent="Your movement data is still here. Try again to get your results.";
        actions.hidden=false; retry.hidden=false; exit.textContent="Back";
        retry.onclick=()=>run(work,onComplete);
        title.focus({preventScroll:true});
      }finally{busy=false;}
    }
    return {run};
  }
  const api={create};
  if(typeof module!=="undefined" && module.exports)module.exports=api;
  else root.RehynAssessmentCompletion=api;
})(typeof window!=="undefined" ? window : globalThis);
