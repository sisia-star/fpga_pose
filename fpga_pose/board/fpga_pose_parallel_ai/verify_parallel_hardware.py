"""Actual PL tests using the eight stored models; not a camera accuracy test."""
import argparse
import fcntl
import json
from pathlib import Path
import socket
import numpy as np
from fpga_classifier import (FPGAKNNClassifier, quantize, hold_geometry_reason,
                             DISTANCE_DENOMINATOR, HOLD_EXERCISES)


def run(project):
    project=Path(project).resolve()
    models=json.loads((project/'data/all_models.json').read_text())
    driver=FPGAKNNClassifier(str(project/'overlays/pose_distance.bit'))
    try:
        state=driver.initialize()
        if not state['ready']:raise RuntimeError(state['error'])
        results={}
        for exercise, model in models.items():
            upload=driver.upload_model(model)
            train=np.array([quantize(row['features']) for row in model['rows']],dtype=np.int64)
            queries=[]
            for label in model['classes']:
                subset=[row for row in model['rows'] if row['label']==label]
                queries.extend([subset[0],subset[-1]])
            for row in queries:
                q=np.array(quantize(row['features']),dtype=np.int64)
                delta=train-q
                distances=np.einsum('ij,ij->i',delta,delta,dtype=np.int64)
                top=np.lexsort((np.arange(len(train)),distances))[:5]
                votes={label:0 for label in model['classes']}
                for i in top:votes[model['rows'][int(i)]['label']]+=1
                label=max(model['classes'],key=lambda x:votes[x])
                raw=min(int(distances[i]) for i in top if model['rows'][int(i)]['label']==label)
                fraction=votes[label]/5
                if fraction<.6 or raw/DISTANCE_DENOMINATOR>model['classThresholds'][label]**2:label=None
                if exercise in HOLD_EXERCISES and label==model['classes'][1]:
                    if hold_geometry_reason(row['features'],exercise):label=None
                actual=driver.predict(upload['model_id'],row['features'])
                if (actual['label']!=label or actual['distance_sum_integer']!=raw
                        or actual['voteFraction']!=fraction or not actual['hardware_completed']):
                    raise RuntimeError('Real PL/model reference mismatch: '+exercise)
            results[exercise]={'rows':len(train),'queries_verified':len(queries),
                               'exact_integer_and_vote_agreement':True}
        final=driver.hardware_status()
        return {'test_kind':'actual-parallel-pl-eight-model-agreement','models':results,
                'queries_verified':sum(v['queries_verified'] for v in results.values()),
                'hardware':final,'cloud_api_called':False,
                'scope':'Stored feature vectors; exact integer/classification agreement. '
                        'Does not measure live camera recognition accuracy or exercise counting accuracy.'}
    finally:driver.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',default='hardware_validation_parallel.json')
    args=p.parse_args()
    with socket.socket() as sock:
        sock.settimeout(1)
        if sock.connect_ex(('127.0.0.1',8081))==0:
            raise SystemExit('Stop the live pose services before this explicit overlay test.')
    with Path('/var/lib/fpga-pose-ai/service.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        result=run(Path(__file__).resolve().parent)
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'models':len(result['models']),'queries_verified':result['queries_verified'],
                      'exact_agreement':True,'output':args.output}))


if __name__=='__main__':main()
