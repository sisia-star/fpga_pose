"""Board/broker integration using named fake hardware, never a real FPGA/API."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fpga_pose_server import FPGAPoseServer
import test_board_dataset as board_fixtures


def measurement():
    return {"schema": "pose-motion-metrics-v1", "exercise_id": "squats",
            "sampled_frames": 10, "accepted_frames": 9,
            "confirmed_completions_in_window": 2,
            "metrics": {"knee_angle_deg": {"left": {"min": 80, "max": 170, "samples": 9}}}}


class CoachBoardIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = board_fixtures.CountdownServerTests()
        self.fixture.setUp()
        self.s = self.fixture.s
        self.fixture.clock.now = 110
        self.s.counter.count = 7
        self.s.ai_coach.configure(True)

    def submit(self, data=None):
        return self.s.evaluate_ai({"monitor_id": self.fixture.mid,
                                   "completed_count": 9999,
                                   "telemetry": data if data is not None else measurement()})

    def test_board_count_and_window_count_are_distinct_and_authoritative(self):
        state = self.submit()
        self.assertEqual(state["state"], "queued")
        job = self.s.ai_coach.relay_poll(self.s.ai_coach.pairing_token)["job"]
        self.assertEqual(job["telemetry"]["completed_count"], 7)
        self.assertEqual(job["telemetry"]["window_reps"], 2)
        self.assertEqual(job["telemetry"]["exercise_id"], "squats")

    def test_pending_cloud_job_does_not_block_local_prediction(self):
        self.submit()
        before = self.fixture.calls
        reply = self.fixture.prediction(104.2)
        self.assertEqual(self.fixture.calls, before + 1)
        self.assertTrue(reply["hardware_completed"])
        self.assertIn("server_predict_ms", reply)
        self.assertEqual(self.s.ai_coach.snapshot()["state"], "queued")

    def test_reset_cancels_old_reply_and_keeps_ai_opt_in(self):
        self.submit()
        token = self.s.ai_coach.pairing_token
        job = self.s.ai_coach.relay_poll(token)["job"]
        self.s.monitor({"action": "reset", "monitor_id": self.fixture.mid})
        value = self.s.ai_coach.relay_result(token, {"job_id": job["job_id"],
            "feedback": {"summary": "stale", "observations": [], "suggestions": [], "limitations": []}})
        self.assertFalse(value["accepted"])
        self.assertIsNone(self.s.ai_coach.snapshot()["result"])
        self.assertTrue(self.s.ai_coach.snapshot()["enabled"])

    def test_wrong_exercise_session_empty_metrics_or_bad_window_rejected(self):
        mutations = [{"exercise_id": "pushups"}, {"confirmed_completions_in_window": 0},
                     {"confirmed_completions_in_window": 8}, {"accepted_frames": "many"},
                     {"accepted_frames": True}, {"metrics": {}}, {"accepted_frames": 1}]
        for update in mutations:
            data = measurement(); data.update(update)
            with self.assertRaises(ValueError): self.submit(data)
        with self.assertRaises(Exception):
            self.s.evaluate_ai({"monitor_id": "old", "telemetry": measurement()})
        self.assertFalse(self.s.ai_coach.snapshot()["busy"])

    def test_disabled_ai_cannot_submit_but_counting_stays_available(self):
        self.s.ai_coach.configure(False)
        with self.assertRaises(ValueError): self.submit()
        self.assertTrue(self.fixture.prediction(104.2)["hardware_completed"])


class CoachHTTPIntegrationTests(unittest.TestCase):
    def test_diagnostic_server_ai_routes_and_json_object_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            page = Path(directory) / "index.html"; page.write_text("<p>offline diagnostic fixture</p>")
            service = FPGAPoseServer(host="127.0.0.1", port=0, page_path=str(page),
                                     bit_path=str(Path(directory) / "missing.bit")).start()
            base = "http://127.0.0.1:%s" % service.port
            def post(path, data):
                request = Request(base + path, data=json.dumps(data).encode(),
                                  headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(request, timeout=3) as response: return json.load(response)
            try:
                with urlopen(base + "/api/ai/status", timeout=3) as response:
                    self.assertEqual(json.load(response)["state"], "disabled")
                self.assertTrue(post("/api/ai/config", {"enabled": True})["enabled"])
                self.assertIsNone(post("/api/ai/relay/poll", {"token": service.ai_coach.pairing_token})["job"])
                for path, data in (("/api/ai/config", []), ("/api/ai/relay/poll", {"token": "bad"})):
                    with self.assertRaises(HTTPError) as caught: post(path, data)
                    self.assertEqual(caught.exception.code, 400)
                    caught.exception.close()
                with urlopen(base + "/api/status", timeout=3) as response:
                    status = json.load(response)
                self.assertFalse(status["ready"])
                self.assertNotIn(service.ai_coach.pairing_token, json.dumps(status))
                self.assertIn("ai", status)
            finally:
                service.stop()


if __name__ == "__main__": unittest.main()
