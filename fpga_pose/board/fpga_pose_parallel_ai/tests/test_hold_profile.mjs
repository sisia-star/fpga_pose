import test from'node:test';import assert from'node:assert/strict';import fs from'node:fs';
import{packHoldPose,extractHoldFeatures,HOLD_FEATURE_SPEC}from'../hold_profile.mjs';
import{MotionMetrics,SpeechCoach}from'../coach_features.mjs';import{canPredict}from'../dataset_ui_helpers.mjs';
const h=fs.readFileSync(new URL('../data/hold_poses.csv',import.meta.url),'utf8').trim().split(/\r?\n/).slice(1);
function source(id){for(const line of h){const a=line.split(',');if(a.at(-1)!==id)continue;const points=Array.from({length:33},(_,i)=>({x:+a[i*3],y:+a[i*3+1],z:+a[i*3+2],visibility:.99}));try{packHoldPose(points,id==='high plank'?'high_plank':id==='knee plank'?'knee_plank':id);return points;}catch{}}throw Error(id);}
for(const[id,label]of[['birddog','birddog'],['high_plank','high plank'],['knee_plank','knee plank']])test(`${id} source/live descriptors agree; no world Z or face required`,()=>{
 const p=source(label),a=packHoldPose(p,id);for(let i=0;i<11;i++)p[i]=null;for(const v of p)if(v)v.z=NaN;
 const b=extractHoldFeatures(p,320,180,{exerciseId:id});assert.equal(b.ok,true,b.reason);assert.deepEqual(b.features,a.features);assert.equal(b.featureSpec,HOLD_FEATURE_SPEC);assert.equal(b.features.length,99);
});
test('planks allow hidden opposite side; bird dog requires contralateral evidence',()=>{const p=source('high plank');const a=packHoldPose(p,'high_plank');for(const i of(a.selectedSide==='left'?[12,14,16,24,26,28]:[11,13,15,23,25,27]))p[i]=null;assert.equal(extractHoldFeatures(p,320,180,{exerciseId:'high_plank'}).ok,true);assert.equal(extractHoldFeatures(p,320,180,{exerciseId:'birddog'}).ok,false);});
test('no accepted hold classification with low confidence core',()=>{const p=source('birddog');p[15].visibility=.1;assert.equal(extractHoldFeatures(p,320,180,{exerciseId:'birddog'}).ok,false);});
test('hold metric whitelist and zero reps are supported',()=>{for(const id of['birddog','high_plank','knee_plank']){const m=new MotionMetrics({exerciseId:id,now:()=>0});assert.equal(m.snapshot().exercise_id,id);assert.equal(m.snapshot().confirmed_completions_in_window,0);}});
test('speech completion is announced once and hold seconds never become repetitions',()=>{
 const said=[],s=new SpeechCoach({speechSynthesis:{getVoices:()=>[],cancel(){},speak:u=>said.push(u.text)},Utterance:class{constructor(t){this.text=t;}}});s.setEnabled(true);
 const a={monitor_id:'m',count:0,training_mode:'hold',effective_hold_seconds:0,remaining_seconds:20};s.updateTraining(a);s.updateTraining({...a,effective_hold_seconds:10,remaining_seconds:10});s.updateTraining({...a,effective_hold_seconds:20,remaining_seconds:0,finished:true});s.updateTraining({...a,effective_hold_seconds:20,remaining_seconds:0,finished:true});
 assert.equal(said.length,2);assert.match(said[0],/10秒/);assert.match(said[1],/完成/);assert.ok(said.every(x=>!x.includes('次')));
});
test('finished sessions cannot submit another live prediction',()=>{assert.equal(canPredict({monitor:{monitor_id:'m',finished:true},countdown:{active:false},generation:1,currentGeneration:1,seq:2,capture:2,boundary:{frame_seq_floor:1,capture_time_floor:1}}),false);});
