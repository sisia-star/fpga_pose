"""File-only v2 reset/DMA capability check; no PYNQ import or hardware access."""
from pathlib import Path
import hashlib
import json
import sys

project = Path(__file__).resolve().parent
if str(project) not in sys.path:
    sys.path.insert(0, str(project))
from fpga_image import validate_v2_hwh

bit = project / "overlays" / "pose_distance.bit"
hwh = bit.with_suffix(".hwh")
for path in (bit, hwh):
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError("缺少本次 v2 Vivado 构建的硬件文件：%s" % path)
    print("文件：", path, "字节：", path.stat().st_size, flush=True)
    print("SHA256：", hashlib.sha256(path.read_bytes()).hexdigest(), flush=True)
print(json.dumps(validate_v2_hwh(hwh), ensure_ascii=False, indent=2), flush=True)
print("v2 文件检查通过；尚未下载位流、读取寄存器或验证实际 DMA 计算。", flush=True)
