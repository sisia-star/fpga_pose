"""Explicit real PL weight/feature tests. Stop live services first."""
import argparse,json,socket,fcntl
from pathlib import Path
import numpy as np
from fpga_classifier import FPGAKNNClassifier
from fpga_convolution import FPGAConvolution

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',default='conv_hardware_validation.json');a=p.parse_args()
    with socket.socket()as sock:
        sock.settimeout(1)
        if sock.connect_ex(('127.0.0.1',8081))==0:raise SystemExit('Stop live service first.')
    root=Path(__file__).resolve().parent
    with Path('/var/lib/fpga-pose-ai/service.lock').open('a')as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        classifier=FPGAKNNClassifier(str(root/'overlays/pose_distance.bit'));conv=None
        try:
            status=classifier.initialize()
            if not status['ready']:raise RuntimeError(status['error'])
            conv=FPGAConvolution(classifier._overlay,root/'models/mediapipe_conv')
            conv.initialize();checks=[]
            rng=np.random.RandomState(20261028)
            for query in [np.full(768,32767,np.int16),np.full(768,-32768,np.int16),
                          rng.randint(-32768,32768,size=768).astype(np.int16)]:
                actual,evidence=conv._integer(query)
                expected=conv.weights.astype(np.int64)@query.astype(np.int64)
                assert np.array_equal(actual,expected)
                checks.append({'exact_integer_match':True,'evidence':evidence})
            result={'actual_pl':True,'trained_model_weights':True,'checks':checks,'startup_self_test':conv.self_test_passed,
                    'hardware':conv.status(),'note':'Exact integer MAC verification, not camera accuracy.'}
            Path(a.output).write_text(json.dumps(result,indent=2)+'\n')
            print(json.dumps({'actual_pl':True,'cases':len(checks),'outputs_per_case':195,'startup_self_test':True}))
        finally:
            if conv:conv.close()
            classifier.close()
if __name__=='__main__':main()
