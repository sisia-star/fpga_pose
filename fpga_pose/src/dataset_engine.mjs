import {extractHoldFeatures,holdGeometryReason,checkHoldFeatures,HOLD_FEATURE_SPEC,isHoldExercise} from './hold_profile.mjs';
/** Offline CSV pose reference engine. Only sample holdout; no invented video groups. */
import {PUSHUPS_FEATURE_SPEC,SITUP_FEATURE_SPEC,checkSideFeatures,sideDatasetFeatures,extractSideFeatures,isSideExercise} from './pose_profile.mjs';
export {PUSHUPS_FEATURE_SPEC,SITUP_FEATURE_SPEC,PUSHUPS_CORE_POINTS,SITUP_CORE_POINTS,PUSHUPS_CANONICAL_POINTS} from './pose_profile.mjs';
export const FEATURE_SPEC = 'image33-hipcenter-pose2d-v1';
export const featureSpecForExercise=exerciseId=>isHoldExercise(exerciseId)?HOLD_FEATURE_SPEC:exerciseId==='pushups'?PUSHUPS_FEATURE_SPEC:exerciseId==='situp'?SITUP_FEATURE_SPEC:FEATURE_SPEC;
export const DATASET_SCHEMA = 'pose-csv-dataset-v1';
export const MODEL_SCHEMA = 'pose-knn-dataset-model-v2';
export const EXERCISES = Object.freeze({
  jumping_jacks: Object.freeze({exercise_id:'jumping_jacks',name:'开合跳',start_label:'jumping_jacks_down',target_label:'jumping_jacks_up'}),
  pushups: Object.freeze({exercise_id:'pushups',name:'俯卧撑',start_label:'pushups_up',target_label:'pushups_down'}),
  situp: Object.freeze({exercise_id:'situp',name:'仰卧起坐',start_label:'situp_down',target_label:'situp_up'}),
  pullups: Object.freeze({exercise_id:'pullups',name:'引体向上',start_label:'pullups_down',target_label:'pullups_up'}),
  squats: Object.freeze({exercise_id:'squats',name:'深蹲',start_label:'squats_up',target_label:'squats_down'}),
  birddog:Object.freeze({"exercise_id": "birddog", "name": "鸟狗式", "start_label": "birddog_other", "target_label": "birddog_hold", "mode": "hold"}),
  high_plank:Object.freeze({"exercise_id": "high_plank", "name": "高位平板支撑", "start_label": "high_plank_other", "target_label": "high_plank_hold", "mode": "hold"}),
  knee_plank:Object.freeze({"exercise_id": "knee_plank", "name": "跪姿平板支撑", "start_label": "knee_plank_other", "target_label": "knee_plank_hold", "mode": "hold"})
});
const ALL_LABELS = Object.values(EXERCISES).filter(x=>x.mode!=='hold').flatMap(x=>[x.start_label,x.target_label]);
const LABEL_SET = new Set(ALL_LABELS), QUALITY_POINTS=[11,12,13,14,15,16,23,24,25,26,27,28];
const DIM=99, SCALE=2048, SEED=20261012, MAX_ROWS=10000, MAX_CSV=8*1024*1024;
const own=(o,k)=>Object.prototype.hasOwnProperty.call(o,k), finite=x=>typeof x==='number'&&Number.isFinite(x);
const object=x=>x!==null&&typeof x==='object'&&!Array.isArray(x);
const fail=message=>{throw new Error(message);};
const trustedModels=new WeakSet();
const quantizedModels=new WeakMap();
const ID = x=>typeof x==='string'&&x.length>0&&x.length<=120&&!/[\x00-\x1f\x7f]/.test(x);
function exactKeys(o, keys, where) {
  if(!object(o)||Object.keys(o).sort().join('|')!==keys.slice().sort().join('|'))fail(`${where}字段不正确`);
}
function freeze(x){if(object(x)||Array.isArray(x)){for(const v of Object.values(x))freeze(v);Object.freeze(x);}return x;}
function same(a,b){return JSON.stringify(a)===JSON.stringify(b);}

