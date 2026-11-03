"""Real PL pixel processing via bounded AXI Simple DMA (PYNQ 3.x).

Only packing/unpacking and JPEG encoding happen on ARM. The 2x2 box average
and half-size operation are implemented in PL. A CPU golden image is used
only for the explicit startup numerical self-test, never for live fallback.
"""
import hashlib
import threading
import time
import xml.etree.ElementTree as ET

import numpy as np

from fpga_classifier import FPGAError, validate_reset_hwh, _meta_int

IMAGE_VLNV = "pose.local:pose:pose_image_preprocess_axi:1.0"
PIXEL_PIPELINE = "fpga-2x2-box-half-bgrx-v2"
MAX_WIDTH, MAX_HEIGHT = 1280, 720
IMAGE_REG = {"control": 0, "width": 4, "height": 8, "id": 12,
             "input_count": 16, "output_count": 20, "last_cycles": 24,
             "completed_count": 28, "error": 32, "max_width": 36, "max_height": 40}
ERROR_CODES = {1: "输入尺寸非法", 2: "运行时修改配置", 3: "TLAST 提前",
               4: "缺少帧尾 TLAST", 5: "TKEEP 非法", 6: "非法寄存器", 7: "地址未对齐"}

# A timed-out DMA may still access CMA memory. Pin its buffers until a NEW
# Overlay download has reset the DMA. Never free them merely because Python
# stopped polling. initialize(after_overlay_download=True) releases them.
_QUARANTINED_BUFFERS = []


def validate_v2_hwh(path):
    """File-only reset and v2 capability checks; no MMIO or bitstream download."""
    result = validate_reset_hwh(path)
    root = ET.parse(path).getroot()
    tag = lambda item: item.tag.rsplit("}", 1)[-1]
    modules = {item.get("INSTANCE"): item for item in root.iter() if tag(item) == "MODULE"}
    if "image_preprocess_0" not in modules or "image_dma_0" not in modules:
        raise FPGAError("此 HWH 是旧版或缺少图像处理/DMA；请用 v2 Vivado 工程生成新的配套 bit 和 HWH")
    image = modules["image_preprocess_0"]
    vlnv = image.get("VLNV") or image.get("MODTYPE")
    if image.get("VLNV") and image.get("VLNV") != IMAGE_VLNV:
        raise FPGAError("图像处理 IP 协议不兼容：%s" % vlnv)
    dma = modules["image_dma_0"]
    def reset_net(module, port):
        values = [item.get("SIGNAME") for item in module.iter()
                  if tag(item) == "PORT" and item.get("NAME") == port]
        if len(values) != 1 or not values[0]:
            raise FPGAError("v2 HWH 复位端口缺失：%s/%s" % (module.get("INSTANCE"), port))
        return values[0]
    peripheral_reset = reset_net(modules["rst_fclk0"], "peripheral_aresetn")
    for module, port in ((image, "s_axi_aresetn"), (dma, "axi_resetn")):
        if reset_net(module, port) != peripheral_reset:
            raise FPGAError("v2 图像/DMA 复位连接不正确：%s/%s" % (module.get("INSTANCE"), port))
    params = {item.get("NAME", "").lower().removeprefix("config."): item.get("VALUE")
              for item in dma.iter() if tag(item) == "PARAMETER"}
    required = {"c_include_sg": 0, "c_micro_dma": 0,
                "c_include_mm2s": 1, "c_include_s2mm": 1,
                "c_m_axis_mm2s_tdata_width": 32, "c_s_axis_s2mm_tdata_width": 32}
    for name, expected in required.items():
        if _meta_int(params.get(name), name) != expected:
            raise FPGAError("DMA 配置不兼容：%s 必须为 %d" % (name, expected))
    length = _meta_int(params.get("c_sg_length_width"), "c_sg_length_width")
    if not 22 <= length <= 26:
        raise FPGAError("DMA 长度位宽必须至少 22，支持完整 BGRX 图像")
    result.update({"pixel_pipeline": PIXEL_PIPELINE, "image_ip": "image_preprocess_0",
                   "dma_ip": "image_dma_0", "dma_length_width": length})
    return result


