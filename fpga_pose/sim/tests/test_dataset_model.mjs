/** node --test tests/test_dataset_model.mjs; real CSV fixtures + hostile import boundaries. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {inflateRawSync} from 'node:zlib';
import {FEATURE_SPEC,EXERCISES,LANDMARK_HEADER,importDatasetZip,importDatasetCSVs,
  validateDataset,trainExerciseModel,validateModel,predictPose,extractFeatures,sha256Bytes,quantize} from '../dataset_engine.mjs';
const archive=await fs.readFile(new URL('../data/archive.zip',import.meta.url));
const dataset=await importDatasetZip(archive);
const clone=x=>JSON.parse(JSON.stringify(x));
function crc(bytes){let c=0xffffffff;for(const b of bytes){c^=b;for(let j=0;j<8;j++)c=c&1?0xedb88320^(c>>>1):c>>>1;}return(c^0xffffffff)>>>0;}
function storedZip(entries) {
  const locals=[],centrals=[];let offset=0;
  for(const [name,value] of entries){const n=Buffer.from(name),data=Buffer.from(value),l=Buffer.alloc(30+n.length),c=Buffer.alloc(46+n.length),checksum=crc(data);
    l.writeUInt32LE(0x04034b50);l.writeUInt16LE(20,4);l.writeUInt32LE(checksum,14);l.writeUInt32LE(data.length,18);l.writeUInt32LE(data.length,22);l.writeUInt16LE(n.length,26);n.copy(l,30);
    c.writeUInt32LE(0x02014b50);c.writeUInt16LE(20,4);c.writeUInt16LE(20,6);c.writeUInt32LE(checksum,16);c.writeUInt32LE(data.length,20);c.writeUInt32LE(data.length,24);c.writeUInt16LE(n.length,28);c.writeUInt32LE(offset,42);n.copy(c,46);
    locals.push(l,data);centrals.push(c);offset+=l.length+data.length;
  }
  const central=Buffer.concat(centrals),end=Buffer.alloc(22);end.writeUInt32LE(0x06054b50);end.writeUInt16LE(entries.length,8);end.writeUInt16LE(entries.length,10);end.writeUInt32LE(central.length,12);end.writeUInt32LE(offset,16);
  return Buffer.concat([...locals,central,end]);
}
// Test-only Python ZIP-compatible extraction via Node standard library, not runtime dependency.
function csvFromArchive(name) {
  let p=0;while(archive.readUInt32LE(p)===0x04034b50){const n=archive.readUInt16LE(p+26),e=archive.readUInt16LE(p+28),path=archive.toString('utf8',p+30,p+30+n);let packed=archive.readUInt32LE(p+18),unpacked=archive.readUInt32LE(p+22);
    if(packed===0xffffffff){let q=p+30+n;while(q<p+30+n+e){const tag=archive.readUInt16LE(q),length=archive.readUInt16LE(q+2);if(tag===1){unpacked=Number(archive.readBigUInt64LE(q+4));packed=Number(archive.readBigUInt64LE(q+12));break;}q+=4+length;}}
    const start=p+30+n+e;if(path===name)return inflateRawSync(archive.subarray(start,start+packed)).toString('utf8');p=start+packed;
  }throw Error(name);
}
const landmarks=csvFromArchive('landmarks.csv'),labels=csvFromArchive('labels.csv');
const pair=[['landmarks.csv',landmarks],['labels.csv',labels]];

test('real archive ZIP64 entry sizes: 1372 -> 1371, five exercises/ten stages, original IDs',()=>{
  assert.equal(dataset.rows.length,1371);assert.equal(dataset.datasetMeta.totalSamples,1372);assert.equal(dataset.datasetMeta.duplicateRowsRemoved,1);
  assert.deepEqual(dataset.classes,Object.values(EXERCISES).filter(e=>e.mode!=='hold').flatMap(e=>[e.start_label,e.target_label]));
  assert.equal(dataset.datasetMeta.videoGroupsAvailable,false);assert.equal(dataset.datasetMeta.coordinateProtocol,FEATURE_SPEC);
  assert.equal(dataset.rows.find(r=>r.sampleId==='208').label,'jumping_jacks_up');assert.equal(dataset.rows.some(r=>r.sampleId==='209'),false);
  assert.ok(dataset.rows.every(r=>Object.keys(r).join(',')==='sampleId,label,features'&&!('sessionId'in r)));
  assert.equal(dataset.datasetMeta.sourceHash,createHash('sha256').update(archive).digest('hex'));
});
test('portable SHA-256 including multi-block inputs agrees with standard implementation',async()=>{
  for(const b of [Buffer.alloc(0),Buffer.from('abc'),Buffer.alloc(200,17),archive])assert.equal(await sha256Bytes(b),createHash('sha256').update(b).digest('hex'));
});
test('CSV pair yields identical rows, canonical columns and length-prefix source hash',async()=>{
  const result=await importDatasetCSVs(landmarks,labels);assert.deepEqual(result.rows,dataset.rows);assert.equal(LANDMARK_HEADER.length,100);
  const a=Buffer.from(landmarks),b=Buffer.from(labels),ab=Buffer.alloc(8+a.length+b.length);ab.writeUInt32BE(a.length,0);a.copy(ab,4);ab.writeUInt32BE(b.length,4+a.length);b.copy(ab,8+a.length);
  assert.equal(result.datasetMeta.sourceHash,createHash('sha256').update(ab).digest('hex'));
});
test('stored ZIP and UTF-8 nested paths are accepted offline',async()=>{
  const d=await importDatasetZip(storedZip(pair.map(([name,value])=>['参考/'+name,value])));assert.deepEqual(d.rows,dataset.rows);
});
test('header reorder/unknown columns, nonfinite/empty numeric values are rejected',async()=>{
  const header=landmarks.split('\r\n')[0].split(',');[header[1],header[2]]=[header[2],header[1]];
  await assert.rejects(importDatasetCSVs(header.join(',')+landmarks.slice(landmarks.indexOf('\r\n')),labels),/标准顺序/);
  await assert.rejects(importDatasetCSVs(landmarks.replace(/(\r\n0,)[^,]+/,'$1NaN'),labels),/无效坐标/);
  await assert.rejects(importDatasetCSVs(landmarks.replace(/(\r\n0,)[^,]+/,'$1'),labels),/无效坐标/);
});
test('IDs must be unique and both CSVs have complete one-to-one association',async()=>{
  await assert.rejects(importDatasetCSVs(landmarks,labels+'\r\n0,jumping_jacks_down\r\n'),/重复/);
  await assert.rejects(importDatasetCSVs(landmarks,labels.replace('0,jumping_jacks_down\r\n','')),/缺少阶段标签/);
  await assert.rejects(importDatasetCSVs(landmarks,labels+'\r\nextra,jumping_jacks_down\r\n'),/关联不完整/);
});
test('unknown stages and cross-label exact duplicate coordinates rejected',async()=>{
  await assert.rejects(importDatasetCSVs(landmarks,labels.replace('0,jumping_jacks_down','0,standing')),/未知阶段标签/);
  await assert.rejects(importDatasetCSVs(landmarks,labels.replace('209,jumping_jacks_up','209,jumping_jacks_down')),/冲突/);
});
test('normalization/hip origin and no-clipping signed16 bounds validated',async()=>{
  const lines=landmarks.split('\r\n'),row=lines[1].split(',');row[1]=String(Number(row[1])*100);lines[1]=row.join(',');
  await assert.rejects(importDatasetCSVs(lines.join('\r\n'),labels),/坐标协议|范围/);
  const d=clone(dataset);d.rows[0].features[2]=16;assert.throws(()=>validateDataset(d),/范围/);
  d.rows[0].features=dataset.rows[0].features.map(x=>x/2);assert.throws(()=>validateDataset(d),/坐标协议/);
});
test('dataset imports reject old schema, invented groups and uncleaned duplicates',()=>{
  const d=clone(dataset);d.schema='pose-dataset-v1';assert.throws(()=>validateDataset(d),/版本不兼容/);
  const d2=clone(dataset);d2.rows[0].sessionId='fake';assert.throws(()=>validateDataset(d2),/字段/);
  const d3=clone(dataset);d3.rows[1].features=d3.rows[0].features;assert.throws(()=>validateDataset(d3),/重复坐标/);
});
test('Q4.11 ties away from zero, extremal exact values, forbidden clipping',()=>{
  const a=Array(99).fill(0);a[0]=.5/2048;a[1]=-.5/2048;a[2]=-16;a[3]=32767/2048;a[4]=1.5/2048;
  assert.deepEqual(quantize(a).slice(0,5),[1,-1,-32768,32767,2]);a[5]=16;assert.throws(()=>quantize(a),/范围/);a[5]=-16.00001;assert.throws(()=>quantize(a),/范围/);
});
test('live IMAGE xyz restores xW/yH/zW without world coordinates, rotation or square assumption',()=>{
  const f=dataset.rows[0].features,w=640,h=360;const points=Array.from({length:33},(_,i)=>({x:(320+f[3*i]*100)/w,y:(180+f[3*i+1]*100)/h,z:f[3*i+2]*100/w,visibility:1,presence:1}));
  const result=extractFeatures(points,w,h);assert.equal(result.ok,true);const hip=[0,1,2].map(a=>(f[69+a]+f[72+a])/2);
  const size=Math.max(...Array.from({length:33},(_,i)=>Math.hypot(f[3*i]-hip[0],f[3*i+1]-hip[1])),2.5*Math.hypot((f[33]+f[36])/2-hip[0],(f[34]+f[37])/2-hip[1]));
  result.features.forEach((x,i)=>assert.ok(Math.abs(x-(f[i]-hip[i%3])/size)<1e-10));
  const z=result.features[2],y=result.features[1];assert.ok(Math.abs(z/((f[2]-hip[2])/size)-1)<1e-10);assert.ok(Math.abs(y/((f[1]-hip[1])/size)-1)<1e-10);
  const bad=clone(points);bad[25].visibility=.49;assert.equal(extractFeatures(bad,w,h).ok,false);assert.equal(extractFeatures(points,0,h).ok,false);
});
test('all five deterministic models use disjoint original IDs and honest sample metrics',()=>{
  for(const exerciseId of Object.keys(EXERCISES).filter(id=>EXERCISES[id].mode!=='hold')){
    const m=trainExerciseModel(dataset,exerciseId);assert.deepEqual(trainExerciseModel(dataset,exerciseId),m);assert.equal(m.schema,'pose-knn-dataset-model-v2');
    assert.deepEqual(m.classes,[EXERCISES[exerciseId].start_label,EXERCISES[exerciseId].target_label]);assert.equal(m.validation.independentVideoValidation,false);assert.equal(m.validation.method,'stratified-sample-holdout');
    assert.deepEqual(m.validation.trainSampleIds,m.rows.map(r=>r.sampleId));assert.ok(m.validation.holdoutSampleIds.every(id=>!m.validation.trainSampleIds.includes(id)));
    assert.equal(m.validation.total,m.validation.holdoutSampleIds.length);assert.ok(m.rows.length<=1024);
    for(const label of m.classes){assert.ok(m.rows.filter(r=>r.label===label).length>=10);assert.ok(m.rows.filter(r=>r.label===label).some(r=>predictPose(m,r.features).label===label));}
    assert.deepEqual(validateModel(JSON.parse(JSON.stringify(m))),m);
  }
});
test('threshold calibration uses training only; changing holdout features does not leak',()=>{
  const original=trainExerciseModel(dataset,'squats'),changed=clone(dataset),hold=new Set(original.validation.holdoutSampleIds);
  for(const row of changed.rows)if(hold.has(row.sampleId))row.features=row.features.map((x,i)=>i%3===0?-x:x);
  const next=trainExerciseModel(changed,'squats');assert.deepEqual(next.classThresholds,original.classThresholds);assert.deepEqual(next.rows,original.rows);
});
test('model rejects overlapping IDs, tampered metrics/classes, old features, fake groups',()=>{
  const base=trainExerciseModel(dataset,'squats');
  for(const mutate of [m=>m.validation.holdoutSampleIds[0]=m.rows[0].sampleId,m=>m.validation.accuracy=.123,m=>m.classes.reverse(),m=>m.featureSpec='world33-hipcenter-torsoscale-v1',m=>m.rows[0].sessionId='fake']){
    const m=clone(base);mutate(m);assert.throws(()=>validateModel(m));
  }
  assert.throws(()=>trainExerciseModel(dataset,'__proto__'),/未知运动/);
});
test('validated models immutable; unknown or invalid query remains rejected',()=>{
  const m=trainExerciseModel(dataset,'squats');assert.throws(()=>{m.classThresholds[m.classes[0]]=999;},TypeError);
  assert.equal(predictPose(m,Array(99).fill(15)).label,null);assert.equal(predictPose(m,Array(99).fill(NaN)).label,null);
});
test('ZIP refuses dangerous paths, duplicate names and duplicate CSV basenames',async()=>{
  for(const path of ['../landmarks.csv','/landmarks.csv','D:/landmarks.csv','x\\landmarks.csv','x//landmarks.csv'])await assert.rejects(importDatasetZip(storedZip([[path,landmarks],['labels.csv',labels]])),/危险路径/);
  await assert.rejects(importDatasetZip(storedZip([...pair,['LANDMARKS.csv',landmarks]])),/重复文件名/);
  await assert.rejects(importDatasetZip(storedZip([...pair,['extra/landmarks.csv',landmarks]])),/多个 landmarks/);
});
test('ZIP refuses CRC corruption, encrypted flags, declared bomb, malformed directory and missing CSV',async()=>{
  const zip=storedZip(pair),bad=Buffer.from(zip);bad[50]^=1;await assert.rejects(importDatasetZip(bad),/CRC/);
  const encrypted=Buffer.from(zip),central=encrypted.indexOf(Buffer.from([0x50,0x4b,0x01,0x02]));encrypted.writeUInt16LE(1,6);encrypted.writeUInt16LE(1,central+8);await assert.rejects(importDatasetZip(encrypted),/加密/);
  const huge=Buffer.from(zip);huge.writeUInt32LE(9*1024*1024,central+24);await assert.rejects(importDatasetZip(huge),/超大文件/);
  await assert.rejects(importDatasetZip(zip.subarray(0,zip.length-1)),/目录损坏/);
  await assert.rejects(importDatasetZip(storedZip([['angles.csv','a,b\n1,2'],['labels.csv',labels]])),/必须包含/);
});

test('lying orientation preserved and absence/presence quality checks pause extraction',()=>{
  const p=Array.from({length:33},()=>({x:.5,y:.5,z:0,visibility:1,presence:1}));
  p[11].x=.25;p[12].x=.25;p[23].x=.65;p[24].x=.65;
  const f=extractFeatures(p,640,360);assert.equal(f.ok,true);assert.ok(f.features[33]<0);assert.equal(f.features[34],0);
  const bad=clone(p);bad[27].presence=.1;assert.equal(extractFeatures(bad,640,360).ok,false);
});
test('ZIP64 extra size high bits, malformed extra lengths and unsupported compression reject',async()=>{
  const extra=Buffer.from(archive),central=extra.indexOf(Buffer.from([0x50,0x4b,0x01,0x02])),name=extra.readUInt16LE(central+28),field=central+46+name;
  extra.writeUInt32LE(1,field+8);await assert.rejects(importDatasetZip(extra),/超过允许大小/);
  const broken=Buffer.from(archive);broken.writeUInt16LE(65535,field+2);await assert.rejects(importDatasetZip(broken),/extra 长度/);
  const zip=storedZip(pair),c=zip.indexOf(Buffer.from([0x50,0x4b,0x01,0x02]));zip.writeUInt16LE(99,c+10);await assert.rejects(importDatasetZip(zip),/压缩/);
});
