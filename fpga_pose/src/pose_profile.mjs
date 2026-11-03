/** Fixed same-side three-point descriptors. Dataset and live frames share these functions.
 * Features 0..2 carry bounded geometry; remaining 96 dimensions are exact zero.
 * The original Q4.11/99-D PL interface is retained, with new software protocol IDs.
 */
export const PUSHUPS_FEATURE_SPEC='image-pushups-side3-angles-v2';
export const SITUP_FEATURE_SPEC='image-situp-side3-angles-v2';
export const PUSHUPS_CORE_POINTS=Object.freeze({left:Object.freeze([11,13,15]),right:Object.freeze([12,14,16])});
export const SITUP_CORE_POINTS=Object.freeze({left:Object.freeze([11,23,25]),right:Object.freeze([12,24,26])});
export const PUSHUPS_CANONICAL_POINTS=PUSHUPS_CORE_POINTS.left;
const configs={pushups:{spec:PUSHUPS_FEATURE_SPEC,ids:PUSHUPS_CORE_POINTS,names:['肩','肘','手腕'],min:[4,4]},
 situp:{spec:SITUP_FEATURE_SPEC,ids:SITUP_CORE_POINTS,names:['肩','髋','膝'],min:[8,6]}};
const finite=x=>typeof x==='number'&&Number.isFinite(x),validSide=x=>x==='left'||x==='right';
export const isSideExercise=id=>Object.hasOwn(configs,id);
export function checkSideFeatures(features){
 if(!Array.isArray(features)||features.length!==99||!features.every(finite))throw Error('侧机位特征必须包含99个有限数值');
 if(features.slice(3).some(x=>x!==0))throw Error('侧机位模型仅允许固定前三维，其他维度必须为零');
 if(features[0]<0||features[0]>8||features[1]<0||features[1]>8||features[2]<=0||features[2]>=2)throw Error('侧机位角度/比例特征范围不正确');
 return features.slice();
}
export const checkPushupFeatures=checkSideFeatures;
function geometry(points){
 if(!Array.isArray(points)||points.length!==3||points.some(p=>!p||!finite(p.x)||!finite(p.y)))throw Error('需要同一侧三个核心点');
 const [a,b,c]=points,ab=Math.hypot(a.x-b.x,a.y-b.y),bc=Math.hypot(c.x-b.x,c.y-b.y);
 if(ab<1e-6||bc<1e-6)throw Error('核心骨段过短，无法可靠计算角度');
 const cosine=((a.x-b.x)*(c.x-b.x)+(a.y-b.y)*(c.y-b.y))/(ab*bc);
 return {ab,bc,angle:Math.acos(Math.max(-1,Math.min(1,cosine))),span:Math.hypot(a.x-c.x,a.y-c.y)};
}
export function packSideTriple(points,exerciseId){
 if(!isSideExercise(exerciseId))throw Error('未知侧机位运动');
 const {ab,bc,angle,span}=geometry(points),features=Array(99).fill(0);
 features[0]=8*angle/Math.PI;
 features[1]=exerciseId==='pushups'?8*Math.min(1,span/(ab+bc)):
   8*Math.atan2(Math.abs(points[0].x-points[1].x),Math.abs(points[0].y-points[1].y))/(Math.PI/2);
 features[2]=2*ab/(ab+bc);
 return checkSideFeatures(features);
}
export const packPushupSide=points=>packSideTriple(points.slice(0,3),'pushups');
export function sideDatasetFeatures(features,exerciseId){
 if(!Array.isArray(features)||features.length!==99||!features.every(finite))throw Error('训练坐标必须是33点图像坐标');
 const conf=configs[exerciseId];if(!conf)throw Error('未知运动');
 const candidates=['left','right'].flatMap(side=>{
  const p=conf.ids[side].map(i=>({x:features[3*i],y:features[3*i+1]}));
  try{const g=geometry(p);return [{points:p,quality:Math.min(g.ab,g.bc)}];}catch{return [];}
 });
 if(!candidates.length)throw Error('参考样本的两侧核心骨段均无效');
 const selected=candidates.reduce((a,b)=>b.quality>a.quality?b:a);
 return packSideTriple(selected.points,exerciseId);
}
export const pushupDatasetFeatures=features=>sideDatasetFeatures(features,'pushups');
export function extractSideFeatures(landmarks,width,height,options={}){
 const conf=configs[options.exerciseId];if(!conf)return {ok:false,reason:'未知侧机位运动'};
 if(!Number.isInteger(width)||!Number.isInteger(height)||width<2||height<2)return {ok:false,reason:'图像宽高无效'};
 if(!Array.isArray(landmarks)||landmarks.length!==33)return {ok:false,reason:'未检测到人体，请让同一侧核心关节入镜'};
 const preferred=options.preferredSide??'auto',previous=options.previousSide??null;
 if(preferred!=='auto'&&!validSide(preferred)||previous!==null&&!validSide(previous))return {ok:false,reason:'侧面选择无效'};
 const candidates={},failures={};
 for(const side of ['left','right']){
  const ids=conf.ids[side],points=ids.map(i=>landmarks[i]),reasons=[];
  points.forEach((p,j)=>{
   const name=(side==='left'?'左':'右')+conf.names[j];
   if(!p||!finite(p.x)||!finite(p.y)){reasons.push(name+'缺失');return;}
   if(p.x<0||p.x>1||p.y<0||p.y>1)reasons.push(name+'在画面外');
   else if(!finite(p.visibility)||p.visibility<.5)reasons.push(name+'可见度'+(finite(p.visibility)?p.visibility.toFixed(2):'无效'));
   else if('presence'in p&&(!finite(p.presence)||p.presence<.5))reasons.push(name+'存在度'+(finite(p.presence)?p.presence.toFixed(2):'无效'));
  });
  if(reasons.length){failures[side]=reasons.join('、');continue;}
  const pixels=points.map(p=>({x:p.x*width,y:p.y*height}));
  try{
   const g=geometry(pixels);
   if(g.ab<conf.min[0]||g.bc<conf.min[1]){failures[side]='核心骨段太小，请靠近或提高画面分辨率';continue;}
   candidates[side]={features:packSideTriple(pixels,options.exerciseId),selectedSide:side,usedLandmarkIds:ids.slice(),
    metricLandmarkIds:ids.concat((options.exerciseId==='pushups'?[side==='left'?23:24]:[side==='left'?27:28]).filter(i=>{const p=landmarks[i];return p&&finite(p.x)&&finite(p.y)&&p.x>=0&&p.x<=1&&p.y>=0&&p.y<=1&&p.visibility>=.5&&(p.presence===undefined||p.presence>=.5);})),
    quality:Math.min(...points.map(p=>Math.min(p.visibility,p.presence??1))),spanPixels:Math.min(g.ab,g.bc),
    jointAngleDeg:g.angle*180/Math.PI,featureSpec:conf.spec,profile:conf.spec};
  }catch(e){failures[side]=e.message;}
 }
 const locked=preferred==='auto'?previous:preferred;
 let side=locked;
 if(!side){
  if(candidates.left&&candidates.right){const a=candidates.left,b=candidates.right;side=Math.abs(a.quality-b.quality)>.05?(a.quality>b.quality?'left':'right'):(b.spanPixels>a.spanPixels?'right':'left');}
  else side=candidates.left?'left':candidates.right?'right':null;
 }
 if(!side||!candidates[side])return {ok:false,selectedSide:locked,usedLandmarkIds:locked?conf.ids[locked].slice():[],
  reason:locked?`已选${locked==='left'?'左':'右'}侧：${failures[locked]||'核心点不清楚'}。可暂停后重新选择可见侧`:
   `左侧：${failures.left||'未确认'}；右侧：${failures.right||'未确认'}`};
 return {ok:true,...candidates[side]};
}
export const extractPushupFeatures=(landmarks,width,height,options={})=>extractSideFeatures(landmarks,width,height,{...options,exerciseId:'pushups'});
