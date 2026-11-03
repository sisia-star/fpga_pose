"""Computer-side official MediaPipe Lite prefix, PL head RPC and 33-point decode.

The trained landmark Conv2D is absent from the prefix. CPU reference is available
only through the explicit reference argument for numerical/performance tests.
"""
from pathlib import Path
import math,time
import cv2
import numpy as np
from ai_edge_litert.interpreter import Interpreter

def sigmoid(x):return 1/(1+np.exp(-np.clip(x,-80,80)))

class MediaPipeConvEngine:
    def __init__(self,model_dir,conv_callback,reference=False):
        root=Path(model_dir);self.conv_callback=conv_callback;self.reference=reference
        name='landmark_full_reference.tflite' if reference else 'landmark_prefix.tflite'
        self.landmark=Interpreter(model_path=str(root/name),num_threads=2);self.landmark.allocate_tensors()
        self.detector=Interpreter(model_path=str(root/'pose_detector.tflite'),num_threads=2);self.detector.allocate_tensors()
        self.anchors=np.array([( (x+.5)/side,(y+.5)/side ) for side,n in [(28,2),(14,2),(7,6)]
                               for y in range(side) for x in range(side) for _ in range(n)],np.float32)
        assert self.anchors.shape==(2254,2)
        self.roi=None;self.frames=0

    def alignment(self,points):
        center=points[0];v=points[1]-center;size=float(np.linalg.norm(v))*2*1.25
        rotation=math.pi/2-math.atan2(-float(v[1]),float(v[0]))
        return (float(center[0]),float(center[1]),size,rotation)

    def detect(self,bgr):
        h,w=bgr.shape[:2];scale=224/max(h,w);nw,nh=round(w*scale),round(h*scale)
        left,top=(224-nw)//2,(224-nh)//2
        canvas=np.zeros((224,224,3),np.uint8)
        canvas[top:top+nh,left:left+nw]=cv2.resize(cv2.cvtColor(bgr,cv2.COLOR_BGR2RGB),(nw,nh))
        self.detector.set_tensor(0,(canvas.astype(np.float32)/127.5-1)[None])
        self.detector.invoke()
        scores=sigmoid(self.detector.get_tensor(429).reshape(-1))
        keep=np.flatnonzero(scores>=.5)
        if not len(keep):return None
        raw=self.detector.get_tensor(441).reshape(2254,12)
        boxes=raw[keep,:4]/224;centers=boxes[:,:2]+self.anchors[keep]
        xy1=centers-boxes[:,2:]/2;xy2=centers+boxes[:,2:]/2
        best=int(np.argmax(scores[keep]))
        inter=np.prod(np.maximum(0,np.minimum(xy2,xy2[best])-np.maximum(xy1,xy1[best])),axis=1)
        area=np.prod(np.maximum(0,xy2-xy1),axis=1);iou=inter/np.maximum(area+area[best]-inter,1e-9)
        selected=np.flatnonzero(iou>.3);ids=keep[selected]
        points=raw[ids,4:].reshape(-1,4,2)/224+self.anchors[ids,None,:]
        points=np.average(points,axis=0,weights=scores[ids])*224
        points=(points-np.array([left,top]))/scale
        return self.alignment(points[:2])

    def infer(self,bgr):
        started=time.perf_counter();h,w=bgr.shape[:2]
        if h<2 or w<2 or h>1080 or w>1920:raise ValueError('Unexpected image dimensions')
        if self.roi is None or self.frames%15==0:self.roi=self.detect(bgr)
        self.frames+=1
        if self.roi is None:return {'points':[],'no_pose':True,'total_ms':(time.perf_counter()-started)*1000}
        cx,cy,size,angle=self.roi
        if not 4<size<4*max(h,w):self.roi=None;return {'points':[],'no_pose':True}
        u=np.array([math.cos(angle),math.sin(angle)]);v=np.array([-math.sin(angle),math.cos(angle)])
        origin=np.array([cx,cy])-size*(u+v)/2
        src=np.array([origin,origin+size*u,origin+size*v],np.float32)
        dst=np.array([[0,0],[256,0],[0,256]],np.float32)
        affine=cv2.getAffineTransform(src,dst)
        roi=cv2.warpAffine(cv2.cvtColor(bgr,cv2.COLOR_BGR2RGB),affine,(256,256),flags=cv2.INTER_LINEAR)
        self.landmark.set_tensor(0,(roi.astype(np.float32)/255)[None]);self.landmark.invoke()
        presence=float(self.landmark.get_tensor(315).reshape(-1)[0])
        if presence<.5:self.roi=None;return {'points':[],'no_pose':True,'presence':presence}
        if self.reference:
            raw=self.landmark.get_tensor(310).reshape(39,5).astype(np.float64);evidence=None
        else:
            features=self.landmark.get_tensor(308).reshape(768)
            response=self.conv_callback(features.tolist())
            evidence=response['evidence']
            if evidence.get('backend')!='fpga-convolution' or not evidence.get('hardware_completed'):
                raise RuntimeError('Missing actual FPGA convolution completion')
            raw=np.array(response['values'],np.float64).reshape(39,5)
        heatmap=sigmoid(self.landmark.get_tensor(283).reshape(64,64,39))
        xy=raw[:,:2]/256
        for index in range(39):
            col,row=xy[index]*64
            if not (0<=col<64 and 0<=row<64):continue
            x0,x1=max(0,int(col)-3),min(64,int(col)+4);y0,y1=max(0,int(row)-3),min(64,int(row)+4)
            patch=heatmap[y0:y1,x0:x1,index]
            if patch.size and float(patch.max())>=.5:
                yy,xx=np.mgrid[y0:y1,x0:x1];den=float(patch.sum())
                xy[index]=[float((xx*patch).sum())/den/64,float((yy*patch).sum())/den/64]
        image_xy=origin[None,:]+size*(xy[:,0:1]*u[None,:]+xy[:,1:2]*v[None,:])
        self.roi=self.alignment(image_xy[33:35])
        points=[{'x':float(image_xy[i,0]/w),'y':float(image_xy[i,1]/h),
                 'z':float(raw[i,2]/256*size/w),'visibility':float(sigmoid(raw[i,3])),
                 'presence':float(sigmoid(raw[i,4]))}for i in range(33)]
        return {'points':points,'convolution':evidence,'presence':presence,
                'total_ms':(time.perf_counter()-started)*1000}
