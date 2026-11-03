from pathlib import Path
import json,hashlib
import numpy as np
from ai_edge_litert.tools import flatbuffer_utils as f
from ai_edge_litert.interpreter import Interpreter,OpResolverType

import argparse
parser=argparse.ArgumentParser(description='Explicit developer model split/quantization, not live inference.')
root=Path(__file__).resolve().parents[1]/'models/mediapipe_conv'
parser.add_argument('--source',type=Path,default=root/'landmark_full_reference.tflite')
parser.add_argument('--detector',type=Path,default=root/'pose_detector.tflite')
parser.add_argument('--output',type=Path,default=root)
args=parser.parse_args();src=args.source;out=args.output;out.mkdir(parents=True,exist_ok=True)
assert hashlib.sha256(src.read_bytes()).hexdigest()=='ad6cfd3c903eb31a4ee788b809e45ecf9fa69923b69b9f3f2d9ae616ff433e58','Unexpected source model'

model=f.read_model(str(src));g=model.subgraphs[0]
interp=Interpreter(model_path=str(src),experimental_op_resolver_type=OpResolverType.BUILTIN_WITHOUT_DEFAULT_DELEGATES,experimental_preserve_all_tensors=True)
interp.allocate_tensors()
ops=interp._get_ops_details()
head=next(o for o in ops if o['op_name']=='CONV_2D' and o['outputs'].tolist()==[309])
assert head['inputs'].tolist()==[308,469,477]
interp.set_tensor(0,np.zeros((1,256,256,3),np.float32));interp.invoke()
w=interp.get_tensor(469).copy().reshape(195,768);bias=interp.get_tensor(477).copy()
scales=np.exp2(np.floor(np.log2(32767/np.maximum(np.max(np.abs(w),axis=1),1e-30)))).astype(np.float64)
scaled=w*scales[:,None];qw=(np.sign(scaled)*np.floor(np.abs(scaled)+.5)).astype(np.int16)
np.savez(out/'head_weights.npz',weights=qw,scales=scales,bias=bias,weights_float=w)
# Dependency closure removes the true landmark Conv2D and its weight/bias loads.
producers={int(t):op for op in g.operators for t in op.outputs}
needed=set();visited=set()
def visit(t):
    if t in visited:return
    visited.add(t)
    op=producers.get(t)
    if op is not None:
        index=next(i for i,x in enumerate(g.operators) if x is op)
        needed.add(index)
        for q in op.inputs:
            if q>=0:visit(int(q))
for t in [308,315,283]:visit(t)
g.operators=[op for i,op in enumerate(g.operators)if i in needed];g.outputs=np.array([308,315,283],np.int32)
assert not any(309 in op.outputs for op in g.operators)
f.write_model(model,str(out/'landmark_prefix.tflite'))
(out/'landmark_full_reference.tflite').write_bytes(src.read_bytes())
(out/'pose_detector.tflite').write_bytes(args.detector.read_bytes())
prefix=Interpreter(model_path=str(out/'landmark_prefix.tflite'),num_threads=1);prefix.allocate_tensors()
rng=np.random.RandomState(20261028);checks=[]
for i in range(3):
    image=rng.rand(1,256,256,3).astype(np.float32)
    interp.set_tensor(0,image);interp.invoke();prefix.set_tensor(0,image);prefix.invoke()
    x=prefix.get_tensor(308).reshape(768)
    y=x.astype(np.float64)@w.T.astype(np.float64)+bias
    ref=interp.get_tensor(310).reshape(195)
    err=float(np.max(np.abs(y-ref)))
    assert err<.005,err
    sx=2**np.floor(np.log2(32767/max(float(np.max(np.abs(x))),1e-30)))
    qx=(np.sign(x)*np.floor(np.abs(x)*sx+.5)).astype(np.int16)
    approx=qw.astype(np.int64)@qx.astype(np.int64)/(scales*sx)+bias
    qe=float(np.max(np.abs(approx-ref)))
    assert qe<.1,qe
    checks.append({'float_split_max_abs':err,'quantized_head_max_abs':qe})
contract={'source_sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'removed_output_tensor':309,
 'prefix_output_tensors':[308,315,283],'input_shape':[1,2,2,192],
 'conv_weights_shape':[195,2,2,192],'stride':[1,1],'padding':'VALID',
 'macs':195*768,'bias_location':'PYNQ PS after PL accumulator dequantization',
 'feature_format':'signed16 dynamic power-of-two per query','weight_format':'signed16 per-output power-of-two',
 'checks':checks,'note':'Same MediaPipe Lite model, actual keypoint convolution removed from CPU graph. Synthetic numerical checks are not camera accuracy tests.'}
(out/'contract.json').write_text(json.dumps(contract,indent=2)+'\n')
print(json.dumps(contract))
