"""Software/mocked DMA regression tests. They do not prove on-board operation."""
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from urllib.request import urlopen
from urllib.error import HTTPError

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from exercise_counter import ExerciseCounter, CounterError
from fpga_classifier import FPGAError, FPGAKNNClassifier
from fpga_image import (BoundedSimpleDMA, FPGAImageProcessor, reference_half_bgr,
                        IMAGE_VLNV, validate_v2_hwh)
from fpga_pose_server import FPGAPoseServer


class CounterTests(unittest.TestCase):
    def setUp(self):
        self.c = ExerciseCounter()
        self.mid = self.c.start("model", "蹲起", "站立", "下蹲", .2, 1.0)["monitor_id"]
        self.seq = 0

    def observe(self, stamp, label, completed=True):
        self.seq += 1
        return self.c.observe(self.mid, "model", self.seq, stamp, label, completed)

    def stable(self, stamp, label):
        self.observe(stamp, label)
        return self.observe(stamp + .21, label)

    def test_full_cycle_counts_once_and_holds_do_not_repeat(self):
        self.stable(0, "站立")
        self.stable(.4, "下蹲")
        self.assertEqual(self.observe(.8, "下蹲")["count"], 0)
        self.assertEqual(self.stable(1, "站立")["count"], 1)
        self.assertEqual(self.observe(1.5, "站立")["count"], 1)
        self.assertEqual(self.observe(1.7, "站立")["count"], 1)

    def test_no_start_no_count(self):
        self.stable(0, "下蹲")
        self.assertEqual(self.stable(.4, "站立")["count"], 0)

    def test_short_label_oscillation_never_confirms_target(self):
        self.stable(0, "站立")
        for stamp, label in ((.3,"下蹲"),(.4,"站立"),(.5,"下蹲"),(.6,"站立")):
            self.observe(stamp, label)
        self.assertEqual(self.stable(.7, "站立")["count"], 0)

    def test_camera_gap_cancels_unfinished_cycle(self):
        self.stable(0, "站立")
        self.stable(.4, "下蹲")
        self.assertEqual(self.stable(3, "站立")["count"], 0)

    def test_long_unknown_cancels_unfinished_cycle(self):
        self.stable(0, "站立")
        self.stable(.4, "下蹲")
        self.observe(.8, None, False)
        self.observe(1.4, None, False)
        self.observe(1.9, None, False)
        self.assertEqual(self.stable(2.1, "站立")["count"], 0)

    def test_slow_valid_transition_does_not_act_like_tracking_loss(self):
        self.stable(0, "站立")
        for stamp in (.4,.9,1.4,1.9,2.4,2.9):self.observe(stamp,None,True)
        self.stable(3.2,"下蹲")
        for stamp in (3.8,4.3,4.8,5.3,5.8,6.3):self.observe(stamp,None,True)
        self.assertEqual(self.stable(6.7,"站立")["count"],1)

    def test_unconfirmed_hardware_label_cannot_advance(self):
        self.stable(0, "站立")
        self.observe(.4, "下蹲", False)
        self.observe(.7, "下蹲", False)
        self.assertEqual(self.stable(.9, "站立")["count"], 0)

    def test_pause_resume_cancels_partial_cycle_preserving_completed_count(self):
        self.stable(0,"站立"); self.stable(.4,"下蹲"); self.stable(.8,"站立")
        self.stable(1.2,"下蹲")
        self.c.control("pause",self.mid)
        self.c.control("resume",self.mid)
        self.assertEqual(self.stable(1.6,"站立")["count"],1)

    def test_reset_preserves_pause_and_clears_count(self):
        self.stable(0,"站立"); self.stable(.4,"下蹲"); self.stable(.8,"站立")
        self.c.control("pause",self.mid)
        snap = self.c.control("reset",self.mid)
        self.assertEqual(snap["count"],0)
        self.assertTrue(snap["paused"])

    def test_duplicate_sequence_and_out_of_order_time_rejected(self):
        self.observe(1,"站立")
        for seq, stamp in ((1,1.1),(0,1.2),(2,.9)):
            with self.assertRaises(CounterError):
                self.c.observe(self.mid,"model",seq,stamp,"站立",True)
        self.assertEqual(self.c.snapshot()["last_frame_seq"],1)

    def test_model_session_mismatch_rejected(self):
        with self.assertRaises(CounterError):
            self.c.observe(self.mid,"different",1,1,"站立",True)
        self.c.invalidate()
        with self.assertRaises(CounterError):
            self.c.observe(self.mid,"model",1,1,"站立",True)

    def test_fault_deactivation_preserves_completed_count(self):
        self.stable(0,"站立");self.stable(.4,"下蹲");self.stable(.8,"站立")
        state=self.c.invalidate("硬件故障",preserve_count=True)
        self.assertEqual(state["count"],1)
        self.assertFalse(state["active"])
        self.assertIsNone(state["monitor_id"])

    def test_bad_settings_rejected(self):
        for dwell, gap in ((0,1),(.2,.2),(float("nan"),1)):
            with self.assertRaises(CounterError):
                self.c.start("m","x","a","b",dwell,gap)