def reference_half_bgr(frame):
    """Exact integer golden for self-test/benchmark only, not live processing."""
    value = np.asarray(frame, dtype=np.uint16)
    return ((value[0::2, 0::2] + value[0::2, 1::2]
             + value[1::2, 0::2] + value[1::2, 1::2] + 2) >> 2).astype(np.uint8)


class BoundedSimpleDMA:
    """AXI DMA 7.1 simple-mode MMIO driver with bounded start/transfer polling.

    Stock PYNQ DMA.start/stop/wait contain unbounded loops. This deliberately
    uses the published AXI DMA register protocol and PYNQ contiguous buffers.
    """
    def __init__(self, mmio, length_width, timeout):
        self.mmio, self.timeout = mmio, float(timeout)
        self.max_bytes = (1 << int(length_width)) - 1
        self._active = {0: None, 0x30: None}
        self._first = {0: True, 0x30: True}
        for offset in (0x30, 0):
            self.mmio.write(offset, 1)
            deadline = time.monotonic() + self.timeout
            while self.mmio.read(offset + 4) & 1:
                if time.monotonic() >= deadline:
                    raise FPGAError("DMA 启动超时，拒绝无限等待", 504)
                time.sleep(0.0002)

    def arm(self, offset, buffer, nbytes):
        if not 0 < nbytes <= self.max_bytes or nbytes > buffer.nbytes:
            raise FPGAError("DMA 传输长度非法")
        status = int(self.mmio.read(offset + 4))
        if status & 0x771 or (not self._first[offset] and not status & 2):
            raise FPGAError("DMA 非空闲或已故障：0x%08X" % status)
        address = int(buffer.physical_address)
        if address & 7:
            raise FPGAError("DMA CMA 地址必须为 8 字节对齐")
        # Both buffers can have dirty CPU cache lines after allocate/reuse.
        buffer.flush()
        self.mmio.write(offset + 0x18, address & 0xFFFFFFFF)
        self.mmio.write(offset + 0x1C, address >> 32)
        self._active[offset] = buffer
        self._first[offset] = False
        self.mmio.write(offset + 0x28, nbytes)

    def poll(self):
        statuses = [int(self.mmio.read(offset + 4)) for offset in (0, 0x30)]
        if any(status & 0x770 for status in statuses):
            raise FPGAError("DMA 传输故障：MM2S=0x%08X S2MM=0x%08X" % tuple(statuses))
        if any(status & 1 for status in statuses):
            raise FPGAError("DMA 传输意外停止")
        return all(status & 2 for status in statuses)

    def finish_receive(self, expected_bytes):
        actual = int(self.mmio.read(0x58))
        if actual != expected_bytes:
            raise FPGAError("DMA 输出长度不匹配：%d，应为 %d" % (actual, expected_bytes))
        self._active[0x30].invalidate()
        self._active = {0: None, 0x30: None}

    def stop(self):
        for offset in (0, 0x30):
            self.mmio.write(offset, 0)
            deadline = time.monotonic() + self.timeout
            while not self.mmio.read(offset + 4) & 1:
                if time.monotonic() >= deadline:
                    raise FPGAError("DMA 停止超时，保留缓冲区直到重新加载位流", 504)
                time.sleep(0.0002)


