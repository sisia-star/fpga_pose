import test from'node:test';import assert from'node:assert/strict';import vm from'node:vm';import fs from'node:fs';
import*as engine from'../dataset_engine.mjs';import*as helper from'../dataset_ui_helpers.mjs';import*as coach from'../coach_features.mjs';
const text=fs.readFileSync(new URL('../fpga_pose_client.mjs',import.meta.url),'utf8'),html=fs.readFileSync(new URL('../tools/frontend_template.html',import.meta.url),'utf8');
function fixture(){
 const nodes=new Map();for(const[,id]of html.matchAll(/id="([^"]+)"/g))nodes.set(id,{value:'',checked:false,disabled:false,textContent:'',hidden:false,files:[],replaceChildren(){},append(){},getContext(){return{};}});
 nodes.get('exercise').value='squats';nodes.get('dwell').value='.15';nodes.get('duration').value='60';
 const context=vm.createContext({document:{getElementById:id=>{if(!nodes.has(id))throw Error('Missing DOM id '+id);return nodes.get(id);},createElement:()=>({append(){}})},engine,helper,coach,resources:{},urls:[],window:{addEventListener(){}},performance,fetch:async()=>{throw Error('unrequested request');},URL,console,AbortController,setTimeout,clearTimeout,Blob,Number,Map,Set});
 vm.runInContext("const $=id=>document.getElementById(id);const sleep=ms=>Promise.resolve();\n"+text.slice(text.indexOf('const holdExercise')).replace('sync();void check();','sync();'),context);
 return {nodes,run:code=>vm.runInContext(code,context)};
}
test('eight modes exist; durations lock during an active session and unlock when finished',()=>{
 const f=fixture();assert.equal(Object.keys(engine.EXERCISES).length,8);f.run("showCounter({monitor_id:'m',count:0,paused:false,finished:false,training_mode:'count',duration_seconds:60,remaining_seconds:59,elapsed_seconds:1,phase:'ready'})");assert.equal(f.nodes.get('duration').disabled,true);
 f.run("showCounter({monitor_id:'m',count:3,paused:false,finished:true,training_mode:'count',duration_seconds:60,remaining_seconds:0,elapsed_seconds:60,phase:'completed'})");assert.equal(f.nodes.get('duration').disabled,false);assert.equal(f.nodes.get('count').textContent,'3');assert.equal(f.nodes.get('pause').disabled,true);
});
test('hold display uses seconds and evaluation is available with zero repetitions',()=>{
 const f=fixture();f.nodes.get('exercise').value='birddog';f.nodes.get('aiEnabled').checked=true;f.run("showCounter({monitor_id:'m',count:0,paused:false,training_mode:'hold',duration_seconds:20,effective_hold_seconds:3.5,remaining_seconds:16.5,elapsed_seconds:8,phase:'holding'})");
 assert.equal(f.nodes.get('count').textContent,'3.5');assert.equal(f.nodes.get('countUnit').textContent,'秒');assert.equal(f.nodes.get('aiEvaluate').disabled,false);assert.equal(f.nodes.get('durationLabel').textContent,'目标有效保持时长');
});
test('initial built-in model action is usable without CSV import',()=>{const f=fixture();assert.equal(f.nodes.get('train').disabled,false);assert.equal(f.nodes.get('export').disabled,true);});
