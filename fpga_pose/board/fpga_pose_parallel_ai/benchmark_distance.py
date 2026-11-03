"""Explicit on-board benchmark. No live CPU fallback is provided.

Stop the live service first. Compare exact signed16 integer squared distances and
stable top5 on the SAME ARM processor; report kernel cycles and complete calls.
The serial baseline and parallel overlay run sequentially, never concurrently.
"""
import argparse
import fcntl
import hashlib
import heapq
import json
import platform
import socket
import statistics
import time
from pathlib import Path

import numpy as np
from fpga_image import validate_v2_hwh

R = dict(control=0, rows=4, train_addr=8, train_data=12, query_addr=16,
         query_data=20, dist_addr=24, lo=28, hi=32, magic=36,
         completed=44, cycles=48, error=52, dims=56, scale=60,
         lanes=64, top_count=68, selector=72, top_row=76, top_lo=80, top_hi=84, padded=88)


def cpu_reference(training, query):
    delta = training - query
    distances = np.einsum('ij,ij->i', delta, delta, dtype=np.int64, optimize=False)
    top = np.lexsort((np.arange(len(training)), distances))[:5]
    return distances, [(int(distances[i]), int(i)) for i in top]


def summary(values):
    return {'median': statistics.median(values), 'min': min(values), 'max': max(values)}


