/* PC-only reference benchmark. NOT a measurement of live FPGA/MediaPipe FPS. */
import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
import {performance} from 'node:perf_hooks';
import {importDatasetZip,trainExerciseModel,predictPose,quantize,EXERCISES} from '../dataset_engine.mjs';

const dataset=await importDatasetZip(await fs.readFile(new URL('../data/archive.zip',import.meta.url)));
const models=Object.fromEntries(Object.keys(EXERCISES).map(id=>[id,trainExerciseModel(dataset,id)]));
const model=models.squats,queries=dataset.rows.filter(row=>model.classes.includes(row.label));
function previousReference(features){
  const query=quantize(features);
  const neighbors=model.rows.map((row,index)=>{
    const words=quantize(row.features);let squared=0;
    for(let i=0;i<99;i++){const delta=query[i]-words[i];squared+=delta*delta;}
    return {index,squared,label:row.label};
  }).sort((a,b)=>a.squared-b.squared||a.index-b.index).slice(0,5);
  const votes=model.classes.map(label=>neighbors.filter(n=>n.label===label).length),best=votes[0]>=votes[1]?0:1;
  const label=model.classes[best],voteFraction=votes[best]/5;
  const distance=Math.sqrt(Math.min(...neighbors.filter(n=>n.label===label).map(n=>n.squared))/99)/2048;
  return {label:voteFraction>=.6&&distance<=model.classThresholds[label]?label:null,voteFraction,distance};
}
for(const query of queries){
  const before=previousReference(query.features),after=predictPose(model,query.features);
  for(const key of ['label','voteFraction','distance'])assert.equal(after[key],before[key]);
}
function measure(fn){
  const start=performance.now();
  for(let i=0;i<1000;i++)fn(queries[i%queries.length].features);
  return performance.now()-start;
}
measure(previousReference);measure(f=>predictPose(model,f));
const median=values=>values.slice().sort((a,b)=>a-b)[Math.floor(values.length/2)];
const before=median(Array.from({length:3},()=>measure(previousReference)));
const after=median(Array.from({length:3},()=>measure(f=>predictPose(model,f))));
console.log(JSON.stringify({scope:'PC reference classification only; not board/live FPS',
  datasetHash:dataset.datasetMeta.sourceHash,parityQueries:queries.length,iterations:1000,
  referenceFullQuantizeAndSortMs:before,quantizeCacheAndTop5Ms:after,
  validation:Object.fromEntries(Object.entries(models).map(([id,m])=>[id,{featureSpec:m.featureSpec,
    train:m.rows.length,correct:m.validation.correct,total:m.validation.total,rejected:m.validation.rejected,
    independentVideoValidation:false}]))},null,2));
