"""Produce an equivalent static graph and standard UINT8 QDQ candidate."""
import argparse,datetime,json,resource
from pathlib import Path
import numpy as np
import onnx
import onnxruntime as ort
import torch
from onnxruntime.quantization import QuantFormat,QuantType,quantize_static
from onnxruntime.quantization.calibrate import CalibraterBase
from model import ECARLNet
from deployment_model import DeploymentNet
from prepare import DATA_SHA,CKPT_SHA,sha,dump
from quantize import Reader
from evaluate_onnx import session

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=Path('artifacts/npu-qdq-v1'))
    p.add_argument('--source',type=Path,default=Path('/home/alan-william/xu2026'))
    p.add_argument('--baseline',type=Path,default=Path('artifacts/baseline-v4'))
    p.add_argument('--all-training',action='store_true',help='Calibrate on all 880 training windows, never validation/test')
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    dataset=a.source/'data/dataset_v4/dataset.npz'
    checkpoint=a.source/'data/models_v4/clf_ckpt/ECA-RLNet_none_all_n40_s0.pt'
    assert sha(dataset)==DATA_SHA and sha(checkpoint)==CKPT_SHA
    manifest=json.loads((a.baseline/'manifest.json').read_text())
    assert sha(a.baseline/'calibration.npz')==manifest['files']['calibration.npz']
    d=np.load(dataset,allow_pickle=False);x,y,split=d['X'],d['y'],d['split'];d.close()
    cal=np.load(a.baseline/'calibration.npz',allow_pickle=False);cx,ci=cal['X'],cal['dataset_index'];cal.close()
    assert np.all(split[ci]==0) and np.array_equal(x[ci],cx)
    if a.all_training:
        ci=np.flatnonzero(split==0);cx=x[ci]
    np.savez(a.output/'calibration.npz',X=cx,dataset_index=ci)
    torch.set_num_threads(2);torch.set_num_interop_threads(1)
    model=ECARLNet().eval()
    model.load_state_dict(torch.load(checkpoint,map_location='cpu',weights_only=False)['state'],strict=True)
    candidate=DeploymentNet(model).eval();ids=np.flatnonzero(split==1)
    reference=[];actual=[]
    with torch.inference_mode():
        # First establish the one-window criterion, then run bounded batches.
        t=torch.from_numpy(x[ids[:1]])
        torch.testing.assert_close(model(t),candidate(t.unsqueeze(2)),rtol=1e-4,atol=3e-5)
        for start in range(0,len(ids),64):
            t=torch.from_numpy(x[ids[start:start+64]])
            reference.append(model(t).numpy());actual.append(candidate(t.unsqueeze(2)).numpy())
    reference,actual=np.concatenate(reference),np.concatenate(actual)
    np.testing.assert_allclose(actual,reference,rtol=1e-4,atol=3e-5)
    assert np.array_equal(actual.argmax(1),reference.argmax(1))
    fp=a.output/'eca_deploy_fp32.onnx';qdq=a.output/'eca_deploy_uint8_qdq.onnx'
    torch.onnx.export(candidate,torch.zeros(1,3,1,500),str(fp),input_names=['waveform'],output_names=['logits'],opset_version=13,dynamo=False)
    graph=onnx.shape_inference.infer_shapes(onnx.load(fp));onnx.checker.check_model(graph);onnx.save(graph,fp)
    forbidden={'Gemm','MatMul','Transpose','ReduceMean','BatchNormalization'}
    assert not forbidden.intersection(n.op_type for n in graph.graph.node)
    for node in graph.graph.node:
        for attr in node.attribute:
            if node.op_type=='Conv' and attr.name=='group':assert attr.i==1
            if node.op_type=='Conv' and attr.name=='dilations':assert list(attr.ints)==[1,1]
    s=session(fp,'ORT_DISABLE_ALL')
    exported=np.concatenate([s.run(None,{'waveform':x[i:i+1].reshape(1,3,1,500)})[0] for i in ids])
    np.testing.assert_allclose(exported,reference,rtol=1e-4,atol=3e-5)
    assert np.array_equal(exported.argmax(1),reference.argmax(1))
    print(f'Equivalent FP32 export validated on 1100 windows. Calibrating QDQ with {len(ci)} train windows.',flush=True)
    # Scope the session override to calibration; never modify the installed runtime.
    original=CalibraterBase.create_inference_session
    def bounded_session(self):
        opts=ort.SessionOptions();opts.intra_op_num_threads=2;opts.inter_op_num_threads=1
        opts.graph_optimization_level=ort.GraphOptimizationLevel.ORT_DISABLE_ALL
        self.infer_session=ort.InferenceSession(self.augmented_model_path,sess_options=opts,providers=self.execution_providers)
    CalibraterBase.create_inference_session=bounded_session
    try:
        quantize_static(str(fp),str(qdq),Reader(cx),quant_format=QuantFormat.QDQ,
            activation_type=QuantType.QUInt8,weight_type=QuantType.QUInt8,per_channel=False,
            op_types_to_quantize=['Conv','Add','Mul','Sigmoid','GlobalAveragePool','MaxPool'],
            # ORT 1.23.2 clears collected ranges without merging when the
            # intermediate-output limit is hit. Keep all scalar min/max ranges.
            extra_options={'DedicatedQDQPair':True})
    finally:CalibraterBase.create_inference_session=original
    qgraph=onnx.load(qdq);onnx.checker.check_model(qgraph)
    assert all(n.domain in ('','ai.onnx') for n in qgraph.graph.node)
    assert not any(n.op_type.startswith('QLinear') or n.op_type=='QGemm' for n in qgraph.graph.node)
    golden=np.load(a.baseline/'golden.npz',allow_pickle=False)
    qs=session(qdq,'ORT_DISABLE_ALL')
    gold_logits=np.concatenate([qs.run(None,{'waveform':v.reshape(1,3,1,500)})[0] for v in golden['X']])
    assert np.isfinite(gold_logits).all()
    np.save(a.output/'golden_qdq_logits.npy',gold_logits)
    record={'scope':'equivalent rewrite and QDQ creation; hardware compatibility pending',
        'created_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'dataset_sha256':DATA_SHA,'checkpoint_sha256':CKPT_SHA,
        'source_hashes':{p.name:sha(p) for p in [Path(__file__),Path(__file__).with_name('deployment_model.py'),Path(__file__).with_name('model.py')]},
        'validation_count':len(ids),'rewrite_max_abs_error':float(np.max(abs(actual-reference))),
        'onnx_max_abs_error':float(np.max(abs(exported-reference))),
        'fp32_top1_agreement':float(np.mean(exported.argmax(1)==reference.argmax(1))),
        'original_parameter_count':sum(p.numel() for p in model.parameters()),
        'expanded_parameter_count':sum(p.numel() for p in candidate.parameters()),
        'calibration_count':len(ci),'calibration_split':'train only','calibration_sha256':sha(a.output/'calibration.npz'),
        'qdq_scope':['Conv','Add','Mul','Sigmoid','GlobalAveragePool','MaxPool'],
        'peak_rss_mib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,
        'versions':{'torch':torch.__version__,'onnx':onnx.__version__,'onnxruntime':ort.__version__}}
    dump(a.output/'adaptation.json',record)
    dump(a.output/'manifest.json',{'schema':1,'dataset_sha256':DATA_SHA,'checkpoint_sha256':CKPT_SHA,
        'files':{name:sha(a.output/name) for name in ['eca_deploy_fp32.onnx','eca_deploy_uint8_qdq.onnx','golden_qdq_logits.npy','calibration.npz','adaptation.json']}})
    print(json.dumps(record,indent=2),flush=True)

if __name__=='__main__':main()
