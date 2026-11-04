"""Experimental UINT8 QOperator candidate, not an Alnpu compatibility claim."""
import argparse
import json
from pathlib import Path
import numpy as np
import onnx
import onnxruntime as ort
from onnxruntime.quantization import CalibrationDataReader, QuantFormat, QuantType, quantize_static
from onnxruntime.quantization.shape_inference import quant_pre_process
from prepare import sha, dump

class Reader(CalibrationDataReader):
    def __init__(self,x): self.items=iter(x)
    def get_next(self):
        x=next(self.items,None)
        return None if x is None else {'waveform':x.reshape(1,3,1,500)}

def main():
    p=argparse.ArgumentParser(); p.add_argument('--artifacts',type=Path,default=Path('artifacts/baseline-v4'))
    p.add_argument('--dataset',type=Path,default=Path('/home/alan-william/xu2026/data/dataset_v4/dataset.npz'))
    a=p.parse_args(); out=a.artifacts
    manifest=json.loads((out/'manifest.json').read_text())
    assert sha(a.dataset)==manifest['dataset_sha256'], 'Dataset hash mismatch'
    for name in ['calibration.npz','eca_2d_fp32.onnx']:
        assert sha(out/name)==manifest['files'][name], 'Artifact hash mismatch: '+name
    cal=np.load(out/'calibration.npz',allow_pickle=False)
    d=np.load(a.dataset,allow_pickle=False)
    x,y,split=d['X'],d['y'],d['split'];d.close()
    assert np.all(split[cal['dataset_index']]==0)
    quant_pre_process(str(out/'eca_2d_fp32.onnx'),str(out/'eca_2d_preprocessed.onnx'))
    target=out/'eca_2d_uint8_candidate.onnx'
    quantize_static(str(out/'eca_2d_preprocessed.onnx'),str(target),Reader(cal['X']),
        quant_format=QuantFormat.QOperator,activation_type=QuantType.QUInt8,
        weight_type=QuantType.QUInt8,per_channel=False,
        op_types_to_quantize=['Conv','MatMul','Gemm'])
    onnx.checker.check_model(onnx.load(target))
    opts=ort.SessionOptions(); opts.intra_op_num_threads=4
    fp=ort.InferenceSession(str(out/'eca_2d_fp32.onnx'),sess_options=opts,providers=['CPUExecutionProvider'])
    q=ort.InferenceSession(str(target),sess_options=opts,providers=['CPUExecutionProvider'])
    ids=np.flatnonzero(split==1)
    xf=[{'waveform':x[i:i+1].reshape(1,3,1,500)} for i in ids]
    lf=np.concatenate([fp.run(None,v)[0] for v in xf]); lq=np.concatenate([q.run(None,v)[0] for v in xf])
    from collections import Counter
    report={'status':'experimental offline candidate; Alnpu acceptance pending',
        'calibration_count':len(cal['X']),'calibration_split':'train','evaluation_split':'val',
        'evaluation_count':len(ids),'input_output_dtype':'float32; internal Conv/Gemm quantization',
        'accuracy_fp32':float(np.mean(lf.argmax(1)==y[ids])),
        'accuracy_candidate':float(np.mean(lq.argmax(1)==y[ids])),
        'top1_agreement':float(np.mean(lf.argmax(1)==lq.argmax(1))),
        'max_abs_logit_error':float(np.max(abs(lq-lf))),
        'ops':dict(Counter(n.op_type for n in onnx.load(target).graph.node)), 'sha256':sha(target)}
    dump(out/'quantization.json',report)
    manifest=json.loads((out/'manifest.json').read_text())
    for f in [target,out/'quantization.json',out/'eca_2d_preprocessed.onnx']: manifest['files'][f.name]=sha(f)
    dump(out/'manifest.json',manifest)
    print(json.dumps(report,indent=2))

if __name__=='__main__': main()
