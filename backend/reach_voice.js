(function(root) {
class VoiceGuide {
  constructor({ synth = globalThis.speechSynthesis, Utterance = globalThis.SpeechSynthesisUtterance, fetchAudio = null, audio = null, onChange = () => {}, timeoutMs = null, captionMs = 1800, progressTimeoutMs = 0 } = {}) {
    this.synth = synth; this.Utterance = Utterance; this.onChange = onChange;
    this.fetchAudio = fetchAudio; this.audio = audio;
    this.timeoutMs = timeoutMs; this.captionMs = captionMs; this.enabled = true;
    this.progressTimeoutMs = progressTimeoutMs;
    this.busy = false; this.failed = false; this.caption = ''; this.status = fetchAudio ? 'Molly voice' : 'Device voice';
  }
  cancel() {
    this.finish?.(false); this.finish = null;
    if (this.audio) { this.audio.pause(); this.audio.onended = null; this.audio.onerror = null; this.audio.onpause = null; }
    this.synth?.cancel(); this.busy = false; this.failed = false;
  }
  speak(text) {
    this.cancel(); this.caption = text; this.busy = true; this.failed = false;
    this.status = this.enabled ? (this.fetchAudio ? 'Preparing Molly voice' : 'Speaking with device voice') : 'Captions only'; this.onChange(this);
    return new Promise(resolve => {
      let timer, resumeTimer, progressTimer, seekListener, done = false, utterance;
      const finish = (ok, error = false) => {
        if (done) return; done = true; clearTimeout(timer); clearTimeout(resumeTimer); clearInterval(progressTimer);
        if (utterance) { utterance.onend = null; utterance.onerror = null; }
        if (this.audio) {
          this.audio.onended = null; this.audio.onerror = null; this.audio.onpause = null;
          if(seekListener)this.audio.removeEventListener('loadedmetadata',seekListener);
          if (error) this.audio.pause();
        }
        this.finish = null; this.busy = false; this.failed = error;
        this.status = error ? 'Voice unavailable. Retry voice or turn it off to continue with captions.'
          : this.enabled ? (this.fetchAudio ? 'Molly voice' : 'Device voice') : 'Captions only';
        this.onChange(this); resolve(ok);
      };
      this.finish = finish;
      if (!this.enabled) { timer = setTimeout(() => finish(true), this.captionMs); return; }
      if (this.fetchAudio) {
        if (!this.audio) { finish(false, true); return; }
        timer = setTimeout(() => finish(false, true), this.timeoutMs ?? 180000);
        Promise.resolve().then(() => this.fetchAudio(text)).then(source => {
          if (done) return;
          const sourceUrl=source.startsWith('data:') || source.startsWith('blob:') ? source : 'data:audio/mpeg;base64,' + source;
          this.audio.src = sourceUrl;
          this.audio.muted = false; this.audio.volume = 1;
          this.audio.onended = () => {
            // A media element can report an end after its source is replaced.
            // Never unlock the movement target after only part of a cue.
            if (this.audio.src !== sourceUrl) finish(false,true);
            else if (Number.isFinite(this.audio.duration) && Number.isFinite(this.audio.currentTime)
                && this.audio.duration - this.audio.currentTime > 0.25) finish(false, true);
            else finish(true);
          };
          if(this.progressTimeoutMs>0){
            let position=0,progressAt=Date.now(),recoveries=0;
            progressTimer=setInterval(()=>{
              if(done)return;
              const now=Date.now(),current=this.audio.currentTime;
              if(this.audio.src!==sourceUrl){finish(false,true);return;}
              if(Number.isFinite(current) && current>position+.03){position=current;progressAt=now;return;}
              if(now-progressAt<this.progressTimeoutMs)return;
              if(this.audio.ended){this.audio.onended?.();return;}
              // Some media/decoder stalls produce neither pause nor error.
              // Reload the already cached clip and resume slightly before the
              // last played word; never treat stalled playback as completion.
              if(++recoveries>2){finish(false,true);return;}
              progressAt=now;this.status='Restoring Molly voice…';this.onChange(this);
              const resumeAt=Math.max(0,position-.15);
              if(seekListener)this.audio.removeEventListener('loadedmetadata',seekListener);
              seekListener=()=>{
                if(done)return;
                if(resumeAt>0 && Number.isFinite(this.audio.duration))this.audio.currentTime=Math.min(resumeAt,Math.max(0,this.audio.duration-.05));
                this.status='Speaking with Molly voice';this.onChange(this);
              };
              this.audio.addEventListener('loadedmetadata',seekListener,{once:true});
              this.audio.load();
              Promise.resolve().then(()=>this.audio.play()).catch(()=>finish(false,true));
            },Math.min(500,this.progressTimeoutMs/2));
          }
          this.audio.onerror = () => finish(false, true);
          this.audio.onpause = () => {
            if (done || !this.audio.paused || this.audio.ended) return;
            // A transient browser/media pause must not leave the patient with
            // a half-spoken instruction while the test waits indefinitely.
            clearTimeout(resumeTimer);
            resumeTimer = setTimeout(() => {
              if (done || !this.audio.paused || this.audio.ended) return;
              Promise.resolve().then(() => this.audio.play()).catch(() => finish(false, true));
            }, 200);
          };
          this.status = 'Speaking with Molly voice'; this.onChange(this);
          return this.audio.play();
        }).catch(() => finish(false, true));
        return;
      }
      if (!this.synth || !this.Utterance) { finish(false, true); return; }
      try {
        utterance = new this.Utterance(text);
        utterance.lang = 'en-GB'; utterance.rate = 0.92;
        const voices = this.synth.getVoices();
        utterance.voice = voices.find(v => v.lang === 'en-GB') || voices.find(v => /^en[-_]/i.test(v.lang)) || null;
        utterance.onend = () => finish(true);
        utterance.onerror = () => finish(false, true);
        timer = setTimeout(() => { finish(false, true); this.synth.cancel(); }, this.timeoutMs ?? Math.max(15000, text.length * 100 + 5000));
        this.synth.speak(utterance);
      } catch { finish(false, true); }
    });
  }
}

root.RehynVoiceGuide={VoiceGuide};
if(typeof module!=="undefined"&&module.exports)module.exports={VoiceGuide};
})(globalThis);