/** Exact signed16 Q4.11, nearest ties away from zero. Never clips. */
export function quantize(features) {
  if(!Array.isArray(features)||features.length!==DIM||!features.every(finite))fail('特征必须含 99 个有限数值');
  return features.map(value=>{
    if(value < -16 || value > 32767/SCALE)fail('特征超出 FPGA signed16-Q4.11 范围，不能裁剪');
    const magnitude=Math.floor(Math.abs(value)*SCALE+0.5), q=value<0?-magnitude:magnitude;
    if(q < -32768 || q > 32767)fail('特征量化超出 signed16 范围');
    return q===0?0:q;
  });
}
function poseSize(features) {
  const hip=[0,1,2].map(a=>(features[23*3+a]+features[24*3+a])/2);
  const sh=[0,1].map(a=>(features[11*3+a]+features[12*3+a])/2);
  let radius=0;
  for(let i=0;i<33;i++)radius=Math.max(radius,Math.hypot(features[3*i]-hip[0],features[3*i+1]-hip[1]));
  return {hip,size:Math.max(2.5*Math.hypot(sh[0]-hip[0],sh[1]-hip[1]),radius)};
}
function checkedFeatures(f, where='坐标') {
  quantize(f);
  const {hip,size}=poseSize(f);
  if(!finite(size)||Math.abs(size-1)>0.001||hip.some(x=>Math.abs(x)>0.001))
    fail(`${where}不符合髋中心/二维 pose_size=100 坐标协议；不可与旧世界坐标特征混用`);
  return f.slice();
}
/** IMAGE xyz restored in pixels, hip centered, pose size=max(2.5*torsoXY,maxRadiusXY). */
export function extractFeatures(imageLandmarks,width,height,options={}) {
  if(isHoldExercise(options.exerciseId))return extractHoldFeatures(imageLandmarks,width,height,options);
  if(isSideExercise(options.exerciseId)){
    const result=extractSideFeatures(imageLandmarks,width,height,options);
    if(result.ok){try{quantize(result.features);}catch(error){return {ok:false,reason:error.message};}}
    return result;
  }
  if(!Number.isInteger(width)||!Number.isInteger(height)||width<2||height<2)
    return {ok:false,reason:'图像宽高无效'};
  if(!Array.isArray(imageLandmarks)||imageLandmarks.length!==33||!imageLandmarks.every(p=>object(p)&&finite(p.x)&&finite(p.y)&&finite(p.z)))
    return {ok:false,reason:'未检测到完整的 33 个人体图像关键点'};
  for(const i of QUALITY_POINTS) {
    const p=imageLandmarks[i];
    if(p.x<0||p.x>1||p.y<0||p.y>1||!finite(p.visibility)||p.visibility<0.5||('presence' in p&&(!finite(p.presence)||p.presence<0.5)))
      return {ok:false,reason:'肩、肘、手腕、髋、膝或脚踝不清楚，请全身入镜并避免遮挡'};
  }
  const pixels=imageLandmarks.flatMap(p=>[p.x*width,p.y*height,p.z*width]);
  const {hip,size}=poseSize(pixels);
  if(!finite(size)||size<1e-4)return {ok:false,reason:'人体尺度过小或无效，请调整站位'};
  const features=pixels.map((v,i)=>(v-hip[i%3])/size);
  try{quantize(features);}catch(error){return {ok:false,reason:error.message};}
  return {ok:true,features,featureSpec:FEATURE_SPEC,profile:FEATURE_SPEC,
    selectedSide:null,usedLandmarkIds:Array.from({length:33},(_,i)=>i)};
}