class FPGAImageProcessor:
    def __init__(self, overlay, timeout=2.0, fault_callback=None):
        if not 0.01 <= float(timeout) <= 30:
            raise ValueError("图像超时必须为 0.01～30 秒")
        self.overlay, self.timeout = overlay, float(timeout)
        self._fault_callback = fault_callback
        self._lock = threading.RLock()
        self._ready = False
        self._error = "图像 FPGA 尚未初始化"
        self._ip = self._dma = None
        self._input = self._output = None
        self._shape = None
        self._poisoned = False
        self._self_test = False
        self._completed = self._cycles = 0
        self._processing_ms = None
        self._input_width = self._input_height = 0
        self._width = self._height = 0
        self._self_test_digest = None

    def initialize(self, after_overlay_download=False):
        with self._lock:
            try:
                if not self.overlay or not self.overlay.is_loaded():
                    raise FPGAError("必须先成功下载本次 v2 Overlay")
                if after_overlay_download:
                    while _QUARANTINED_BUFFERS:
                        _QUARANTINED_BUFFERS.pop().freebuffer()
                image_meta = self.overlay.ip_dict.get("image_preprocess_0", {})
                dma_meta = self.overlay.ip_dict.get("image_dma_0", {})
                if image_meta.get("type") != IMAGE_VLNV or dma_meta.get("type") != "xilinx.com:ip:axi_dma:7.1":
                    raise FPGAError("缺少 v2 图像处理和 AXI DMA IP，不能使用旧版 bit/HWH")
                if _meta_int(image_meta.get("addr_range"), "图像地址范围") < 44:
                    raise FPGAError("图像 IP 寄存器范围不完整")
                self._ip = self.overlay.image_preprocess_0
                if int(self._ip.read(12)) != 0x50494D32:
                    raise FPGAError("图像处理硬件 ID 不匹配，应为 0x50494D32")
                if int(self._ip.read(36)) != MAX_WIDTH or int(self._ip.read(40)) != MAX_HEIGHT:
                    raise FPGAError("图像处理硬件尺寸上限不兼容")
                control = int(self._ip.read(0))
                if control & 9:
                    raise FPGAError("图像处理 IP 下载后不是无故障空闲状态")
                params = {str(key).lower(): value for key, value in dma_meta.get("parameters", {}).items()}
                length = _meta_int(params.get("c_sg_length_width"), "DMA 长度位宽")
                if not 22 <= length <= 26 or _meta_int(params.get("c_include_sg"), "DMA SG") != 0:
                    raise FPGAError("DMA 长度位宽或模式不兼容")
                from pynq import MMIO
                address = _meta_int(dma_meta.get("phys_addr"), "DMA 地址")
                span = _meta_int(dma_meta.get("addr_range"), "DMA 地址范围")
                if address % 4 or span < 0x5C:
                    raise FPGAError("DMA MMIO 地址范围无效")
                self._dma = BoundedSimpleDMA(MMIO(address, span), length, self.timeout)
                test = ((np.arange(4 * 8 * 3, dtype=np.uint16).reshape(4, 8, 3) * 37 + 11) % 256).astype(np.uint8)
                actual, evidence = self._process(test)
                expected = reference_half_bgr(test)
                if not np.array_equal(actual, expected):
                    raise FPGAError("FPGA 图像数值自检失败：输出与逐像素整数参考不同")
                self._self_test_digest = hashlib.sha256(actual.tobytes()).hexdigest()
                self._self_test, self._ready, self._error = True, True, None
            except Exception as exc:
                self._fault("图像 FPGA 初始化失败：%s" % exc)
            return self.status()

    def _fault(self, reason):
        self._ready = False
        self._error = str(reason)
        self._poisoned = True
        if self._fault_callback:
            self._fault_callback(self._error)

    def status(self):
        # Cached only: status/HTTP must not keep reading a possibly sick bus.
        with self._lock:
            return {"ready": self._ready, "backend": "fpga", "pixel_pipeline": PIXEL_PIPELINE,
                    "self_test_passed": self._self_test, "self_test_sha256": self._self_test_digest,
                    "completed_count": self._completed, "last_cycles": self._cycles,
                    "last_processing_ms": self._processing_ms, "input_width": self._input_width,
                    "input_height": self._input_height, "width": self._width, "height": self._height,
                    "hardware_id": "0x50494D32", "error": self._error,
                    "max_width": MAX_WIDTH, "max_height": MAX_HEIGHT,
                    "timing_scope": "pack + DMA + PL + unpack, excluding capture/JPEG/network"}

    def process(self, frame):
        with self._lock:
            if not self._ready:
                raise FPGAError(self._error or "图像 FPGA 不可用")
            if not self.overlay.is_loaded():
                self._fault("Overlay 已被其他程序替换，请停止服务并重新加载")
                raise FPGAError(self._error)
            try:
                return self._process(frame)
            except Exception as exc:
                self._fault("图像 DMA/PL 处理失败，无 CPU 回退：%s" % exc)
                if isinstance(exc, FPGAError):
                    raise FPGAError(self._error, exc.status) from exc
                raise FPGAError(self._error) from exc

    def _buffers(self, height, width):
        if self._shape == (height, width):
            return
        if self._input is not None:
            self._input.freebuffer()
        if self._output is not None:
            self._output.freebuffer()
        self._input = self._output = None
        self._shape = None
        from pynq import allocate
        self._input = allocate(shape=(height, width), dtype=np.uint32)
        self._output = allocate(shape=(height // 2, width // 2), dtype=np.uint32)
        self._shape = (height, width)

    def _process(self, frame):
        if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
            raise FPGAError("摄像头输入必须为 uint8 BGR 三通道图像")
        height, width = frame.shape[:2]
        if width % 2 or height % 2 or not 2 <= width <= MAX_WIDTH or not 2 <= height <= MAX_HEIGHT:
            raise FPGAError("FPGA 输入宽高必须为偶数，且不超过 1280×720；请调整摄像头格式")
        started = time.monotonic()
        self._buffers(height, width)
        # B inbits7:0, G15:8, R23:16, unusedtopbyte zero.
        packed = self._input.view(np.uint8).reshape(height, width, 4)
        packed[:, :, :3] = frame
        packed[:, :, 3] = 0
        before = int(self._ip.read(28)) & 0xFFFFFFFF
        control = int(self._ip.read(0))
        if control & 9:
            raise FPGAError("图像 IP 非空闲或已故障")
        self._ip.write(4, int(width))
        self._ip.write(8, int(height))
        # Arm S2MM FIRST so no output can be lost to backpressure.
        self._dma.arm(0x30, self._output, width * height)
        self._ip.write(0, 1)
        self._dma.arm(0, self._input, width * height * 4)
        deadline = time.monotonic() + self.timeout
        while True:
            control = int(self._ip.read(0))
            if control & 8:
                code = int(self._ip.read(32))
                raise FPGAError("图像 PL fault %d：%s" % (code, ERROR_CODES.get(code, "未知错误")))
            dma_done = self._dma.poll()
            if control & 2 and not control & 1 and dma_done:
                break
            if time.monotonic() >= deadline:
                raise FPGAError("等待图像 DMA/PL 完成超时，不会改为 CPU 图像处理；请重载位流", 504)
            time.sleep(0.0002)
        self._dma.finish_receive(width * height)
        completed = int(self._ip.read(28)) & 0xFFFFFFFF
        cycles = int(self._ip.read(24)) & 0xFFFFFFFF
        inputs, outputs = int(self._ip.read(16)), int(self._ip.read(20))
        if ((completed - before) & 0xFFFFFFFF) != 1 or not cycles or inputs != width * height or outputs != width * height // 4:
            raise FPGAError("图像硬件计数/周期不符合本次 DMA 帧，拒绝使用输出")
        raw = self._output.view(np.uint8).reshape(height // 2, width // 2, 4)
        if np.any(raw[:, :, 3]):
            raise FPGAError("图像 BGRX 输出保留字节非零，协议不匹配")
        result = raw[:, :, :3].copy()
        self._completed, self._cycles = completed, cycles
        self._processing_ms = (time.monotonic() - started) * 1000
        self._input_width, self._input_height = int(width), int(height)
        self._width, self._height = width // 2, height // 2
        evidence = {"backend": "fpga", "pixel_pipeline": PIXEL_PIPELINE,
                    "completed_count": completed, "last_cycles": cycles,
                    "input_count": inputs, "output_count": outputs,
                    "processing_ms": self._processing_ms, "hardware_completed": True}
        return result, evidence

    def close(self):
        with self._lock:
            self._ready = False
            if self._dma is not None and not self._poisoned:
                try:
                    self._dma.stop()
                except Exception as exc:
                    self._poisoned = True
                    self._error = str(exc)
            for buffer in (self._input, self._output):
                if buffer is not None:
                    if self._poisoned:
                        _QUARANTINED_BUFFERS.append(buffer)
                    else:
                        buffer.freebuffer()
            self._input = self._output = None
            self._ip = self._dma = None
            self._shape = None
