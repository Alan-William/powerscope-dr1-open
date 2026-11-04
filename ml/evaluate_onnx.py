"""Bounded CPU evaluation of an existing ONNX candidate; never recreates it."""
import argparse,datetime,hashlib,json,resource,time
from pathlib import Path
import numpy as np
import onnx
import onnxruntime as ort

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def session(path,optimization):
    opts=ort.SessionOptions()
    opts.intra_op_num_threads=2;opts.inter_op_num_threads=1
    opts.execution_mode=ort.ExecutionMode.ORT_SEQUENTIAL
    opts.graph_optimization_level=getattr(ort.GraphOptimizationLevel,optimization)
    return ort.InferenceSession(str(path),sess_options=opts,providers=['CPUExecutionProvider'])

def main():
    p=argparse.ArgumentParser()
    p.add_argument('candidate',type=Path);p.add_argument('--reference',type=Path,required=True)
    p.add_argument('--dataset',type=Path,default=Path('/home/alan-william/xu2026/data/dataset_v4/dataset.npz'))
    p.add_argument('--manifest',type=Path,default=Path('artifacts/baseline-v4/manifest.json'))
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--limit',type=int,default=0)
    p.add_argument('--optimization',default='ORT_DISABLE_ALL',choices=['ORT_DISABLE_ALL','ORT_ENABLE_BASIC','ORT_ENABLE_ALL'])
    a=p.parse_args();manifest=json.loads(a.manifest.read_text())
    assert sha(a.dataset)==manifest['dataset_sha256'],'Dataset mismatch'
    assert sha(a.reference)==manifest['files'][a.reference.name],'Reference mismatch'
    d=np.load(a.dataset,allow_pickle=False)
    x,y,split=d['X'],d['y'],d['split'];d.close()
    ids=np.flatnonzero(split==1)
    if a.limit:ids=ids[:a.limit]
    graph=onnx.load(a.candidate);onnx.checker.check_model(graph)
    print('Creating reference session',flush=True);fp=session(a.reference,a.optimization)
    print('Creating candidate session',flush=True);q=session(a.candidate,a.optimization)
    assert q.get_inputs()[0].shape==[1,3,1,500]
    lf=[];lq=[];times=[]
    for j,i in enumerate(ids):
        inputs={'waveform':x[i:i+1].reshape(1,3,1,500)}
        lf.append(fp.run(None,inputs)[0]);begin=time.perf_counter()
        lq.append(q.run(None,inputs)[0]);times.append((time.perf_counter()-begin)*1000)
        if j==0 or (j+1)%100==0:print(f'{j+1}/{len(ids)} samples; rss={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.1f} MiB',flush=True)
    lf=np.concatenate(lf);lq=np.concatenate(lq)
    assert np.isfinite(lq).all()
    from collections import Counter
    report={'status':'offline CPU evaluation complete; Alnpu acceptance pending',
        'created_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'evaluation_script_sha256':sha(__file__),'dataset_sha256':sha(a.dataset),
        'reference_sha256':sha(a.reference),'sha256':sha(a.candidate),
        'evaluation_split':'val','evaluation_count':len(ids),'test_used':False,
        'accuracy_fp32':float(np.mean(lf.argmax(1)==y[ids])),
        'accuracy_candidate':float(np.mean(lq.argmax(1)==y[ids])),
        'top1_agreement':float(np.mean(lf.argmax(1)==lq.argmax(1))),
        'max_abs_logit_error':float(np.max(abs(lq-lf))),
        'rmse_logits':float(np.sqrt(np.mean((lq-lf)**2))),
        'changed_dataset_indices':ids[lf.argmax(1)!=lq.argmax(1)].tolist(),
        'host_cpu_timing_ms':{'first':times[0],'p50':float(np.median(times)),'p95':float(np.percentile(times,95))},
        'peak_rss_mib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,
        'runtime':{'onnxruntime':ort.__version__,'provider':'CPUExecutionProvider','threads':2,'optimization':a.optimization},
        'ops':dict(Counter((n.domain or 'ai.onnx')+':'+n.op_type for n in graph.graph.node))}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2),flush=True)

if __name__=='__main__':main()
