"""JS/Python real-model protocol parity and explicitly mocked PL distance tests.

FakeDistanceIP computes distances in the test fixture only. Production code always
reads them from the PL core. These tests do not measure actual PYNQ throughput.
"""
import copy
import heapq
import json
from pathlib import Path
import subprocess
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fpga_classifier import (FPGAKNNClassifier, PUSHUPS_FEATURE_SPEC, REG, quantize,
                             validate_model, validate_pose_protocol)


class FakeDistanceIP:
    def __init__(self, model):
        self.rows = [quantize(row["features"]) for row in model["rows"]]
        self.query = []
        self.distances = []
        self.control = 4
        self.count = 0
        self.address = 0
        self.writes = []
        self.top_selector = 0
        self.top = []
        self.reads = []

    def write(self, register, value):
        self.writes.append((register, value))
        if register == REG["query_addr"]:
            self.query = []
        elif register == REG["query_data"]:
            self.query.append(value if value < 32768 else value - 65536)
        elif register == REG["control"]:
            self.distances = [sum((a - b) ** 2 for a, b in zip(row, self.query))
                              for row in self.rows]
            self.count += 1
            self.control = 6
            self.top = sorted(range(len(self.rows)), key=lambda i: (self.distances[i], i))[:5]
        elif register == REG["top_selector"]:
            self.top_selector = value
        elif register == REG["dist_addr"]:
            self.address = value

    def read(self, register):
        self.reads.append(register)
        if register == REG["control"]: return self.control
        if register == REG["completed_count"]: return self.count
        if register == REG["last_cycles"]: return 12345
        if register == REG["error"]: return 0
        if register == REG["top_count"]: return len(self.top)
        if register == REG["top_row"]: return self.top[self.top_selector]
        if register == REG["top_lo"]: return self.distances[self.top[self.top_selector]] & 0xffffffff
        if register == REG["top_hi"]: return self.distances[self.top[self.top_selector]] >> 32
        if register == REG["dist_lo"]: return self.distances[self.address] & 0xffffffff
        if register == REG["dist_hi"]:
            result = self.distances[self.address] >> 32
            self.address += 1
            return result
        return 0


class ProfileBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = """
          import fs from 'node:fs/promises';
          import {importDatasetZip,trainExerciseModel,predictPose,quantize} from './dataset_engine.mjs';
          const d=await importDatasetZip(await fs.readFile('data/archive.zip'));
          const result=Object.fromEntries(['jumping_jacks','pushups','situp','pullups','squats'].map(e=>{
            const model=trainExerciseModel(d,e);
            const queries=model.rows.filter((_,i)=>i%17===0).map(r=>({features:r.features,
              words:quantize(r.features),expected:predictPose(model,r.features)}));
            return [e,{model,queries}];
          }));
          process.stdout.write(JSON.stringify(result));
        """
        cls.fixtures = json.loads(subprocess.run(
            ["node", "--input-type=module", "-e", source], cwd=ROOT, check=True,
            text=True, capture_output=True, timeout=30).stdout)

    def test_real_models_validate_and_canonicalize_identically_in_python(self):
        for exercise, fixture in self.fixtures.items():
            self.assertEqual(validate_model(fixture["model"]), fixture["model"], exercise)
            for query in fixture["queries"]:
                self.assertEqual(quantize(query["features"]), query["words"])

    def test_real_queries_match_js_classification_via_named_fake_pl(self):
        for exercise, fixture in self.fixtures.items():
            model = validate_model(fixture["model"])
            driver = FPGAKNNClassifier()
            driver._ready = True
            driver._error = None
            driver._model = model
            driver._model_id = "test-model"
            driver._overlay = types.SimpleNamespace(is_loaded=lambda: True)
            driver._ip = FakeDistanceIP(model)
            driver._padded_dimensions = 128
            driver._parallel_lanes = 32
            driver._fclk_mhz = 100
            for query in fixture["queries"]:
                result = driver.predict("test-model", query["features"])
                self.assertEqual(result["label"], query["expected"]["label"], exercise)
                self.assertEqual(result["voteFraction"], query["expected"]["voteFraction"])
                self.assertAlmostEqual(result["distance"], query["expected"]["distance"], places=14)
                self.assertTrue(result["hardware_completed"])
                self.assertEqual(result["result_selection_backend"], "fpga-top5")
                self.assertEqual(len(driver._ip.query), 128)
                self.assertFalse(any(reg in (REG['dist_lo'], REG['dist_hi']) for reg in driver._ip.reads))

    def test_corrupt_hardware_top5_is_rejected_without_cpu_fallback(self):
        from fpga_classifier import FPGAError
        fixture = self.fixtures['squats']
        model = validate_model(fixture['model'])
        for attack in ('duplicate', 'reverse', 'out_of_range'):
            class CorruptIP(FakeDistanceIP):
                def write(self, register, value):
                    super().write(register, value)
                    if register == REG['control']:
                        if attack == 'duplicate': self.top[1] = self.top[0]
                        if attack == 'reverse': self.top.reverse()
                def read(self, register):
                    if attack == 'out_of_range' and register == REG['top_row']:
                        return len(self.rows)
                    return super().read(register)
            driver = FPGAKNNClassifier()
            driver._ready = True
            driver._model, driver._model_id = model, 'test-model'
            driver._padded_dimensions, driver._parallel_lanes, driver._fclk_mhz = 128, 32, 100
            driver._overlay = types.SimpleNamespace(is_loaded=lambda: True)
            driver._ip = CorruptIP(model)
            with self.assertRaises(FPGAError):
                driver.predict('test-model', fixture['queries'][0]['features'])
            self.assertFalse(driver._ready)
            self.assertNotIn(REG['dist_lo'], driver._ip.reads)

    def test_profile_mixing_and_nonzero_padding_reject_before_any_mmio(self):
        fixture = self.fixtures["pushups"]
        driver = FPGAKNNClassifier()
        driver._ready = True
        driver._model = validate_model(fixture["model"])
        driver._model_id = "test-model"
        def forbidden(*args): raise AssertionError("invalid profile touched MMIO")
        driver._ip = types.SimpleNamespace(read=forbidden, write=forbidden)
        invalid = [0.0] * 99
        with self.assertRaises(ValueError): driver.predict("test-model", invalid)
        for index in (3, 35, 69, 72):
            invalid = fixture["queries"][0]["features"][:]
            invalid[index] = .1
            with self.assertRaises(ValueError): driver.predict("test-model", invalid)
        self.assertTrue(driver._ready)
        legacy = copy.deepcopy(fixture["model"])
        legacy["featureSpec"] = "image33-hipcenter-pose2d-v1"
        with self.assertRaises(ValueError): validate_model(legacy)

    def test_fixed_profile_checks_angle_ratio_bounds_and_padding(self):
        features = self.fixtures["pushups"]["queries"][0]["features"]
        self.assertEqual(validate_pose_protocol(features, PUSHUPS_FEATURE_SPEC), features)
        for index, value in ((0, -.4), (1, 8.1), (2, 0), (35, .001)):
            invalid = features[:]
            invalid[index] = value
            with self.assertRaises(ValueError): validate_pose_protocol(invalid, PUSHUPS_FEATURE_SPEC)

    def test_top_five_selection_preserves_distance_and_index_ties(self):
        for distances in ([4] * 20, list(range(30, 0, -1)), [5, 1, 2, 1, 7, 1, 1, 1, 0]):
            key = lambda index: (distances[index], index)
            self.assertEqual(heapq.nsmallest(5, range(len(distances)), key=key),
                             sorted(range(len(distances)), key=key)[:5])


if __name__ == "__main__":
    unittest.main()
