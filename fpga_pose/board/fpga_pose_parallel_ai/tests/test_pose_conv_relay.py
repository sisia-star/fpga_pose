"""Protocol-only tests; fake convolution is named and never used in production."""
import base64,threading,time,types,unittest
from pose_conv_relay import PoseConvRelay
from fpga_classifier import FPGAError

class PoseConvRelayTests(unittest.TestCase):
    def setUp(self):
        self.calls=0
        def convolve(features):
            self.calls+=1
            return {'values':[0.]*195,'evidence':{'hardware_completed':True,'backend':'fpga-convolution','hardware_count':self.calls}}
        self.owner=types.SimpleNamespace(ai_coach=types.SimpleNamespace(pairing_token='unit-test-token'),
            convolution=types.SimpleNamespace(process=convolve),
            _camera_service=types.SimpleNamespace(frame_record=lambda seq:{'frame_seq':seq}))
        self.relay=PoseConvRelay(self.owner)
        self.body={'frame_seq':1,'jpeg_b64':base64.b64encode(b'\xff\xd8test-only-jpeg').decode()}
        self.token={'token':'unit-test-token'}
    def start_job(self):
        self.relay.last_seen=time.monotonic()
        self.result={};self.error=[]
        def run():
            try:self.result.update(self.relay.submit(self.body))
            except Exception as e:self.error.append(e)
        self.thread=threading.Thread(target=run);self.thread.start()
        deadline=time.monotonic()+1
        while self.relay.job is None and time.monotonic()<deadline:time.sleep(.001)
        return self.relay.poll(self.token)['job']
    def tearDown(self):
        self.relay.close()
        if hasattr(self,'thread'):self.thread.join(2)
    def test_unauthorized_and_unconnected_are_rejected(self):
        with self.assertRaises(FPGAError):self.relay.poll({'token':'wrong'})
        with self.assertRaises(FPGAError):self.relay.submit(self.body)
        self.assertEqual(self.calls,0)
    def test_landmarks_require_real_convolution_protocol_evidence(self):
        job=self.start_job()
        points=[dict(x=0.,y=0.,z=0.,visibility=1.,presence=1.)for _ in range(33)]
        with self.assertRaises(FPGAError):
            self.relay.complete({**self.token,'job_id':job['id'],'points':points})
        self.relay.convolve({**self.token,'job_id':job['id'],'features':[0.]*768})
        self.relay.complete({**self.token,'job_id':job['id'],'points':points})
        self.thread.join(1)
        self.assertFalse(self.error);self.assertEqual(self.result['frame_seq'],1)
        self.assertTrue(self.result['convolution']['hardware_completed'])
    def test_no_person_can_complete_without_running_head(self):
        job=self.start_job()
        self.relay.complete({**self.token,'job_id':job['id'],'points':[]})
        self.thread.join(1);self.assertEqual(self.result['points'],[]);self.assertEqual(self.calls,0)
    def test_expired_and_overlapping_requests_are_rejected(self):
        job=self.start_job()
        with self.assertRaises(FPGAError):self.relay.submit(self.body)
        with self.assertRaises(FPGAError):self.relay.convolve({**self.token,'job_id':'expired','features':[]})
        self.relay.complete({**self.token,'job_id':job['id'],'points':[],'error':'test stop'})
        self.thread.join(1);self.assertIsInstance(self.error[0],FPGAError)

if __name__=='__main__':unittest.main()
