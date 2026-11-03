import unittest,sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from training_session import TrainingSession
from exercise_counter import CounterError
from ai_coach import validate_telemetry
from fpga_classifier import validate_model,hold_geometry_reason

class Clock:
    def __init__(self):self.now=100.
    def __call__(self):return self.now

class TrainingTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock();self.s=TrainingSession(self.clock)
        self.mid=self.s.start('m','x','other','hold',.2,1.5)['monitor_id'];self.seq=0
    def configure(self,mode='hold',duration=5):self.s.configure_training(mode,duration)
    def obs(self,t,label='hold',done=True):
        self.clock.now=t;self.seq+=1
        return self.s.observe(self.mid,'m',self.seq,t,label,done)
    def test_countdown_excluded_and_deadline_without_frames(self):
        self.configure('count',5);self.clock.now=102.9;self.assertEqual(self.s.snapshot()['elapsed_seconds'],0)
        self.clock.now=107.9;self.assertFalse(self.s.snapshot()['finished'])
        self.clock.now=108;self.assertTrue(self.s.snapshot()['finished'])
        self.assertEqual(self.s.snapshot()['remaining_seconds'],0)
        self.assertEqual(self.s.snapshot()['elapsed_seconds'],5)
    def test_pause_and_resume_countdown_dont_consume_budget(self):
        self.configure('count',10);self.clock.now=105;self.s.control('pause',self.mid)
        self.clock.now=200;self.assertEqual(self.s.snapshot()['elapsed_seconds'],2)
        self.s.control('resume',self.mid);self.clock.now=203;self.assertEqual(self.s.snapshot()['elapsed_seconds'],2)
        self.clock.now=211;self.assertTrue(self.s.snapshot()['finished'])
    def test_time_up_cannot_count_inflight_cycle(self):
        self.configure('count',5)
        for t,l in [(103,'other'),(103.3,'other'),(104,'hold'),(104.3,'hold')]:self.obs(t,l)
        self.obs(108.1,'other');self.assertEqual(self.s.snapshot()['count'],0)
    def test_complete_cycle_inside_time(self):
        self.configure('count',5)
        for t,l in [(103,'other'),(103.3,'other'),(104,'hold'),(104.3,'hold'),(105,'other'),(105.3,'other')]:self.obs(t,l)
        self.assertEqual(self.s.snapshot()['count'],1)
    def test_valid_hold_completes_at_target_without_repetitions(self):
        self.configure()
        for i in range(28):self.obs(103+i*.2)
        s=self.s.snapshot();self.assertTrue(s['finished']);self.assertEqual(s['effective_hold_seconds'],5);self.assertEqual(s['count'],0)
        self.clock.now=1000;self.assertEqual(self.s.snapshot()['effective_hold_seconds'],5)
    def test_missing_or_rejected_frames_do_not_earn_time(self):
        self.configure();self.obs(103);self.obs(103.3);before=self.s.effective
        self.obs(103.6,None,False);self.obs(104);self.assertEqual(self.s.effective,before)
        self.obs(104.3);self.assertGreater(self.s.effective,before)
    def test_snapshot_never_adds_hold_time(self):
        self.configure();self.obs(103);self.obs(103.3);before=self.s.effective
        self.clock.now=200;self.assertEqual(self.s.snapshot()['effective_hold_seconds'],round(before,3));self.assertFalse(self.s.finished)
        self.obs(201);self.assertEqual(self.s.effective,before)
    def test_duplicates_do_not_earn_time(self):
        self.configure();self.obs(103)
        with self.assertRaises(CounterError):self.s.observe(self.mid,'m',self.seq,103.3,'hold',True)
        self.assertEqual(self.s.effective,0)
    def test_pause_breaks_hold_continuity(self):
        self.configure();self.obs(103);self.obs(103.3);before=self.s.effective
        self.s.control('pause',self.mid);self.clock.now=110;self.s.control('resume',self.mid);self.obs(113.1)
        self.assertEqual(before,self.s.effective)
    def test_reset_completed_session_preserves_configuration(self):
        self.configure('count',5);self.clock.now=109;self.assertTrue(self.s.snapshot()['finished'])
        self.s.control('reset',self.mid);self.assertFalse(self.s.finished);self.assertEqual(self.s.duration,5);self.assertEqual(self.s.snapshot()['remaining_seconds'],5)
    def test_bad_duration_and_modes(self):
        for d in [True,0,4,3601,float('nan'),'60']:
            with self.assertRaises(CounterError):self.configure(duration=d)
    def test_hold_ai_accepts_zero_reps_and_requires_measured_seconds(self):
        p={'exercise_id':'birddog','monitor_id':'m','completed_count':0,'window_reps':0,'measurement':{'accepted_frames':10},'training':{'mode':'hold','effective_hold_seconds':3,'duration_seconds':10}}
        self.assertEqual(validate_telemetry(p)['completed_count'],0)
        p['training']['effective_hold_seconds']=0
        with self.assertRaises(ValueError):validate_telemetry(p)
    def test_three_real_hold_models_validate_and_ids_disjoint(self):
        models=json.loads((Path(__file__).resolve().parents[1]/'data/hold_models.json').read_text(encoding='utf-8'))
        for m in models.values():
            clean=validate_model(m);self.assertEqual(validate_model(clean),clean)
            self.assertFalse(set(clean['validation']['trainSampleIds'])&set(clean['validation']['holdoutSampleIds']))
            self.assertLessEqual(len(clean['rows']),1024)

if __name__=='__main__':unittest.main()
