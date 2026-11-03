"""systemd owner for the existing real-PL camera/classifier service."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time
from private_token import load_or_create


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--token-file', default='/var/lib/fpga-pose-ai/pairing.token')
    parser.add_argument('--camera', default='/dev/video0')
    parser.add_argument('--port', type=int, default=8081)
    args = parser.parse_args()
    project = Path(args.project).resolve()
    if not (project / 'fpga_pose_server.py').is_file():
        raise RuntimeError('Project not found')
    token = load_or_create(args.token_file)
    lock_path = Path(args.token_file).parent / 'service.lock'
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('An automatic board service already owns this project') from None
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        camera = Path(args.camera)
        if str(camera).startswith('/dev/') and not camera.exists():
            print('Waiting for USB camera; no FPGA download yet.', flush=True)
        while str(camera).startswith('/dev/') and not camera.exists():
            if stop.wait(1):
                return
        os.environ.setdefault('XILINX_XRT', '/usr')
        os.environ.setdefault('BOARD', 'Pynq-Z2')
        os.environ['AI_COACH_PROVIDER'] = 'relay'
        # The board never needs a cloud key.
        os.chdir(project)
        sys.path.insert(0, str(project))
        from fpga_pose_server import FPGAPoseServer
        service = FPGAPoseServer(camera=args.camera, port=args.port,
            page_path=str(project / 'fpga_multiaction_pose.html'),
            bit_path=str(project / 'overlays/pose_distance.bit'),
            width=640, height=360, kernel_timeout=2.0)
        service.ai_coach.pairing_token = token
        try:
            # FPGAPoseServer binds its port before programming PL, so a live
            # Notebook holding 8081 prevents a second Overlay download.
            service.start()
            deadline = time.monotonic() + 15
            while not stop.is_set():
                state = service.status()
                if state['ready'] and state['running'] and state['frame_id'] > 0:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('FPGA/camera startup failed; inspect service journal')
                stop.wait(.2)
            if stop.is_set():
                return
            print(json.dumps({'event': 'ready', 'port': service.port,
                'camera': args.camera, 'ai_provider': 'relay',
                'pairing': 'persistent', 'ai_enabled': False}), flush=True)
            while not stop.wait(2):
                state = service.status()
                if not state['ready'] or not state['running']:
                    raise RuntimeError('FPGA/camera pipeline lost; supervisor will restart')
        finally:
            service.stop()


if __name__ == '__main__':
    main()
