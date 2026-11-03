/** Offline preparation only. Real-time distances are always computed in PL. */
import fs from 'node:fs';import path from 'node:path';import {fileURLToPath} from 'node:url';import {createHash} from 'node:crypto';
import {packHoldPose,HOLD_FEATURE_SPEC} from '../hold_profile.mjs';
import {quantize,validateModel,predictPose,MODEL_SCHEMA,EXERCISES} from '../dataset_engine.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..'),raw=fs.readFileSync(path.join(root,'data/hold_poses.csv'));
const lines=raw.toString('utf8').replace(/^\uFEFF/,'').trim().split(/\r?\n/),header=lines.shift().split(','),li=header.indexOf('label');
if(header.length!==108||li!==107)throw Error('Expected supplied 33XYZ+8angles+label CSV');
const source=lines.map((line,i)=>{const cells=line.split(',');if(cells.length!==108)throw Error('CSV width');const nums=cells.slice(0,107).map(Number);if(!nums.every(Number.isFinite))throw Error('CSV number');return {sampleId:`holdcsv-${i+1}`,label:cells[li],points:Array.from({length:33},(_,j)=>({x:nums[j*3],y:nums[j*3+1],z:nums[j*3+2]}))};});
const hash=createHash('sha256').update(raw).digest('hex'),models={},report={sourceHash:hash,totalSamples:source.length,sourceClassCounts:{},models:{}};
for(const r of source)report.sourceClassCounts[r.label]=(report.sourceClassCounts[r.label]||0)+1;
function random(seed){return ()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;};}
function shuffled(a,r){a=a.slice();for(let i=a.length-1;i>0;i--){const j=Math.floor(r()*(i+1));[a[i],a[j]]=[a[j],a[i]];}return a;}
const words=f=>quantize(f),distance=(a,b)=>{let n=0;for(let i=0;i<99;i++)n+=(a[i]-b[i])**2;return n;},rms=x=>Math.sqrt(x/(99*2048**2));
for(const [id,label]of [['birddog','birddog'],['high_plank','high plank'],['knee_plank','knee plank']]){
 const classes=[id+'_other',id+'_hold'],rng=random(20261012),groups=new Map(),seen=new Set();let discarded=0,invalid=0,duplicates=0;
 for(const row of source){let features;try{features=packHoldPose(row.points,id).features;}catch{discarded++;invalid++;continue;}
  const key=JSON.stringify(features);if(seen.has(key)){discarded++;duplicates++;continue;}seen.add(key);
  const item={sampleId:row.sampleId,label:classes[Number(row.label===label)],features};
  if(!groups.has(row.label))groups.set(row.label,[]);groups.get(row.label).push(item);
 }
 let train=[],holdout=[];
 // Source-class stratification keeps every negative exercise represented.
 for(const [sourceLabel,g]of groups){const a=shuffled(g,rng),n=Math.max(5,Math.floor(a.length*.2));if(a.length-n<10)throw Error('Too few samples '+sourceLabel);holdout.push(...a.slice(0,n));train.push(...a.slice(n));}
 const rows=classes.flatMap(c=>shuffled(train.filter(x=>x.label===c),rng).slice(0,512));
 const thresholds={};for(const c of classes){const g=rows.filter(r=>r.label===c),q=g.map(r=>words(r.features)),near=q.map((a,i)=>rms(Math.min(...q.filter((_,j)=>j!==i).map(b=>distance(a,b))))).sort((a,b)=>a-b);thresholds[c]=Math.max(.04,near[Math.floor((near.length-1)*.95)]*1.5);}
 const model={schema:MODEL_SCHEMA,featureSpec:HOLD_FEATURE_SPEC,exerciseId:id,classes,k:5,minVoteFraction:.6,classThresholds:thresholds,rows,
 datasetMeta:{sourceKind:'pose-csv',sourceHash:hash,coordinateProtocol:HOLD_FEATURE_SPEC,normalizedPoseSize:1,videoGroupsAvailable:false,totalSamples:source.length-invalid,duplicateRowsRemoved:duplicates}};
 // A validator-consistent initial matrix allows the shared quantized predictor;
 // replace it immediately with the actual held-out results below.
 const m=classes.map(c=>[0,0,holdout.filter(r=>r.label===c).length]),n=holdout.length;
 model.validation={method:'stratified-sample-holdout',independentVideoValidation:false,seed:20261012,splitFraction:.2,correct:0,total:n,rejected:n,accuracy:0,coverage:0,acceptedAccuracy:null,confusionMatrix:m,labels:classes,predictedLabels:[...classes,'未确定'],rejectedColumnIndex:2,trainSampleIds:rows.map(r=>r.sampleId),holdoutSampleIds:holdout.map(r=>r.sampleId)};
 const initial=validateModel(model),matrix=[[0,0,0],[0,0,0]];let correct=0,rejected=0;
 for(const r of holdout){const p=predictPose(initial,r.features),a=classes.indexOf(r.label),b=p.label===null?2:classes.indexOf(p.label);matrix[a][b]++;if(b===2)rejected++;else if(a===b)correct++;}
 model.validation={...model.validation,correct,rejected,accuracy:correct/n,coverage:(n-rejected)/n,acceptedAccuracy:n===rejected?null:correct/(n-rejected),confusionMatrix:matrix};
 models[id]=validateModel(model);report.models[id]={rows:rows.length,discarded,invalidCoreCoordinates:invalid,exactFeatureDuplicates:duplicates,total:n,correct,rejected,accuracy:correct/n,confusionMatrix:matrix,positiveSamples:groups.get(label)?.length??0};
}
fs.writeFileSync(path.join(root,'data/hold_models.json'),JSON.stringify(models));fs.writeFileSync(path.join(root,'data/hold_dataset_report.json'),JSON.stringify(report,null,2)+'\n');
console.log(JSON.stringify(report));
