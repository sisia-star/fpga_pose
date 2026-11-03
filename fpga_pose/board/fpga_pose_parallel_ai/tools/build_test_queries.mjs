import fs from 'node:fs';import {fileURLToPath}from'node:url';import path from'node:path';
import {importDatasetZip,trainExerciseModel,predictPose}from'../dataset_engine.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..'),dataset=await importDatasetZip(fs.readFileSync(path.join(root,'data/archive.zip')));
const models=JSON.parse(fs.readFileSync(path.join(root,'data/hold_models.json'))),queries={};
for(const id of ['squats','pushups','situp','jumping_jacks','pullups'])models[id]=trainExerciseModel(dataset,id);
for(const[id,model]of Object.entries(models)){
 const q=[];for(const label of model.classes){const row=model.rows.find(r=>r.label===label&&predictPose(model,r.features).label===label);if(!row)throw Error('No positive regression sample '+id+' '+label);q.push({features:row.features,expected:label,sourceSampleId:row.sampleId});}
 queries[id]=q;
}
fs.writeFileSync(path.join(root,'data/all_models.json'),JSON.stringify(models));fs.writeFileSync(path.join(root,'data/hardware_test_queries.json'),JSON.stringify(queries));
console.log(JSON.stringify({models:Object.fromEntries(Object.entries(models).map(([k,v])=>[k,v.rows.length])),queries:Object.fromEntries(Object.entries(queries).map(([k,v])=>[k,v.length]))}));
