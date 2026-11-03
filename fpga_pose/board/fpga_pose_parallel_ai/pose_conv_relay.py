"""Same-origin browser jobs; PC prefix; mandatory real PL head; no cloud images."""
import base64,hmac,math,secrets,threading,time
from collections import OrderedDict
from fpga_classifier import FPGAError

class PoseConvRelay:
    def __init__(self,owner):
        self.owner=owner;self.cv=threading.Condition();self.job=None;self.result=None
        self.last_seen=0.;self.closed=False
        self.completed=OrderedDict()
    def has_completed(self,seq):
        with self.cv:
            return seq in self.completed and time.monotonic()-self.completed[seq]<5
    def authorize(self,token):
        if not isinstance(token,str) or not hmac.compare_digest(token,self.owner.ai_coach.pairing_token):
            raise FPGAError('Pose relay pairing rejected',403)
    def snapshot(self):
        return {'backend':'mediapipe-lite-split-conv','relay_connected':time.monotonic()-self.last_seen<15,
                'busy':self.job is not None,'error':None,'model':'MediaPipe Lite 33 points'}
    def submit(self,body):
        seq=body.get('frame_seq');encoded=body.get('jpeg_b64')
        if not isinstance(seq,int) or isinstance(seq,bool) or not isinstance(encoded,str) or len(encoded)>400000:
            raise ValueError('Invalid pose frame')
        self.owner._camera_service.frame_record(seq)
        jpeg=base64.b64decode(encoded,validate=True)
        if not jpeg.startswith(b'\xff\xd8'):raise ValueError('Expected JPEG image')
        with self.cv:
            if self.job is not None:raise FPGAError('Pose relay busy',409)
            if time.monotonic()-self.last_seen>=15:raise FPGAError('请先启动电脑卷积助手',503)
            job={'id':secrets.token_hex(12),'frame_seq':seq,'jpeg_b64':encoded,'claimed':False,
                 'created':time.monotonic(),'evidence':None}
            self.job=job;self.result=None;self.cv.notify_all()
            deadline=time.monotonic()+5
            try:
                while self.result is None and not self.closed:
                    left=deadline-time.monotonic()
                    if left<=0:raise FPGAError('电脑姿态/FPGA卷积处理超时；无旧模型回退',504)
                    self.cv.wait(min(left,1))
                if self.closed:raise FPGAError('Pose service stopped')
                result=self.result
                if result.get('error'):raise FPGAError(result['error'])
                return result
            finally:self.job=None;self.result=None;self.cv.notify_all()
    def poll(self,body):
        self.authorize(body.get('token'))
        with self.cv:
            self.last_seen=time.monotonic();deadline=time.monotonic()+5
            while not self.closed:
                if self.job is not None and not self.job['claimed']:
                    self.job['claimed']=True
                    return {'job':{k:self.job[k]for k in ('id','frame_seq','jpeg_b64')}}
                left=deadline-time.monotonic()
                if left<=0:break
                self.cv.wait(left)
            return {'job':None}
    def convolve(self,body):
        self.authorize(body.get('token'))
        with self.cv:
            job=self.job
            if job is None or body.get('job_id')!=job['id'] or not job['claimed']:
                raise FPGAError('Expired pose job',409)
        result=self.owner.convolution.process(body.get('features'))
        with self.cv:
            if self.job is not job:raise FPGAError('Expired convolution result',409)
            job['evidence']=result['evidence']
        return result
    def complete(self,body):
        self.authorize(body.get('token'))
        points=body.get('points',[])
        if not isinstance(points,list) or len(points) not in (0,33):raise ValueError('Expected 33 landmarks or no pose')
        for p in points:
            if not isinstance(p,dict) or any(not isinstance(p.get(k),(int,float)) or not math.isfinite(p[k])
                    for k in ('x','y','z','visibility','presence')):raise ValueError('Invalid landmark')
        with self.cv:
            job=self.job
            if job is None or job['id']!=body.get('job_id'):raise FPGAError('Expired pose completion',409)
            if points and not job['evidence']:raise FPGAError('No real FPGA convolution evidence',409)
            if points:
                self.completed[job['frame_seq']]=time.monotonic()
                while len(self.completed)>32:self.completed.popitem(last=False)
            self.last_seen=time.monotonic()
            self.result={'points':points,'frame_seq':job['frame_seq'],'convolution':job['evidence'],
                         'error':body.get('error'),'model_backend':'mediapipe-lite-split-conv'}
            self.cv.notify_all()
        return {'accepted':True}
    def close(self):
        with self.cv:self.closed=True;self.cv.notify_all()