/** SHA-256 fallback works on plain HTTP without crypto.subtle or external libraries. */
export async function sha256Bytes(input) {
  const bytes=input instanceof Uint8Array?input:input instanceof ArrayBuffer?new Uint8Array(input):null;
  if(!bytes||bytes.length>32*1024*1024)fail('摘要输入无效或超过 32 MB');
  const k=[0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
  const h=[0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
  const padded=new Uint8Array(Math.ceil((bytes.length+9)/64)*64);padded.set(bytes);padded[bytes.length]=0x80;
  const dv=new DataView(padded.buffer);dv.setUint32(padded.length-4,bytes.length*8);dv.setUint32(padded.length-8,Math.floor(bytes.length/0x20000000));
  const rotr=(x,n)=>(x>>>n)|(x<<(32-n)),w=new Uint32Array(64);
  for(let block=0;block<padded.length;block+=64){
    for(let i=0;i<16;i++)w[i]=dv.getUint32(block+4*i);
    for(let i=16;i<64;i++){const a=w[i-15],b=w[i-2];w[i]=(w[i-16]+(rotr(a,7)^rotr(a,18)^(a>>>3))+w[i-7]+(rotr(b,17)^rotr(b,19)^(b>>>10)))>>>0;}
    let [a,b,c,d,e,f,g,j]=h;
    for(let i=0;i<64;i++){const t1=(j+(rotr(e,6)^rotr(e,11)^rotr(e,25))+((e&f)^(~e&g))+k[i]+w[i])>>>0,t2=((rotr(a,2)^rotr(a,13)^rotr(a,22))+((a&b)^(a&c)^(b&c)))>>>0;j=g;g=f;f=e;e=(d+t1)>>>0;d=c;c=b;b=a;a=(t1+t2)>>>0;}
    const values=[a,b,c,d,e,f,g,j];for(let i=0;i<8;i++)h[i]=(h[i]+values[i])>>>0;
  }
  return h.map(x=>x.toString(16).padStart(8,'0')).join('');
}

const LANDMARK_NAMES=['nose','left_eye_inner','left_eye','left_eye_outer','right_eye_inner','right_eye','right_eye_outer','left_ear','right_ear','mouth_left','mouth_right','left_shoulder','right_shoulder','left_elbow','right_elbow','left_wrist','right_wrist','left_pinky_1','right_pinky_1','left_index_1','right_index_1','left_thumb_2','right_thumb_2','left_hip','right_hip','left_knee','right_knee','left_ankle','right_ankle','left_heel','right_heel','left_foot_index','right_foot_index'];
export const LANDMARK_HEADER=Object.freeze(['pose_id',...LANDMARK_NAMES.flatMap(name=>['x','y','z'].map(a=>`${a}_${name}`))]);
function parseCSV(text, expectedWidth, where) {
  if(typeof text!=='string'||text.length>MAX_CSV||new TextEncoder().encode(text).length>MAX_CSV)fail(`${where}超过 8 MB 或文本无效`);
  if(text.charCodeAt(0)===0xfeff)text=text.slice(1);
  const rows=[];let row=[],cell='',quoted=false,afterQuote=false;
  const pushCell=()=>{if(cell.length>200)fail(`${where}单个字段过长`);row.push(cell);cell='';afterQuote=false;if(row.length>expectedWidth)fail(`${where}列数过多`);};
  const pushRow=()=>{pushCell();if(row.length===1&&row[0]===''){row=[];return;}if(row.length!==expectedWidth)fail(`${where}第 ${rows.length+1} 行列数不正确`);rows.push(row);row=[];if(rows.length>MAX_ROWS+1)fail(`${where}样本数超过 ${MAX_ROWS}`);};
  for(let i=0;i<text.length;i++){
    const ch=text[i];
    if(quoted){if(ch==='"'){if(text[i+1]==='"'){cell+='"';i++;}else{quoted=false;afterQuote=true;}}else cell+=ch;}
    else if(ch==='"'){if(cell||afterQuote)fail(`${where}CSV 引号位置错误`);quoted=true;}
    else if(ch===',')pushCell();
    else if(ch==='\n'||ch==='\r'){if(ch==='\r'&&text[i+1]==='\n')i++;pushRow();}
    else {if(afterQuote)fail(`${where}CSV 引号后存在多余文本`);cell+=ch;}
    if(cell.length>200)fail(`${where}单个字段过长`);
  }
  if(quoted)fail(`${where}CSV 引号未闭合`);
  if(cell||row.length||afterQuote)pushRow();
  if(rows.length<2)fail(`${where}没有样本`);return rows;
}
function csvDataset(landmarksText,labelsText,sourceHash) {
  const lm=parseCSV(landmarksText,100,'landmarks.csv'),lab=parseCSV(labelsText,2,'labels.csv');
  const canonical=lm[0].map(x=>x.replace(/^([xyz])_(left|right)_(pinky|index|thumb)$/,(_,a,side,joint)=>`${a}_${side}_${joint}_${joint==='thumb'?2:1}`));
  if(!same(canonical,LANDMARK_HEADER))fail('landmarks.csv 必须是 pose_id + 标准顺序 33 点 xyz 的 100 列');
  if(!same(lab[0],['pose_id','pose']))fail('labels.csv 表头必须为 pose_id,pose');
  const labels=new Map();for(const [id,label] of lab.slice(1)){if(!ID(id)||labels.has(id))fail('labels.csv pose_id 无效或重复');if(!LABEL_SET.has(label))fail(`未知阶段标签：${label}`);labels.set(id,label);}
  const rows=[],ids=new Set(),coords=new Map();let duplicateRowsRemoved=0;
  for(const row of lm.slice(1)){
    const id=row[0];if(!ID(id)||ids.has(id))fail('landmarks.csv pose_id 无效或重复');ids.add(id);
    if(!labels.has(id))fail(`pose_id ${id} 缺少阶段标签`);
    const features=row.slice(1).map(value=>{if(!value.trim()||!/^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$/.test(value))fail(`pose_id ${id} 包含无效坐标`);return Number(value)/100;});
    checkedFeatures(features,`pose_id ${id}`);
    const label=labels.get(id),key=JSON.stringify(features.map(v=>v===0?0:v));
    if(coords.has(key)){if(coords.get(key)!==label)fail('相同坐标出现互相冲突的阶段标签');duplicateRowsRemoved++;continue;}
    coords.set(key,label);rows.push({sampleId:id,label,features});
  }
  if(labels.size!==ids.size||[...labels.keys()].some(id=>!ids.has(id)))fail('labels.csv 与 landmarks.csv 的 pose_id 关联不完整');
  return validateDataset({schema:DATASET_SCHEMA,featureSpec:FEATURE_SPEC,classes:ALL_LABELS.slice(),rows,datasetMeta:{sourceKind:'pose-csv',sourceHash,coordinateProtocol:FEATURE_SPEC,normalizedPoseSize:100,videoGroupsAvailable:false,totalSamples:lm.length-1,duplicateRowsRemoved}});
}
export async function importDatasetCSVs(landmarksText,labelsText) {
  const encoder=new TextEncoder(),a=encoder.encode(landmarksText),b=encoder.encode(labelsText);
  if(a.length>MAX_CSV||b.length>MAX_CSV)fail('CSV 文件超过 8 MB');
  const combined=new Uint8Array(8+a.length+b.length),view=new DataView(combined.buffer);
  view.setUint32(0,a.length);combined.set(a,4);view.setUint32(4+a.length,b.length);combined.set(b,8+a.length);
  return csvDataset(landmarksText,labelsText,await sha256Bytes(combined));
}
function metaCheck(meta, sampleTotal,hold=false) {
  exactKeys(meta,['sourceKind','sourceHash','coordinateProtocol','normalizedPoseSize','videoGroupsAvailable','totalSamples','duplicateRowsRemoved'],'数据来源');
  if(meta.sourceKind!=='pose-csv'||(typeof meta.sourceHash!=='string'||!/^[0-9a-f]{64}$/.test(meta.sourceHash))||meta.coordinateProtocol!==(hold?HOLD_FEATURE_SPEC:FEATURE_SPEC)||meta.normalizedPoseSize!==(hold?1:100)||meta.videoGroupsAvailable!==false||!Number.isInteger(meta.totalSamples)||!Number.isInteger(meta.duplicateRowsRemoved)||meta.duplicateRowsRemoved<0||meta.totalSamples>MAX_ROWS||meta.totalSamples-meta.duplicateRowsRemoved<sampleTotal)fail('数据来源元数据不正确；该数据不含独立视频分组');
  return {...meta};
}
export function validateDataset(dataset) {
  exactKeys(dataset,['schema','featureSpec','classes','rows','datasetMeta'],'数据集');
  if(dataset.schema!==DATASET_SCHEMA||dataset.featureSpec!==FEATURE_SPEC)fail('数据集版本不兼容，旧版世界坐标/视频模板不能用于此版');
  if(!same(dataset.classes,ALL_LABELS))fail('数据集 classes 必须按目录包含全部 10 个阶段标签');
  if(!Array.isArray(dataset.rows)||!dataset.rows.length||dataset.rows.length>MAX_ROWS)fail('数据集样本数量无效');
  const ids=new Set(),coords=new Map(),seenLabels=new Set();
  const rows=dataset.rows.map(row=>{
    exactKeys(row,['sampleId','label','features'],'数据样本');
    if(!ID(row.sampleId)||ids.has(row.sampleId)||!LABEL_SET.has(row.label))fail('数据样本 ID 重复/无效或标签未知');ids.add(row.sampleId);seenLabels.add(row.label);
    const features=checkedFeatures(row.features),key=JSON.stringify(features.map(v=>v===0?0:v));
    if(coords.has(key))fail(coords.get(key)===row.label?'数据集仍有重复坐标，请通过 CSV 导入清理':'数据集有冲突标签的相同坐标');coords.set(key,row.label);
    return {sampleId:row.sampleId,label:row.label,features};
  });
  if(ALL_LABELS.some(label=>!seenLabels.has(label)))fail('数据集必须包含五种动作的全部 10 个阶段类别');
  const datasetMeta=metaCheck(dataset.datasetMeta,rows.length);
  if(datasetMeta.totalSamples-datasetMeta.duplicateRowsRemoved!==rows.length)fail('数据集总样本数与去重记录不一致');
  return {schema:DATASET_SCHEMA,featureSpec:FEATURE_SPEC,classes:ALL_LABELS.slice(),rows,datasetMeta};
}

const CRC_TABLE=new Uint32Array(256);
for(let i=0;i<256;i++){let c=i;for(let j=0;j<8;j++)c=(c&1)?0xedb88320^(c>>>1):c>>>1;CRC_TABLE[i]=c>>>0;}
function crc32(bytes){let c=0xffffffff;for(const b of bytes)c=CRC_TABLE[(c^b)&255]^(c>>>8);return (c^0xffffffff)>>>0;}
async function boundedInflate(compressed, expected) {
  if(typeof DecompressionStream!=='function')fail('当前浏览器不支持 ZIP 解压，请使用新版 Chrome/Edge 或分别导入两个 CSV');
  let stream;try{stream=new Blob([compressed]).stream().pipeThrough(new DecompressionStream('deflate-raw'));}catch{fail('当前浏览器不支持 ZIP deflate，请使用新版 Chrome/Edge 或分别导入两个 CSV');}
  const reader=stream.getReader(),chunks=[];let total=0;
  try{for(;;){const {value,done}=await reader.read();if(done)break;total+=value.length;if(total>expected||total>MAX_CSV){await reader.cancel();fail('ZIP 解压长度超过声明或允许上限');}chunks.push(value);}}catch(error){fail(`ZIP deflate 数据损坏：${error.message}`);}
  if(total!==expected)fail('ZIP 解压长度与声明不一致');const output=new Uint8Array(total);let p=0;for(const c of chunks){output.set(c,p);p+=c.length;}return output;
}
/** Strict single-volume ZIP stored/deflate. CRC checked for every entry, never extracts paths. */
export async function importDatasetZip(input) {
  const bytes=input instanceof ArrayBuffer?new Uint8Array(input):input instanceof Uint8Array?input:null;
  if(!bytes||bytes.length<22||bytes.length>32*1024*1024)fail('ZIP 大小无效或超过 32 MB');
  const view=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength),u16=p=>view.getUint16(p,true),u32=p=>view.getUint32(p,true),sig=(p,s)=>p>=0&&p+4<=bytes.length&&u32(p)===s;
  let e=-1;for(let p=bytes.length-22;p>=Math.max(0,bytes.length-65557);p--)if(sig(p,0x06054b50)&&p+22+u16(p+20)===bytes.length){e=p;break;}
  if(e<0||u16(e+4)||u16(e+6)||u16(e+8)!==u16(e+10))fail('ZIP 目录损坏或不是单卷 ZIP');
  const count=u16(e+10),size=u32(e+12),offset=u32(e+16);
  if(count<2||count>64||count===65535||offset+size!==e||offset>bytes.length)fail('ZIP 目录数量/范围不正确，不支持 ZIP64');
  const decoder=new TextDecoder('utf-8',{fatal:true}),names=new Set(),ranges=[],entries=[];let p=offset,total=0;
  function sizeExtras(extraStart,length,unpacked,packed,local,disk) {
    let q=extraStart,end=q+length,found=false;
    while(q<end){
      if(q+4>end)fail('ZIP extra 字段损坏');const tag=u16(q),n=u16(q+2);q+=4;
      if(q+n>end)fail('ZIP extra 长度损坏');
      if(tag===1){
        if(found)fail('重复 ZIP64 extra');found=true;let r=q;
        const read64=()=>{if(r+8>q+n)fail('ZIP64 长度字段缺失');const low=u32(r),high=u32(r+4);r+=8;if(high)fail('ZIP64 声明超过允许大小');return low;};
        if(unpacked===0xffffffff)unpacked=read64();if(packed===0xffffffff)packed=read64();if(local===0xffffffff)local=read64();
        if(disk===0xffff){if(r+4>q+n)fail('ZIP64 分卷字段缺失');disk=u32(r);}
      }
      q+=n;
    }
    if([unpacked,packed,local].some(v=>v===0xffffffff)||disk===0xffff)fail('ZIP64 extra 缺失');
    return {unpacked,packed,local,disk};
  }
  for(let i=0;i<count;i++){
    if(!sig(p,0x02014b50)||p+46>e)fail('ZIP 中央目录损坏');
    const flags=u16(p+8),method=u16(p+10),crc=u32(p+16),n=u16(p+28),extra=u16(p+30),comment=u16(p+32),external=u32(p+38);
    let packed=u32(p+20),unpacked=u32(p+24),disk=u16(p+34),local=u32(p+42);
    if(p+46+n+extra+comment>e||n<1||n>240)fail('ZIP extra/名称范围不正确');
    ({packed,unpacked,disk,local}=sizeExtras(p+46+n,extra,unpacked,packed,local,disk));
    if(disk||flags&~0x80e||![0,8].includes(method)||unpacked>MAX_CSV||packed>32*1024*1024)fail('ZIP 包含不支持的加密/压缩/分卷条目或超大文件');
    let name;try{name=decoder.decode(bytes.subarray(p+46,p+46+n));}catch{fail('ZIP 文件名不是 UTF-8');}
    const parts=name.split('/'),directory=name.endsWith('/');
    if(name.includes('\\')||name.startsWith('/')||name.includes(':')||/[\x00-\x1f\x7f]/.test(name)||parts.some((part,j)=>part==='.'||part==='..'||(!part&&!(directory&&j===parts.length-1)))||name.normalize('NFC')!==name||((external>>>16)&0xf000)===0xa000)fail('ZIP 包含危险路径或符号链接');
    const key=name.toLowerCase();if(names.has(key))fail('ZIP 包含重复文件名');names.add(key);
    if(directory&&unpacked)fail('ZIP 目录条目有数据');
    total+=unpacked;if(total>16*1024*1024)fail('ZIP 总解压大小超过 16 MB');
    if(!sig(local,0x04034b50)||local+30>offset||u16(local+6)!==flags||u16(local+8)!==method)fail('ZIP 本地头与中央目录不一致');
    const ln=u16(local+26),le=u16(local+28),start=local+30+ln+le,end=start+packed;
    if(local+30+ln>offset||start>offset||end>offset||ln!==n||!bytes.subarray(local+30,local+30+ln).every((v,j)=>v===bytes[p+46+j]))fail('ZIP 本地文件名或数据范围不一致');
    const localSizes=sizeExtras(local+30+ln,le,u32(local+22),u32(local+18),0,0);
    if(!(flags&8)&&(u32(local+14)!==crc||localSizes.packed!==packed||localSizes.unpacked!==unpacked))fail('ZIP 本地 CRC/长度与目录不一致');
    ranges.push([local,end]);entries.push({name,directory,method,crc,unpacked,start,end});p+=46+n+extra+comment;
  }
  if(p!==e)fail('ZIP 目录长度不一致');ranges.sort((a,b)=>a[0]-b[0]);for(let i=1;i<ranges.length;i++)if(ranges[i][0]<ranges[i-1][1])fail('ZIP 条目数据范围重叠');
  let landmarks=null,labels=null;
  for(const entry of entries){
    const compressed=bytes.subarray(entry.start,entry.end),content=entry.method===0?compressed.slice():await boundedInflate(compressed,entry.unpacked);
    if(content.length!==entry.unpacked||crc32(content)!==entry.crc)fail(`ZIP 文件 CRC 校验失败：${entry.name}`);
    if(entry.directory)continue;
    const base=entry.name.split('/').at(-1).toLowerCase();
    if(base==='landmarks.csv'||base==='labels.csv'){
      let value;try{value=decoder.decode(content);}catch{fail(`${base} 不是 UTF-8 文本`);}
      if(base==='landmarks.csv'){if(landmarks!==null)fail('ZIP 包含多个 landmarks.csv');landmarks=value;}
      else {if(labels!==null)fail('ZIP 包含多个 labels.csv');labels=value;}
    }
  }
  if(landmarks===null||labels===null)fail('ZIP 必须包含 landmarks.csv 和 labels.csv');
  return csvDataset(landmarks,labels,await sha256Bytes(bytes));
}

function sumSquared(a,b){let s=0;for(let i=0;i<DIM;i++){const d=a[i]-b[i];s+=d*d;}return s;}
function rms(s){return Math.sqrt(s/DIM)/SCALE;}
function quantile(values,q){const v=values.slice().sort((a,b)=>a-b),p=(v.length-1)*q,l=Math.floor(p),h=Math.ceil(p);return v[l]+(v[h]-v[l])*(p-l);}
function calibration(rows,minRadius=0.04) {
  const q=rows.map(r=>quantize(r.features)),nearest=q.map(()=>Infinity);
  for(let i=0;i<q.length;i++)for(let j=i+1;j<q.length;j++){const d=sumSquared(q[i],q[j]);nearest[i]=Math.min(nearest[i],d);nearest[j]=Math.min(nearest[j],d);}
  if(nearest.some(x=>!finite(x)))fail('训练参考样本不足，无法校准距离阈值');
  return Math.max(minRadius,quantile(nearest.map(rms),0.95)*1.5);
}
function rng(seed){let s=seed>>>0;return ()=>{s=(s+0x6d2b79f5)>>>0;let t=s;t=Math.imul(t^(t>>>15),t|1);t^=t+Math.imul(t^(t>>>7),t|61);return ((t^(t>>>14))>>>0)/4294967296;};}
function shuffle(values,random){const a=values.slice();for(let i=a.length-1;i>0;i--){const j=Math.floor(random()*(i+1));[a[i],a[j]]=[a[j],a[i]];}return a;}
function predictInternal(model, features) {
  let query;try{query=quantize(features);}catch(error){return {label:null,voteFraction:0,distance:Infinity,reason:error.message};}
  if(isSideExercise(model.exerciseId)){try{checkSideFeatures(features);}catch(error){return {label:null,voteFraction:0,distance:Infinity,reason:error.message};}}
  // Cache immutable model words, and maintain only the five smallest distances.
  // Tie order matches the previous full stable sort exactly (distance, row index).
  let words=quantizedModels.get(model);
  if(!words){words=model.rows.map(row=>quantize(row.features));quantizedModels.set(model,words);}
  const neighbors=[];
  model.rows.forEach((row,index)=>{
    const candidate={label:row.label,distanceSquared:sumSquared(words[index],query),index};
    let place=neighbors.length;
    while(place>0&&candidate.distanceSquared<neighbors[place-1].distanceSquared)place--;
    if(place<5){neighbors.splice(place,0,candidate);if(neighbors.length>5)neighbors.pop();}
  });
  const counts=model.classes.map(label=>neighbors.filter(n=>n.label===label).length),best=counts[0]>=counts[1]?0:1,label=model.classes[best],voteFraction=counts[best]/5;
  const distance=rms(Math.min(...neighbors.filter(n=>n.label===label).map(n=>n.distanceSquared)));
  if(voteFraction<0.6)return {label:null,voteFraction,distance,reason:'邻居投票不足 60%，暂时无法确定阶段'};
  if(distance>model.classThresholds[label])return {label:null,voteFraction,distance,reason:'与参考姿态距离过大，当前阶段未确定'};
  if(isHoldExercise(model.exerciseId)&&label===model.classes[1]){const reason=holdGeometryReason(features,model.exerciseId);if(reason)return {label:null,voteFraction,distance,reason};}
  return {label,voteFraction,distance,reason:'通过邻居一致度与参考距离检查'};
}
function validationFor(model,holdout) {
  const labels=model.classes.slice(),confusionMatrix=labels.map(()=>[0,0,0]);let correct=0,rejected=0;
  for(const row of holdout){const p=predictInternal(model,row.features),i=labels.indexOf(row.label);if(p.label===null){confusionMatrix[i][2]++;rejected++;}else{const j=labels.indexOf(p.label);confusionMatrix[i][j]++;if(i===j)correct++;}}
  const total=holdout.length;
  return {method:'stratified-sample-holdout',independentVideoValidation:false,seed:SEED,splitFraction:0.2,correct,total,rejected,accuracy:correct/total,coverage:(total-rejected)/total,acceptedAccuracy:total===rejected?null:correct/(total-rejected),confusionMatrix,labels,predictedLabels:[...labels,'未确定'],rejectedColumnIndex:2,trainSampleIds:model.rows.map(r=>r.sampleId),holdoutSampleIds:holdout.map(r=>r.sampleId)};
}
export function trainExerciseModel(dataset,exerciseId) {
  const clean=validateDataset(dataset),exercise=own(EXERCISES,exerciseId)?EXERCISES[exerciseId]:null;if(!exercise)fail('未知运动类型');
  const classes=[exercise.start_label,exercise.target_label],random=rng(SEED),rows=[],holdout=[];
  for(const label of classes){
    const group=shuffle(clean.rows.filter(r=>r.label===label).sort((a,b)=>(a.sampleId<b.sampleId?-1:a.sampleId>b.sampleId?1:0)),random),n=Math.max(5,Math.floor(group.length*0.2));
    if(group.length-n<10)fail(`阶段 ${label} 至少需要 10 个训练样本和 5 个验证样本`);
    const transform=row=>({...row,features:isSideExercise(exerciseId)?sideDatasetFeatures(row.features,exerciseId):row.features.slice()});
    holdout.push(...group.slice(0,n).map(transform));rows.push(...group.slice(n,n+512).map(transform));
  }
  const classThresholds=Object.fromEntries(classes.map(label=>[label,calibration(rows.filter(r=>r.label===label),isSideExercise(exerciseId)?.10:.04)]));
  const model={schema:MODEL_SCHEMA,featureSpec:featureSpecForExercise(exerciseId),exerciseId,classes,k:5,minVoteFraction:0.6,classThresholds,rows:rows.map(r=>({sampleId:r.sampleId,label:r.label,features:r.features.slice()})),datasetMeta:{...clean.datasetMeta}};
  model.validation=validationFor(model,holdout);return validateModel(model);
}
function ids(values,where){if(!Array.isArray(values)||values.some(x=>!ID(x))||new Set(values).size!==values.length)fail(`${where}无效或重复`);return values.slice();}
function validatedMetrics(v,classes,rows) {
  exactKeys(v,['method','independentVideoValidation','seed','splitFraction','correct','total','rejected','accuracy','coverage','acceptedAccuracy','confusionMatrix','labels','predictedLabels','rejectedColumnIndex','trainSampleIds','holdoutSampleIds'],'验证结果');
  if(v.method!=='stratified-sample-holdout'||v.independentVideoValidation!==false||v.seed!==SEED||v.splitFraction!==0.2)fail('验证方式不正确；没有独立视频验证');
  const train=ids(v.trainSampleIds,'训练样本 ID'),holdout=ids(v.holdoutSampleIds,'验证样本 ID');
  if(!same(train,rows.map(r=>r.sampleId))||holdout.some(id=>train.includes(id)))fail('训练/验证样本 ID 重叠或训练记录不一致');
  if(![v.correct,v.total,v.rejected].every(x=>Number.isInteger(x)&&x>=0)||v.total<10||v.total>2500||v.total!==holdout.length||v.correct+v.rejected>v.total)fail('验证计数无效');
  if(!same(v.labels,classes)||!same(v.predictedLabels,[...classes,'未确定'])||v.rejectedColumnIndex!==2)fail('验证类别列不正确');
  const m=v.confusionMatrix;if(!Array.isArray(m)||m.length!==2||m.some(row=>!Array.isArray(row)||row.length!==3||row.some(x=>!Number.isInteger(x)||x<0)||row.reduce((a,b)=>a+b,0)<5))fail('混淆矩阵格式无效或每阶段验证样本不足 5 个');
  if(m.flat().reduce((a,b)=>a+b,0)!==v.total||m[0][0]+m[1][1]!==v.correct||m[0][2]+m[1][2]!==v.rejected)fail('验证计数与混淆矩阵不一致');
  const accuracy=v.correct/v.total,coverage=(v.total-v.rejected)/v.total,accepted=v.total===v.rejected?null:v.correct/(v.total-v.rejected);
  if(!finite(v.accuracy)||!finite(v.coverage)||Math.abs(v.accuracy-accuracy)>1e-12||Math.abs(v.coverage-coverage)>1e-12||(accepted===null?v.acceptedAccuracy!==null:!finite(v.acceptedAccuracy)||Math.abs(v.acceptedAccuracy-accepted)>1e-12))fail('验证比例与计数不一致');
  return {...v,confusionMatrix:m.map(r=>r.slice()),labels:classes.slice(),predictedLabels:[...classes,'未确定'],trainSampleIds:train,holdoutSampleIds:holdout};
}
export function validateModel(model) {
  exactKeys(model,['schema','featureSpec','exerciseId','classes','k','minVoteFraction','classThresholds','rows','datasetMeta','validation'],'模型');
  const exercise=own(EXERCISES,model.exerciseId)?EXERCISES[model.exerciseId]:null;
  if(model.schema!==MODEL_SCHEMA||model.featureSpec!==featureSpecForExercise(model.exerciseId)||!exercise||!same(model.classes,[exercise.start_label,exercise.target_label])||model.k!==5||model.minVoteFraction!==0.6)fail('模型版本/动作类别不兼容；俯卧撑和仰卧起坐需重新训练同侧三点模型，不能混用旧模型');
  if(!Array.isArray(model.rows)||model.rows.length<20||model.rows.length>1024)fail('模型需要 20～1024 个训练样本');
  exactKeys(model.classThresholds,model.classes,'类别距离阈值');
  if(model.classes.some(label=>!finite(model.classThresholds[label])||model.classThresholds[label]<0.04||model.classThresholds[label]>256))fail('类别距离阈值无效');
  const seen=new Set(),coords=new Set(),rows=model.rows.map(row=>{
    exactKeys(row,['sampleId','label','features'],'模型样本');
    if(!ID(row.sampleId)||seen.has(row.sampleId)||!model.classes.includes(row.label))fail('模型样本 ID/类别无效');seen.add(row.sampleId);
    quantize(row.features);
    const features=isHoldExercise(model.exerciseId)?checkHoldFeatures(row.features):isSideExercise(model.exerciseId)?checkSideFeatures(row.features):checkedFeatures(row.features),key=JSON.stringify(features.map(v=>v===0?0:v));if(coords.has(key))fail('模型包含重复坐标');coords.add(key);return {sampleId:row.sampleId,label:row.label,features};
  });
  if(model.classes.some(label=>{const n=rows.filter(r=>r.label===label).length;return n<10||n>512;}))fail('每阶段需要 10～512 个训练样本');
  const validation=validatedMetrics(model.validation,model.classes,rows),datasetMeta=metaCheck(model.datasetMeta,rows.length+validation.total,isHoldExercise(model.exerciseId));
  const clean={schema:MODEL_SCHEMA,featureSpec:model.featureSpec,exerciseId:model.exerciseId,classes:model.classes.slice(),k:5,minVoteFraction:0.6,classThresholds:{...model.classThresholds},rows,datasetMeta,validation};
  freeze(clean);trustedModels.add(clean);return clean;
}
export function predictPose(model,features){return predictInternal(trustedModels.has(model)?model:validateModel(model),features);}
