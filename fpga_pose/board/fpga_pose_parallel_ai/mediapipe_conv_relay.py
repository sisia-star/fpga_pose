"""Run on the computer; images stay on LAN, no DeepSeek image upload."""
import argparse,base64,json,os,time
from pathlib import Path
from urllib.request import build_opener,ProxyHandler,Request
import cv2
import numpy as np
from mediapipe_conv_engine import MediaPipeConvEngine

def main():
    p=argparse.ArgumentParser();p.add_argument('--board',default='http://192.168.2.99:8081')
    args=p.parse_args();board=args.board.rstrip('/');token=os.environ.get('AI_COACH_PAIR_TOKEN','')
    if not token:raise SystemExit('Set AI_COACH_PAIR_TOKEN through the encrypted Windows launcher.')
    opener=build_opener(ProxyHandler({}))
    def post(path,body):
        data=json.dumps({'token':token,**body},allow_nan=False).encode()
        with opener.open(Request(board+path,data=data,headers={'Content-Type':'application/json'}),timeout=10)as response:
            return json.load(response)
    current={}
    def convolve(features):
        return post('/api/pose/relay/conv',{'job_id':current['id'],'features':features})
    engine=MediaPipeConvEngine(Path(__file__).resolve().parent/'models/mediapipe_conv',convolve)
    print('MediaPipe卷积助手已启动：电脑运行前缀，FPGA运行关键点输出卷积。',flush=True)
    while True:
        try:
            job=post('/api/pose/relay/poll',{}).get('job')
            if not job:continue
            current.clear();current.update(job)
            image=cv2.imdecode(np.frombuffer(base64.b64decode(job['jpeg_b64']),np.uint8),cv2.IMREAD_COLOR)
            if image is None:raise ValueError('Invalid camera JPEG')
            result=engine.infer(image)
            post('/api/pose/relay/result',{'job_id':job['id'],'points':result['points']})
        except KeyboardInterrupt:return
        except Exception as exc:
            # No CPU head fallback and no stale landmarks on an error.
            if current:
                try:post('/api/pose/relay/result',{'job_id':current['id'],'points':[],'error':str(exc)[:300]})
                except Exception:pass
            engine.roi=None
            print('等待卷积服务重新连接：'+str(exc)[:200],flush=True);time.sleep(2)

if __name__=='__main__':main()
