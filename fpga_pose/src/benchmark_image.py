"""Explicit on-board benchmark; stop the camera service before running.

CPU reference is used ONLY in this benchmark and numerical startup self-test.
The live service never substitutes it for the PL image kernel.
"""
import argparse
import json
import platform
import statistics
import time
from pathlib import Path

import numpy as np

from fpga_classifier import FPGAKNNClassifier
from fpga_image import FPGAImageProcessor, reference_half_bgr, validate_v2_hwh


def benchmark(bit_path, width=640, height=360, repetitions=20):
    if width % 2 or height % 2 or not 2 <= width <= 1280 or not 2 <= height <= 720:
        raise ValueError("宽高需要为偶数，最大 1280×720")
    if not 3 <= repetitions <= 100:
        raise ValueError("重复次数需要为 3～100")
    bit_path = Path(bit_path).resolve()
    validate_v2_hwh(bit_path.with_suffix(".hwh"))
    classifier = FPGAKNNClassifier(str(bit_path))
    image = None
    try:
        hardware = classifier.initialize()
        if not hardware["ready"]:
            raise RuntimeError(hardware["error"])
        image = FPGAImageProcessor(classifier._overlay, fault_callback=classifier.mark_unavailable)
        image_state = image.initialize(after_overlay_download=True)
        if not image_state["ready"]:
            raise RuntimeError(image_state["error"])
        from pynq import Clocks
        configured_mhz = float(Clocks.fclk0_mhz)
        frame = np.random.RandomState(20261011).randint(0, 256, (height, width, 3)).astype(np.uint8)
        expected = reference_half_bgr(frame)
        # Warm allocations, cache and native library paths before measuring.
        for _ in range(3):
            reference_half_bgr(frame)
            actual, _ = image.process(frame)
            if not np.array_equal(actual, expected):
                raise RuntimeError("PL 像素与 CPU 整数参考不同，停止基准测试")
        cpu_ms, total_ms, cycle_ms, cycles = [], [], [], []
        for _ in range(repetitions):
            started = time.perf_counter()
            reference_half_bgr(frame)
            cpu_ms.append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            actual, evidence = image.process(frame)
            total_ms.append((time.perf_counter() - started) * 1000)
            if not np.array_equal(actual, expected) or not evidence["hardware_completed"]:
                raise RuntimeError("PL 数值或完成证据不匹配，停止基准测试")
            cycles.append(evidence["last_cycles"])
            cycle_ms.append(evidence["last_cycles"] / configured_mhz / 1000)
        cpu_median, pl_total_median = statistics.median(cpu_ms), statistics.median(total_ms)
        report = {"schema": "fpga-pixel-benchmark-v2", "platform": platform.platform(),
                  "input_size": [width, height], "output_size": [width // 2, height // 2],
                  "repetitions": repetitions, "pixels_exact_match": True,
                  "fclk0_configured_mhz": configured_mhz,
                  "cpu_numpy_exact_reference_median_ms": cpu_median,
                  "fpga_pack_dma_pl_unpack_median_ms": pl_total_median,
                  "pl_cycles_median": statistics.median(cycles),
                  "pl_cycle_time_median_ms": statistics.median(cycle_ms),
                  "cpu_reference_over_fpga_full_call_ratio": cpu_median / pl_total_median,
                  "hardware_completed_count": image.status()["completed_count"],
                  "bitstream_sha256": hardware["bitstream_sha256"],
                  "hwh_sha256": hardware["hwh_sha256"],
                  "scope_note": "同一帧、相同整数算法；CPU 为向量化 NumPy 参考。PL 周期受流等待影响；"
                                "完整 PL 调用包含打包、DMA 和解包。这里不含摄像头、JPEG、网络或 MediaPipe，"
                                "也不代表与所有优化 CPU 实现相比的加速比。"}
        return report
    finally:
        if image is not None:
            image.close()
        classifier.close()


def main():
    parser = argparse.ArgumentParser(description="停止实时服务后，比较真实 PL 图像处理与 CPU 整数参考")
    parser.add_argument("--bit", default="overlays/pose_distance.bit")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--output", default="image_benchmark.json")
    args = parser.parse_args()
    report = benchmark(args.bit, args.width, args.height, args.repetitions)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
