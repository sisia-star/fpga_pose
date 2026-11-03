const $ = id => document.getElementById(id), urls = [], sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function bytes(id) {const text=atob($(id).textContent.trim()), output=new Uint8Array(text.length);for(let i=0;i<text.length;i++)output[i]=text.charCodeAt(i);return output;}
function assetURL(id,type='text/javascript') {const url=URL.createObjectURL(new Blob([bytes(id)],{type}));urls.push(url);return url;}
const profileModule=assetURL('profile'),holdModule=assetURL('holdprofile');
const engineSource=new TextDecoder().decode(bytes('engine')).replaceAll('./pose_profile.mjs',profileModule).replaceAll('./hold_profile.mjs',holdModule);
const engineModule=URL.createObjectURL(new Blob([engineSource],{type:'text/javascript'}));urls.push(engineModule);
const resources={engine:engineModule,helper:assetURL('helper'),coach:assetURL('coach'),worker:assetURL('worker'),
  runtime:assetURL('runtime'),loader:assetURL('loader'),wasm:assetURL('wasm','application/wasm')};
const [engine,helper,coach]=await Promise.all([import(resources.engine),import(resources.helper),import(resources.coach)]);
const holdExercise=id=>['birddog','high_plank','knee_plank'].includes(id);
const sideExercise=id=>['pushups','situp','high_plank','knee_plank'].includes(id);
const coreNames=id=>id==='pushups'?'肩、肘、手腕':id==='situp'?'肩、髋、膝':'肩、肘、腕、髋、膝、踝';
const stageNames={squats_up:'站立到位',squats_down:'下蹲到位',pushups_up:'伸臂支撑到位',pushups_down:'屈臂下降到位',
  situp_down:'躺下到位',situp_up:'上身抬起到位',pullups_down:'伸臂下垂到位',pullups_up:'屈臂拉起到位',
  jumping_jacks_down:'合拢到位',jumping_jacks_up:'张开到位',birddog_other:'非鸟狗保持姿态',birddog_hold:'鸟狗式保持',high_plank_other:'非高位平板姿态',high_plank_hold:'高位平板保持',knee_plank_other:'非跪姿平板姿态',knee_plank_hold:'跪姿平板保持'};
let dataset=null,model=null,deployed=null,monitor=null,countdown=null,boundary=null,
  busy=false,live=false,closed=false,liveToken=0,liveWorker=null,controlGeneration=0,controlPending=false,statusPollBusy=false;
