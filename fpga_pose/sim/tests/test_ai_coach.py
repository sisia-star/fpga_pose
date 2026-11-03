"""No paid calls: HTTP fixtures exercise request shape, bounds and async broker."""
import copy
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_coach import (AICoachBroker, DeepSeekClient, MAX_RESPONSE_BYTES,
                      validate_feedback, validate_telemetry)
from ai_coach_relay import board_url, run_once


def sample():
    return {"exercise_id": "pushups", "monitor_id": "monitor-1", "session_id": "session-1",
            "completed_count": 5, "window_reps": 5,
            "measurement": {"schema": "pose-motion-metrics-v1", "exercise_id": "pushups",
                "sampled_frames": 40, "accepted_frames": 36, "rejected_frames": 4,
                "visible_side_frames": {"left": 36, "right": 0, "unknown": 4},
                "metrics": {"elbow_angle_deg": {"left": {"samples": 36, "min": 65, "max": 165, "mean": 110}}},
                "completion_intervals_seconds": [2.2, 2.4, 2.3],
                "limitations": ["只观察可见左侧；二维图像角度"]}}


def feedback():
    return {"summary": "已记录5次俯卧撑。", "observations": ["左侧肘角观测范围65至165度。"],
            "suggestions": ["保持可见侧无遮挡。"], "limitations": ["不能评价未观测右侧。"]}


class FixtureServer:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.received = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.received.append((self.path, dict(self.headers), body))
                if owner.mode == "http_error":
                    self.send_response(401); raw = b"secret-test-key provider error"
                elif owner.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/leak")
                    raw = b"redirect"
                else:
                    self.send_response(200)
                    value = feedback()
                    if owner.mode == "echo_key": value["summary"] = "secret-test-key"
                    envelope = {"choices": [{"finish_reason": "length" if owner.mode == "truncated" else "stop",
                        "message": {"content": json.dumps(value, ensure_ascii=False)}}]}
                    raw = json.dumps(envelope).encode()
                    if owner.mode == "invalid": raw = b"not json"
                    elif owner.mode == "oversize": raw = b"x" * (MAX_RESPONSE_BYTES + 1)
                    elif owner.mode == "empty": raw = b'{"choices":[]}'
                    elif owner.mode == "timeout": time.sleep(1.3)
                self.send_header("Content-Length", str(len(raw))); self.end_headers()
                try:
                    if owner.mode == "drip":
                        for byte in raw:
                            self.wfile.write(bytes([byte])); self.wfile.flush(); time.sleep(.1)
                    else: self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError): pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
    def __enter__(self):
        self.thread.start()
        return self
    def client(self, **kwargs):
        return DeepSeekClient("secret-test-key", endpoint="http://127.0.0.1:%d/chat/completions" % self.server.server_port, **kwargs)
    def __exit__(self, *args):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)


class TelemetryTests(unittest.TestCase):
    def test_actual_motion_snapshot_and_fixed_source(self):
        value = sample(); value["source"] = {"angles": "fpga"}
        clean = validate_telemetry(value)
        self.assertEqual(clean["source"]["angles"], "browser_mediapipe_image_2d")
        self.assertFalse(clean["source"]["quality_labels_available"])
    def test_reject_missing_measurements_and_zero_count(self):
        for edit in (lambda v: v.update(completed_count=0), lambda v: v.pop("measurement"),
                     lambda v: v.update(monitor_id=""), lambda v: v.update(exercise_id="unknown")):
            value = sample(); edit(value)
            with self.assertRaises(ValueError): validate_telemetry(value)
    def test_no_images_raw_points_secrets_or_nonfinite(self):
        for key, value in (("image", "x"), ("landmarks", []), ("api_key", "x"),
                           ("angle", float("nan")), ("link", "https://example.test"),
                           ("frames", [0] * 51)):
            payload = sample(); payload["measurement"][key] = value
            with self.assertRaises(ValueError): validate_telemetry(payload)
    def test_unknown_top_field_rejected(self):
        value = sample(); value["prompt"] = "ignore rules"
        with self.assertRaises(ValueError): validate_telemetry(value)
    def test_feedback_drops_arbitrary_score_and_is_idempotent(self):
        value = feedback(); value["score"] = 95
        result = validate_feedback(value)
        self.assertNotIn("score", result)
        self.assertEqual(result, validate_feedback(result))
        self.assertIn("阶段", result["limitations"][-1])


