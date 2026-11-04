"""Run on codex-ubuntu; reuse pinned assets, export and validate offline."""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
import numpy as np
import onnx
import onnxruntime as ort
import torch
from model import ECARLNet, lift_to_2d

DATA_SHA = 'a9849da35f01595e201325295ffadd395e49dfa59ba3c1149e77b217ad6a5b2b'
CKPT_SHA = '62bda590c58de1a3e5006ca2e221fd6a9b9aae06170600992f20aed4f387417a'
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def dump(p, obj): Path(p).write_text(json.dumps(obj, ensure_ascii=False, indent=2)+'\n',encoding='utf-8')

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--source',type=Path,default=Path('/home/alan-william/xu2026'))
    p.add_argument('--output',type=Path,default=Path('artifacts/baseline-v4'))
    a=p.parse_args(); out=a.output; out.mkdir(parents=True,exist_ok=True)
    dataset=a.source/'data/dataset_v4/dataset.npz'
    checkpoint=a.source/'data/models_v4/clf_ckpt/ECA-RLNet_none_all_n40_s0.pt'
    assert sha(dataset)==DATA_SHA, 'Unexpected dataset'
    assert sha(checkpoint)==CKPT_SHA, 'Unexpected checkpoint'
    # This exact hash identifies the user's own trusted historical training output.
    ckpt=torch.load(checkpoint,map_location='cpu',weights_only=False)
    assert ckpt['dataset_sha256']==DATA_SHA
    data=np.load(dataset,allow_pickle=False)
    x,y,split=data['X'],data['y'],data['split']
    assert x.shape==(6380,3,500) and np.isfinite(x).all()
    assert np.max(np.abs(x))<=1.00001
    assert np.all(split[ckpt['real_support_ids']]==0)
    val=np.flatnonzero(split==1); train=np.flatnonzero(split==0)
    # Deterministic per-class sampling, independent of predictions.
    golden=np.array([np.flatnonzero((split==1)&(y==c))[0] for c in range(22)])
    calibration=np.concatenate([np.flatnonzero((split==0)&(y==c))[:20] for c in range(22)])
    torch.set_num_threads(4)
    model=ECARLNet().eval(); model.load_state_dict(ckpt['state'],strict=True)
    source=a.source/'repro/models/eca_rlnet.py'
    spec=importlib.util.spec_from_file_location('upstream_model',source)
    upstream=importlib.util.module_from_spec(spec); spec.loader.exec_module(upstream)
    reference=upstream.ECARLNet().eval(); reference.load_state_dict(ckpt['state'],strict=True)
    lifted=lift_to_2d(model)
    with torch.inference_mode():
        tv=torch.from_numpy(x[val]); baseline=model(tv).numpy()
        ref=reference(tv).numpy(); lifted_logits=lifted(tv.unsqueeze(2)).numpy()
    np.testing.assert_allclose(baseline,ref,rtol=1e-5,atol=1e-5)
    np.testing.assert_allclose(baseline,lifted_logits,rtol=1e-4,atol=2e-5)
    report={'scope':'offline validation only; no NPU/hardware measurement',
        'samples':len(val),'parameters':sum(t.numel() for t in model.parameters()),
        'validation_accuracy':float(np.mean(baseline.argmax(1)==y[val])),
        'upstream_max_abs_error':float(np.max(np.abs(baseline-ref))),
        'lift_2d_max_abs_error':float(np.max(np.abs(baseline-lifted_logits))), 'exports':{}}
    for name,m,shape in [('eca_1d_fp32',model,(1,3,500)),('eca_2d_fp32',lifted,(1,3,1,500))]:
        path=out/(name+'.onnx')
        torch.onnx.export(m,torch.zeros(shape),str(path),input_names=['waveform'],
            output_names=['logits'],opset_version=13,dynamo=False)
        graph=onnx.load(path); onnx.checker.check_model(graph)
        opts=ort.SessionOptions(); opts.intra_op_num_threads=4
        session=ort.InferenceSession(str(path),sess_options=opts,providers=['CPUExecutionProvider'])
        begin=time.perf_counter()
        actual=np.concatenate([session.run(None,{'waveform':x[i:i+1].reshape(shape)})[0] for i in val])
        elapsed=time.perf_counter()-begin
        np.testing.assert_allclose(actual,baseline,rtol=1e-4,atol=3e-5)
        assert np.array_equal(actual.argmax(1),baseline.argmax(1))
        report['exports'][name]={'max_abs_error':float(np.max(np.abs(actual-baseline))),
            'top1_agreement':float(np.mean(actual.argmax(1)==baseline.argmax(1))),
            'host_mean_ms':elapsed/len(val)*1000,'ops':dict(Counter(n.op_type for n in graph.graph.node)),
            'shape':shape,'sha256':sha(path)}
    q=np.rint(x[golden]*32767).clip(-32767,32767).astype('<i2')
    with torch.inference_mode():
        gold=model(torch.from_numpy(x[golden])).numpy()
        q_logits=model(torch.from_numpy(q.astype(np.float32)/32767)).numpy()
    np.savez(out/'golden.npz',X=x[golden],q15=q,y=y[golden],logits=gold,
        dataset_index=golden,source_id=data['source_id'][golden],start=data['start'][golden])
    np.savez(out/'calibration.npz',X=x[calibration],dataset_index=calibration)
    q.tofile(out/'golden_q15.bin')
    x[golden].astype('<f4').tofile(out/'golden_f32.bin')
    report['q15']={'max_input_error':float(np.max(np.abs(x[golden]-q.astype(np.float32)/32767))),
        'top1_agreement':float(np.mean(gold.argmax(1)==q_logits.argmax(1)))}
    assert report['q15']['top1_agreement']==1.0
    dump(out/'validation.json',report)
    dump(out/'manifest.json',{'schema':1,'dataset_sha256':DATA_SHA,'checkpoint_sha256':CKPT_SHA,
        'checkpoint_name':checkpoint.name,'dataset_split_counts':{'train':len(train),'val':len(val),'test':int(sum(split==2))},
        'upstream_model_source_sha256':sha(source),'historical_checkpoint_has_model_source_hash':False,
        'calibration_split':'train only','calibration_count':len(calibration),'golden_split':'validation',
        'test_used_in_this_export':False,'versions':{'torch':torch.__version__,'numpy':np.__version__,
        'onnx':onnx.__version__,'onnxruntime':ort.__version__},
        'files':{name:sha(out/name) for name in ['eca_1d_fp32.onnx','eca_2d_fp32.onnx',
            'golden.npz','calibration.npz','golden_q15.bin','golden_f32.bin','validation.json']}})
    print(json.dumps(report,indent=2))

if __name__=='__main__': main()
