/** Source CSV and live MediaPipe use normalized image XY (not world coordinates).
 * No source pixel aspect ratio is supplied, so angles stay in normalized XY.
 * The descriptor is not a calibrated anatomical joint angle. */
export const HOLD_FEATURE_SPEC='image-hold-limb-geometry-v1';
export const isHoldExercise=id=>['birddog','high_plank','knee_plank'].includes(id);
const SIDES={left:[11,13,15,23,25,27],right:[12,14,16,24,26,28]};
const finite=x=>typeof x==='number'&&Number.isFinite(x);
export function checkHoldFeatures(f){
 if(!Array.isArray(f)||f.length!==99||f.some(x=>!finite(x))||f.slice(18).some(x=>x!==0)||f.slice(0,18).some(x=>x< -8||x>8))throw Error('保持姿态特征协议不匹配');return f.slice();
}
const length=(a,b)=>Math.hypot(a.x-b.x,a.y-b.y);
function angle(a,b,c){const u=length(a,b),v=length(c,b);if(u<.005||v<.005)throw Error('核心骨段过短');return Math.acos(Math.max(-1,Math.min(1,((a.x-b.x)*(c.x-b.x)+(a.y-b.y)*(c.y-b.y))/(u*v))));}
function sideGeometry(p){
 const [s,e,w,h,k,a]=p,torso=length(s,h);if(torso<.025)throw Error('躯干过小，请调整机位');
 const vertical=(a,b)=>{const n=length(a,b);if(n<.005)throw Error('核心骨段过短');return 4*Math.abs(a.y-b.y)/n;};
 return [4*angle(s,e,w)/Math.PI,4*angle(s,h,k)/Math.PI,4*angle(h,k,a)/Math.PI,
  4*angle(h,s,e)/Math.PI,vertical(s,h),vertical(e,w),vertical(h,k),vertical(k,a),Math.max(-4,Math.min(4,(w.y-h.y)/torso))];
}
function available(points,ids,live){return ids.every(i=>{const p=points[i];return p&&finite(p.x)&&finite(p.y)&&p.x>=0&&p.x<=1&&p.y>=0&&p.y<=1&&(!live||(finite(p.visibility)&&p.visibility>=.5&&(!('presence'in p)||finite(p.presence)&&p.presence>=.5)));});}
export function packHoldPose(points,exerciseId,{live=false,preferredSide='auto',previousSide=null}={}){
 if(!isHoldExercise(exerciseId)||!Array.isArray(points)||points.length!==33)throw Error('保持姿态需要33点输出');
 const candidates={};
 for(const [side,ids]of Object.entries(SIDES))if(available(points,ids,live)){
  try{const p=ids.map(i=>points[i]);candidates[side]={f:sideGeometry(p),span:length(p[0],p[3])+length(p[3],p[4]),ids};}catch{}
 }
 let parts,selectedSide=null,usedLandmarkIds;
 if(exerciseId==='birddog'){
  if(!candidates.left||!candidates.right)throw Error('鸟狗式需要两侧肩、肘、腕、髋、膝、踝可见；请用侧前方机位避免手脚重叠');
  parts=[candidates.left.f,candidates.right.f].sort((a,b)=>{for(let i=0;i<9;i++)if(a[i]!==b[i])return a[i]-b[i];return 0;});
  usedLandmarkIds=[...SIDES.left,...SIDES.right];
 }else{
  const locked=preferredSide==='auto'?previousSide:preferredSide;
  selectedSide=locked||(candidates.left&&candidates.right?(candidates.left.span>=candidates.right.span?'left':'right'):candidates.left?'left':candidates.right?'right':null);
  if(!selectedSide||!candidates[selectedSide])throw Error('请让可见侧的肩、肘、手腕、髋、膝和踝清晰入镜；可暂停后切换可见侧');
  parts=[candidates[selectedSide].f,candidates[selectedSide].f];usedLandmarkIds=candidates[selectedSide].ids;
 }
 return {features:checkHoldFeatures([...parts.flat(),...Array(81).fill(0)]),selectedSide,usedLandmarkIds,metricLandmarkIds:usedLandmarkIds.slice(),featureSpec:HOLD_FEATURE_SPEC,profile:HOLD_FEATURE_SPEC};
}
export function extractHoldFeatures(points,width,height,options={}){
 try{return {ok:true,...packHoldPose(points,options.exerciseId,{live:true,...options})};}catch(e){return {ok:false,reason:e.message};}
}
/** Conservative supplementary gates; recognition still requires a real PL vote. */
export function holdGeometryReason(features,exerciseId){
 const a=features.slice(0,9),b=features.slice(9,18),deg=x=>x*45;
 if(exerciseId==='high_plank'){
  if(a[4]>.95*4)return '请将身体调整为支撑姿态';
  if(deg(a[0])<120)return '请伸直支撑手臂';
  if(deg(a[1])<120||deg(a[2])<125)return '请让肩、髋与腿部保持伸展';
 }else if(exerciseId==='knee_plank'){
  if(a[4]>.95*4)return '请保持跪姿支撑';
  if(deg(a[1])<115)return '请抬起髋部，避免坐回脚跟';
  if(deg(a[2])>170)return '请将膝盖落地，保持跪姿';
 }else if(exerciseId==='birddog'){
  if(Math.min(a[4],b[4])>3.8)return '请保持四点支撑的躯干方向';
  const extended=(x,y)=>deg(x[3])>95&&deg(x[0])>120&&deg(y[1])>120&&deg(y[2])>120;
  if(!extended(a,b)&&!extended(b,a))return '请伸展一侧手臂与对侧腿，并保持清晰可见';
 }
 return null;
}
