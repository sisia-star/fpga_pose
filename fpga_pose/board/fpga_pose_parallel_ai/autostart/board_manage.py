"""Install/remove only this project's owned systemd unit. Run on PYNQ as root."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import tempfile
from urllib.request import build_opener, ProxyHandler
from private_token import load_or_create, read_token

UNIT = 'fpga-pose-parallel-ai.service'
MARKER = '# FPGA_POSE_PARALLEL_AUTOSTART_V2'
TOKEN = Path('/var/lib/fpga-pose-ai/pairing.token')


def quote(value):
    text = str(value)
    if any(c in text for c in '\r\n\x00'):
        raise ValueError('Invalid service path')
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def render_unit(project, python):
    project, python = Path(project).resolve(), Path(python).expanduser().absolute()
    # ExecStart unquotes argv, but WorkingDirectory takes the entire raw value.
    # Shell-style quotes on WorkingDirectory make the path non-absolute in v249.
    directory = str(project)
    if any(c in directory for c in '\r\n\x00'):
        raise ValueError('Invalid service path')
    directory = directory.replace('%', '%%')
    argv = ' '.join(quote(x) for x in [python, '-u', project/'autostart/board_runner.py',
        '--project', project, '--token-file', TOKEN])
    return f'''{MARKER}
[Unit]
Description=FPGA Pose Parallel AI: 32 lanes, PL top5, camera and AI pairing
Conflicts=fpga-pose-ai.service
After=network.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=root
WorkingDirectory={directory}
Environment=XILINX_XRT=/usr
Environment=BOARD=Pynq-Z2
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONIOENCODING=utf-8
ExecStart={argv}
Restart=on-failure
RestartSec=15
TimeoutStopSec=20
KillMode=control-group
UMask=0077
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
'''


def call(*args, check=True):
    return subprocess.run(args, check=check)


def validate_unit(content):
    """Validate with the installed systemd parser before publishing any unit."""
    with tempfile.TemporaryDirectory(prefix='fpga-pose-unit-') as folder:
        candidate = Path(folder) / UNIT
        candidate.write_text(content, encoding='utf-8')
        result = subprocess.run(['systemd-analyze', 'verify', str(candidate)],
                                capture_output=True, text=True)
    if result.returncode:
        raise SystemExit('Service configuration validation failed; existing unit unchanged:\n'
                         + result.stderr.strip())


def owned(path):
    return path.is_file() and path.read_text(encoding='utf-8').startswith(MARKER + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install', 'show-token', 'status', 'uninstall'])
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--project', default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise SystemExit('Run on the PYNQ board with sudo; no Windows installation here.')
    target = Path('/etc/systemd/system') / UNIT
    if args.action == 'show-token':
        print(read_token(TOKEN))
        return
    if args.action == 'status':
        call('systemctl', '--no-pager', '--full', 'status', UNIT, check=False)
        return
    if target.exists() and not owned(target):
        raise SystemExit('An unrelated unit has this name; no changes made.')
    if args.action == 'uninstall':
        if not owned(target):
            print('No owned service to remove.')
            return
        call('systemctl', 'disable', '--now', UNIT)
        target.unlink()
        call('systemctl', 'daemon-reload')
        print('Automatic service removed; pairing file retained for reinstall.')
        return
    project = Path(args.project).resolve()
    python = Path(args.python).expanduser().absolute()
    for name in ['fpga_pose_server.py', 'ai_coach.py', 'autostart/board_runner.py',
                 'overlays/pose_distance.bit', 'overlays/pose_distance.hwh',
                 'data/all_models.json', 'fpga_multiaction_pose.html']:
        if not (project/name).is_file():
            raise SystemExit('Missing project file: ' + name)
    probe = subprocess.run([str(python), '-c',
        'import sys,importlib.util; assert sys.version_info >= (3,10); '
        'assert all(importlib.util.find_spec(x) for x in ("pynq","cv2","numpy"))'],
        capture_output=True, text=True)
    if probe.returncode:
        raise SystemExit('Use the Python interpreter printed by the working Jupyter kernel.')
    already_active = subprocess.run(['systemctl', 'is-active', '--quiet', UNIT]).returncode == 0
    if already_active:
        raise SystemExit('Automatic service is already active. Use systemctl restart to restart it.')
    with socket.socket() as sock:
        sock.settimeout(1)
        if sock.connect_ex(('127.0.0.1', 8081)) == 0:
            raise SystemExit('Port 8081 is occupied: stop fpga_server in its existing Notebook first.')
    state_dir = TOKEN.parent
    if state_dir.is_symlink():
        raise SystemExit('Unexpected state directory symlink')
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    load_or_create(TOKEN)  # not printed into system logs
    content = render_unit(project, python)
    validate_unit(content)
    candidate = target.with_suffix('.service.fpga-pose-new')
    candidate.write_text(content, encoding='utf-8')
    os.chmod(candidate, 0o644)
    os.replace(candidate, target)
    call('systemctl', 'daemon-reload')
    call('systemctl', 'reset-failed', UNIT, check=False)
    call('systemctl', 'enable', '--now', UNIT)
    opener = build_opener(ProxyHandler({}))
    for _ in range(45):
        try:
            with opener.open('http://127.0.0.1:8081/api/status', timeout=2) as response:
                state = json.load(response)
            if state['ready'] and state['running']:
                print('Installed; real FPGA and camera ready. Pairing token stays unchanged across reboot.')
                print('Get the token once: sudo <this-python> autostart/board_manage.py show-token')
                return
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(1)
    raise SystemExit('Service installed, but not yet ready. Check: sudo journalctl -u fpga-pose-parallel-ai -n 40 --no-pager')


if __name__ == '__main__':
    main()