let aiState=null,aiPending=false,lastAIResult=null,lastAutoAICount=0,lastAIPoll=0,aiPollBusy=false,lastAutoAttempt=0,completedAIMonitor=null;
const canvas=$('liveCanvas'),context=canvas.getContext('2d');
const bones=[[11,12],[11,13],[13,15],[12,14],[14,16],[11,23],[12,24],[23,24],[23,25],[25,27],[24,26],[26,28]];
const message=(id,text)=>{$(id).textContent=text;};
const speech=new coach.SpeechCoach({onStatus:text=>message('voiceStatus',text)});
const metrics=new coach.MotionMetrics({exerciseId:'squats'});
function profile() {return helper.exerciseProfile(engine.EXERCISES,$('exercise').value,$('dwell').value);}
function sync() {
  const counting=!!monitor?.monitor_id&&!monitor.paused&&!monitor.finished, locked=busy||controlPending||counting;
  for(const id of ['loadPreset','datasetZip','landmarksCsv','labelsCsv','loadCsv','exercise','dwell','duration','import'])$(id).disabled=locked;
  $('train').disabled=locked;$('deploy').disabled=locked||!model;
  $('export').disabled=busy||!model||!dataset||holdExercise($('exercise').value);$('check').disabled=controlPending;
  $('preview').disabled=live||busy||controlPending;$('stop').disabled=!live||controlPending;
  $('start').disabled=busy||controlPending||!deployed||counting;
  $('cancelCountdown').disabled=!countdown?.active||controlPending;
  $('pause').disabled=!monitor?.monitor_id||monitor.paused||controlPending;
  $('resume').disabled=!monitor?.monitor_id||!monitor.paused||controlPending||!live;
  $('reset').disabled=!monitor?.monitor_id||controlPending;
  if(monitor?.finished){$('pause').disabled=true;$('resume').disabled=true;}
  const hold=holdExercise($('exercise').value);message('durationLabel',hold?'目标有效保持时长':'本组计数时长');message('countUnit',hold?'秒':'次');message('modeHint',hold?'动作稳定到位后累计有效保持时间；遮挡、偏离或画面间隔过大时暂停累计。完成后语音提示。':'三秒准备结束后开始本组倒计时，时间到停止计数；暂停不消耗训练时间。');
  $('pushupSide').disabled=locked||!sideExercise($('exercise').value);
  $('aiEnabled').disabled=aiPending;
  $('aiEvaluate').disabled=aiPending||!$('aiEnabled').checked||!monitor?.monitor_id||!(monitor.training_mode==='hold'?monitor.effective_hold_seconds>=3:monitor.count>0)||countdown?.active===true||aiState?.busy===true;
}
function workerClient(withPose=false) {
  const worker=new Worker(resources.worker), pending=new Map();let serial=0;
  worker.onmessage=event=>{const data=event.data,item=pending.get(data.id);if(!item)return;pending.delete(data.id);clearTimeout(item.timer);
    data.ok?item.resolve(data.value):item.reject(new Error(data.error));};
  worker.onerror=event=>{for(const item of pending.values()){clearTimeout(item.timer);item.reject(new Error(event.message||'数据处理线程出错'));}pending.clear();};
  const client={request(type,payload={},transfer=[],timeout=90000){return new Promise((resolve,reject)=>{
    const id=++serial,timer=setTimeout(()=>{pending.delete(id);reject(new Error('处理超时，请刷新页面后重试'));},timeout);
    pending.set(id,{resolve,reject,timer});worker.postMessage({id,type,payload},transfer);});},
    close(){worker.terminate();for(const item of pending.values()){clearTimeout(item.timer);item.reject(new Error('处理已停止'));}pending.clear();}};
  client.ready=client.request('init',{engine:resources.engine,...(withPose?{poseEndpoint:location.origin+'/api/pose/submit'}: {})});
  return client;
}
async function api(path,payload,timeout=10000) {
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeout);
  try {const response=await fetch(path,{method:payload===undefined?'GET':'POST',cache:'no-store',signal:controller.signal,
    ...(payload===undefined?{}:{headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})});
    let body;try{body=await response.json();}catch{throw new Error('开发板未返回有效数据，请运行新版服务');}
    if(!response.ok){const error=new Error(body.error||body.message||`接口错误 ${response.status}`);error.status=response.status;throw error;}return body;
  } catch(error) {if(error.name==='AbortError')throw new Error('开发板连接超时');throw error;}finally{clearTimeout(timer);}
}
function evidence(status) {
  const hw=status.hardware||status,image=status.image||{},conv=status.convolution||{};
  const ready=hw.ready===true&&hw.backend==='fpga'&&image.ready===true&&image.backend==='fpga'&&image.self_test_passed===true&&conv.ready===true&&conv.self_test_passed===true;
  message('hardwareStatus',ready?'图像、卷积、距离 FPGA 模块已就绪。':`硬件未就绪：${conv.error||image.error||hw.error||'请初始化卷积位流并完成自检'}`);
  message('imageEvidence',`图像 PL 完成：${image.completed_count??'—'} 次；最近周期：${image.last_cycles??'—'}`);
  message('poseEvidence',`卷积 PL：${conv.last?.hardware_count??0} 块；周期：${conv.last?.cycles??'—'}；距离 PL：${hw.inference_count??'—'} 次；周期：${hw.last_cycles??'—'}`);return ready;
}
function showCounter(value) {
  if(!value)return;monitor=value;message('count',value.training_mode==='hold'?Number(value.effective_hold_seconds||0).toFixed(1):String(value.count??0));const p=profile();
  speech.updateCounter(value);speech.updateTraining(value);metrics.confirmedCounter(value);
  const states={waiting_start:`等待${stageNames[p.start_label]}`,ready:`已${stageNames[p.start_label]}，等待${stageNames[p.target_label]}`,
    target_reached:`已${stageNames[p.target_label]}，等待返回${stageNames[p.start_label]}`,countdown:'准备中',waiting_hold:'等待保持姿态到位',confirming_hold:'正在确认稳定姿态',holding:'正在累计有效保持时间',completed:'本组训练完成'};
  message('phase',states[value.phase]||'等待起始姿态');
  message('monitorStatus',value.reason||'尚未开始');
  message('timeStatus',value.duration_seconds?`目标 ${value.duration_seconds} 秒；${value.training_mode==='hold'?'有效保持':'本组经过'} ${Number(value.training_mode==='hold'?value.effective_hold_seconds:value.elapsed_seconds).toFixed(1)} 秒；剩余 ${Number(value.remaining_seconds||0).toFixed(1)} 秒${value.finished?'；本组已结束':''}`:'尚未开始训练');sync();
}
function setCountdown(value) {
  countdown=helper.countdownState(value,monitor?.monitor_id);$('countdownBox').hidden=!countdown.active;
  speech.countdown(countdown,monitor?.monitor_id);
  if(countdown.active){message('countdown',String(countdown.display));message('result','准备中，不识别阶段');message('quality','三秒准备期间仅显示画面');}sync();
}
function acceptControl(value) {
  const counter=value.exercise||value.counter;
  if(!counter||value.backend!=='fpga')throw new Error('开发板未确认监测控制');
  if(counter.monitor_id)boundary=helper.validateBoundary(value);else boundary=null;
  showCounter(counter);setCountdown(value);
}
async function check() {
  const generation=controlGeneration;
  try{const status=await api('/api/status');if(generation!==controlGeneration||controlPending)return;evidence(status);showAI(status.ai);
    if(monitor?.monitor_id){if(status.exercise?.monitor_id!==monitor.monitor_id||status.hardware?.model_id!==deployed)
      throw new Error('开发板模型或计数会话已改变，请重新部署');
      boundary=helper.validateBoundary(status);showCounter(status.exercise);setCountdown(status);}}
  catch(error){message('hardwareStatus',`连接检查失败：${error.message}`);}sync();
}
function showAI(value) {
  if(!value)return;aiState=value;
  if(!aiPending)$('aiEnabled').checked=value.enabled===true;
  const names={disabled:'AI 已关闭',waiting_relay:'等待联网电脑的 AI 助手连接',ready:'AI 已就绪',queued:'已提交，等待电脑助手',running:'DeepSeek 正在评估',complete:'AI 点评已返回',error:'AI 暂不可用'};
  message('aiStatus',`${names[value.state]||'AI 状态待确认'}${value.error?`：${value.error}`:''}${value.cooldown_seconds>0?`；下次请求约 ${Math.ceil(value.cooldown_seconds)} 秒后`:''}。实时计数独立运行。`);
  const result=value.result;
  if(result&&result.monitor_id===monitor?.monitor_id&&result.exercise_id===$('exercise').value&&result.job_id!==lastAIResult){
    lastAIResult=result.job_id;const f=result.feedback;
    if(f&&typeof f.summary==='string'){
      const lines=[`${holdExercise($('exercise').value)?'保持训练点评':`截至第 ${result.completed_count} 次`}：${f.summary}`];
      for(const[key,title]of[['observations','观察'],['suggestions','建议'],['limitations','观测限制']])
        if(Array.isArray(f[key])&&f[key].length)lines.push(`${title}：\n${f[key].map(text=>`• ${text}`).join('\n')}`);
      message('aiResult',lines.join('\n\n'));
      if(!monitor?.paused&&!countdown?.active)speech.remind(`ai-${result.job_id}`,f.summary);
    }
  }sync();
}
async function configureAI() {
  if(aiPending)return;const enabled=$('aiEnabled').checked;aiPending=true;sync();
  try{showAI(await api('/api/ai/config',{enabled}));}
  catch(error){$('aiEnabled').checked=aiState?.enabled===true;message('aiStatus',`AI 设置失败：${error.message}`);}
  finally{aiPending=false;sync();}
}
async function evaluateAI(automatic=false) {
  if(aiPending||!$('aiEnabled').checked||!monitor?.monitor_id||!(monitor.training_mode==='hold'?monitor.effective_hold_seconds>=3:monitor.count>0)||countdown?.active||aiState?.busy)return;
  if(automatic&&monitor.finished&&completedAIMonitor===monitor.monitor_id)return;
  if(automatic&&(performance.now()-lastAutoAttempt<10000||aiState?.cooldown_seconds>0))return;
  const telemetry=metrics.snapshot();
  if(telemetry.accepted_frames<3||!Object.keys(telemetry.metrics).length){
    if(!automatic)message('aiStatus','清晰的关节记录不足，请继续完成动作后再评估。');return;
  }
  aiPending=true;lastAutoAttempt=performance.now();sync();const id=monitor.monitor_id,generation=controlGeneration,count=monitor.count;
  try{const value=await api('/api/ai/evaluate',{monitor_id:id,telemetry});
    if(generation!==controlGeneration||id!==monitor?.monitor_id)return;
    if(monitor.finished)completedAIMonitor=monitor.monitor_id;
    lastAutoAICount=monitor.training_mode==='hold'?monitor.effective_hold_seconds:count;showAI(value);
  }catch(error){if(generation===controlGeneration)message('aiStatus',`AI 评估未提交：${error.message}。运动计数继续。`);}
  finally{aiPending=false;sync();}
}
async function pollAI() {
  if(aiPollBusy||aiPending||performance.now()-lastAIPoll<1500)return;
  lastAIPoll=performance.now();aiPollBusy=true;const generation=controlGeneration;
  try{const value=await api('/api/ai/status');if(!closed&&generation===controlGeneration)showAI(value);}
  catch(error){message('aiStatus',`AI 状态暂时无法获取：${error.message}。运动计数继续。`);}
  finally{aiPollBusy=false;}
}
function clearModel() {model=null;deployed=null;monitor=null;countdown=null;boundary=null;controlGeneration++;
  speech.reset();metrics.setExercise($('exercise').value);lastAutoAICount=0;lastAIResult=null;message('aiResult','');
  $('validationRows').replaceChildren();message('trainingStatus',holdExercise($('exercise').value)?'该保持项目已有数据集参考模型，点击生成参考模型即可载入。':dataset?'数据已载入，选择运动后生成模型。':'可直接选择运动并生成随包参考模型；也可先载入计数数据集。');
  message('report','');message('deployStatus','模型尚未部署。');$('countdownBox').hidden=true;message('count','0');sync();}
