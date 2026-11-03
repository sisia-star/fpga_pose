"""PYNQ driver for the Vivado-only AXI-Lite pose distance RTL core.

PL stores signed16 Q4.11 vectors in BRAM and returns uint64 sums of squared
differences. PL also selects stable top5; ARM quantizes, transfers words, votes and applies rejection gates;
it never computes sample/query distances and never falls back to CPU inference.
The fixed register protocol is accepted only after HWH and hardware ID checks.
"""

import hashlib
import json
import math
import numbers
import re
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

FEATURE_SPEC = "image33-hipcenter-pose2d-v1"
PUSHUPS_FEATURE_SPEC = "image-pushups-side3-angles-v2"
SITUP_FEATURE_SPEC = "image-situp-side3-angles-v2"
HOLD_FEATURE_SPEC = "image-hold-limb-geometry-v1"
HOLD_EXERCISES = {"birddog", "high_plank", "knee_plank"}
MODEL_SCHEMA = "pose-knn-dataset-model-v2"
EXERCISE_CATALOG = json.loads(Path(__file__).with_name("exercise_catalog.json").read_text(encoding="utf-8"))
DIMENSIONS = 99
MAX_ROWS = 1024
MAX_VALIDATION_SAMPLES = 2500
MAX_DATASET_SAMPLES = 10000
FEATURE_SCALE = 2048
MIN_FEATURE = -16.0
MAX_FEATURE = 32767 / FEATURE_SCALE
MAX_RADIUS = 256.0
DISTANCE_DENOMINATOR = DIMENSIONS * FEATURE_SCALE ** 2
MAX_RAW_DISTANCE = DIMENSIONS * 65535 ** 2
REG = {
    "control": 0x00, "row_count": 0x04, "train_addr": 0x08, "train_data": 0x0C,
    "query_addr": 0x10, "query_data": 0x14, "dist_addr": 0x18,
    "dist_lo": 0x1C, "dist_hi": 0x20, "id": 0x24, "max_rows": 0x28,
    "completed_count": 0x2C, "last_cycles": 0x30, "error": 0x34,
    "dimensions": 0x38, "feature_scale": 0x3C,
    "parallel_lanes": 0x40, "top_count": 0x44, "top_selector": 0x48,
    "top_row": 0x4C, "top_lo": 0x50, "top_hi": 0x54,
    "padded_dimensions": 0x58, "protocol_version": 0x5C,
}
ERROR_CODES = {0: "OK", 1: "busy 时拒绝写入/start", 2: "ROW_COUNT 非法",
               3: "TRAIN_ADDR 越界", 4: "QUERY_ADDR 越界", 5: "DIST_ADDR 越界",
               6: "不可写/非法寄存器", 7: "总线地址未对齐"}


