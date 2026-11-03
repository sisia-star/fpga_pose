export const PIXEL_PIPELINE = 'fpga-2x2-box-half-bgrx-v2';
export const TEMPLATE_SCHEMA = 'pose-reference-dataset-v4-ai';

export function exerciseProfile(catalog, exerciseId, dwellSeconds) {
  const entry = catalog[exerciseId], dwell = Number(dwellSeconds);
  if (!entry || !Number.isFinite(dwell) || dwell < 0.1 || dwell > 2)
    throw new Error('请选择支持的运动，到位确认时间应为 0.1～2 秒');
  return {exercise_id: exerciseId, name: entry.name, start_label: entry.start_label,
    target_label: entry.target_label, dwell_seconds: dwell,
    max_gap_seconds: Math.max(1.5, dwell + 0.1)};
}
export function validateBoundary(value) {
  const boundary = {frame_seq_floor: value?.frame_seq_floor, capture_time_floor: value?.capture_time_floor};
  if (!Number.isSafeInteger(boundary.frame_seq_floor) || boundary.frame_seq_floor < 0 ||
      !Number.isFinite(boundary.capture_time_floor) || boundary.capture_time_floor < 0)
    throw new Error('开发板未返回有效的新帧边界，请运行新版服务');
  return boundary;
}
export function frameAfterBoundary(seq, capture, boundary) {
  return !!boundary && Number.isSafeInteger(seq) && Number.isFinite(capture) &&
    seq > boundary.frame_seq_floor && capture > boundary.capture_time_floor;
}
export function countdownState(value, monitorId) {
  const state = value?.countdown;
  if (!state || typeof state.active !== 'boolean' || state.seconds !== 3 ||
      !Number.isFinite(state.remaining_seconds) || state.remaining_seconds < 0 ||
      state.remaining_seconds > 3 || !Number.isFinite(state.server_time) ||
      (state.active && (!Number.isFinite(state.deadline) || state.monitor_id !== monitorId)))
    throw new Error('开发板未返回有效的三秒倒计时状态');
  return {...state, display: state.active ? Math.max(1, Math.ceil(state.remaining_seconds)) : null};
}
export function canPredict({monitor, countdown, controlPending, generation, currentGeneration, seq, capture, boundary}) {
  return !!monitor?.monitor_id && monitor.paused !== true && monitor.finished !== true && countdown?.active === false &&
    !controlPending && generation === currentGeneration && frameAfterBoundary(seq, capture, boundary);
}
export function datasetSummary(dataset, catalog) {
  const counts = new Map();
  for (const row of dataset.rows) counts.set(row.label, (counts.get(row.label) || 0) + 1);
  return Object.values(catalog).map(exercise => ({...exercise,
    startCount: counts.get(exercise.start_label) || 0, targetCount: counts.get(exercise.target_label) || 0}));
}
export function verifyPrediction(value, modelId, classes) {
  if (value?.backend !== 'fpga' || value.model_id !== modelId || value.hardware_completed !== true ||
      !Number.isInteger(value.last_cycles) || value.last_cycles <= 0 ||
      (value.label !== null && !classes.includes(value.label)))
    throw new Error('预测缺少 FPGA 完成证据，已停止计数');
  return true;
}
