/* Classic worker supports the offline WASM loader's importScripts. */
let engine, landmarker, poseEndpoint, previousSide=null, trackingGeneration=null, candidateSide=null, candidateFrames=0;
onmessage = async event => {
  const {id, type, payload} = event.data;
  try {
    let value;
    if (type === 'init') {
      engine = await import(payload.engine); poseEndpoint=payload.poseEndpoint; 
      if (payload.runtime) {
        const vision = await import(payload.runtime);
        landmarker = await vision.PoseLandmarker.createFromOptions({wasmLoaderPath: payload.loader,
          wasmBinaryPath: payload.wasm}, {baseOptions: {modelAssetBuffer: payload.model, delegate: 'CPU'},
          runningMode: 'VIDEO', numPoses: 1, minPoseDetectionConfidence: 0.5,
          minPosePresenceConfidence: 0.5, minTrackingConfidence: 0.5, outputSegmentationMasks: false});
      }
      value = {ready: true};
    } else if (type === 'zip') value = await engine.importDatasetZip(payload.buffer);
    else if (type === 'csv') value = await engine.importDatasetCSVs(payload.landmarks, payload.labels);
    else if (type === 'train') value = engine.trainExerciseModel(engine.validateDataset(payload.dataset), payload.exerciseId);
    else if (type === 'template') {
      const template = payload.template;
      if (!['pose-reference-dataset-v4-ai','pose-reference-dataset-v3'].includes(template?.schema) ||
          template.featureSpec !== engine.featureSpecForExercise(template.exerciseId))
        throw new Error('请导入本版导出的数据集模板；旧版世界坐标或视频模板不能使用');
      const dataset = engine.validateDataset(template.dataset), model = engine.validateModel(template.model);
      if (template.exerciseId !== model.exerciseId || template.profile?.exercise_id !== model.exerciseId)
        throw new Error('模板运动与参考模型不一致');
      const rebuilt = engine.trainExerciseModel(dataset, template.exerciseId);
      if (JSON.stringify(rebuilt) !== JSON.stringify(model)) throw new Error('模板模型与规范数据集不一致，请重新生成');
      value = {dataset, model, profile: template.profile};
    } else if (type === 'pose') {
      if (!landmarker && !poseEndpoint) throw new Error('姿态模型尚未初始化');
      if (trackingGeneration !== payload.generation) {
        previousSide=null; candidateSide=null; candidateFrames=0; trackingGeneration=payload.generation;
      }
      const started=performance.now();
      const width = payload.bitmap.width, height = payload.bitmap.height;
      let points, convEvidence=null;
      if(poseEndpoint){
        const jpeg_b64=new FileReaderSync().readAsDataURL(payload.frameBlob).split(',')[1];payload.bitmap.close();
        const response=await fetch(poseEndpoint,{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({frame_seq:payload.frame_seq,jpeg_b64}),signal:AbortSignal.timeout(7000)});
        const result=await response.json();if(!response.ok)throw Error(result.error||'FPGA卷积识别失败');
        if(result.frame_seq!==payload.frame_seq)throw Error('姿态帧序号不一致');
        points=result.points||[];convEvidence=result.convolution;
        if(points.length && !result.convolution?.hardware_completed)throw Error('缺少真实卷积计算证据');
      }else{
        const result=landmarker.detectForVideo(payload.bitmap,payload.timestamp);payload.bitmap.close();points=result.landmarks?.[0]||[];
      }
      const features=engine.extractFeatures(points, width, height, {
        exerciseId:payload.exerciseId, preferredSide:payload.preferredSide || 'auto', previousSide});
      if(features.ok && features.selectedSide && !previousSide && (payload.preferredSide || 'auto')==='auto'){
        if(candidateSide===features.selectedSide)candidateFrames++;else{candidateSide=features.selectedSide;candidateFrames=1;}
        if(candidateFrames>=3)previousSide=features.selectedSide;
        else {features.ok=false;features.reason=`正在确认${candidateSide==='left'?'左':'右'}侧核心点（${candidateFrames}/3），请保持机位`;}
      }else if(!features.ok && !previousSide){candidateSide=null;candidateFrames=0;}
      value = {...features, points, convolution:convEvidence, width, height, extractMs:performance.now()-started};
    } else throw new Error('未知数据处理任务');
    postMessage({id, ok: true, value});
  } catch (error) {
    try {payload?.bitmap?.close();} catch {}
    postMessage({id, ok: false, error: error.message || String(error)});
  }
};