def run(bit_path, rows_list, repetitions):
    from pynq import Overlay, Clocks
    bit = Path(bit_path).resolve()
    validate_v2_hwh(bit.with_suffix('.hwh'))
    overlay = Overlay(str(bit), download=True)
    ip = overlay.pose_distance_0
    magic = int(ip.read(R['magic']))
    if magic not in (0x504B4E31, 0x504B5032):
        raise RuntimeError('Unsupported hardware ID')
    parallel = magic == 0x504B5032
    lanes = int(ip.read(R['lanes'])) if parallel else 1
    stride = int(ip.read(R['padded'])) if parallel else 99
    mhz = float(Clocks.fclk0_mhz)
    if (not 99.9 <= mhz <= 100.1 or int(ip.read(R['dims'])) != 99
            or int(ip.read(R['scale'])) != 2048):
        raise RuntimeError('Unexpected hardware dimensions, scale or clock')
    cases = []
    for rows in rows_list:
        rng = np.random.RandomState(20261028 + rows)
        training = rng.randint(-4096, 4097, size=(rows, 99)).astype(np.int64)
        # Deliberate ties check stable lower-row ordering.
        if rows >= 20:
            training[10:16] = training[0]
        query = training[0].copy()
        padded_training = np.pad(training, ((0, 0), (0, stride-99)))
        padded_query = np.pad(query, (0, stride-99))
        distances, wanted = cpu_reference(training, query)
        t0 = time.perf_counter()
        ip.write(R['train_addr'], 0)
        for word in padded_training.flat:
            ip.write(R['train_data'], int(word) & 65535)
        ip.write(R['rows'], rows)
        model_upload_ms = (time.perf_counter()-t0)*1000

        def hardware_call(full_verify=False):
            start = time.perf_counter()
            ip.write(R['query_addr'], 0)
            for word in padded_query:
                ip.write(R['query_data'], int(word) & 65535)
            uploaded = time.perf_counter()
            before = int(ip.read(R['completed']))
            ip.write(R['control'], 1)
            deadline = time.monotonic()+2
            while True:
                status = int(ip.read(R['control']))
                if status & 8:
                    raise RuntimeError('Hardware fault: '+str(ip.read(R['error'])))
                if status & 2 and not status & 1:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('Hardware completion timed out')
            finished = time.perf_counter()
            cycles = int(ip.read(R['cycles']))
            if ((int(ip.read(R['completed']))-before) & 0xffffffff) != 1 or not cycles:
                raise RuntimeError('Completion evidence mismatch')
            if parallel:
                if int(ip.read(R['top_count'])) != 5:
                    raise RuntimeError('Missing hardware top5')
                top = []
                for rank in range(5):
                    ip.write(R['selector'], rank)
                    index = int(ip.read(R['top_row']))
                    low = int(ip.read(R['top_lo'])); high = int(ip.read(R['top_hi']))
                    top.append(((high << 32) | low, index))
            if not parallel or full_verify:
                ip.write(R['dist_addr'], 0)
                actual = []
                for _ in range(rows):
                    low = int(ip.read(R['lo'])); high = int(ip.read(R['hi']))
                    actual.append((high << 32) | low)
                if not parallel:
                    indices = heapq.nsmallest(5, range(rows), key=lambda i: (actual[i], i))
                    top = [(actual[i], i) for i in indices]
            end = time.perf_counter()
            if (not parallel or full_verify) and actual != distances.tolist():
                raise RuntimeError('A PL distance differs from the exact NumPy reference')
            if top != wanted:
                raise RuntimeError('Stable top5 differs from exact CPU result')
            return {'query_upload_ms': (uploaded-start)*1000,
                    'host_start_poll_ms': (finished-uploaded)*1000,
                    'result_readback_ms': (end-finished)*1000,
                    'complete_call_ms': (end-start)*1000,
                    'pl_cycles': cycles, 'pl_compute_ms': cycles/mhz/1000}

        hardware_call(full_verify=True)
        for _ in range(3):
            cpu_reference(training, query); hardware_call()
        samples, cpu_times = [], []
        for _ in range(repetitions):
            started = time.perf_counter(); cpu_reference(training, query)
            cpu_times.append((time.perf_counter()-started)*1000)
            samples.append(hardware_call())
        result = {key: summary([sample[key] for sample in samples]) for key in samples[0]}
        cpu_median = statistics.median(cpu_times)
        cases.append({'rows': rows, 'dimensions': 99, 'exact_distances_verified': True,
                      'stable_top5_verified': True, 'model_upload_ms': model_upload_ms,
                      'cpu_numpy_distance_and_top5_ms': summary(cpu_times), 'fpga': result,
                      'cpu_over_fpga_complete_call_ratio': cpu_median/result['complete_call_ms']['median']})
    return {'schema': 'pose-distance-benchmark-v2', 'variant': 'parallel' if parallel else 'serial',
            'platform': platform.platform(), 'lanes': lanes, 'physical_stride': stride,
            'fclk0_configured_mhz': mhz, 'repetitions': repetitions,
            'bit_sha256': hashlib.sha256(bit.read_bytes()).hexdigest(),
            'hwh_sha256': hashlib.sha256(bit.with_suffix('.hwh').read_bytes()).hexdigest(),
            'cases': cases,
            'scope': 'Synthetic deterministic vectors; SAME board ARM NumPy int64 baseline. '
                     'Exact integer output and stable top5 verified. Complete FPGA call includes '
                     'query writes, start/poll, result readback and top5 selection; excludes one-time '
                     'model upload, camera, JPEG, network and MediaPipe. Ratios below 1 mean CPU is faster. '
                     'This is not a camera FPS or recognition accuracy measurement.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bit', default='overlays/pose_distance.bit')
    parser.add_argument('--rows', type=int, nargs='+', default=[20, 128, 512, 1024])
    parser.add_argument('--repetitions', type=int, default=20)
    parser.add_argument('--output', default='distance_benchmark.json')
    args = parser.parse_args()
    if not 3 <= args.repetitions <= 100 or any(not 5 <= n <= 1024 for n in args.rows):
        parser.error('repetitions must be 3..100 and rows must be 5..1024')
    with socket.socket() as sock:
        sock.settimeout(1)
        if sock.connect_ex(('127.0.0.1', 8081)) == 0:
            raise SystemExit('Stop the active pose service first; no bitstream was loaded.')
    # Same lock as both systemd runners, prevents concurrent FPGA ownership.
    with Path('/var/lib/fpga-pose-ai/service.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = run(args.bit, args.rows, args.repetitions)
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'variant': report['variant'], 'lanes': report['lanes'],
                      'cases': len(report['cases']), 'verified': True, 'output': args.output}))


if __name__ == '__main__':
    main()
