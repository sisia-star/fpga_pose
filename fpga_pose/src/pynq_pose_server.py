"""USB capture owner: every published frame has passed through the PL pixel core."""
from collections import OrderedDict
import threading
import time

import cv2

from fpga_classifier import FPGAError


class PoseCameraServer:
    """Capture USB BGR frames, run real DMA/PL, then JPEG encode on ARM."""
    def __init__(self, processor, camera="/dev/video0", width=640, height=360,
                 max_fps=20, frame_cache_size=256):
        self.processor = processor
        self.camera = camera
        self.request_width, self.request_height = int(width), int(height)
        self.max_fps = float(max_fps)
        self._cache_size = int(frame_cache_size)
        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._capture_thread = None
        self._running = False
        self._jpeg = None
        self._frame_id = 0
        self._frame_time = None
        self._width = self._height = 0
        self._error = None
        self._frames = OrderedDict()
        self._last_evidence = None

    def status(self):
        with self._state_lock:
            age = None if self._frame_time is None else max(0.0, time.monotonic() - self._frame_time)
            return {"running": self._running, "camera": str(self.camera),
                    "frame_id": self._frame_id, "width": self._width, "height": self._height,
                    "frame_age_seconds": None if age is None else round(age, 3),
                    "error": self._error, "frame_image_evidence": self._last_evidence}

    def start(self):
        if self._capture_thread is not None and self._capture_thread.is_alive():
            raise RuntimeError("旧摄像头线程仍在运行，请先停止服务")
        if not self.processor.status()["ready"]:
            raise FPGAError("图像 PL 自检未通过，禁止发布 CPU 原图")
        cap = cv2.VideoCapture(self.camera, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError("无法打开摄像头 %s，请关闭其他取图程序" % self.camera)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.request_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.request_height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._stop_event.clear()
        with self._state_lock:
            self._running = True
            self._error = None
        self._capture_thread = threading.Thread(target=self._capture, args=(cap,),
                                                name="PYNQ-PL-camera", daemon=True)
        self._capture_thread.start()
        return self

    def latest(self):
        with self._state_lock:
            age = None if self._frame_time is None else time.monotonic() - self._frame_time
            if not self._running or self._jpeg is None or age is None or age > 3.0:
                raise FPGAError(self._error or "暂无新 PL 图像，请检查 /api/status")
            return self._jpeg, self._frame_id, self._frame_time, self._last_evidence.copy()

    def frame_record(self, frame_seq, max_age=3.0):
        with self._state_lock:
            if isinstance(frame_seq, bool) or not isinstance(frame_seq, int):
                raise FPGAError("frame_seq 必须为开发板返回的整数序号", 400)
            value = self._frames.get(frame_seq)
            if value is None or time.monotonic() - value["capture_time"] > max_age:
                raise FPGAError("画面已过期或未经过本服务的 FPGA 图像处理，请获取新帧", 409)
            return dict(value)

    def _capture(self, cap):
        last_processed = 0.0
        try:
            while not self._stop_event.is_set():
                ok, frame = cap.read()
                captured_at = time.monotonic()
                if not ok or frame is None:
                    with self._state_lock:
                        self._error = "摄像头读取失败，计数等待有效画面"
                    self._stop_event.wait(0.1)
                    continue
                if captured_at - last_processed < 1.0 / self.max_fps:
                    continue
                # No cv2.resize or CPU preprocessing fallback here.
                processed, evidence = self.processor.process(frame)
                ok, encoded = cv2.imencode(".jpg", processed, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if not ok:
                    with self._state_lock:
                        self._error = "PL 图像的 JPEG 编码失败"
                    continue
                height, width = processed.shape[:2]
                with self._state_lock:
                    self._jpeg = encoded.tobytes()
                    self._frame_id += 1
                    self._frame_time = captured_at
                    self._width, self._height = int(width), int(height)
                    self._last_evidence = dict(evidence)
                    self._frames[self._frame_id] = {"frame_seq": self._frame_id,
                            "capture_time": captured_at, "image": dict(evidence)}
                    while len(self._frames) > self._cache_size:
                        self._frames.popitem(last=False)
                    self._error = None
                last_processed = captured_at
        except Exception as exc:
            with self._state_lock:
                self._error = "摄像头/FPGA 图像管线已停止：%s" % exc
        finally:
            cap.release()
            with self._state_lock:
                self._running = False

    def stop(self):
        self._stop_event.set()
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=5)
            if self._capture_thread.is_alive():
                raise RuntimeError("摄像头读取尚未返回；不能同时释放相机或 DMA，请等待或重启开发板")
            self._capture_thread = None
        with self._state_lock:
            self._running = False
            self._jpeg = None
            self._frame_time = None
            self._frames.clear()
        return self
