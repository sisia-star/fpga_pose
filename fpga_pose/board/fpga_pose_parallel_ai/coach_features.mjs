/* Local speech and measured telemetry. No network, classification, or guessed counts. */
export const METRICS_SCHEMA = 'pose-motion-metrics-v1';
const EXERCISES = new Set(['squats','jumping_jacks','pushups','situp','pullups','birddog','high_plank','knee_plank']);
const SIDES = {left:{shoulder:11,elbow:13,wrist:15,hip:23,knee:25,ankle:27},right:{shoulder:12,elbow:14,wrist:16,hip:24,knee:26,ankle:28}};
const rounded = value => Math.round(value*1000)/1000;
const defaultNow = () => globalThis.performance?.now?.() ?? Date.now();

export function visiblePoint(point, threshold=0.5) {
  return !!point && Number.isFinite(point.x) && Number.isFinite(point.y) && point.x>=0 && point.x<=1 && point.y>=0 && point.y<=1 &&
    Number.isFinite(point.visibility) && point.visibility>=threshold && (point.presence===undefined || Number.isFinite(point.presence)&&point.presence>=threshold);
}
export function jointAngle2D(a,b,c,width,height) {
  if (![width,height].every(v=>Number.isFinite(v)&&v>0) || ![a,b,c].every(p=>p&&Number.isFinite(p.x)&&Number.isFinite(p.y))) return null;
  const ux=(a.x-b.x)*width,uy=(a.y-b.y)*height,vx=(c.x-b.x)*width,vy=(c.y-b.y)*height,
    denominator=Math.hypot(ux,uy)*Math.hypot(vx,vy);
  if(denominator<1e-8)return null;
  return Math.acos(Math.max(-1,Math.min(1,(ux*vx+uy*vy)/denominator)))*180/Math.PI;
}
export function torsoInclination2D(shoulder,hip,width,height) {
  if (![width,height].every(v=>Number.isFinite(v)&&v>0) || ![shoulder,hip].every(p=>p&&Number.isFinite(p.x)&&Number.isFinite(p.y))) return null;
  const dx=(shoulder.x-hip.x)*width,dy=(shoulder.y-hip.y)*height;
  if(Math.hypot(dx,dy)<1e-8)return null;
  return Math.atan2(Math.abs(dx),Math.abs(dy))*180/Math.PI;
}