class MockBuffer(np.ndarray):
    next_address = 0x100000
    def __new__(cls, shape, dtype):
        result = np.zeros(shape,dtype=dtype).view(cls)
        result.physical_address = cls.next_address
        cls.next_address += 0x400000
        result.freed = False
        result.flushes = result.invalidates = 0
        return result
    def flush(self): self.flushes += 1
    def invalidate(self): self.invalidates += 1
    def freebuffer(self): self.freed = True


class MockImageIP:
    def __init__(self):
        self.reg = {0:0,12:0x50494D32,28:0,36:1280,40:720}
    def read(self, offset): return self.reg.get(offset,0)
    def write(self, offset, value):
        self.reg[offset] = value
        if offset == 0 and value == 1: self.reg[0] = 1


class MockDMA:
    def __init__(self, ip, buffers, mode="success"):
        self.ip,self.buffers,self.mode = ip,buffers,mode
        self.reg={4:1,0x34:1}
        self.events=[]
        self.reads=0
    def read(self,offset):
        self.reads+=1
        return self.reg.get(offset,0)
    def write(self,offset,value):
        self.events.append((offset,value))
        self.reg[offset]=value
        if offset in (0,0x30):
            self.reg[offset+4] = 1 if value == 0 else (1 if self.mode=="start_timeout" else 2)
        if offset == 0x58: self.reg[0x34] = 0
        if offset == 0x28:
            self.reg[4]=0
            if self.mode == "error": self.reg[4]=0x40
            if self.mode in ("success", "wrong_length", "wrong_pixels", "wrong_counts"):
                src=self.buffers[self.reg[0x18]]
                dst=self.buffers[self.reg[0x48]]
                h,w=self.ip.reg[8],self.ip.reg[4]
                source=src.view(np.uint8).reshape(h,w,4)[:,:,:3]
                out=dst.view(np.uint8).reshape(h//2,w//2,4)
                out[:,:,:3]=reference_half_bgr(source)
                out[:,:,3]=0
                self.ip.reg.update({0:2,16:w*h,20:w*h//4,24:w*h+7,28:self.ip.reg[28]+1})
                self.reg[4]=self.reg[0x34]=2
                if self.mode == "wrong_length":self.reg[0x58]-=4
                if self.mode == "wrong_pixels":out[0,0,0]^=1
                if self.mode == "wrong_counts":self.ip.reg[20]+=1


class ImageTests(unittest.TestCase):
    def run_processor(self, mode="success"):
        buffers={}
        ip=MockImageIP()
        mmio=MockDMA(ip,buffers,mode)
        def allocate(shape,dtype):
            buf=MockBuffer(shape,dtype);buffers[buf.physical_address]=buf;return buf
        fake_pynq=types.SimpleNamespace(allocate=allocate,MMIO=lambda a,s:mmio)
        overlay=types.SimpleNamespace(is_loaded=lambda:True,image_preprocess_0=ip,
            ip_dict={"image_preprocess_0":{"type":IMAGE_VLNV,"addr_range":65536},
                     "image_dma_0":{"type":"xilinx.com:ip:axi_dma:7.1","phys_addr":0x40400000,
                                    "addr_range":65536,"parameters":{"c_sg_length_width":"23","c_include_sg":"0"}}})
        faults=[]
        p=FPGAImageProcessor(overlay,timeout=.01,fault_callback=faults.append)
        return p,mmio,buffers,fake_pynq,faults

    def test_numerical_selftest_and_live_rounding_count_evidence(self):
        p,mmio,buffers,fake,faults=self.run_processor()
        with patch.dict(sys.modules,{"pynq":fake}):
            self.assertTrue(p.initialize()["self_test_passed"])
            frame=np.arange(6*10*3,dtype=np.uint8).reshape(6,10,3)
            actual,evidence=p.process(frame)
            self.assertTrue(np.array_equal(actual,reference_half_bgr(frame)))
            self.assertEqual(evidence["completed_count"],2)
            self.assertEqual(evidence["input_count"],60)
            self.assertEqual(evidence["output_count"],15)
            arms=[offset for offset,value in mmio.events if offset in (0x58,0x28)]
            self.assertEqual(arms,[0x58,0x28,0x58,0x28])
            self.assertEqual(p._input.flushes,1)
            self.assertEqual(p._output.invalidates,1)
            p.close()
        self.assertFalse(faults)

    def test_dma_error_never_produces_frame_and_status_never_rereads_bus(self):
        p,mmio,buffers,fake,faults=self.run_processor("error")
        with patch.dict(sys.modules,{"pynq":fake}):
            state=p.initialize()
            self.assertFalse(state["ready"])
            self.assertFalse(state["self_test_passed"])
            reads=mmio.reads
            for _ in range(3):p.status()
            self.assertEqual(mmio.reads,reads)
            with self.assertRaises(FPGAError):p.process(np.zeros((4,8,3),np.uint8))
            self.assertEqual(mmio.reads,reads)
            p.close()
            self.assertTrue(all(not b.freed for b in buffers.values()))
        self.assertTrue(faults)

    def test_timeout_is_bounded_and_preserves_active_buffers(self):
        p,mmio,buffers,fake,faults=self.run_processor("timeout")
        with patch.dict(sys.modules,{"pynq":fake}):
            started=time.monotonic()
            state=p.initialize()
            self.assertFalse(state["ready"])
            self.assertIn("超时",state["error"])
            self.assertLess(time.monotonic()-started,.2)
            p.close()
            self.assertTrue(all(not b.freed for b in buffers.values()))

    def test_dma_start_is_bounded(self):
        mmio=MockDMA(MockImageIP(),{},"start_timeout")
        started=time.monotonic()
        with self.assertRaises(FPGAError):BoundedSimpleDMA(mmio,23,.01)
        self.assertLess(time.monotonic()-started,.2)

    def test_corrupt_pixel_length_and_hardware_counts_reject_startup(self):
        for mode in ("wrong_length", "wrong_pixels", "wrong_counts"):
            with self.subTest(mode=mode):
                p,mmio,buffers,fake,faults=self.run_processor(mode)
                with patch.dict(sys.modules,{"pynq":fake}):
                    state=p.initialize()
                    self.assertFalse(state["ready"])
                    self.assertFalse(state["self_test_passed"])
                    p.close()
                self.assertTrue(faults)

    def test_odd_dimension_has_no_cpu_resize_fallback(self):
        p,mmio,buffers,fake,faults=self.run_processor()
        with patch.dict(sys.modules,{"pynq":fake}):
            self.assertTrue(p.initialize()["ready"])
            with self.assertRaises(FPGAError):p.process(np.zeros((5,8,3),np.uint8))
            self.assertFalse(p.status()["ready"])
            p.close()

    def test_classifier_unavailable_status_does_not_touch_mmio(self):
        c=FPGAKNNClassifier()
        c._ip=types.SimpleNamespace(read=lambda offset:(_ for _ in ()).throw(AssertionError("MMIO")))
        c.mark_unavailable("DMA故障")
        self.assertFalse(c.hardware_status()["ready"])


def make_hwh(include_v2=True):
    root=ET.Element("SYSTEM")
    def module(name, params, ports, vlnv=None):
        attr={"INSTANCE":name}
        if vlnv:attr["VLNV"]=vlnv
        m=ET.SubElement(root,"MODULE",attr)
        for k,v in params.items():ET.SubElement(m,"PARAMETER",NAME=k,VALUE=str(v))
        for k,v in ports.items():ET.SubElement(m,"PORT",NAME=k,SIGNAME=v)
    module("const_one",{"CONST_WIDTH":1,"CONST_VAL":1},{"dout":"one"})
    module("const_zero",{"CONST_WIDTH":1,"CONST_VAL":0},{"dout":"zero"})
    module("processing_system7_0",{},{"FCLK_RESET0_N":"ps_reset"})
    module("rst_fclk0",{"C_EXT_RESET_HIGH":0,"C_AUX_RESET_HIGH":0},
           {"aux_reset_in":"one","dcm_locked":"one","mb_debug_sys_rst":"zero",
            "ext_reset_in":"ps_reset","peripheral_aresetn":"resetn"})
    module("pose_distance_0",{},{"s_axi_aresetn":"resetn"})
    if include_v2:
        module("image_preprocess_0",{},{"s_axi_aresetn":"resetn"},IMAGE_VLNV)
        module("image_dma_0",{"C_INCLUDE_SG":0,"C_MICRO_DMA":0,"C_INCLUDE_MM2S":1,
            "C_INCLUDE_S2MM":1,"C_M_AXIS_MM2S_TDATA_WIDTH":32,"C_S_AXIS_S2MM_TDATA_WIDTH":32,
            "C_SG_LENGTH_WIDTH":23},{"axi_resetn":"resetn"})
    return root


class GuardTests(unittest.TestCase):
    def check(self,root):
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/"pose.hwh"
            ET.ElementTree(root).write(file)
            return validate_v2_hwh(file)
    def test_valid_v2_file_only(self):self.assertEqual(self.check(make_hwh())["dma_length_width"],23)
    def test_old_overlay_rejected(self):
        with self.assertRaises(FPGAError):self.check(make_hwh(False))
    def test_small_dma_length_rejected(self):
        root=make_hwh()
        for item in root.iter("PARAMETER"):
            if item.get("NAME")=="C_SG_LENGTH_WIDTH":item.set("VALUE","14")
        with self.assertRaises(FPGAError):self.check(root)
    def test_bad_reset_rejected(self):
        root=make_hwh()
        for item in root.iter("PORT"):
            if item.get("NAME")=="aux_reset_in":item.set("SIGNAME","zero")
        with self.assertRaises(FPGAError):self.check(root)
    def test_image_dma_reset_is_checked_before_download(self):
        for name, port in (("image_preprocess_0", "s_axi_aresetn"), ("image_dma_0", "axi_resetn")):
            root=make_hwh()
            module=next(item for item in root.iter("MODULE") if item.get("INSTANCE")==name)
            next(item for item in module.iter("PORT") if item.get("NAME")==port).set("SIGNAME","wrong")
            with self.assertRaises(FPGAError):self.check(root)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.now=100.0
        self.s=FPGAPoseServer(clock=lambda:self.now)
        self.s._running=True
        self.s.image=types.SimpleNamespace(status=lambda:{"ready":True})
        self.calls=0
        def predict(model,features):
            self.calls+=1
            return {"label":"站立","hardware_completed":True,"backend":"fpga","inference_count":self.calls}
        self.s.classifier=types.SimpleNamespace(_ready=True,_model_id="model",
            _model={"classes":["站立","下蹲"]},_inference_count=0,predict=predict)
        self.s._profile={"exercise_id":"squats","name":"深蹲","start_label":"站立",
                         "target_label":"下蹲","dwell_seconds":.25,"max_gap_seconds":1.5}
        now=self.now+3
        self.records={seq:{"frame_seq":seq,"capture_time":now+seq*.01,
                          "image":{"hardware_completed":True,"completed_count":seq}}
                      for seq in range(11,20)}
        def frame_record(seq):
            if seq not in self.records:raise FPGAError("stale",409)
            return self.records[seq]
        self.s._camera_service=types.SimpleNamespace(status=lambda:{"frame_id":10},frame_record=frame_record)
        result=self.s.monitor({"action":"start","model_id":"model","name":"深蹲",
                               "start_label":"站立","target_label":"下蹲"})
        self.mid=result["monitor_id"]
        self.start_result=result
        self.now+=3.05
    def body(self,seq,features=None):
        return {"monitor_id":self.mid,"model_id":"model","frame_seq":seq,"features":features}
    def test_null_features_records_loss_without_fake_classification(self):
        result=self.s.predict(self.body(11))
        self.assertFalse(result["hardware_completed"])
        self.assertEqual(result["counter"]["last_frame_seq"],11)
        self.assertEqual(self.calls,0)

    def test_monitor_returns_authoritative_capture_and_sequence_floors(self):
        self.assertEqual(self.start_result["frame_seq_floor"],10)
        self.assertEqual(self.start_result["capture_time_floor"],self.s._monitor_capture_floor)
        self.assertEqual(self.start_result["capture_time_floor"],103.0)
        for action in ("pause","resume","reset"):
            before=self.now
            result=self.s.monitor({"action":action,"monitor_id":self.mid,
                                   "capture_time_floor":-1,"frame_seq_floor":-1})
            after=self.now+3 if action=="resume" else self.now
            self.assertEqual(result["frame_seq_floor"],10)
            self.assertGreaterEqual(result["capture_time_floor"],before)
            self.assertLessEqual(result["capture_time_floor"],after)
            self.assertEqual(result["capture_time_floor"],self.s._monitor_capture_floor)

    def test_duplicate_frame_rejected_before_second_pl_job(self):
        result=self.s.predict(self.body(11,[0]*99))
        self.assertTrue(result["image"]["hardware_completed"])
        with self.assertRaises(FPGAError):self.s.predict(self.body(11,[0]*99))
        self.assertEqual(self.calls,1)
    def test_stale_frame_rejected_before_pl_job(self):
        with self.assertRaises(FPGAError):self.s.predict(self.body(999,[0]*99))
        self.assertEqual(self.calls,0)
    def test_pre_control_capture_with_new_sequence_is_rejected(self):
        self.records[11]["capture_time"] = self.s._monitor_capture_floor - .01
        with self.assertRaises(FPGAError):self.s.predict(self.body(11,[0]*99))
        self.assertEqual(self.calls,0)
    def test_client_timestamp_is_ignored(self):
        body=self.body(11)
        body["capture_time"]=float("inf")
        result=self.s.predict(body)
        self.assertEqual(result["capture_time"],self.records[11]["capture_time"])
    def test_wrong_model_cannot_advance(self):
        body=self.body(11,[0]*99);body["model_id"]="wrong"
        with self.assertRaises(FPGAError):self.s.predict(body)
        self.assertEqual(self.calls,0)


class StartupHTTPTests(unittest.TestCase):
    def test_missing_new_overlay_keeps_diagnostic_page_without_raw_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            page=Path(directory)/"index.html"
            page.write_text("<html>diagnostic</html>")
            service=FPGAPoseServer(host="127.0.0.1",port=0,page_path=str(page),
                                  bit_path=str(Path(directory)/"missing.bit"))
            fake=types.SimpleNamespace(Overlay=lambda *a,**kw:(_ for _ in ()).throw(AssertionError("download")))
            with patch.dict(sys.modules,{"pynq":fake}):
                service.start()
                try:
                    base="http://127.0.0.1:%d"%service.port
                    with urlopen(base+"/api/status") as response:
                        result=json.load(response)
                    self.assertFalse(result["ready"])
                    self.assertFalse(result["image"]["self_test_passed"])
                    self.assertIn("HWH",result["error"])
                    with urlopen(base+"/") as response:
                        self.assertIn(b"diagnostic",response.read())
                    with self.assertRaises(HTTPError) as caught:urlopen(base+"/api/frame")
                    self.assertEqual(caught.exception.code,503)
                    self.assertEqual(json.loads(caught.exception.read())["backend"],"fpga")
                finally:
                    service.stop()


if __name__=="__main__":unittest.main(verbosity=2)
