import assert from 'node:assert/strict';import fs from 'node:fs/promises';import vm from 'node:vm';import * as engine from '../dataset_engine.mjs';
let points;const replies=[];
const source=(await fs.readFile(new URL('../pose_dataset_worker.mjs',import.meta.url),'utf8')).replace('await import(payload.engine)','testEngine').replace('await import(payload.runtime)','testVision');
const context=vm.createContext({testEngine:engine,testVision:{PoseLandmarker:{async createFromOptions(){return{detectForVideo(){return{landmarks:[points]};}};}}},performance,postMessage:r=>replies.push(r)});
vm.runInContext(source,context);let serial=0;
const request=async(type,payload)=>{await context.onmessage({data:{id:++serial,type,payload}});const r=replies.at(-1);assert.equal(r.ok,true,r.error);return r.value;};
function pose(side){const p=Array(33).fill(null),ids=side==='left'?[11,13,15]:[12,14,16];[[.3,.3],[.4,.5],[.6,.4]].forEach(([x,y],i)=>p[ids[i]]={x,y,visibility:.95});return p;}
await request('init',{engine:'mock-import',runtime:'mock-import'});
const frame=(generation=1,preferredSide='auto')=>request('pose',{bitmap:{width:640,height:360,close(){}},timestamp:serial,generation,exerciseId:'pushups',preferredSide});
points=pose('left');assert.equal((await frame()).ok,false);assert.equal((await frame()).ok,false);assert.equal((await frame()).ok,true);
points=pose('right');assert.equal((await frame()).ok,false,'locked side may not switch mid-rep');
assert.equal((await frame(2)).ok,false);assert.equal((await frame(2)).ok,false);assert.equal((await frame(2)).ok,true,'new generation reacquires after three frames');
assert.equal((await frame(3,'right')).ok,true,'manual visible side is explicit');
console.log('Worker side lock: stable warm-up, loss, generation reset, and manual side passed.');
