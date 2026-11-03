"""Real MediaPipe landmark head MACs on PL; no CPU inference fallback."""
import hashlib,math,threading,time
from pathlib import Path
import numpy as np
from fpga_classifier import FPGAError
from fpga_image import BoundedSimpleDMA,_QUARANTINED_BUFFERS

class FPGAConvolution:
    def __init__(self,overlay,model_dir,timeout=2.0,fault_callback=None):
        self.overlay=overlay;self.timeout=timeout;self.fault_callback=fault_callback
        self.lock=threading.RLock();self.ready=False;self.error=None;self.self_test_passed=False
        self.packets=[];self.output=None;self.dma=None;self.poisoned=False;self.calls=0;self.last=None
        path=Path(model_dir)/'head_weights.npz'
        data=np.load(path,allow_pickle=False)
        self.weights=data['weights'].astype(np.int16)
        self.scales=data['scales'].astype(np.float64);self.bias=data['bias'].astype(np.float64)
        if self.weights.shape!=(195,768):raise FPGAError('MediaPipe head weights shape mismatch')
        self.model_hash=hashlib.sha256(path.read_bytes()).hexdigest()

    def initialize(self):
        from pynq import allocate,MMIO
        self.ip=self.overlay.pose_conv_0
        if (self.ip.read(8)!=0x50434e31 or self.ip.read(12)!=768 or self.ip.read(16)!=36):
            raise FPGAError('Convolution IP identity/dimensions mismatch')
        description=self.overlay.ip_dict.get('conv_dma_0',{})
        params=description.get('parameters',{})
        length=int(params.get('c_sg_length_width',params.get('C_SG_LENGTH_WIDTH',23)))
        self.dma=BoundedSimpleDMA(MMIO(int(description["phys_addr"]),int(description["addr_range"])),length,self.timeout)
        for begin in range(0,195,36):
            rows=min(36,195-begin)
            packet=allocate(shape=(768*37,),dtype=np.uint32)
            tile=np.zeros((36,768),np.int16);tile[:rows]=self.weights[begin:begin+rows]
            packet[768:]=tile.T.reshape(-1).astype(np.uint16).astype(np.uint32)
            self.packets.append(packet)
        self.output=allocate(shape=(36,),dtype=np.int64)
        self.ready=True
        # Exact startup test of the actual model weights and hardware arithmetic.
        query=(np.arange(768)%251-125).astype(np.int16)
        actual,_=self._integer(query)
        wanted=self.weights.astype(np.int64)@query.astype(np.int64)
        if not np.array_equal(actual,wanted):raise FPGAError('Convolution exact startup test failed')
        self.self_test_passed=True
        return self.status()

    def _integer(self,query):
        outputs=[];all_cycles=0;start=time.perf_counter()
        with self.lock:
            if not self.ready:raise FPGAError(self.error or 'Convolution unavailable')
            try:
                for index,packet in enumerate(self.packets):
                    packet[:768]=query.astype(np.uint16).astype(np.uint32)
                    before=int(self.ip.read(24))
                    self.ip.write(0,1)
                    self.dma.arm(0x30,self.output,self.output.nbytes)
                    self.dma.arm(0,packet,packet.nbytes)
                    deadline=time.monotonic()+self.timeout
                    while not self.dma.poll():
                        if self.ip.read(0)&8:raise FPGAError('PL convolution fault '+str(self.ip.read(28)))
                        if time.monotonic()>deadline:raise FPGAError('Convolution DMA timeout')
                    self.dma.finish_receive(self.output.nbytes)
                    control=int(self.ip.read(0));count=int(self.ip.read(24));cycles=int(self.ip.read(20))
                    if control&9 or not control&2 or ((count-before)&0xffffffff)!=1 or cycles==0:
                        raise FPGAError('Convolution completion evidence mismatch')
                    n=min(36,195-index*36);outputs.extend(self.output[:n].tolist());all_cycles+=cycles
                self.calls+=1
                evidence={'backend':'fpga-convolution','hardware_completed':True,'completed_tiles':6,
                          'hardware_count':int(self.ip.read(24)),'cycles':all_cycles,
                          'full_call_ms':(time.perf_counter()-start)*1000,'calls':self.calls,'model_sha256':self.model_hash}
                self.last=evidence
                return np.asarray(outputs,np.int64),evidence
            except Exception as exc:
                self.ready=False;self.error=str(exc);self.poisoned=True
                if self.fault_callback:self.fault_callback(self.error)
                raise

    def process(self,features):
        x=np.asarray(features,np.float64)
        if x.shape!=(768,) or not np.isfinite(x).all():raise ValueError('Convolution needs 768 finite features')
        maximum=float(np.max(np.abs(x)))
        scale=2.0**math.floor(math.log2(32767/maximum)) if maximum else 1.0
        quant=np.sign(x)*np.floor(np.abs(x)*scale+.5)
        if not np.isfinite(quant).all() or np.max(np.abs(quant))>32767:raise ValueError('Convolution input quantization overflow')
        raw,evidence=self._integer(quant.astype(np.int16))
        result=raw.astype(np.float64)/(self.scales*scale)+self.bias
        return {'values':result.tolist(),'evidence':evidence,'input_scale':scale}

    def status(self):
        return {'ready':self.ready,'self_test_passed':self.self_test_passed,'error':self.error,
                'lanes':36,'features':768,'outputs':195,'calls':self.calls,'last':self.last,
                'model_sha256':self.model_hash,'operation':'MediaPipe Lite trained Conv2D(2x2x192 ->195); CPU adds bias after dequantization'}

    def close(self):
        with self.lock:
            self.ready=False
            buffers=[*self.packets]+([self.output]if self.output is not None else [])
            if self.poisoned:_QUARANTINED_BUFFERS.extend(buffers)
            else:
                if self.dma:self.dma.stop()
                for buffer in buffers:buffer.freebuffer()
            self.packets=[];self.output=None
