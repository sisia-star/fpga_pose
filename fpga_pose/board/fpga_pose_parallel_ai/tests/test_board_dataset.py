"""Dataset schema and authoritative countdown tests with named fake hardware.

These tests do not assert actual board timing, camera accuracy, or PL throughput.
No wall-clock sleeps are used for the three-second countdown.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fpga_classifier import (EXERCISE_CATALOG, FEATURE_SPEC, MODEL_SCHEMA, PUSHUPS_FEATURE_SPEC, SITUP_FEATURE_SPEC,
                             FPGAError, FPGAKNNClassifier, quantize, validate_model)
from fpga_pose_server import FPGAPoseServer
from exercise_counter import CounterError


def model_fixture(exercise="squats"):
    """A synthetic contract fixture; it is not an accuracy validation dataset."""
    catalog = EXERCISE_CATALOG[exercise]
    classes = [catalog["start_label"], catalog["target_label"]]
    rows = []
    for class_index, label in enumerate(classes):
        for index in range(10):
            features = [0.0] * 99
            features[2] = (index + class_index * 10) / 1000
            features[11 * 3 + 1] = features[12 * 3 + 1] = .16
            features[31 * 3] = 1.0
            if exercise in ("pushups", "situp"):
                features = [0.0] * 99
                features[0] = 2 + class_index * 4 + index / 1000
                features[1] = 3 + index / 1000
                features[2] = .8 + index / 1000
            rows.append({"sampleId": "%s_%s" % (label, index), "label": label,
                         "features": features})
    return {"schema": MODEL_SCHEMA, "featureSpec": {"pushups": PUSHUPS_FEATURE_SPEC, "situp": SITUP_FEATURE_SPEC}.get(exercise, FEATURE_SPEC), "exerciseId": exercise,
            "classes": classes, "k": 5, "minVoteFraction": .6,
            "classThresholds": dict.fromkeys(classes, .1), "rows": rows,
            "datasetMeta": {"sourceKind": "pose-csv", "sourceHash": "a" * 64,
                "coordinateProtocol": FEATURE_SPEC, "normalizedPoseSize": 100,
                "videoGroupsAvailable": False, "totalSamples": 30, "duplicateRowsRemoved": 0},
            "validation": {"method": "stratified-sample-holdout",
                "independentVideoValidation": False, "seed": 20261012, "splitFraction": .2,
                "correct": 8, "total": 10, "rejected": 2, "accuracy": .8,
                "coverage": .8, "acceptedAccuracy": 1.0, "labels": classes[:],
                "predictedLabels": classes + ["未确定"], "rejectedColumnIndex": 2,
                "confusionMatrix": [[4, 0, 1], [0, 4, 1]],
                "trainSampleIds": [row["sampleId"] for row in rows],
                "holdoutSampleIds": ["test_%s_%s" % (label, index)
                                      for label in classes for index in range(5)]}}


class ModelSchemaTests(unittest.TestCase):
    def test_five_catalog_models_valid_and_canonicalization_stable(self):
        for exercise in (k for k,v in EXERCISE_CATALOG.items() if v.get("mode") != "hold"):
            clean = validate_model(model_fixture(exercise))
            self.assertEqual(clean, validate_model(clean))
            self.assertIs(clean["validation"]["independentVideoValidation"], False)

    def test_legacy_schema_or_feature_protocol_rejected(self):
        for key, value in (("schema", "pose-knn-model-v1"),
                           ("featureSpec", "world33-hipcenter-torsoscale-v1")):
            model = model_fixture(); model[key] = value
            with self.assertRaises(ValueError): validate_model(model)

    def test_catalog_classes_order_and_exercise_are_strict(self):
        for mutation in (lambda m: m["classes"].reverse(),
                         lambda m: m.update(exerciseId="custom"),
                         lambda m: m.update(exerciseId="pushups")):
            model = model_fixture(); mutation(model)
            with self.assertRaises(ValueError): validate_model(model)

    def test_no_fake_video_groups_or_session_ids(self):
        mutations = [lambda m: m["rows"][0].update(sessionId="fake-video"),
                     lambda m: m["rows"][0].update(videoId="fake-video"),
                     lambda m: m["datasetMeta"].update(videoGroupsAvailable=True),
                     lambda m: m["validation"].update(independentVideoValidation=True),
                     lambda m: m["validation"].update(trainSessionIds=["fake"])]
        for mutation in mutations:
            model = model_fixture(); mutation(model)
            with self.assertRaises(ValueError): validate_model(model)

    def test_sample_ids_must_be_unique_ordered_and_nonoverlapping(self):
        mutations = [lambda m: m["rows"][1].update(sampleId=m["rows"][0]["sampleId"]),
                     lambda m: m["validation"]["trainSampleIds"].reverse(),
                     lambda m: m["validation"]["holdoutSampleIds"].__setitem__(0, m["rows"][0]["sampleId"]),
                     lambda m: m["validation"]["holdoutSampleIds"].pop()]
        for mutation in mutations:
            model = model_fixture(); mutation(model)
            with self.assertRaises(ValueError): validate_model(model)

    def test_metrics_matrix_and_fixed_split_contract_match(self):
        mutations = [lambda m: m["validation"].update(accuracy=.99),
                     lambda m: m["validation"].update(coverage=.99),
                     lambda m: m["validation"].update(acceptedAccuracy=.99),
                     lambda m: m["validation"].update(seed=1),
                     lambda m: m["validation"].update(splitFraction=.5),
                     lambda m: m["validation"].update(method="video-holdout"),
                     lambda m: m["validation"]["confusionMatrix"][0].__setitem__(0, 5)]
        for mutation in mutations:
            model = model_fixture(); mutation(model)
            with self.assertRaises(ValueError): validate_model(model)

    def test_dataset_metadata_bounded_and_provenance_hash_required(self):
        mutations = [lambda m: m["datasetMeta"].update(sourceHash="not-a-hash"),
                     lambda m: m["datasetMeta"].update(normalizedPoseSize=True),
                     lambda m: m["datasetMeta"].update(totalSamples=25),
                     lambda m: m["datasetMeta"].update(duplicateRowsRemoved=31),
                     lambda m: m["datasetMeta"].update(coordinateProtocol="world")]
        for mutation in mutations:
            model = model_fixture(); mutation(model)
            with self.assertRaises(ValueError): validate_model(model)

    def test_no_quantization_clipping_and_ties_away_from_zero(self):
        self.assertEqual(quantize([.5 / 2048, -.5 / 2048] + [0] * 97)[:2], [1, -1])
        for value in (16, -16.001, float("nan"), float("inf"), True):
            model = model_fixture(); model["rows"][0]["features"][0] = value
            with self.assertRaises(ValueError): validate_model(model)

    def test_wrong_hip_origin_or_pose_size_rejected(self):
        for index, value in ((23 * 3, .01), (31 * 3, 1.01)):
            model = model_fixture(); model["rows"][0]["features"][index] = value
            with self.assertRaises(ValueError): validate_model(model)

    def test_duplicate_coordinate_rows_rejected_before_model_write(self):
        model = model_fixture()
        model["rows"][1]["features"] = model["rows"][0]["features"][:]
        with self.assertRaises(ValueError): validate_model(model)
        model = model_fixture()
        model["rows"][10]["features"] = model["rows"][0]["features"][:]
        with self.assertRaises(ValueError): validate_model(model)

    def test_dataset_total_limit_matches_ten_thousand_importer_limit(self):
        model = model_fixture(); model["datasetMeta"]["totalSamples"] = 10000
        self.assertEqual(validate_model(model)["datasetMeta"]["totalSamples"], 10000)
        model["datasetMeta"]["totalSamples"] = 10001
        with self.assertRaises(ValueError): validate_model(model)

    def test_bad_live_coordinate_protocol_is_rejected_before_mmio(self):
        driver = FPGAKNNClassifier()
        driver._ready = True
        driver._model = model_fixture()
        driver._model_id = "model"
        def forbidden(*args): raise AssertionError("MMIO must not happen for invalid features")
        driver._ip = types.SimpleNamespace(read=forbidden, write=forbidden)
        with self.assertRaises(ValueError): driver.predict("model", [0.0] * 99)
        self.assertTrue(driver._ready)


class FakeClock:
    def __init__(self): self.now = 100.0
    def __call__(self): return self.now


class CountdownServerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.s = FPGAPoseServer(clock=self.clock)
        self.s._running = True
        self.s.image = types.SimpleNamespace(status=lambda: {"ready": True, "error": None})
        self.calls = 0
        self.current_label = "squats_up"
        def predict(model, features):
            self.calls += 1
            return {"label": self.current_label, "backend": "fpga", "hardware_completed": True}
        def upload(model):
            self.s.classifier._model = validate_model(model)
            self.s.classifier._model_id = "new-model"
            return {"backend": "fpga", "model_id": "new-model"}
        def mark(reason): self.s.classifier._ready = False
        self.s.classifier = types.SimpleNamespace(_ready=True, _error=None, _model_id="model",
            _model=model_fixture(), _inference_count=0, predict=predict, upload_model=upload,
            mark_unavailable=mark, hardware_status=lambda: {"ready": self.s.classifier._ready})
        self.s._profile = dict(EXERCISE_CATALOG["squats"], dwell_seconds=.2, max_gap_seconds=1.5)
        self.seq = 10
        self.records = {}
        def frame_record(seq):
            if seq not in self.records: raise FPGAError("expired frame", 409)
            return self.records[seq]
        self.s._camera_service = types.SimpleNamespace(status=lambda:
            {"frame_id": self.seq, "running": True}, frame_record=frame_record)
        self.ack = self.s.monitor({"action": "start", "model_id": "model",
                                   "countdown_seconds": 0, "capture_time_floor": -999})
        self.mid = self.ack["monitor_id"]

    def prediction(self, captured_at, label="squats_up", seq=None, features=None, mid=None):
        self.seq = self.seq + 1 if seq is None else seq
        self.records[self.seq] = {"frame_seq": self.seq, "capture_time": captured_at,
            "image": {"hardware_completed": True, "completed_count": self.seq, "last_cycles": 100}}
        self.current_label = label
        return self.s.predict({"model_id": "model", "monitor_id": mid or self.mid,
            "frame_seq": self.seq, "features": [0.0] * 99 if features is None else features,
            "capture_time": 99999, "countdown_seconds": 0})

    def stable(self, stamp, label):
        self.prediction(stamp, label)
        return self.prediction(stamp + .21, label)

    def test_authoritative_three_seconds_and_status_remaining(self):
        self.assertEqual(self.ack["countdown"]["remaining_seconds"], 3.0)
        self.assertEqual(self.ack["capture_time_floor"], 103.0)
        self.assertEqual(self.ack["counter"]["phase"], "countdown")
        self.clock.now = 101.2
        state = self.s.status()
        self.assertAlmostEqual(state["countdown"]["remaining_seconds"], 1.8)
        self.assertEqual(state["counter"]["status"], "countdown")
        self.clock.now = 103
        state = self.s.status()
        self.assertFalse(state["countdown"]["active"])
        self.assertEqual(state["counter"]["phase"], "waiting_start")
        self.assertEqual(state["capture_time_floor"], 103)

    def test_early_predict_and_unmonitored_bypass_cannot_launch_pl(self):
        self.clock.now = 102.999
        with self.assertRaises(FPGAError): self.prediction(103.1)
        with self.assertRaises(FPGAError):
            self.s.predict({"model_id": "model", "features": [0.0] * 99})
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.s.counter.count, 0)

    def test_deadline_frame_old_sequence_and_old_capture_rejected_before_pl(self):
        self.clock.now = 104
        for seq, stamp in ((11, 102.99), (12, 103.0), (10, 103.2)):
            with self.assertRaises(FPGAError): self.prediction(stamp, seq=seq)
        self.assertEqual(self.calls, 0)
        result = self.prediction(103.2, seq=20)
        self.assertTrue(result["hardware_completed"])
        self.assertEqual(self.calls, 1)

    def test_starting_from_target_cannot_count_a_half_cycle(self):
        self.clock.now = 110
        self.stable(103.1, "squats_down")
        state = self.stable(103.6, "squats_up")
        self.assertEqual(state["counter"]["count"], 0)
        self.assertEqual(state["counter"]["phase"], "ready")
        self.stable(104.1, "squats_down")
        state = self.stable(104.6, "squats_up")
        self.assertEqual(state["counter"]["count"], 1)

    def test_pause_cancels_countdown_and_resume_gets_new_deadline(self):
        self.clock.now = 101
        paused = self.s.monitor({"action": "pause", "monitor_id": self.mid})
        self.assertFalse(paused["countdown"]["active"])
        with self.assertRaises(FPGAError): self.prediction(101.1)
        resumed = self.s.monitor({"action": "resume", "monitor_id": self.mid})
        self.assertEqual(resumed["countdown"]["deadline"], 104)
        self.clock.now = 103.9
        with self.assertRaises(FPGAError): self.prediction(104.1)
        self.clock.now = 104.2
        with self.assertRaises(FPGAError): self.prediction(103.99)
        self.assertEqual(self.calls, 0)

    def test_cancel_stop_and_new_start_invalidate_previous_monitor(self):
        for action in ("cancel", "stop"):
            old = self.mid
            ack = self.s.monitor({"action": action, "monitor_id": old})
            self.assertIsNone(ack["monitor_id"])
            self.assertFalse(ack["countdown"]["active"])
            self.clock.now += 4
            with self.assertRaises(CounterError): self.prediction(self.clock.now, mid=old)
            self.mid = self.s.monitor({"action": "start", "model_id": "model"})["monitor_id"]
            self.assertNotEqual(self.mid, old)
        self.assertEqual(self.calls, 0)

    def test_reset_cancels_pending_clears_count_and_rejects_older_frame(self):
        self.clock.now = 102
        self.s.counter.count = 4
        ack = self.s.monitor({"action": "reset", "monitor_id": self.mid})
        self.assertFalse(ack["countdown"]["active"])
        self.assertEqual(ack["counter"]["count"], 0)
        self.assertEqual(ack["counter"]["phase"], "waiting_start")
        with self.assertRaises(FPGAError): self.prediction(101.9)
        self.assertEqual(self.calls, 0)

    def test_deployment_model_change_cancels_pending_and_old_request(self):
        result = self.s.upload_model(model_fixture("pushups"))
        self.assertFalse(result["countdown"]["active"])
        self.assertIsNone(result["counter"]["monitor_id"])
        self.clock.now = 104
        with self.assertRaises(CounterError): self.prediction(104.1)
        with self.assertRaises(FPGAError): self.s.monitor({"action": "start", "model_id": "model"})
        self.assertEqual(self.calls, 0)

    def test_profile_cannot_reverse_stages_or_change_exercise(self):
        for key, value in (("exercise_id", "pushups"), ("start_label", "squats_down"),
                           ("name", "自定义运动")):
            model = model_fixture()
            model["exercise_profile"] = {key: value}
            with self.assertRaises(ValueError): self.s.upload_model(model)
        with self.assertRaises(ValueError):
            self.s.monitor({"action": "start", "model_id": "model", "start_label": "squats_down"})
        self.assertTrue(self.s._countdown()["active"])

    def test_fault_cancels_countdown_no_pl_fallback(self):
        self.s.counter.count = 2
        self.s._hardware_fault("test fault")
        self.clock.now = 104
        self.assertFalse(self.s._countdown()["active"])
        self.assertIsNone(self.s.counter.monitor_id)
        self.assertEqual(self.s.counter.count, 2)
        with self.assertRaises(FPGAError): self.prediction(104.1)
        self.assertEqual(self.calls, 0)

    def test_duplicate_and_out_of_order_timestamp_rejected_before_pl(self):
        self.clock.now = 104
        self.prediction(103.5, seq=20)
        for seq, stamp in ((20, 103.6), (19, 103.7), (21, 103.4)):
            with self.assertRaises(FPGAError): self.prediction(stamp, seq=seq)
        self.assertEqual(self.calls, 1)


class DatasetEndpointTests(unittest.TestCase):
    def test_bundled_zip_served_even_when_missing_overlay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / "index.html"; page.write_text("diagnostic", encoding="utf-8")
            (root / "data").mkdir(); (root / "data" / "archive.zip").write_bytes(b"PK-test-only")
            s = FPGAPoseServer(host="127.0.0.1", port=0, page_path=page, bit_path=root / "missing.bit")
            fake = types.SimpleNamespace(Overlay=lambda *a, **kw:
                (_ for _ in ()).throw(AssertionError("hardware download must not occur")))
            with patch.dict(sys.modules, {"pynq": fake}):
                s.start()
                try:
                    base = "http://127.0.0.1:%s" % s.port
                    with urlopen(base + "/api/dataset") as response:
                        self.assertEqual(response.headers["Content-Type"], "application/zip")
                        self.assertEqual(response.read(), b"PK-test-only")
                    with urlopen(base + "/api/status") as response:
                        status = json.load(response)
                    self.assertFalse(status["ready"])
                    self.assertFalse(status["countdown"]["active"])
                    with self.assertRaises(HTTPError) as exc: urlopen(base + "/api/frame")
                    self.assertEqual(exc.exception.code, 503)
                finally:
                    s.stop()


if __name__ == "__main__": unittest.main(verbosity=2)
