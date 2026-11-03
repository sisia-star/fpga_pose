"""PYNQ PS server: real PL pixels + PL pose distances + PS exercise counting."""
import argparse
import json
import math
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from fpga_classifier import FPGAKNNClassifier, FPGAError, validate_model, EXERCISE_CATALOG
from fpga_image import FPGAImageProcessor, validate_v2_hwh, PIXEL_PIPELINE
from exercise_counter import ExerciseCounter, CounterError
from training_session import TrainingSession
from pynq_pose_server import PoseCameraServer
from ai_coach import AICoachBroker, TELEMETRY_SCHEMA
from fpga_convolution import FPGAConvolution
from pose_conv_relay import PoseConvRelay

MAX_JSON_BYTES = 8 * 1024 * 1024
COUNTDOWN_SECONDS = 3.0


class FPGAPoseServer:
    def __init__(self, camera="/dev/video0", host="0.0.0.0", port=8081,
                 page_path="fpga_multiaction_pose.html", bit_path="overlays/pose_distance.bit",
                 ip_name="pose_distance_0", kernel_timeout=2.0, width=640, height=360,
                 clock=None):
        self.camera, self.host, self.port = camera, host, int(port)
        self.width, self.height = int(width), int(height)
        self.page_path = Path(page_path).resolve()
        self.dataset_path = self.page_path.parent / "data" / "archive.zip"
        self.classifier = FPGAKNNClassifier(bit_path, ip_name, kernel_timeout)
        self.image = None
        self.counter = TrainingSession(clock or time.monotonic)
        self.ai_coach = AICoachBroker()
        self.convolution = None
        self.pose_relay = PoseConvRelay(self)
        self._profile = None
        self._camera_service = None
        self._server = self._http_thread = None
        self._running = False
        self._startup_error = None
        self._lifecycle_lock = threading.Lock()
        self._api_lock = threading.RLock()
        self._monitor_frame_floor = 0
        self._monitor_capture_floor = 0.0
        self._clock = clock or time.monotonic
        self._countdown_deadline = None
        self._countdown_monitor_id = None

    def _cancel_countdown(self):
        self._countdown_deadline = None
        self._countdown_monitor_id = None

    def _countdown(self, now=None):
        """Monotonic countdown; expiration never changes the capture floor."""
        now = self._clock() if now is None else now
        state = self.counter.snapshot()
        deadline = self._countdown_deadline
        valid = (deadline is not None and state["active"] and not state["paused"]
                 and not state.get("finished") and state["monitor_id"] == self._countdown_monitor_id)
        remaining = max(0.0, deadline - now) if valid else 0.0
        return {"active": bool(valid and remaining > 0.0), "seconds": COUNTDOWN_SECONDS,
                "remaining_seconds": remaining, "deadline": deadline if valid else None,
                "server_time": now, "monitor_id": state["monitor_id"]}

    def _exercise_snapshot(self, now=None):
        with self.counter._lock:
            snapshot = self.counter.snapshot()
            countdown = self._countdown(now)
            snapshot.update({"countdown_active": countdown["active"],
                             "countdown_remaining_seconds": countdown["remaining_seconds"],
                             "countdown_deadline": countdown["deadline"]})
            if countdown["active"]:
                snapshot.update({"status": "countdown", "phase": "countdown",
                                 "reason": "准备开始，三秒倒计时期间不计数"})
            return snapshot

    def _hardware_fault(self, reason):
        self.classifier.mark_unavailable(reason)
        self.ai_coach.invalidate()
        self._cancel_countdown()
        self.counter.invalidate("硬件故障，监测已停止：%s" % reason, preserve_count=True)

    def status(self):
        camera = self._camera_service
        result = (camera.status() if camera else
                  {"running": False, "camera": str(self.camera), "frame_id": 0,
                   "width": 0, "height": 0, "frame_age_seconds": None, "error": self._startup_error})
        result["running"] = bool(self._running and result["running"])
        result["project_version"] = "parallel-ai-conv-20261028"
        result["convolution"] = self.convolution.status() if self.convolution else {"ready":False}
        result["pose_relay"] = self.pose_relay.snapshot()
        result["hardware"] = self.classifier.hardware_status()
        result["image"] = (self.image.status() if self.image else
                           {"ready": False, "backend": "fpga", "self_test_passed": False,
                            "pixel_pipeline": PIXEL_PIPELINE, "error": self._startup_error or "图像 PL 尚未初始化"})
        result["ready"] = bool(result["hardware"]["ready"] and result["image"]["ready"])
        # HTTP snapshot/control/predict share this lock. The image fault callback
        # never acquires it, avoiding a lock cycle with an active DMA operation.
        with self._api_lock:
            now = self._clock()
            result["exercise"] = self._exercise_snapshot(now)
            result["countdown"] = self._countdown(now)
            result["frame_seq_floor"] = self._monitor_frame_floor
            result["capture_time_floor"] = self._monitor_capture_floor
        result["counter"] = result["exercise"]
        result["exercise_profile"] = self._profile
        result["ai"] = self.ai_coach.snapshot()
        if self._startup_error:
            result["error"] = self._startup_error
        return result

    def _pipeline_ready(self):
        if not self._running:
            raise FPGAError("服务尚未启动或正在停止")
        if not self.image or not self.image.status()["ready"] or not self.classifier._ready:
            raise FPGAError(self._startup_error or (self.image.status()["error"] if self.image else None)
                            or self.classifier._error or "完整 FPGA 管线未就绪")

    def upload_model(self, body):
        if not isinstance(body, dict):
            raise ValueError("模型必须为 JSON 对象")
        clean = validate_model(body)
        catalog = EXERCISE_CATALOG[clean["exerciseId"]]
        supplied = body.get("exercise_profile", {})
        if not isinstance(supplied, dict):
            raise ValueError("exercise_profile 必须为对象")
        for key in ("exercise_id", "name", "start_label", "target_label"):
            if key in supplied and supplied[key] != catalog[key]:
                raise ValueError("运动资料必须与已部署数据集运动目录一致，不能修改动作阶段")
        temporary = ExerciseCounter()
        temporary.start("validate", catalog["name"], catalog["start_label"], catalog["target_label"],
                        supplied.get("dwell_seconds", 0.25), supplied.get("max_gap_seconds", 1.5))
        profile = dict(catalog, dwell_seconds=temporary.dwell, max_gap_seconds=temporary.max_gap)
        with self._api_lock:
            self._pipeline_ready()
            # Validation succeeded; BRAM writes invalidate previous monitor even
            # if deployment itself fails midway.
            self.counter.invalidate()
            self.ai_coach.invalidate()
            self._cancel_countdown()
            self._profile = None
            result = self.classifier.upload_model(clean)
            self._profile = profile
            result["exercise_profile"] = profile
            result["exercise"] = self._exercise_snapshot()
            result["counter"] = result["exercise"]
            result["countdown"] = self._countdown()
            return result

    def monitor(self, body, specific_action=None):
        if not isinstance(body, dict):
            raise ValueError("监测请求必须为 JSON 对象")
        action = specific_action or body.get("action")
        with self._api_lock:
            if action not in ("cancel", "stop"):
                self._pipeline_ready()
            if action == "start":
                model_id = body.get("model_id")
                if self.classifier._model is None or model_id != self.classifier._model_id:
                    raise FPGAError("请先部署当前运动模型", 409)
                profile = self._profile
                if not profile:
                    raise FPGAError("运动目录未绑定当前模型，请重新部署", 409)
                for key in ("exercise_id", "name", "start_label", "target_label",
                            "dwell_seconds", "max_gap_seconds"):
                    if key in body and body[key] != profile[key]:
                        raise ValueError("开始监测必须使用已部署模型的运动资料")
                if "exercise_name" in body and body["exercise_name"] != profile["name"]:
                    raise ValueError("运动名称与已部署模型不匹配")
                snapshot = self.counter.start(model_id, profile["name"],
                    profile["start_label"], profile["target_label"],
                    profile["dwell_seconds"], profile["max_gap_seconds"])
                snapshot = self.counter.configure_training(profile.get("mode", "count"), body.get("duration_seconds", 60))
            elif action in ("cancel", "stop"):
                self.counter.require_monitor(body.get("monitor_id"))
                snapshot = self.counter.invalidate("监测已取消；请重新开始", preserve_count=True)
            else:
                snapshot = self.counter.control(action, body.get("monitor_id"))
            # A control operation cannot count predictions from older frames.
            camera = self._camera_service
            now = self._clock()
            self._cancel_countdown()
            if action in ("start", "resume"):
                self._countdown_deadline = now + COUNTDOWN_SECONDS
                self._countdown_monitor_id = snapshot["monitor_id"]
            self._monitor_capture_floor = (self._countdown_deadline
                                           if self._countdown_deadline is not None else now)
            self._monitor_frame_floor = camera.status()["frame_id"] if camera else 0
            snapshot = self._exercise_snapshot(now)
            self.ai_coach.invalidate()
            return {"backend": "fpga", "monitor_id": snapshot["monitor_id"],
                    "exercise": snapshot, "counter": snapshot,
                    "countdown": self._countdown(now),
                    "frame_seq_floor": self._monitor_frame_floor,
                    "capture_time_floor": self._monitor_capture_floor}

    def evaluate_ai(self, body):
        """Enqueue a summary; no Internet operation or LLM runs under this lock."""
        if not isinstance(body, dict) or not isinstance(body.get("telemetry"), dict):
            raise ValueError("请提供本次运动的测量统计")
        with self._api_lock:
            self._pipeline_ready()
            self.counter.require_monitor(body.get("monitor_id"))
            state = self.counter.snapshot()
            if self._countdown()["active"] or not self._profile:
                raise ValueError("准备结束并获得有效运动记录后再评估")
            measurement = body["telemetry"]
            if measurement.get("exercise_id") != self._profile["exercise_id"]:
                raise ValueError("测量统计的运动类型与当前模型不一致")
            hold = state.get("training_mode") == "hold"
            window_reps = measurement.get("confirmed_completions_in_window")
            if hold:
                if state.get("effective_hold_seconds", 0) < 3:
                    raise ValueError("至少累计3秒有效保持时间后再评估")
                window_reps = 0
            elif (isinstance(window_reps, bool) or not isinstance(window_reps, int)
                    or not 1 <= window_reps <= min(state["count"], 50)):
                # A finished session may have older completions outside the
                # browser's recent angle window. Keep that distinction explicit.
                if state.get("finished") and state["count"] > 0:
                    window_reps = 0
                else:
                    raise ValueError("当前统计窗口没有已确认完整动作，请继续运动后再评估")
            accepted_frames = measurement.get("accepted_frames")
            if (isinstance(accepted_frames, bool) or not isinstance(accepted_frames, int) or accepted_frames < 3
                    or not isinstance(measurement.get("metrics"), dict) or not measurement["metrics"]):
                raise ValueError("可用关节测量不足，暂不能评估姿态")
            telemetry = {"schema": TELEMETRY_SCHEMA,
                "exercise_id": self._profile["exercise_id"],
                "monitor_id": state["monitor_id"],
                "session_id": getattr(self.classifier, "session_id", "board"),
                "completed_count": state["count"], "window_reps": window_reps,
                "measurement": measurement,
                "training": {"mode": state.get("training_mode", "count"),
                    "duration_seconds": state.get("duration_seconds"),
                    "effective_hold_seconds": state.get("effective_hold_seconds", 0),
                    "elapsed_seconds": state.get("elapsed_seconds", 0),
                    "finished": state.get("finished", False)}}
            return self.ai_coach.submit(telemetry)

    def predict(self, body):
        request_started = time.monotonic()
        if not isinstance(body, dict):
            raise ValueError("预测请求必须为 JSON 对象")
        with self._api_lock:
            self._pipeline_ready()
            if self._countdown()["active"]:
                raise FPGAError("三秒倒计时尚未结束，请等待服务端确认并使用之后的新画面", 409)
            monitor_id = body.get("monitor_id")
            record = None
            if monitor_id:
                self.counter.require_monitor(monitor_id)
                if self.counter.snapshot()["paused"]:
                    raise FPGAError("监测已暂停，请继续后重新等待三秒并保持起始姿态", 409)
                if body.get("model_id") != self.counter.model_id:
                    raise FPGAError("监测模型不匹配", 409)
                if self._camera_service is None:
                    raise FPGAError("摄像头尚未提供经过 PL 处理的帧")
                record = self._camera_service.frame_record(body.get("frame_seq"))
                if (record["frame_seq"] <= self._monitor_frame_floor
                        or record["capture_time"] <= self._monitor_capture_floor):
                    raise FPGAError("请使用开始/继续/重置监测之后的新画面", 409)
                state = self.counter.snapshot()
                if record["frame_seq"] <= state["last_frame_seq"]:
                    raise FPGAError("拒绝重复或乱序画面", 409)
                captured = record["capture_time"]
                if (isinstance(captured, bool) or not isinstance(captured, (int, float))
                        or not math.isfinite(captured)
                        or (state["last_capture_time"] is not None
                            and captured <= state["last_capture_time"])):
                    raise FPGAError("拒绝无效或乱序采集时间", 409)
            if body.get("features") is None:
                if not monitor_id:
                    raise ValueError("features 为空只用于当前监测会话报告跟踪丢失")
                result = {"label": None, "reason": "人体关键点不完整，暂停推进动作阶段",
                          "backend": "fpga", "model_id": body.get("model_id"),
                          "hardware_completed": False, "inference_count": self.classifier._inference_count}
            else:
                try:
                    if self.convolution is not None and not self.pose_relay.has_completed(body.get("frame_seq")):
                        raise FPGAError("本帧未完成 FPGA 输出卷积，请刷新网页并启动电脑卷积助手", 409)
                    result = self.classifier.predict(body.get("model_id"), body.get("features"))
                except Exception:
                    if not self.classifier._ready and self.image:
                        self.image._fault("分类 PL 故障，共享 FPGA 管线已停止；请重新加载位流")
                        self._cancel_countdown()
                        self.counter.invalidate("硬件故障，监测已停止", preserve_count=True)
                    raise
            if monitor_id:
                snapshot = self.counter.observe(monitor_id, body.get("model_id"),
                    record["frame_seq"], record["capture_time"], result.get("label"),
                    result.get("hardware_completed") is True)
                snapshot = self._exercise_snapshot()
                result.update({"exercise": snapshot, "counter": snapshot,
                               "frame_seq": record["frame_seq"], "capture_time": record["capture_time"],
                               "image": record["image"]})
            else:
                result["exercise"] = self._exercise_snapshot()
                result["counter"] = result["exercise"]
            result["countdown"] = self._countdown()
            result["server_predict_ms"] = (time.monotonic() - request_started) * 1000
            return result

    def start(self):
        with self._lifecycle_lock:
            if self._running:
                raise RuntimeError("服务已运行，请先执行 fpga_server.stop()")
            if self._camera_service and self._camera_service._capture_thread and self._camera_service._capture_thread.is_alive():
                raise RuntimeError("旧摄像头线程仍在运行，不能重新加载位流")
            if not self.page_path.is_file():
                raise FileNotFoundError("缺少网页：%s" % self.page_path)
            owner = self

            class Handler(BaseHTTPRequestHandler):
                def setup(self):
                    super().setup()
                    self.connection.settimeout(15)

                def log_message(self, fmt, *args):
                    pass

                def _send(self, code, body, content_type, extra=None):
                    try:
                        self.send_response(code)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        for key, value in (extra or {}).items():
                            self.send_header(key, str(value))
                        self.end_headers()
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError, socket.timeout):
                        pass

                def _json(self, code, value):
                    self._send(code, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                               "application/json; charset=utf-8")

                def _error(self, code, message):
                    status = owner.status()
                    self._json(code, {"backend": "fpga", "error": message, "reason": message,
                                      "ready": status["ready"], "hardware": status["hardware"],
                                      "image": status["image"], "exercise": status["exercise"],
                                      "counter": status["exercise"], "countdown": status["countdown"],
                                      "frame_seq_floor": status["frame_seq_floor"],
                                      "capture_time_floor": status["capture_time_floor"]})

                def do_GET(self):
                    path = urlsplit(self.path).path
                    if path in ("/", "/index.html"):
                        self._send(200, owner.page_path.read_bytes(), "text/html; charset=utf-8")
                    elif path in ("/status", "/api/status"):
                        self._json(200, owner.status())
                    elif path == "/api/ai/status":
                        self._json(200, owner.ai_coach.snapshot())
                    elif path == "/api/hold-model":
                        exercise = parse_qs(urlsplit(self.path).query).get("exercise", [""])[0]
                        models_path = owner.page_path.parent / "data" / "all_models.json"
                        if exercise not in EXERCISE_CATALOG or not models_path.is_file():
                            self._send(404, b"Hold model not found", "text/plain")
                        else:
                            models = json.loads(models_path.read_text(encoding="utf-8"))
                            self._json(200, validate_model(models[exercise]))
                    elif path == "/api/dataset":
                        if owner.dataset_path.is_file():
                            self._send(200, owner.dataset_path.read_bytes(), "application/zip",
                                       {"Content-Disposition": 'attachment; filename="archive.zip"'})
                        else:
                            self._send(404, b"Bundled dataset missing", "text/plain; charset=utf-8")
                    elif path == "/api/hardware":
                        result = owner.classifier.hardware_status()
                        result["image"] = owner.image.status() if owner.image else {"ready": False}
                        self._json(200, result)
                    elif path in ("/frame.jpg", "/api/frame"):
                        try:
                            if owner._camera_service is None:
                                raise FPGAError(owner._startup_error or "摄像头未启动")
                            jpeg, seq, stamp, evidence = owner._camera_service.latest()
                            self._send(200, jpeg, "image/jpeg", {"X-Frame-Seq": seq, "X-Frame-Id": seq,
                                "X-Capture-Time": "%.9f" % stamp, "X-Pixel-Pipeline": PIXEL_PIPELINE,
                                "X-Frame-Age-Ms": "%.3f" % max(0, (time.monotonic() - stamp) * 1000),
                                "X-Image-Completed": evidence["completed_count"],
                                "X-Image-Cycles": evidence["last_cycles"]})
                        except FPGAError as exc:
                            self._error(exc.status, str(exc))
                    else:
                        self._send(404, b"Not found", "text/plain; charset=utf-8")

                def _body(self):
                    if self.headers.get("Transfer-Encoding"):
                        raise FPGAError("不支持分块 JSON 上传", 400)
                    if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                        raise FPGAError("请求必须为 application/json", 415)
                    value = self.headers.get("Content-Length")
                    if value is None:
                        raise FPGAError("请求缺少 Content-Length", 411)
                    try:
                        length = int(value)
                    except ValueError as exc:
                        raise FPGAError("Content-Length 无效", 400) from exc
                    if not 1 <= length <= MAX_JSON_BYTES:
                        raise FPGAError("JSON 请求长度必须为 1～8 MiB", 413)
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise FPGAError("JSON 请求未完整接收", 400)
                    def invalid_constant(value):
                        raise ValueError("JSON 不允许 %s" % value)
                    return json.loads(raw.decode("utf-8"), parse_constant=invalid_constant)

                def do_POST(self):
                    path = urlsplit(self.path).path
                    actions = {"/api/exercise/" + action: action for action in ("start", "pause", "resume", "reset", "cancel", "stop")}
                    pose_paths = ("/api/pose/submit", "/api/pose/relay/poll", "/api/pose/relay/conv", "/api/pose/relay/result")
                    ai_paths = ("/api/ai/config", "/api/ai/evaluate", "/api/ai/relay/poll", "/api/ai/relay/result")
                    if path not in ("/api/model", "/api/predict", "/api/monitor") + ai_paths + pose_paths and path not in actions:
                        self._send(404, b"Not found", "text/plain; charset=utf-8")
                        return
                    try:
                        body = self._body()
                        if not isinstance(body, dict):
                            raise ValueError("请求必须为 JSON 对象")
                        if path == "/api/pose/submit":
                            result = owner.pose_relay.submit(body)
                        elif path == "/api/pose/relay/poll":
                            result = owner.pose_relay.poll(body)
                        elif path == "/api/pose/relay/conv":
                            result = owner.pose_relay.convolve(body)
                        elif path == "/api/pose/relay/result":
                            result = owner.pose_relay.complete(body)
                        elif path == "/api/ai/config":
                            result = owner.ai_coach.configure(body.get("enabled"))
                        elif path == "/api/ai/evaluate":
                            result = owner.evaluate_ai(body)
                        elif path == "/api/ai/relay/poll":
                            result = owner.ai_coach.relay_poll(body.get("token"))
                        elif path == "/api/ai/relay/result":
                            result = owner.ai_coach.relay_result(body.get("token"), body)
                        elif path == "/api/model":
                            result = owner.upload_model(body)
                        elif path == "/api/predict":
                            result = owner.predict(body)
                        else:
                            result = owner.monitor(body, actions.get(path))
                        self._json(200, result)
                    except FPGAError as exc:
                        self._error(exc.status, str(exc))
                    except CounterError as exc:
                        self._error(409, str(exc))
                    except (ValueError, UnicodeError) as exc:
                        self._error(400, str(exc))
                    except socket.timeout:
                        self._error(408, "JSON 请求接收超时")
                    except Exception as exc:
                        self._error(500, "服务请求失败：%s" % exc)

            try:
                server = ThreadingHTTPServer((self.host, self.port), Handler)
            except OSError as exc:
                raise RuntimeError("端口被占用或无法监听：%s" % exc) from exc
            server.daemon_threads = True
            self._startup_error = None
            self._cancel_countdown()
            self.ai_coach.invalidate()
            self.counter.invalidate("请先导入 CSV 数据集并部署运动模型")
            try:
                # Reject old overlays BEFORE importing PYNQ/downloading PL.
                validate_v2_hwh(self.classifier.bit_path.with_suffix(".hwh"))
                hardware = self.classifier.initialize()
                if not hardware["ready"]:
                    raise FPGAError(hardware["error"])
                self.image = FPGAImageProcessor(self.classifier._overlay,
                    self.classifier.kernel_timeout, self._hardware_fault)
                state = self.image.initialize(after_overlay_download=True)
                if not state["ready"]:
                    raise FPGAError(state["error"])
                self.convolution = FPGAConvolution(self.classifier._overlay, self.page_path.parent/"models/mediapipe_conv",
                    self.classifier.kernel_timeout, self._hardware_fault)
                self.convolution.initialize()
                self._camera_service = PoseCameraServer(self.image, self.camera, self.width, self.height)
                self._camera_service.start()
            except Exception as exc:
                self._startup_error = str(exc)
                self.classifier.mark_unavailable(self._startup_error)
                if self.image:
                    self.image._ready = False
                # The diagnostic webpage remains available, no raw CPU frames.
            self._server = server
            self.port = server.server_address[1]
            self._running = True
            self._http_thread = threading.Thread(target=server.serve_forever,
                kwargs={"poll_interval": 0.1}, name="FPGA-exercise-http", daemon=True)
            self._http_thread.start()
        return self

    def stop(self):
        with self._lifecycle_lock:
            self._running = False
            self.ai_coach.close()
            self.pose_relay.close()
            if self._server:
                try:
                    if self._http_thread and self._http_thread.is_alive():
                        self._server.shutdown()
                finally:
                    self._server.server_close()
                    self._server = None
            if self._http_thread:
                self._http_thread.join(timeout=2)
                self._http_thread = None
            # A blocked native camera read cannot safely release active DMA.
            if self._camera_service:
                self._camera_service.stop()
                self._camera_service = None
            with self._api_lock:
                if self.image:
                    self.image.close()
                    self.image = None
                if self.convolution:
                    self.convolution.close()
                    self.convolution = None
                self.classifier.close()
                self._cancel_countdown()
                self.counter.invalidate("服务已停止")
        return self


def main():
    parser = argparse.ArgumentParser(description="PYNQ 数据集版：PL 图像与姿态距离、三秒准备和运动计数")
    parser.add_argument("--camera", default="/dev/video0")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--page", default="fpga_multiaction_pose.html")
    parser.add_argument("--bit", default="overlays/pose_distance.bit")
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args()
    service = FPGAPoseServer(args.camera, args.host, args.port, args.page,
                            args.bit, kernel_timeout=args.timeout)
    try:
        service.start()
        print(json.dumps(service.status(), ensure_ascii=False, indent=2), flush=True)
        print("网页：http://开发板IP:%d/；Ctrl-C 停止" % service.port, flush=True)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()


if __name__ == "__main__":
    main()