export class SpeechCoach {
  constructor({speechSynthesis=globalThis.speechSynthesis,Utterance=globalThis.SpeechSynthesisUtterance,now=defaultNow,onStatus=()=>{}}={}) {
    this.synth=speechSynthesis;this.Utterance=Utterance;this.now=now;this.onStatus=onStatus;
    this.training=null;this.enabled=false;this.counter=null;this.countdownState=null;this.reminders=new Map();this.voice=null;
    this.voiceListener=()=>this.refreshVoices();this.synth?.addEventListener?.('voiceschanged',this.voiceListener);this.refreshVoices();
  }
  get supported(){return !!(this.synth&&typeof this.synth.speak==='function'&&typeof this.synth.cancel==='function'&&typeof this.Utterance==='function');}
  refreshVoices(){
    const voices=this.synth?.getVoices?.()||[],chinese=voices.filter(v=>/^zh(?:-|_)/i.test(v.lang||''));
    this.voice=chinese.find(v=>v.localService)||chinese[0]||null;
    const status=!this.supported?'此浏览器不支持语音播报':!this.enabled?'语音已关闭':this.voice?`语音已开启：${this.voice.name}${this.voice.localService?'（本地）':'（可能需要联网）'}`:'语音已开启，使用系统中文语音；若无声音请检查系统语音包';
    this.onStatus(status);return this.voice;
  }
  setEnabled(value){this.enabled=!!value&&this.supported;this.stop();this.refreshVoices();return this.enabled;}
  say(text){
    if(!this.enabled||!this.supported||typeof text!=='string'||!text.trim())return false;
    // Only the newest short event is useful during exercise; never build a speech queue.
    try{this.synth.cancel();const utterance=new this.Utterance(text.slice(0,160));utterance.lang=this.voice?.lang||'zh-CN';
      if(this.voice)utterance.voice=this.voice;utterance.rate=1.1;utterance.onerror=()=>this.onStatus('语音未播放，请点击测试语音或检查系统中文语音包');
      this.synth.speak(utterance);return true;}catch{this.onStatus('语音播放失败，运动计数仍继续');return false;}
  }
  test(){return this.say('语音播报已准备好，完成动作后会播报次数。');}
  stop(){try{this.synth?.cancel?.();}catch{}this.countdownState=null;}
  reset(){this.stop();this.counter=null;this.training=null;this.reminders.clear();}
  updateTraining(value){
    if(!value?.monitor_id)return;
    const prior=this.training,next={id:value.monitor_id,finished:value.finished===true,step:Math.floor((value.effective_hold_seconds||0)/10),remaining:Math.ceil(value.remaining_seconds??Infinity)};
    this.training=next;
    if(!prior||prior.id!==next.id)return;
    if(next.finished&&!prior.finished){this.say(value.training_mode==='hold'?'目标保持时间已完成，请休息。':`时间到，本组完成${value.count}次，请休息。`);return;}
    if(value.paused||value.countdown_active)return;
    if(value.training_mode==='hold'&&next.step>prior.step)this.say(`已有效保持${next.step*10}秒`);
    else if(value.training_mode==='count'&&[10,5].includes(next.remaining)&&next.remaining!==prior.remaining)this.say(`还剩${next.remaining}秒`);
  }
  updateCounter(value){
    if(!value||typeof value.monitor_id!=='string'||!value.monitor_id||!Number.isSafeInteger(value.count)||value.count<0){this.counter=null;this.stop();return false;}
    const next={id:value.monitor_id,count:value.count,paused:value.paused===true},previous=this.counter;this.counter=next;
    if(!previous||previous.id!==next.id||next.count<previous.count){this.stop();return false;}
    if(next.paused){if(!previous.paused){this.stop();this.say('已暂停');}return false;}
    // Resuming starts a fresh baseline. Only an increase in a confirmed, continuous monitor counts.
    if(previous.paused)return false;
    if(next.count>previous.count){this.say(`${next.count} 次`);return true;}return false;
  }
  countdown(value,sessionId){
    if(typeof sessionId!=='string'||!sessionId){this.countdownState=null;return false;}
    const active=value?.active===true,display=Number(value?.display),previous=this.countdownState;
    if(active){if(!Number.isInteger(display)||display<1||display>3)return false;
      this.countdownState={id:sessionId,active:true,display};
      if(!previous||previous.id!==sessionId||!previous.active||previous.display!==display)return this.say(String(display));
    }else{this.countdownState={id:sessionId,active:false,display:null};
      if(previous?.id===sessionId&&previous.active&&this.counter?.id===sessionId&&!this.counter.paused)return this.say('开始');}
    return false;
  }
  remind(key,text,intervalMs=15000){
    if(!this.enabled||typeof key!=='string')return false;const now=this.now(),last=this.reminders.get(key);
    if(last!==undefined&&now-last<Math.max(1000,intervalMs))return false;
    if(this.reminders.size>=64&&!this.reminders.has(key))this.reminders.delete(this.reminders.keys().next().value);
    this.reminders.set(key,now);return this.say(text);
  }
  dispose(){this.reset();this.synth?.removeEventListener?.('voiceschanged',this.voiceListener);}
}

