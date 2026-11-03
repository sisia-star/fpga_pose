"""Board-owned timed repetitions and effective hold time, using monotonic clocks.
No browser clock, client counts, silent intervals or duplicate frames earn time.
"""
from exercise_counter import ExerciseCounter, CounterError, _number


class TrainingSession(ExerciseCounter):
    def __init__(self, clock):
        self.clock = clock
        self.mode = 'count'
        self.duration = None
        self.finished = False
        self._armed_at = None
        self._elapsed_before = 0.0
        self.effective = 0.0
        self._hold_last = None
        self._hold_since = None
        self.hold_gap = 0.75
        super().__init__()

    def configure_training(self, mode, duration, countdown_seconds=3):
        if mode not in ('count', 'hold'):
            raise CounterError('训练模式无效')
        value = _number(duration, '训练时长（秒）', 5, 3600)
        with self._lock:
            self.mode, self.duration = mode, value
            self.finished = False
            self._elapsed_before = self.effective = 0.0
            self._armed_at = self.clock() + countdown_seconds
            self._hold_last = self._hold_since = None
            return self.snapshot()

    def _elapsed(self, now):
        extra = max(0.0, now - self._armed_at) if self._armed_at is not None and not self.paused else 0.0
        return self._elapsed_before + extra

    def tick(self, now=None):
        with self._lock:
            now = self.clock() if now is None else now
            if self.active and self.duration is not None and not self.finished:
                if self.mode == 'count' and self._elapsed(now) >= self.duration:
                    self._elapsed_before = self.duration
                    self._finish('规定时间已到，本组计数完成')
            return self.finished

    def _finish(self, reason):
        if self.mode == 'hold':
            self._elapsed_before = self._elapsed(self.clock())
        self.finished = True
        self._armed_at = None
        self._hold_last = self._hold_since = None
        self._cancel_cycle()
        self.phase = 'completed'
        self.reason = reason

    def invalidate(self, reason='模型已变化，请重新开始监测', preserve_count=False):
        with self._lock:
            self._armed_at = None
            self.finished = False
            self._hold_last = self._hold_since = None
            if not preserve_count:
                self.duration = None
                self.effective = self._elapsed_before = 0.0
            return super().invalidate(reason, preserve_count)

    def control(self, action, monitor_id):
        with self._lock:
            self.tick()
            if self.finished and action != 'reset':
                raise CounterError('本组已完成，请重新开始或清零')
            now = self.clock()
            if action == 'pause' and not self.paused:
                self._elapsed_before = self._elapsed(now)
                self._armed_at = None
            result = super().control(action, monitor_id)
            if action == 'resume':
                self._armed_at = now + 3.0
            if action == 'reset':
                self.effective = self._elapsed_before = 0.0
                self.finished = False
                self._armed_at = now
                self.paused = False
            self._hold_last = self._hold_since = None
            return self.snapshot()

    def observe(self, monitor_id, model_id, frame_seq, captured_at, label, hardware_completed):
        with self._lock:
            self.tick()
            if self.finished:
                self.require_monitor(monitor_id)
                return self.snapshot()
            if self.mode != 'hold':
                return super().observe(monitor_id, model_id, frame_seq, captured_at, label, hardware_completed)
            # Parent enforces session/model, monotonic sequence and board capture
            # timestamps. Its repetition state is irrelevant for hold mode.
            previous = self._last_time
            super().observe(monitor_id, model_id, frame_seq, captured_at, label, hardware_completed)
            if self.paused:
                return self.snapshot()
            valid = hardware_completed is True and label == self.target_label
            if not valid:
                self._hold_last = self._hold_since = None
                self.phase = 'waiting_hold'
                self.reason = '姿态未到位或关键点不清楚，有效保持计时暂停'
            else:
                continuous = (self._hold_last is not None and previous is not None
                              and 0 < captured_at - self._hold_last <= self.hold_gap)
                if not continuous:
                    self._hold_since = captured_at
                elif captured_at - self._hold_since >= self.dwell:
                    self.effective = min(self.duration, self.effective + captured_at - self._hold_last)
                self._hold_last = captured_at
                self.phase = 'holding' if captured_at - self._hold_since >= self.dwell else 'confirming_hold'
                self.reason = '姿态已确认，累计有效保持时间' if self.phase == 'holding' else '请稳定保持姿态'
                if self.effective >= self.duration:
                    self._finish('目标有效保持时间已完成')
            return self.snapshot()

    def snapshot(self):
        with self._lock:
            self.tick()
            value = super().snapshot()
            elapsed = self._elapsed(self.clock())
            remaining = None if self.duration is None else max(0.0, self.duration - (self.effective if self.mode == 'hold' else elapsed))
            value.update({'training_mode': self.mode, 'duration_seconds': self.duration,
                          'elapsed_seconds': round(elapsed, 3), 'effective_hold_seconds': round(self.effective, 3),
                          'remaining_seconds': None if remaining is None else round(remaining, 3), 'finished': self.finished})
            if self.finished:
                value.update(status='completed', phase='completed')
            return value
