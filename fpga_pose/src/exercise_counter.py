"""PS exercise state machine. Only confirmed PL classifications advance it."""
import math
import threading
import uuid


class CounterError(ValueError):
    pass


def _number(value, name, low, high):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not low <= value <= high):
        raise CounterError("%s 必须为 %.2f～%.2f" % (name, low, high))
    return float(value)


class ExerciseCounter:
    """Time-based start -> target -> start counter; no client-provided count."""
    def __init__(self):
        self._lock = threading.RLock()
        self.monitor_id = None
        self.active = False
        self.paused = False
        self.model_id = None
        self.action = None
        self.start_label = None
        self.target_label = None
        self.dwell = 0.25
        self.max_gap = 1.5
        self.count = 0
        self.phase = "waiting_start"
        self._candidate = None
        self._candidate_since = None
        self._last_seq = -1
        self._last_time = None
        self._unknown_since = None
        self.last_count_time = None
        self.reason = "请部署模型并开始监测"

    def _cancel_cycle(self):
        self.phase = "waiting_start"
        self._candidate = self._candidate_since = None
        self._unknown_since = None

    def invalidate(self, reason="模型已变化，请重新开始监测", preserve_count=False):
        with self._lock:
            self.active = False
            self.paused = False
            self.monitor_id = None
            self.model_id = None
            if not preserve_count:
                self.count = 0
                self.last_count_time = None
            self._cancel_cycle()
            self._last_seq, self._last_time = -1, None
            self.reason = reason
            return self.snapshot()

    def start(self, model_id, action, start_label, target_label,
              dwell_seconds=0.25, max_gap_seconds=1.5):
        if not all(isinstance(v, str) and 0 < len(v.strip()) <= 120
                   for v in (model_id, action, start_label, target_label)):
            raise CounterError("模型、动作及阶段名称必须为非空字符串")
        if start_label == target_label:
            raise CounterError("起始与到位阶段必须使用不同类别")
        dwell = _number(dwell_seconds, "阶段稳定时间", 0.1, 2.0)
        gap = _number(max_gap_seconds, "最大跟踪间隔", dwell + 0.1, 10.0)
        with self._lock:
            self.monitor_id = uuid.uuid4().hex
            self.model_id, self.action = model_id, action
            self.start_label, self.target_label = start_label, target_label
            self.dwell, self.max_gap = dwell, gap
            self.count = 0
            self.last_count_time = None
            self.active, self.paused = True, False
            self._last_seq, self._last_time = -1, None
            self._cancel_cycle()
            self.reason = "先保持起始姿态，再完成到位和返回"
            return self.snapshot()

    def require_monitor(self, monitor_id):
        if not self.active or not monitor_id or monitor_id != self.monitor_id:
            raise CounterError("监测会话无效，请重新开始监测")

    def control(self, action, monitor_id):
        with self._lock:
            self.require_monitor(monitor_id)
            if action == "pause":
                self.paused = True
                self.reason = "已暂停；未完成的一次动作已取消"
            elif action == "resume":
                self.paused = False
                self.reason = "已继续，请重新保持起始姿态"
            elif action == "reset":
                self.count = 0
                self.last_count_time = None
                self.reason = "次数已清零，请重新保持起始姿态"
            else:
                raise CounterError("未知监测操作")
            self._cancel_cycle()
            return self.snapshot()

    def observe(self, monitor_id, model_id, frame_seq, captured_at,
                label, hardware_completed):
        """Timestamps must come from the board frame cache, not request JSON."""
        with self._lock:
            self.require_monitor(monitor_id)
            if model_id != self.model_id:
                raise CounterError("监测模型已变化")
            if (isinstance(frame_seq, bool) or not isinstance(frame_seq, int)
                    or frame_seq <= self._last_seq):
                raise CounterError("拒绝重复或乱序画面")
            if (not isinstance(captured_at, (int, float)) or not math.isfinite(captured_at)
                    or (self._last_time is not None and captured_at <= self._last_time)):
                raise CounterError("拒绝无效或乱序采集时间")
            if (self._last_time is not None
                    and captured_at - self._last_time > self.max_gap):
                self._cancel_cycle()
                self.reason = "跟踪中断，未完成动作已取消"
            self._last_seq, self._last_time = frame_seq, float(captured_at)
            if self.paused:
                return self.snapshot()
            if hardware_completed and label not in (self.start_label, self.target_label):
                # Fully tracked poses between endpoints are normal during a
                # slow repetition. They must not be mistaken for tracking loss.
                self._candidate = self._candidate_since = None
                self._unknown_since = None
                self.reason = "动作过渡中，等待起始或到位阶段"
                return self.snapshot()
            if not hardware_completed:
                self._candidate = self._candidate_since = None
                if self._unknown_since is None:
                    self._unknown_since = captured_at
                if captured_at - self._unknown_since >= self.max_gap:
                    self._cancel_cycle()
                self.reason = "姿态未确定，等待有效画面"
                return self.snapshot()
            self._unknown_since = None
            if label != self._candidate:
                self._candidate, self._candidate_since = label, captured_at
                return self.snapshot()
            if captured_at - self._candidate_since < self.dwell:
                return self.snapshot()
            if self.phase == "waiting_start" and label == self.start_label:
                self.phase = "ready"
                self.reason = "起始已确认，请完成动作到位"
            elif self.phase == "ready" and label == self.target_label:
                self.phase = "target_reached"
                self.reason = "到位已确认，请返回起始姿态"
            elif self.phase == "target_reached" and label == self.start_label:
                self.count += 1
                self.last_count_time = captured_at
                self.phase = "ready"
                self.reason = "完整动作已计数，请继续"
            return self.snapshot()

    def snapshot(self):
        with self._lock:
            return {"monitor_id": self.monitor_id, "active": self.active,
                    "paused": self.paused,
                    "status": "paused" if self.paused else "running" if self.active else "idle",
                    "model_id": self.model_id, "action": self.action,
                    "start_label": self.start_label, "target_label": self.target_label,
                    "count": self.count, "phase": self.phase,
                    "dwell_seconds": self.dwell, "max_gap_seconds": self.max_gap,
                    "last_frame_seq": self._last_seq, "last_capture_time": self._last_time,
                    "last_count_time": self.last_count_time, "reason": self.reason}