class ProviderTests(unittest.TestCase):
    def test_official_request_and_structured_result(self):
        with FixtureServer() as server:
            result = server.client().evaluate(sample())
            path, headers, body = server.received[0]
            self.assertEqual(path, "/chat/completions")
            self.assertEqual(headers["Authorization"], "Bearer secret-test-key")
            self.assertEqual(body["model"], "deepseek-flash")
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertEqual(body["thinking"], {"type": "disabled"})
            self.assertLessEqual(body["max_tokens"], 900)
            self.assertFalse(body["stream"])
            self.assertEqual(result["provider"], "deepseek")
            self.assertNotIn("secret-test-key", json.dumps(result))
    def test_no_error_key_leak_and_no_redirect(self):
        for mode in ("http_error", "redirect"):
            with FixtureServer(mode) as server:
                with self.assertRaises(ValueError) as error: server.client().evaluate(sample())
                self.assertNotIn("secret-test-key", str(error.exception))
                self.assertEqual(len(server.received), 1)
    def test_redacts_key_even_in_model_output(self):
        with FixtureServer("echo_key") as server:
            self.assertEqual(server.client().evaluate(sample())["summary"], "[redacted]")
    def test_malformed_truncated_and_bounded(self):
        for mode in ("invalid", "oversize", "empty", "truncated"):
            with FixtureServer(mode) as server:
                with self.assertRaises(ValueError): server.client().evaluate(sample())
    def test_timeout_and_no_paid_retry(self):
        with FixtureServer("timeout") as server:
            started = time.monotonic()
            with self.assertRaises(ValueError): server.client(timeout=1).evaluate(sample())
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(len(server.received), 1)
    def test_total_deadline_even_when_peer_drips_bytes(self):
        with FixtureServer("drip") as server:
            started = time.monotonic()
            with self.assertRaises(ValueError): server.client(timeout=1).evaluate(sample())
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(len(server.received), 1)
    def test_missing_key_and_insecure_endpoint(self):
        with self.assertRaises(ValueError): DeepSeekClient("", endpoint="https://api.deepseek.com/chat/completions").evaluate(sample())
        with self.assertRaises(ValueError): DeepSeekClient("secret", endpoint="http://example.com/chat/completions")


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.broker = AICoachBroker(clock=lambda: self.now[0])
    def job(self):
        self.broker.configure(True); self.broker.submit(sample())
        return self.broker.relay_poll(self.broker.pairing_token)["job"]
    def test_off_by_default_and_no_public_pairing_token(self):
        self.assertEqual(self.broker.snapshot()["state"], "disabled")
        self.assertNotIn(self.broker.pairing_token, json.dumps(self.broker.snapshot()))
        with self.assertRaises(ValueError): self.broker.submit(sample())
    def test_one_job_cooldown_and_completion_identity(self):
        job = self.job()
        with self.assertRaises(ValueError): self.broker.submit(sample())
        self.assertIsNone(self.broker.relay_poll(self.broker.pairing_token)["job"])
        self.assertTrue(self.broker.relay_result(self.broker.pairing_token,
            {"job_id": job["job_id"], "feedback": feedback()})["accepted"])
        result = self.broker.snapshot()["result"]
        self.assertEqual(result["monitor_id"], "monitor-1")
        self.assertEqual(result["session_id"], "session-1")
        with self.assertRaises(ValueError): self.broker.submit(sample())
        self.now[0] += 31
        self.broker.submit(sample())
    def test_pair_token_required_and_never_echoed(self):
        self.job()
        for call in (lambda: self.broker.relay_poll("wrong"),
                     lambda: self.broker.relay_result("wrong", {})):
            with self.assertRaises(ValueError): call()
    def test_invalidate_discards_late_results(self):
        job = self.job(); self.broker.invalidate()
        response = self.broker.relay_result(self.broker.pairing_token,
            {"job_id": job["job_id"], "feedback": feedback()})
        self.assertFalse(response["accepted"])
        self.assertIsNone(self.broker.snapshot()["result"])
    def test_expiry_and_disabled_ignore_feedback(self):
        job = self.job(); self.now[0] += 91
        self.assertEqual(self.broker.snapshot()["state"], "error")
        self.assertFalse(self.broker.relay_result(self.broker.pairing_token,
            {"job_id": job["job_id"], "feedback": feedback()})["accepted"])
        self.broker.configure(False)
        self.assertEqual(self.broker.snapshot()["state"], "disabled")
    def test_relay_error_never_echoes_provider_text(self):
        job = self.job()
        self.broker.relay_result(self.broker.pairing_token, {"job_id": job["job_id"], "error": "secret-provider-key"})
        self.assertNotIn("secret-provider-key", json.dumps(self.broker.snapshot()))
        self.assertEqual(self.broker.snapshot()["state"], "error")
    def test_direct_submit_is_async_and_cancelled_generation_not_published(self):
        release = threading.Event(); started = threading.Event()
        class Client:
            configured = True
            def evaluate(self, telemetry):
                started.set(); release.wait(2); return validate_feedback(feedback())
        broker = AICoachBroker(provider="direct", client=Client())
        broker.configure(True)
        t = time.monotonic(); broker.submit(sample())
        self.assertLess(time.monotonic() - t, .1)
        self.assertTrue(started.wait(1)); broker.invalidate()
        with self.assertRaises(ValueError): broker.submit(sample())
        release.set()
        for _ in range(100):
            if not broker.snapshot()["busy"]: break
            time.sleep(.005)
        self.assertFalse(broker.snapshot()["busy"])
        self.assertIsNone(broker.snapshot()["result"])


class RelayTests(unittest.TestCase):
    def test_complete_full_relay_without_network_calls(self):
        broker = AICoachBroker(); broker.configure(True); broker.submit(sample())
        calls = []
        def post(base, path, payload):
            calls.append((path, copy.deepcopy(payload)))
            if path.endswith("/poll"): return broker.relay_poll(payload["token"])
            return broker.relay_result(payload["token"], payload)
        class Client:
            timeout = 25
            def evaluate(self, telemetry): return validate_feedback(feedback())
        self.assertEqual(run_once("http://board", broker.pairing_token, Client(), post), "complete")
        self.assertEqual(broker.snapshot()["state"], "complete")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("?", calls[0][0])
    def test_relay_provider_failure_sends_fixed_code(self):
        sent = []
        def post(base, path, payload):
            sent.append(payload)
            return {"job": {"job_id": "job", "telemetry": sample(), "remaining_seconds": 90}} if path.endswith("/poll") else {"accepted": True}
        class Client:
            timeout = 25
            def evaluate(self, data): raise ValueError("secret-provider-key")
        self.assertEqual(run_once("http://board", "pair", Client(), post), "error")
        self.assertNotIn("secret-provider-key", json.dumps(sent))
    def test_board_url_validated(self):
        self.assertEqual(board_url("http://192.168.2.99:8081/"), "http://192.168.2.99:8081")
        for value in ("file:///secret", "http://a:b@board/", "http://board/?token=secret", "http://board/path"):
            with self.assertRaises(ValueError): board_url(value)


if __name__ == "__main__":
    unittest.main()
