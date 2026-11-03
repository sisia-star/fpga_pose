# Latest convolution project only

Version: parallel-ai-conv-20261028. This package contains only the latest
application, hardware RTL/build scripts, tests, models, data and final bit/hwh.
Previous bitstreams, baseline RTL, serial comparison records, old deployment
reports, old version restoration tools and duplicate build snapshots are excluded.

Vivado 2022.2: source start_vivado_2022_2.tcl in an empty session.
Update POSE_BOARD_REPO in that entry if the board repository path differs.
Board PYNQ 3.0.1: use the working PYNQ Python interpreter to install
  sudo /usr/local/share/pynq-venv/bin/python3 autostart/board_manage.py install --python /usr/local/share/pynq-venv/bin/python3
Do not run a second notebook server on port 8081 while the service is running.
Board service: fpga-pose-parallel-ai.service. Notebook checks service status.

Windows Python 3.14:
  py -3.14 -m venv .conv-venv
  .\.conv-venv\Scripts\python.exe -m pip install -r requirements-conv.txt
Configure your own API key and pairing token:
  powershell -NoProfile -ExecutionPolicy Bypass -File autostart\windows_setup.ps1
Then install the convolution helper:
  powershell -NoProfile -ExecutionPolicy Bypass -File autostart\windows_conv_setup.ps1
Check existing scheduled tasks for their actual project path before reusing.
Open http://192.168.2.99:8081/?v=parallel-ai-conv-20261028
Select and deploy an action model before starting training.

The three PL kernels perform image mean/downsampling, a trained output convolution,
and 32-lane pose distances/top5. Other network processing runs on Windows.
DeepSeek is optional and receives requested summaries only.
Current routed reports are in reports_conv/. CSV accuracy is not live-camera accuracy.
Hardware validation scripts require stopped live services and exclusive hardware use.
Notebook execution outputs were cleared in the submission copy.
SHA256SUMS.json inventories all included files except itself.
Board upload location required by the notebook:
/home/xilinx/jupyter_notebooks/fpga_pose_parallel_ai_20261028
Rename the extracted directory to fpga_pose_parallel_ai_20261028 before uploading.
parallel_version.json is required by the current notebook; it describes the current convolution version.
pynq_pose_server.py is the camera-server dependency used by fpga_pose_server.py.