class FPGAError(RuntimeError):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def _finite(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_VALIDATION_SAMPLES


def _dataset_integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_DATASET_SAMPLES


def _text(value, what):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s必须是非空字符串" % what)
    try:
        length = len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise ValueError("%s包含无效 Unicode 字符" % what) from exc
    if length > 120:
        raise ValueError("%s不能超过 120 个字符" % what)
    return value


def validate_features(value, what="姿态特征"):
    if not isinstance(value, list) or len(value) != DIMENSIONS:
        raise ValueError("%s必须包含 99 个数值" % what)
    if not all(_finite(item) and MIN_FEATURE <= item <= MAX_FEATURE for item in value):
        raise ValueError("%s必须为有限数值并位于 [-16, 15.99951171875]，Q4.11 不允许裁剪" % what)
    return [float(item) for item in value]


def feature_spec_for_exercise(exercise_id):
    if exercise_id in HOLD_EXERCISES:
        return HOLD_FEATURE_SPEC
    return {"pushups": PUSHUPS_FEATURE_SPEC, "situp": SITUP_FEATURE_SPEC}.get(exercise_id, FEATURE_SPEC)


def validate_pose_protocol(features, feature_spec=FEATURE_SPEC):
    """Dataset/live image-space hip-origin pose-size protocol, without rotation."""
    if feature_spec == HOLD_FEATURE_SPEC:
        if any(v != 0 for v in features[18:]) or any(not -8 <= v <= 8 for v in features[:18]):
            raise ValueError("保持姿态特征必须为18维有界几何量，其余维度为零")
        return features
    if feature_spec in (PUSHUPS_FEATURE_SPEC, SITUP_FEATURE_SPEC):
        if any(value != 0 for value in features[3:]):
            raise ValueError("同侧三点模型只允许固定前三维，其他维度必须为零")
        if not (0 <= features[0] <= 8 and 0 <= features[1] <= 8 and 0 < features[2] < 2):
            raise ValueError("侧机位角度/比例特征范围不正确")
        return features
    if feature_spec != FEATURE_SPEC:
        raise ValueError("姿态特征版本不兼容")
    hip = [(features[23 * 3 + axis] + features[24 * 3 + axis]) / 2 for axis in range(3)]
    shoulder = [(features[11 * 3 + axis] + features[12 * 3 + axis]) / 2 for axis in range(2)]
    torso = math.hypot(shoulder[0] - hip[0], shoulder[1] - hip[1])
    radius = max(math.hypot(features[index * 3] - hip[0],
                            features[index * 3 + 1] - hip[1]) for index in range(33))
    if any(abs(value) > .001 for value in hip) or abs(max(2.5 * torso, radius) - 1.0) > .001:
        raise ValueError("姿态必须为图像坐标的髋中心原点、二维姿态尺寸归一化协议")
    return features


def hold_geometry_reason(features, exercise_id):
    a, b = features[:9], features[9:18]
    if exercise_id == "high_plank":
        if a[4] > 3.8: return "请将身体调整为支撑姿态"
        if a[0] * 45 < 120: return "请伸直支撑手臂"
        if a[1]*45 < 120 or a[2]*45 < 125: return "请让肩、髋与腿部保持伸展"
    elif exercise_id == "knee_plank":
        if a[4] > 3.8: return "请保持跪姿支撑"
        if a[1] * 45 < 115: return "请抬起髋部，避免坐回脚跟"
        if a[2] * 45 > 170: return "请将膝盖落地，保持跪姿"
    elif exercise_id == "birddog":
        if min(a[4], b[4]) > 3.8: return "请保持四点支撑的躯干方向"
        def extended(x, y):
            return x[3]*45 > 95 and x[0]*45 > 120 and y[1]*45 > 120 and y[2]*45 > 120
        if not extended(a, b) and not extended(b, a):
            return "请伸展一侧手臂与对侧腿，并保持清晰可见"
    return None


def _id_list(value, what):
    if not isinstance(value, list):
        raise ValueError("%s必须是数组" % what)
    result = [_text(item, what) for item in value]
    if len(set(result)) != len(result):
        raise ValueError("%s不能重复" % what)
    return result


def validate_model(model):
    """Check dataset provenance and sample holdout, never invent video groups.

    Metrics are checked for internal consistency here; recomputing distances and
    matching original CSV samples is done in the browser dataset import engine.
    This service does not run CPU distance inference to replace the PL core.
    """
    if not isinstance(model, dict) or model.get("schema") != MODEL_SCHEMA:
        raise ValueError("模型格式必须为 %s；旧视频模型不可混用" % MODEL_SCHEMA)
    exercise_id = model.get("exerciseId")
    if not isinstance(exercise_id, str) or exercise_id not in EXERCISE_CATALOG:
        raise ValueError("运动必须为数据集支持的五种运动之一")
    feature_spec = feature_spec_for_exercise(exercise_id)
    if model.get("featureSpec") != feature_spec:
        raise ValueError("模型特征版本不兼容；俯卧撑和仰卧起坐需要重新训练同侧三点模型")
    catalog = EXERCISE_CATALOG[exercise_id]
    classes = [catalog["start_label"], catalog["target_label"]]
    if model.get("classes") != classes:
        raise ValueError("阶段类别与运动目录的起始/到位顺序不一致")
    if (model.get("k") != 5 or not isinstance(model.get("k"), int)
            or isinstance(model.get("k"), bool) or model.get("minVoteFraction") != 0.6):
        raise ValueError("模型要求 k=5，邻居投票阈值为 0.6")
    thresholds = model.get("classThresholds")
    if not isinstance(thresholds, dict) or set(thresholds) != set(classes):
        raise ValueError("模型各类别的距离阈值缺失或存在额外类别")
    clean_thresholds = {}
    for label in classes:
        radius = thresholds[label]
        if not _finite(radius) or not 0.04 <= radius <= MAX_RADIUS:
            raise ValueError("类别“%s”的 RMS 距离阈值必须为 0.04～256" % label)
        clean_thresholds[label] = float(radius)
    raw_rows = model.get("rows")
    if not isinstance(raw_rows, list) or not len(classes) * 10 <= len(raw_rows) <= MAX_ROWS:
        raise ValueError("训练样本至少为类别数×10，且不能超过 1024 个")
    sample_ids = []
    counts = dict.fromkeys(classes, 0)
    coordinates = set()
    rows = []
    for index, row in enumerate(raw_rows):
        if not isinstance(row, dict) or row.get("label") not in classes:
            raise ValueError("第 %s 个训练样本类别无效" % (index + 1))
        if "sessionId" in row or "videoId" in row:
            raise ValueError("CSV 未提供视频分组，不能伪造视频/录制段 ID")
        sid = _text(row.get("sampleId"), "样本 ID")
        label = row["label"]
        sample_ids.append(sid)
        counts[label] += 1
        features = validate_pose_protocol(validate_features(row.get("features"), "训练特征"), feature_spec)
        coordinate_key = tuple(features)
        if coordinate_key in coordinates:
            raise ValueError("训练模型不能包含重复关键点坐标，请先对原数据集去重")
        coordinates.add(coordinate_key)
        rows.append({"label": label, "sampleId": sid, "features": features})
    if len(set(sample_ids)) != len(sample_ids) or any(not 10 <= count <= 512 for count in counts.values()):
        raise ValueError("训练样本 ID 必须唯一，每阶段必须为 10～512 个样本")
    meta = model.get("datasetMeta")
    if (not isinstance(meta, dict) or meta.get("sourceKind") != "pose-csv"
            or not isinstance(meta.get("sourceHash"), str)
            or re.fullmatch(r"[0-9a-f]{64}", meta["sourceHash"]) is None
            or meta.get("coordinateProtocol") != (HOLD_FEATURE_SPEC if exercise_id in HOLD_EXERCISES else FEATURE_SPEC)
            or meta.get("normalizedPoseSize") != (1 if exercise_id in HOLD_EXERCISES else 100)
            or not isinstance(meta.get("normalizedPoseSize"), int)
            or isinstance(meta.get("normalizedPoseSize"), bool)
            or meta.get("videoGroupsAvailable") is not False
            or not _dataset_integer(meta.get("totalSamples")) or meta["totalSamples"] < 1
            or not _dataset_integer(meta.get("duplicateRowsRemoved"))
            or meta["duplicateRowsRemoved"] >= meta["totalSamples"]):
        raise ValueError("数据集来源、坐标协议、总数或无视频分组记录无效")
    clean_meta = {key: meta[key] for key in ("sourceKind", "sourceHash", "coordinateProtocol",
        "normalizedPoseSize", "videoGroupsAvailable", "totalSamples", "duplicateRowsRemoved")}
    validation = model.get("validation")
    if not isinstance(validation, dict):
        raise ValueError("模型缺少按样本分层留出验证记录")
    if (validation.get("method") != "stratified-sample-holdout"
            or validation.get("independentVideoValidation") is not False
            or validation.get("seed") != 20261012 or not isinstance(validation.get("seed"), int)
            or isinstance(validation.get("seed"), bool)
            or validation.get("splitFraction") != 0.2):
        raise ValueError("验证必须为固定种子 20% 样本分层留出，不能声称独立视频验证")
    correct, total, rejected = (validation.get(key) for key in ("correct", "total", "rejected"))
    if (not all(_integer(item) for item in (correct, total, rejected))
            or total < len(classes) * 5 or correct + rejected > total):
        raise ValueError("模型验证计数无效")
    accuracy, coverage = validation.get("accuracy"), validation.get("coverage")
    if (not _finite(accuracy) or not _finite(coverage)
            or abs(accuracy - correct / total) > 1e-9
            or abs(coverage - (total - rejected) / total) > 1e-9):
        raise ValueError("模型验证指标与计数不一致")
    if (validation.get("labels") != classes
            or validation.get("predictedLabels") != classes + ["未确定"]
            or validation.get("rejectedColumnIndex") != len(classes)
            or isinstance(validation.get("rejectedColumnIndex"), bool)):
        raise ValueError("模型验证类别或混淆矩阵列顺序不一致")
    matrix = validation.get("confusionMatrix")
    if (not isinstance(matrix, list) or len(matrix) != len(classes)
            or any(not isinstance(row, list) or len(row) != len(classes) + 1
                   or not all(_integer(item) for item in row) for row in matrix)):
        raise ValueError("模型混淆矩阵尺寸或计数无效")
    if (sum(map(sum, matrix)) != total
            or sum(row[index] for index, row in enumerate(matrix)) != correct
            or sum(row[-1] for row in matrix) != rejected
            or any(sum(row) < 5 for row in matrix)):
        raise ValueError("模型混淆矩阵与验证计数不一致")
    if "trainSessionIds" in validation or "holdoutSessionIds" in validation:
        raise ValueError("不能将无视频分组的 CSV 验证伪装为独立录制段验证")
    train_ids = _id_list(validation.get("trainSampleIds"), "训练样本 ID")
    holdout_ids = _id_list(validation.get("holdoutSampleIds"), "验证样本 ID")
    if (train_ids != sample_ids or len(holdout_ids) != total
            or set(holdout_ids).intersection(sample_ids)
            or len(sample_ids) + total > meta["totalSamples"] - meta["duplicateRowsRemoved"]):
        raise ValueError("训练/验证样本 ID 重叠、顺序不一致或超过去重后数据总量")
    accepted_accuracy = None if total == rejected else correct / (total - rejected)
    supplied = validation.get("acceptedAccuracy")
    if ("acceptedAccuracy" not in validation
            or (accepted_accuracy is None and supplied is not None)
            or (accepted_accuracy is not None and
                (not _finite(supplied) or abs(supplied - accepted_accuracy) > 1e-9))):
        raise ValueError("已接受样本准确率与验证计数不一致")
    clean_validation = {
        "method": "stratified-sample-holdout", "independentVideoValidation": False,
        "seed": 20261012, "splitFraction": 0.2,
        "correct": correct, "total": total, "rejected": rejected,
        "accuracy": correct / total, "coverage": (total - rejected) / total,
        "acceptedAccuracy": accepted_accuracy, "confusionMatrix": [row[:] for row in matrix],
        "labels": classes[:], "predictedLabels": classes + ["未确定"],
        "rejectedColumnIndex": len(classes), "trainSampleIds": train_ids,
        "holdoutSampleIds": holdout_ids,
    }
    return {"schema": MODEL_SCHEMA, "featureSpec": feature_spec, "exerciseId": exercise_id,
            "classes": classes, "k": 5, "minVoteFraction": 0.6,
            "classThresholds": clean_thresholds, "rows": rows,
            "datasetMeta": clean_meta, "validation": clean_validation}


def _meta_int(value, what):
    try:
        if isinstance(value, str):
            result = int(value, 0)
        elif isinstance(value, numbers.Integral) and not isinstance(value, bool):
            result = int(value)
        else:
            raise ValueError()
    except (TypeError, ValueError) as exc:
        raise FPGAError("HWH 中 %s 不是有效整数" % what) from exc
    if result < 0:
        raise FPGAError("HWH 中 %s 不能为负数" % what)
    return result


def validate_reset_hwh(hwh_path):
    """Check this design's reset wiring from XML before any PL programming.

    This catches the known AUX-low wiring error; it is not a measurement of
    physical reset release or proof that the bitstream matches its HWH.
    """
    try:
        root = ET.parse(hwh_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise FPGAError("无法读取 HWH 复位配置：%s" % exc) from exc
    def tag(element):
        return element.tag.rsplit("}", 1)[-1]
    modules = {item.get("INSTANCE"): item for item in root.iter()
               if tag(item) == "MODULE"}
    required = ("rst_fclk0", "const_one", "const_zero", "processing_system7_0", "pose_distance_0")
    if any(name not in modules for name in required):
        raise FPGAError("HWH 缺少本设计的复位模块；请用修正版重新生成配套 bit/HWH")
    def parameters(name):
        return {item.get("NAME"): item.get("VALUE") for item in modules[name].iter()
                if tag(item) == "PARAMETER"}
    def net(name, port):
        matches = [item.get("SIGNAME") for item in modules[name].iter()
                   if tag(item) == "PORT" and item.get("NAME") == port]
        if len(matches) != 1 or not matches[0]:
            raise FPGAError("HWH 复位连接缺失或不明确：%s/%s" % (name, port))
        return matches[0]
    reset_parameters = parameters("rst_fclk0")
    for key in ("C_EXT_RESET_HIGH", "C_AUX_RESET_HIGH"):
        if _meta_int(reset_parameters.get(key), key) != 0:
            raise FPGAError("HWH 复位极性不符合本设计：%s 必须为 0" % key)
    for name, expected in (("const_one", 1), ("const_zero", 0)):
        values = parameters(name)
        if (_meta_int(values.get("CONST_WIDTH"), name + "/CONST_WIDTH") != 1
                or _meta_int(values.get("CONST_VAL"), name + "/CONST_VAL") != expected):
            raise FPGAError("HWH 复位常量不正确：%s 必须为 1 位、值 %d" % (name, expected))
    connections = (
        ("const_one", "dout", "rst_fclk0", "aux_reset_in"),
        ("const_one", "dout", "rst_fclk0", "dcm_locked"),
        ("const_zero", "dout", "rst_fclk0", "mb_debug_sys_rst"),
        ("processing_system7_0", "FCLK_RESET0_N", "rst_fclk0", "ext_reset_in"),
        ("rst_fclk0", "peripheral_aresetn", "pose_distance_0", "s_axi_aresetn"),
    )
    for source, output, sink, input_port in connections:
        if net(source, output) != net(sink, input_port):
            raise FPGAError("HWH 复位连接错误：%s/%s 应接 %s/%s；请重新生成配套 bit/HWH" %
                            (sink, input_port, source, output))
    return {"ext_reset_active": "low", "aux_reset_active": "low",
            "aux_reset_net": net("rst_fclk0", "aux_reset_in"),
            "aux_reset_constant": 1}


def quantize(features):
    """Nearest integer; exact half-integers round away from zero, never clip."""
    values = validate_features(features)
    result = []
    for value in values:
        scaled = value * FEATURE_SCALE
        integer = math.floor(scaled + 0.5) if scaled >= 0 else math.ceil(scaled - 0.5)
        if not -32768 <= integer <= 32767:
            raise ValueError("Q4.11 量化越界，不允许裁剪")
        result.append(integer)
    return result


class FPGAKNNClassifier:
    """One serialized model/query session using the actual PL completion counter."""

    def __init__(self, bit_path="overlays/pose_distance.bit", ip_name="pose_distance_0",
                 kernel_timeout=2.0):
        if not _finite(kernel_timeout) or not 0.01 <= kernel_timeout <= 30:
            raise ValueError("kernel_timeout 必须为 0.01～30 秒")
        self.bit_path = Path(bit_path).resolve()
        self.ip_name = ip_name
        self.kernel_timeout = float(kernel_timeout)
        self.session_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._overlay = None
        self._ip = None
        self._ready = False
        self._error = "FPGA 尚未初始化"
        self._model = None
        self._model_id = None
        self._model_hash = None
        self._generation = 0
        self._inflight = False
        self._inference_count = 0
        self._last_cycles = 0
        self._last_kernel_ms = None
        self._bit_hash = None
        self._hwh_hash = None
        self._ip_address = None
        self._parallel_lanes = None
        self._padded_dimensions = None
        self._fclk_mhz = None
        self._last_predict_ms = None

    def initialize(self):
        """A real download resets the RTL; missing files/device stay unavailable."""
        with self._lock:
            if self._ready:
                return self.hardware_status()
            self._clear_model()
            try:
                hwh_path = self.bit_path.with_suffix(".hwh")
                if not self.bit_path.is_file() or not hwh_path.is_file():
                    raise FPGAError("需要实际生成并匹配的 .bit/.hwh：%s 与 %s" %
                                    (self.bit_path, hwh_path))
                validate_reset_hwh(hwh_path)
                from pynq import Overlay
                # PYNQ 3.0.1 can retain a pickled IP map from the previous
                # overlay after an in-place release. The service owns the
                # FPGA lock here; invalidate metadata only, then parse this HWH.
                from pynq.pl_server.embedded_device import _get_bitstream_handler
                _get_bitstream_handler(str(self.bit_path))._clear_cache()
                overlay = Overlay(str(self.bit_path), download=False)
                if "pose_conv_0" not in overlay.ip_dict or "conv_dma_0" not in overlay.ip_dict:
                    raise FPGAError("本卷积版需要包含 pose_conv_0 与 conv_dma_0 的配套位流")
                overlay.download()
                if not overlay.is_loaded():
                    raise FPGAError("PYNQ 未确认 Overlay 已加载")
                description = overlay.ip_dict.get(self.ip_name)
                if not isinstance(description, dict):
                    raise FPGAError("HWH 未找到 IP %s；实际 IP：%s" %
                                    (self.ip_name, ", ".join(overlay.ip_dict)))
                vlnv = description.get("type", "")
                if vlnv != "pose.local:pose:pose_knn_distance_axi:1.0":
                    raise FPGAError("HWH IP 类型与 RTL 协议不一致：%s" % vlnv)
                address = _meta_int(description.get("phys_addr"), "phys_addr")
                span = _meta_int(description.get("addr_range"), "addr_range")
                if address % 4 or span < 0x60 or address + span > 1 << 32:
                    raise FPGAError("HWH IP 地址必须对齐且包含 0x00～0x5f 寄存器")
                ip = overlay
                for part in self.ip_name.split("/"):
                    ip = getattr(ip, part)
                if int(ip.read(REG["id"])) != 0x504B5032:
                    raise FPGAError("PL ID_MAGIC 不匹配，拒绝使用错误的比特流")
                if (int(ip.read(REG["max_rows"])) != MAX_ROWS
                        or int(ip.read(REG["dimensions"])) != DIMENSIONS
                        or int(ip.read(REG["feature_scale"])) != FEATURE_SCALE):
                    raise FPGAError("PL MAX_ROWS/DIMS/SCALE 与驱动不兼容")
                lanes = int(ip.read(REG["parallel_lanes"]))
                padded = int(ip.read(REG["padded_dimensions"]))
                if (lanes not in (2, 4, 8, 16, 32)
                        or padded != ((DIMENSIONS + lanes - 1) // lanes) * lanes
                        or int(ip.read(REG["protocol_version"])) != 2):
                    raise FPGAError("需要并行距离核 v2，与旧版串行位流不可混用")
                from pynq import Clocks
                frequency = float(Clocks.fclk0_mhz)
                if not 99.9 <= frequency <= 100.1:
                    raise FPGAError("并行版本要求 FCLK0 配置为 100 MHz")
                self._parallel_lanes, self._padded_dimensions, self._fclk_mhz = lanes, padded, frequency
                control = int(ip.read(REG["control"]))
                if control & 0x09 or not control & 0x04:
                    raise FPGAError("PL 下载后没有进入无故障空闲状态，请检查时钟/复位")
                self._overlay, self._ip, self._ip_address = overlay, ip, address
                self._bit_hash = hashlib.sha256(self.bit_path.read_bytes()).hexdigest()
                self._hwh_hash = hashlib.sha256(hwh_path.read_bytes()).hexdigest()
                self._inflight = False
                self._inference_count = int(ip.read(REG["completed_count"])) & 0xFFFFFFFF
                self._last_cycles = int(ip.read(REG["last_cycles"])) & 0xFFFFFFFF
                self._last_kernel_ms = None
                self._ready, self._error = True, None
            except Exception as exc:
                self._ready = False
                self._ip = None
                self._overlay = None
                self._error = "FPGA 初始化失败：%s" % exc
            return self.hardware_status()

    def _clear_model(self):
        self._model = None
        self._model_id = None
        self._model_hash = None

    def _read_status(self):
        control = int(self._ip.read(REG["control"]))
        self._inflight = bool(control & 1)
        self._inference_count = int(self._ip.read(REG["completed_count"])) & 0xFFFFFFFF
        self._last_cycles = int(self._ip.read(REG["last_cycles"])) & 0xFFFFFFFF
        error_code = int(self._ip.read(REG["error"]))
        if control & 8:
            self._ready = False
            self._error = "PL fault %s：%s；请重新下载 Overlay" % (
                error_code, ERROR_CODES.get(error_code, "未知硬件错误"))
        return control, error_code

    def hardware_status(self):
        with self._lock:
            control, error_code = 0, None
            if self._ready and self._ip is not None:
                try:
                    if not self._overlay.is_loaded():
                        self._ready = False
                        self._error = "Overlay 已被其他程序替换，请重新启动服务"
                    else:
                        control, error_code = self._read_status()
                except Exception as exc:
                    self._ready = False
                    self._error = "读取 FPGA 状态失败：%s" % exc
            return {"ready": self._ready, "backend": "fpga", "model_id": self._model_id,
                    "inference_count": self._inference_count, "last_kernel_ms": self._last_kernel_ms,
                    "last_cycles": self._last_cycles, "ip_name": self.ip_name,
                    "bit_path": str(self.bit_path), "error": self._error,
                    "session_id": self.session_id, "model_hash": self._model_hash,
                    "bitstream_sha256": self._bit_hash, "hwh_sha256": self._hwh_hash,
                    "parallel_lanes": self._parallel_lanes,
                    "padded_dimensions": self._padded_dimensions,
                    "result_selection_backend": "fpga-top5",
                    "fclk0_configured_mhz": self._fclk_mhz,
                    "pl_compute_ms": None if not self._fclk_mhz else self._last_cycles / self._fclk_mhz / 1000,
                    "last_predict_ms": self._last_predict_ms,
                    "register_offsets": REG.copy(), "ip_phys_addr": self._ip_address,
                    "busy": bool(control & 1), "fault": bool(control & 8), "error_code": error_code,
                    "feature_scale": FEATURE_SCALE, "max_rows": MAX_ROWS, "dimensions": DIMENSIONS,
                    "numeric_format": "signed16-Q4.11", "rounding": "nearest-ties-away-from-zero",
                    "min_feature": MIN_FEATURE, "max_feature": MAX_FEATURE,
                    "rows": 0 if self._model is None else len(self._model["rows"]),
                    "feature_spec": None if self._model is None else self._model["featureSpec"]}

    def _require_ready(self):
        if not self._ready or self._ip is None:
            raise FPGAError(self._error or "FPGA 不可用")
        if not self._overlay.is_loaded():
            self._ready, self._error = False, "Overlay 已被其他程序替换，请停止并重新启动服务"
            self._clear_model()
            raise FPGAError(self._error)
        control, error_code = self._read_status()
        if control & 8:
            self._clear_model()
            raise FPGAError(self._error)
        if control & 1 or not control & 4:
            self._ready, self._error = False, "PL 内核不是空闲状态，请重新下载 Overlay"
            self._clear_model()
            raise FPGAError(self._error)

    def upload_model(self, model):
        # All validation/quantization happens before touching PL or the old model.
        clean = validate_model(model)

        canonical = json.dumps(clean, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        with self._lock:
            self._require_ready()
            # Banks implement one model image, not two interchangeable model slots.
            training_words = [word for row in clean["rows"]
                              for word in quantize(row["features"]) +
                              [0] * (self._padded_dimensions - DIMENSIONS)]
            self._clear_model()
            try:
                self._ip.write(REG["train_addr"], 0)
                for word in training_words:
                    self._ip.write(REG["train_data"], word & 0xFFFF)
                self._ip.write(REG["row_count"], len(clean["rows"]))
                control, error_code = self._read_status()
                if control & 9 or not control & 4 or error_code:
                    raise FPGAError("PL 模型写入异常，原模型已失效；请重新下载 Overlay")
            except Exception as exc:
                self._ready, self._error = False, "PL 模型写入失败，原模型已失效：%s" % exc
                raise FPGAError(self._error) from exc
            self._generation += 1
            self._model_id = "%s:%s:%s" % (self.session_id, self._generation, digest[:16])
            self._model_hash = digest
            self._model = clean
            return {"backend": "fpga", "ready": True, "model_id": self._model_id,
                    "rows": len(clean["rows"]), "classes": clean["classes"][:],
                    "model_hash": digest, "session_id": self.session_id,
                    "feature_scale": FEATURE_SCALE, "max_rows": MAX_ROWS}

    def predict(self, model_id, features):
        # A bad frame must not invalidate an otherwise valid deployed model.
        values = validate_features(features)
        if not isinstance(model_id, str) or not model_id or len(model_id) > 200:
            raise FPGAError("请求缺少有效 model_id，请先上传分类器", 409)
        with self._lock:
            if self._model is None:
                raise FPGAError("尚未上传分类器，请先训练并提交模型", 409)
            if model_id != self._model_id:
                raise FPGAError("model_id 不匹配，分类器已变化；请重新上传当前模型", 409)
            words = quantize(validate_pose_protocol(values, self._model["featureSpec"]))
            words += [0] * (self._padded_dimensions - DIMENSIONS)
            self._require_ready()
            predict_started = time.perf_counter()
            try:
                self._ip.write(REG["query_addr"], 0)
                for word in words:
                    self._ip.write(REG["query_data"], word & 0xFFFF)
                before_count = int(self._ip.read(REG["completed_count"])) & 0xFFFFFFFF
                started = time.monotonic()
                self._ip.write(REG["control"], 1)
                self._inflight = True
                deadline = started + self.kernel_timeout
                while True:
                    control = int(self._ip.read(REG["control"]))
                    if control & 8:
                        code = int(self._ip.read(REG["error"]))
                        raise FPGAError("PL fault %s：%s" % (code, ERROR_CODES.get(code, "未知硬件错误")))
                    if control & 2 and not control & 1:
                        break
                    if time.monotonic() >= deadline:
                        raise FPGAError("等待 PL 完成超时（%.3f 秒），无 CPU 回退；请重新下载 Overlay" %
                                        self.kernel_timeout, 504)
                    # Short bounded PL run: avoid a fixed 0.5 ms polling sleep.
                kernel_ms = (time.monotonic() - started) * 1000
                completed = int(self._ip.read(REG["completed_count"])) & 0xFFFFFFFF
                cycles = int(self._ip.read(REG["last_cycles"])) & 0xFFFFFFFF
                if ((completed - before_count) & 0xFFFFFFFF) != 1 or cycles == 0:
                    raise FPGAError("PL done 与实际硬件完成计数/周期不对应，拒绝此次结果")
                self._inflight = False
                self._inference_count, self._last_cycles = completed, cycles
                if int(self._ip.read(REG["top_count"])) != 5:
                    raise FPGAError("PL top5 结果数量无效")
                neighbors, raw_distances = [], {}
                previous = None
                for rank in range(5):
                    self._ip.write(REG["top_selector"], rank)
                    index = int(self._ip.read(REG["top_row"]))
                    low = int(self._ip.read(REG["top_lo"])) & 0xFFFFFFFF
                    high = int(self._ip.read(REG["top_hi"])) & 0xFFFFFFFF
                    raw = (high << 32) | low
                    pair = (raw, index)
                    if (index >= len(self._model["rows"]) or index in raw_distances
                            or raw > MAX_RAW_DISTANCE or (previous is not None and pair <= previous)):
                        raise FPGAError("PL top5 行号、距离范围或稳定排序无效")
                    neighbors.append(index); raw_distances[index] = raw; previous = pair
                control, error_code = self._read_status()
                if control & 9 or error_code:
                    raise FPGAError("PL 距离读取发生硬件错误，拒绝结果")
            except Exception as exc:
                self._ready, self._error = False, str(exc)
                if isinstance(exc, FPGAError):
                    raise
                raise FPGAError("FPGA 读写失败，无 CPU 回退：%s" % exc) from exc
            self._last_kernel_ms = kernel_ms
            model = self._model
            # Distances AND stable top5 selection were performed by PL.
            # PS retains class voting and all original rejection/hold gates.
            votes = {label: 0 for label in model["classes"]}
            for index in neighbors:
                votes[model["rows"][index]["label"]] += 1
            label = max(model["classes"], key=lambda name: votes[name])
            fraction = votes[label] / 5
            best_raw = min(raw_distances[index] for index in neighbors
                           if model["rows"][index]["label"] == label)
            best_squared = best_raw / DISTANCE_DENOMINATOR
            if fraction < 0.6:
                label, reason = None, "邻居投票不足 60%，暂时无法确定姿态"
            elif best_squared > model["classThresholds"][label] ** 2:
                label, reason = None, "与已学习的姿态距离过大，可能是未知姿态"
            else:
                reason = "PL 定点距离计算完成，通过邻居一致度与距离检查"
            if model["exerciseId"] in HOLD_EXERCISES and label == model["classes"][1]:
                gate_reason = hold_geometry_reason(values, model["exerciseId"])
                if gate_reason:
                    label, reason = None, gate_reason
            self._last_predict_ms = (time.perf_counter() - predict_started) * 1000
            return {"parallel_lanes": self._parallel_lanes, "result_selection_backend": "fpga-top5",
                    "pl_compute_ms": cycles / self._fclk_mhz / 1000,
                    "predict_total_ms": self._last_predict_ms, "distance_results_read": 5,
                    "label": label, "voteFraction": fraction, "distance": math.sqrt(best_squared),
                    "distance_squared": best_squared, "distance_sum_integer": best_raw,
                    "reason": reason, "backend": "fpga", "model_id": self._model_id,
                    "inference_count": self._inference_count, "kernel_ms": kernel_ms,
                    "last_cycles": cycles, "kernel_cycles": cycles,
                    "model_hash": self._model_hash, "session_id": self.session_id,
                    "hardware_completed": True, "rows": len(model["rows"]),
                    "feature_scale": FEATURE_SCALE, "numeric_format": "signed16-Q4.11"}

    def close(self):
        """Stop the software backend; this is NOT an RTL abort/reset command."""
        with self._lock:
            self._ready = False
            self._clear_model()
            self._error = "FPGA 服务已停止；stop 不复位 PL，重启会重新下载 Overlay"
            self._ip = None
            # No CMA/DMA buffers exist in this protocol. In-flight PL work may
            # finish independently; do not pretend it was aborted by this method.

    def mark_unavailable(self, reason):
        """A shared-bus fault must never trigger further diagnostic MMIO reads."""
        with self._lock:
            self._ready = False
            self._error = str(reason)
            self._ip = None
            self._clear_model()