export class MotionMetrics {
  constructor({exerciseId='squats',visibility=0.5,maxFrames=300,maxWindowSeconds=60,now=defaultNow}={}){
    if(!Number.isFinite(visibility)||visibility<0||visibility>1)throw new Error('Invalid visibility threshold');
    if(!Number.isInteger(maxFrames)||maxFrames<2||maxFrames>3000||!Number.isFinite(maxWindowSeconds)||maxWindowSeconds<=0||maxWindowSeconds>600)throw new Error('Invalid telemetry bounds');
    this.visibility=visibility;this.maxFrames=maxFrames;this.maxWindowMs=maxWindowSeconds*1000;this.now=now;this.setExercise(exerciseId);
  }
  setExercise(exerciseId){if(!EXERCISES.has(exerciseId))throw new Error('Unknown exercise');this.exerciseId=exerciseId;this.reset();}
  reset(){this.frames=[];this.completions=[];this.counter=null;this.lastTimestamp=null;this.lastCompletion=null;this.lastBoardCompletion=null;}
  clearContinuity(){this.lastCompletion=null;this.lastBoardCompletion=null;}
  observe({points,width,height,timestampMs=this.now(),accepted=false,sideProfile=null}={}){
    if(!Number.isFinite(timestampMs)||timestampMs<0||this.lastTimestamp!==null&&timestampMs<=this.lastTimestamp)return false;
    if(!Number.isFinite(width)||!Number.isFinite(height)||width<=0||height<=0)return false;
    this.lastTimestamp=timestampMs;const values={},valid=Array.isArray(points)&&accepted===true;
    let selected=typeof sideProfile==='string'?sideProfile:sideProfile?.selectedSide;
    const ids=Array.isArray(sideProfile?.metricLandmarkIds||sideProfile?.usedLandmarkIds)?new Set(sideProfile.metricLandmarkIds||sideProfile.usedLandmarkIds):null;
    const good=id=>valid&&(!ids||ids.has(id))&&visiblePoint(points[id],this.visibility);
    const permittedSides=['pushups','situp','high_plank','knee_plank'].includes(this.exerciseId)?(['left','right'].includes(selected)?[selected]:[]):['left','right'];
    let sideCount=0;for(const side of permittedSides){const s=SIDES[side];let measured=false;
      const angle=(name,a,b,c)=>{if([a,b,c].every(good)){const result=jointAngle2D(points[a],points[b],points[c],width,height);
        if(result!==null){(values[name]??={})[side]=result;measured=true;}}};
      angle('elbow_angle_deg',s.shoulder,s.elbow,s.wrist);
      if(good(s.shoulder)&&good(s.hip)){const result=torsoInclination2D(points[s.shoulder],points[s.hip],width,height);
        if(result!==null){(values.torso_inclination_deg??={})[side]=result;measured=true;}}
      if(this.exerciseId!=='pushups'){
        angle('knee_angle_deg',s.hip,s.knee,s.ankle);angle('hip_angle_deg',s.shoulder,s.hip,s.knee);
        angle('shoulder_angle_deg',s.hip,s.shoulder,s.elbow);
      }
      if(measured){sideCount++;selected=sideCount===1?side:'bilateral';}
    }
    const visible=valid?points.reduce((count,point)=>count+Number(visiblePoint(point,this.visibility)),0):0;
    this.frames.push({time:timestampMs,accepted:valid,side:sideCount?selected:'unknown',visible,values});this.prune(timestampMs);return true;
  }
  confirmedCounter(value,timestampMs=this.now()){
    if(!value||typeof value.monitor_id!=='string'||!value.monitor_id||!Number.isSafeInteger(value.count)||value.count<0||!Number.isFinite(timestampMs))return false;
    const previous=this.counter,next={id:value.monitor_id,count:value.count,paused:value.paused===true};
    if(previous&&(previous.id!==next.id||next.count<previous.count)){this.reset();}
    this.counter=next;
    if(!previous||previous.id!==next.id||next.count<previous.count||next.paused||previous.paused){this.clearContinuity();return false;}
    if(next.count===previous.count)return false;
    const delta=next.count-previous.count;
    // Skipped server counts do not provide individual repetition times.
    const boardTime=Number.isFinite(value.last_count_time)&&value.last_count_time>=0?value.last_count_time:null;
    let interval=null,source='unavailable';
    if(delta===1&&this.lastCompletion!==null){
      if(boardTime!==null&&this.lastBoardCompletion!==null){
        if(boardTime>this.lastBoardCompletion){interval=boardTime-this.lastBoardCompletion;source='board_capture_time';}
      }else if(value.last_count_time===undefined||value.last_count_time===null){
        const elapsed=(timestampMs-this.lastCompletion)/1000;if(elapsed>0){interval=elapsed;source='browser_confirmation_time_estimate';}
      }
    }
    this.completions.push({time:timestampMs,count:next.count,delta,interval,source});
    this.lastCompletion=timestampMs;this.lastBoardCompletion=boardTime;this.prune(timestampMs);return true;
  }
  prune(time){
    while(this.frames.length&&(this.frames.length>this.maxFrames||this.frames[0].time<time-this.maxWindowMs))this.frames.shift();
    const cutoff=Math.max(time-this.maxWindowMs,this.frames[0]?.time??0);
    while(this.completions.length&&(this.completions.length>32||this.completions[0].time<cutoff))this.completions.shift();
    for(const completion of this.completions)if(completion.interval!==null&&completion.time-completion.interval*1000<cutoff)completion.interval=null;
  }
  snapshot(){
    const current=this.now();if(Number.isFinite(current)&&current>=(this.lastTimestamp??0))this.prune(current);
    const frames=this.frames,metrics={},sides={left:0,right:0,bilateral:0,unknown:0};
    for(const frame of frames){sides[frame.side]++;for(const[name,values]of Object.entries(frame.values))for(const[side,value]of Object.entries(values)){
      const group=metrics[name]??={};const state=group[side]??={samples:0,min:value,max:value,total:0};
      state.samples++;state.min=Math.min(state.min,value);state.max=Math.max(state.max,value);state.total+=value;
    }}
    for(const values of Object.values(metrics))for(const state of Object.values(values)){state.mean=rounded(state.total/state.samples);state.min=rounded(state.min);state.max=rounded(state.max);delete state.total;}
    const accepted=frames.filter(frame=>frame.accepted).length;
    const limitations=['角度来自按图像宽高校正的二维投影，不代表关节真实三维角度。','参考数据只有动作阶段标签；相似度不能证明动作标准，也不能生成经过验证的标准度分数。','关键点被遮挡、置信度不足或不在画面内时，不计算对应指标。'];
    if(this.exerciseId==='situp')limitations.push('仰卧起坐只使用选定侧肩、髋、膝；脚踝可见时补充膝角，不评价遮挡侧。');
    if(this.exerciseId==='pushups')limitations.push('俯卧撑只统计选定侧肩、肘、腕、髋四点；不评价双侧对称性、膝部或腿部姿态。');
    if(['birddog','high_plank','knee_plank'].includes(this.exerciseId))limitations.push('保持时间由板端有效姿态帧累计；分类数据未标注姿态质量。');
    limitations.push('完成间隔仅表示相邻已确认计数的时间差，首个动作、跳过计数及暂停间隔不能作为单次动作耗时。');
    const intervalSources=new Set(this.completions.filter(c=>c.interval!==null).map(c=>c.source));
    const intervalSource=intervalSources.size>1?'mixed':intervalSources.values().next().value||'unavailable';
    if(intervalSources.has('browser_confirmation_time_estimate'))limitations.push('部分完成间隔使用浏览器收到计数的时间估算，受网络与处理延迟影响；不能作为精确动作耗时。');
    return {schema:METRICS_SCHEMA,exercise_id:this.exerciseId,window_seconds:frames.length>1?rounded((frames.at(-1).time-frames[0].time)/1000):0,
      sampled_frames:frames.length,accepted_frames:accepted,rejected_frames:frames.length-accepted,visible_side_frames:sides,metrics,
      completion_intervals_seconds:this.completions.filter(c=>c.interval!==null).map(c=>rounded(c.interval)),completion_interval_source:intervalSource,
      confirmed_completions_in_window:this.completions.reduce((sum,item)=>sum+item.delta,0),limitations};
  }
}