function renderDataset() {
  $('datasetRows').replaceChildren();if(!dataset)return;
  for(const row of helper.datasetSummary(dataset,engine.EXERCISES)){
    const tr=document.createElement('tr');for(const value of[row.name,`${stageNames[row.start_label]}：${row.startCount}`,`${stageNames[row.target_label]}：${row.targetCount}`]){
      const td=document.createElement('td');td.textContent=value;tr.append(td);}$('datasetRows').append(tr);}
  message('datasetStatus',`已载入 ${dataset.datasetMeta.totalSamples} 条原始样本，移除 ${dataset.datasetMeta.duplicateRowsRemoved} 条重复，保留 ${dataset.rows.length} 条；${dataset.classes.length} 个阶段。`);
}
async function importData(kind,payload) {
  if(busy||controlPending)return;busy=true;sync();let worker;
  try{message('datasetStatus','正在检查数据文件、坐标和标签……');worker=workerClient();await worker.ready;
    let result;if(kind==='preset'){
      const response=await fetch('/api/dataset',{cache:'no-store',signal:AbortSignal.timeout(20000)});
      if(!response.ok)throw new Error(`无法载入内置数据集 ${response.status}，也可选择本地 archive.zip`);
      const buffer=await response.arrayBuffer();result=await worker.request('zip',{buffer},[buffer]);
    }else if(kind==='zip'){
      if(payload.size>32*1024*1024)throw new Error('ZIP 文件超过 32 MB');const buffer=await payload.arrayBuffer();result=await worker.request('zip',{buffer},[buffer]);
    }else{
      const [landmarks,labels]=payload;if(!landmarks||!labels)throw new Error('请同时选择 landmarks.csv 和 labels.csv');
      if(landmarks.size>32*1024*1024||labels.size>4*1024*1024)throw new Error('CSV 文件超过允许大小');
      result=await worker.request('csv',{landmarks:await landmarks.text(),labels:await labels.text()});
    }
    dataset=engine.validateDataset(result);clearModel();renderDataset();
  }catch(error){message('datasetStatus',`载入失败：${error.message}`);}finally{worker?.close();busy=false;$('datasetZip').value='';sync();}
}
function showValidation() {
  $('validationRows').replaceChildren();const v=model.validation;
  message('trainingStatus',`${engine.EXERCISES[model.exerciseId].name}参考模型：${model.rows.length} 条训练样本；留出样本 ${v.correct}/${v.total} 正确（${(v.accuracy*100).toFixed(1)}%），未确定 ${v.rejected} 条。`);
  const accepted=v.acceptedAccuracy===null?'无已接受样本':`${(v.acceptedAccuracy*100).toFixed(1)}%`;
  message('report',`验证按姿态样本分层划分，未按原视频隔离。覆盖率 ${(v.coverage*100).toFixed(1)}%；已接受样本准确率 ${accepted}。这些结果不能代替独立摄像头人工计数测试。`);
  for(const [index,label] of model.classes.entries()){const tr=document.createElement('tr');
    const testCount=v.confusionMatrix[index].reduce((sum,value)=>sum+value,0),trainCount=model.rows.filter(row=>row.label===label).length;
    for(const value of[stageNames[label],trainCount,testCount]){const td=document.createElement('td');td.textContent=String(value);tr.append(td);}$('validationRows').append(tr);}
}
async function train() {
  if(busy)return;busy=true;deployed=null;sync();let worker;
  try{profile();message('trainingStatus','正在生成参考模型并计算留出样本验证……');
    if(!dataset||holdExercise($('exercise').value))model=engine.validateModel(await api('/api/hold-model?exercise='+encodeURIComponent($('exercise').value)));
    else{worker=workerClient();await worker.ready;model=engine.validateModel(await worker.request('train',{dataset,exerciseId:$('exercise').value}));}showValidation();
    message('deployStatus','参考模型已生成，可以部署到 FPGA；请用独立实时动作核对计数。');
  }catch(error){model=null;message('trainingStatus',`生成失败：${error.message}`);}finally{worker?.close();busy=false;sync();}
}
async function deploy() {
  if(!model||busy)return;busy=true;sync();
  try{const p=profile();if(p.exercise_id!==model.exerciseId)throw new Error('运动已改变，请重新生成参考模型');
    if(!evidence(await api('/api/status')))throw new Error('三个 FPGA 模块尚未通过初始化自检');
    message('deployStatus','正在部署参考模型到 FPGA……');const value=await api('/api/model',{...model,exercise_profile:p},30000);
    if(value.backend!=='fpga'||value.ready!==true||typeof value.model_id!=='string'||value.rows!==model.rows.length)
      throw new Error('开发板未确认 FPGA 模型部署');
    deployed=value.model_id;monitor=null;countdown=null;boundary=null;controlGeneration++;
    speech.reset();metrics.setExercise(model.exerciseId);lastAutoAICount=0;lastAIResult=null;message('aiResult','');
    message('deployStatus',`已部署 ${value.rows} 条 ${p.name}参考样本，可以开始三秒准备。`);
  }catch(error){deployed=null;message('deployStatus',`部署失败：${error.message}`);}finally{busy=false;sync();}
}
function drawPose(bitmap,points=[],usedLandmarkIds=null) {
  canvas.width=bitmap.width;canvas.height=bitmap.height;context.drawImage(bitmap,0,0);canvas.hidden=false;
  const used=usedLandmarkIds?new Set(usedLandmarkIds):null;
  context.lineWidth=2;context.strokeStyle='#38f1b0';for(const[a,b]of bones){if(used&&(!used.has(a)||!used.has(b)))continue;if(!points[a]||!points[b]||points[a].visibility<0.5||points[b].visibility<0.5)continue;
    context.beginPath();context.moveTo(points[a].x*canvas.width,points[a].y*canvas.height);context.lineTo(points[b].x*canvas.width,points[b].y*canvas.height);context.stroke();}
  context.fillStyle='#ffef78';for(const[i,p]of points.entries()){if(used&&!used.has(i)||p.visibility<0.5)continue;context.beginPath();context.arc(p.x*canvas.width,p.y*canvas.height,2,0,Math.PI*2);context.fill();}
}
async function openPreview() {
  if(live)return;if(controlPending)throw new Error('监测控制尚未完成');
  const status=await api('/api/status');if(!evidence(status))throw new Error('请先初始化图像 FPGA 模块');
  if(status.running!==true||!Number.isInteger(status.frame_id)||status.frame_id<1||!Number.isFinite(status.frame_age_seconds)||status.frame_age_seconds>3)
    throw new Error('尚无新摄像头图像，请检查摄像头状态');
  message('streamStatus','正在初始化内置姿态模型……');liveWorker=workerClient(true);
  try{await liveWorker.ready;}catch(error){liveWorker.close();liveWorker=null;throw error;}
  live=true;sync();void liveLoop(++liveToken);
}
async function start() {
  if(!deployed||busy||controlPending)return;busy=true;sync();
  speech.reset();metrics.setExercise($('exercise').value);lastAutoAICount=0;lastAIResult=null;message('aiResult','');
  try{await openPreview();controlPending=true;const generation=++controlGeneration;sync();
    const duration=Number($('duration').value);if(!Number.isFinite(duration)||duration<5||duration>3600)throw Error('训练时长应为5～3600秒');
    const value=await api('/api/monitor',{action:'start',model_id:deployed,...profile(),duration_seconds:duration});
    if(generation!==controlGeneration)return;acceptControl(value);message('streamStatus','三秒准备开始；画面可以预览，暂不识别阶段。');
  }catch(error){message('streamStatus',`启动失败：${error.message}`);}finally{controlPending=false;busy=false;sync();}
}
async function pollMonitor(token) {
  if(statusPollBusy||controlPending||!monitor?.monitor_id)return;
  statusPollBusy=true;const generation=controlGeneration,id=monitor.monitor_id;
  try{const status=await api('/api/status');if(!live||token!==liveToken||controlPending||generation!==controlGeneration)return;
    if(status.exercise?.monitor_id!==id||status.hardware?.model_id!==deployed)throw new Error('开发板模型或计数会话已改变，请重新部署');
    if(!evidence(status))throw new Error('FPGA 管线出现故障，已停止计数');
    boundary=helper.validateBoundary(status);showCounter(status.exercise);setCountdown(status);showAI(status.ai);
  }finally{statusPollBusy=false;}
}
async function liveLoop(token) {
  let lastSeq=null,lastPoll=0,lastFrame=0,errors=0,qualityLostSince=null;
  while(live&&token===liveToken&&!closed){let bitmap,drawBitmap,requestGeneration=controlGeneration;const loopStarted=performance.now();
    try{
      if($('aiEnabled').checked)void pollAI();
      const now=performance.now();if(monitor?.monitor_id&&now-lastPoll>(countdown?.active?150:500)){
        await pollMonitor(token);lastPoll=now;}
      const response=await fetch('/api/frame',{cache:'no-store',signal:AbortSignal.timeout(10000)});
      if(!response.ok)throw new Error(`获取摄像头画面失败 ${response.status}`);
      const sh=response.headers.get('X-Frame-Seq'),ch=response.headers.get('X-Capture-Time'),seq=Number(sh),capture=Number(ch),
        completed=Number(response.headers.get('X-Image-Completed')),cycles=Number(response.headers.get('X-Image-Cycles'));
      if(response.headers.get('X-Pixel-Pipeline')!==helper.PIXEL_PIPELINE||sh===null||ch===null||!Number.isSafeInteger(seq)||seq<0||
          !Number.isFinite(capture)||!Number.isInteger(completed)||completed<1||!Number.isInteger(cycles)||cycles<1)
        throw new Error('摄像头画面缺少 FPGA 图像处理完成证据');
      if(seq===lastSeq){await response.body?.cancel();await sleep(30);continue;}lastSeq=seq;
      const age=Number(response.headers.get('X-Frame-Age-Ms'));
      if(response.headers.has('X-Frame-Age-Ms')&&(!Number.isFinite(age)||age>1500)){
        await response.body?.cancel();message('streamStatus','画面延迟过大，正在等待新帧，不推进计数。');await sleep(50);continue;
      }
      const frameBlob=await response.blob();bitmap=await createImageBitmap(frameBlob);if(!live||token!==liveToken){bitmap.close();break;}
      const allowed=()=>helper.canPredict({monitor,countdown,controlPending,generation:requestGeneration,currentGeneration:controlGeneration,seq,capture,boundary});
      if(!allowed()){
        qualityLostSince=null;
        drawPose(bitmap);bitmap.close();bitmap=null;
        if(countdown?.active){message('quality','准备中，仅预览画面');message('result','三秒准备中');}
        else if(monitor?.finished){message('quality','本组已完成；可请求 AI 点评或开始新一组');if($('aiEnabled').checked&&lastAutoAICount!==-1){void evaluateAI(true);}}
        else if(monitor?.paused){message('quality','已暂停，仅预览画面');}
        else message('quality','仅预览，尚未开始计数');
      }else{
        drawBitmap=await createImageBitmap(bitmap);const result=await liveWorker.request('pose',{
          bitmap,frameBlob,frame_seq:seq,timestamp:performance.now(),exerciseId:model.exerciseId,preferredSide:$('pushupSide').value,
          generation:requestGeneration},[bitmap]);bitmap=null;
        if(!live||token!==liveToken){drawBitmap.close();break;}
        if(!allowed()){
          // An older extraction may finish after pause/resume or countdown start.
          // Keep only its image; do not publish its pose or classification.
          drawPose(drawBitmap);drawBitmap.close();drawBitmap=null;await sleep(20);continue;
        }
        drawPose(drawBitmap,result.points||[],sideExercise(model.exerciseId)?(result.metricLandmarkIds||result.usedLandmarkIds||[]):null);drawBitmap.close();drawBitmap=null;
        message('quality',result.ok?(sideExercise(model.exerciseId)?`${result.selectedSide==='left'?'左':'右'}侧${coreNames(model.exerciseId)}可用${Number.isFinite(result.jointAngleDeg)?`；关节角 ${result.jointAngleDeg.toFixed(0)}°`:''}`:'关键点完整，已生成姿态特征'):result.reason);
        if(!allowed()){await sleep(20);continue;}
        const sessionId=monitor.monitor_id,reply=await api('/api/predict',{model_id:deployed,monitor_id:sessionId,frame_seq:seq,features:result.ok?result.features:null});
        if(!live||token!==liveToken)break;if(controlPending||requestGeneration!==controlGeneration){await sleep(20);continue;}
        if(result.ok){helper.verifyPrediction(reply,deployed,model.classes);message('result',reply.label?stageNames[reply.label]:(reply.reason||'过渡姿态或未确定'));
          message('poseEvidence',`${result.convolution?`卷积 PL：${result.convolution.hardware_count} 块；周期：${result.convolution.cycles}；`:""}距离 PL：${reply.inference_count} 次；周期：${reply.last_cycles}`);}
        else message('result',result.reason||'核心点不清楚，暂不推进动作');
        const returned=reply.exercise||reply.counter;if(returned?.monitor_id!==sessionId)throw new Error('开发板计数会话已改变');
        metrics.observe({points:result.points,width:result.width,height:result.height,timestampMs:performance.now(),accepted:result.ok,sideProfile:result});
        showCounter(returned);
        if(result.ok)qualityLostSince=null;
        else{qualityLostSince??=performance.now();if(performance.now()-qualityLostSince>1500)speech.remind('tracking-lost',sideExercise(model.exerciseId)?`请让同一侧${coreNames(model.exerciseId)}保持清晰。`:'请调整站位，让身体关键点清晰入镜。');}
        message('latency',`姿态提取 ${Number(result.extractMs||0).toFixed(0)} ms；板端预测 ${Number(reply.server_predict_ms||0).toFixed(0)} ms；本次处理 ${(performance.now()-loopStarted).toFixed(0)} ms`);
        if((returned.training_mode==='hold'?returned.effective_hold_seconds-lastAutoAICount>=30:returned.count-lastAutoAICount>=5)||returned.finished)void evaluateAI(true);
      }
      const ended=performance.now();if(lastFrame)message('rate',`画面更新：${(1000/(ended-lastFrame)).toFixed(1)} 帧/秒`);lastFrame=ended;
      message('streamStatus',countdown?.active?'请准备，倒计时结束后等待新的起始姿态。':'图像经过 FPGA 预处理；计数只显示开发板结果。');errors=0;
      // One frame in flight, latest-only fetching, at most 20Hz; no fixed delay on slow inference.
      await sleep(Math.max(0,50-(performance.now()-loopStarted)));
    }catch(error){bitmap?.close();drawBitmap?.close();if(!live||token!==liveToken)break;
      if(error.status===409&&(controlPending||requestGeneration!==controlGeneration)){await sleep(30);continue;}
      message('streamStatus',`实时监测暂缓：${error.message}`);errors++;
      if(error.status===409||errors>=3){await stop();break;}await sleep(400);}
  }
}
async function monitorAction(action) {
  if(!monitor?.monitor_id||controlPending)return;speech.reset();metrics.clearContinuity();
  if(action==='reset'){metrics.reset();lastAutoAICount=0;lastAIResult=null;message('aiResult','');}
  controlPending=true;const generation=++controlGeneration,id=monitor.monitor_id;sync();
  try{const value=await api('/api/monitor',{action,monitor_id:id});if(generation!==controlGeneration)return;
    const returned=value.exercise||value.counter;if(action!=='cancel'&&returned?.monitor_id!==id)throw new Error('开发板计数会话已改变');
    acceptControl(value);
    if(action==='pause')speech.remind('pause','已暂停');
    if(action==='reset')speech.remind('reset','次数已清零，请准备起始姿态');
    if(action==='cancel'){message('monitorStatus','已取消准备，画面继续预览。');speech.remind('cancel','已取消准备');}
  }catch(error){message('monitorStatus',`控制未确认：${error.message}`);
    // Do not continue with a local guess after an unacknowledged board control.
    live=false;liveToken++;liveWorker?.close();liveWorker=null;monitor=null;countdown=null;boundary=null;deployed=null;
  }finally{controlPending=false;sync();}
}
async function stop() {
  speech.reset();metrics.clearContinuity();
  live=false;liveToken++;const generation=++controlGeneration,id=monitor?.monitor_id;
  liveWorker?.close();liveWorker=null;controlPending=true;sync();
  try{if(id&&!monitor?.finished){const value=await api('/api/monitor',{action:'cancel',monitor_id:id});if(generation===controlGeneration)acceptControl(value);}}
  catch(error){message('monitorStatus',`停止未获确认：${error.message}。请在开发板检查会话。`);}
  finally{controlPending=false;sync();message('streamStatus','预览已停止。');}
}
function saveJSON(value,name) {
  const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'})),link=document.createElement('a');
  link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
async function importTemplate(file) {
  if(!file||busy)return;busy=true;sync();let worker;
  try{if(file.size>32*1024*1024)throw new Error('模板文件超过 32 MB');const template=JSON.parse(await file.text());
    if(template.pixelPipeline!==helper.PIXEL_PIPELINE)throw new Error('模板的图像处理协议不匹配');
    worker=workerClient();await worker.ready;const result=await worker.request('template',{template});
    const p=helper.exerciseProfile(engine.EXERCISES,result.profile.exercise_id,result.profile.dwell_seconds);
    if(Object.keys(p).length!==Object.keys(result.profile).length)throw new Error('模板的计数配置字段不正确');
    for(const key of Object.keys(p))if(p[key]!==result.profile[key])throw new Error('模板的运动或计数配置不一致');
    dataset=result.dataset;model=result.model;deployed=null;monitor=null;countdown=null;boundary=null;controlGeneration++;
    $('exercise').value=model.exerciseId;$('dwell').value=String(p.dwell_seconds);renderDataset();showValidation();
    speech.reset();metrics.setExercise(model.exerciseId);lastAutoAICount=0;lastAIResult=null;message('aiResult','');
    message('deployStatus','数据集模板已通过重训一致性检查，请重新部署到 FPGA。');
  }catch(error){message('trainingStatus',`模板导入失败：${error.message}`);}finally{worker?.close();busy=false;$('import').value='';sync();}
}
$('loadPreset').onclick=()=>void importData('preset');$('datasetZip').onchange=event=>{const file=event.target.files[0];if(file)void importData('zip',file);};
$('loadCsv').onclick=()=>void importData('csv',[$('landmarksCsv').files[0],$('labelsCsv').files[0]]);
$('exercise').onchange=()=>clearModel();$('dwell').onchange=()=>{deployed=null;monitor=null;countdown=null;controlGeneration++;sync();message('deployStatus','到位确认时间已改变，请重新部署。');};
$('pushupSide').onchange=()=>{controlGeneration++;speech.reset();metrics.clearContinuity();message('quality','侧面选择已改变；继续后重新确认起始姿态。');};
$('voiceEnabled').onchange=()=>{speech.setEnabled($('voiceEnabled').checked);if($('voiceEnabled').checked){speech.updateCounter(monitor);speech.test();}};
$('voiceTest').onclick=()=>{$('voiceEnabled').checked=speech.setEnabled(true);speech.updateCounter(monitor);speech.test();};
$('aiEnabled').onchange=()=>void configureAI();$('aiEvaluate').onclick=()=>void evaluateAI(false);
$('train').onclick=()=>void train();$('deploy').onclick=()=>void deploy();$('check').onclick=()=>void check();
$('preview').onclick=async()=>{busy=true;sync();try{await openPreview();}catch(error){message('streamStatus',`预览失败：${error.message}`);}finally{busy=false;sync();}};
$('start').onclick=()=>void start();$('cancelCountdown').onclick=()=>void monitorAction('cancel');$('pause').onclick=()=>void monitorAction('pause');
$('resume').onclick=()=>void monitorAction('resume');$('reset').onclick=()=>void monitorAction('reset');$('stop').onclick=()=>void stop();
$('export').onclick=()=>saveJSON({schema:helper.TEMPLATE_SCHEMA,featureSpec:model.featureSpec,pixelPipeline:helper.PIXEL_PIPELINE,
  exerciseId:model.exerciseId,profile:profile(),dataset,model},`fpga_pose_${model.exerciseId}_dataset.json`);
$('import').onchange=event=>void importTemplate(event.target.files[0]);
window.addEventListener('pagehide',()=>{closed=true;live=false;liveToken++;controlGeneration++;liveWorker?.close();speech.dispose();
  if(monitor?.monitor_id)fetch('/api/monitor',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'cancel',monitor_id:monitor.monitor_id}),keepalive:true}).catch(()=>{});
  urls.forEach(url=>URL.revokeObjectURL(url));});
sync();void check();
