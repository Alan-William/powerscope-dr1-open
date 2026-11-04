"""Untrained two-stage Alnpu gate; both stages contain all neural arithmetic."""
import argparse,json,hashlib
from pathlib import Path
import numpy as np
import torch
import onnxruntime as ort
from network import Student,inputs
from export_qdq import export
p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
a.output.mkdir(parents=True,exist_ok=False)
torch.set_num_threads(2);torch.manual_seed(20261031)
rng=np.random.default_rng(203);waves=rng.uniform(-1,1,(32,3,500)).astype('f4');cal=inputs(waves,'stats')
assert np.array_equal(cal[:,:3].reshape(32,3,1024)[:,:,:500],waves)
probe=cal[:16].copy();probe[0]=0;probe[-1]=probe[-2];probe.tofile(a.output/'input.f32')
m=Student().eval();d=m.deploy();trunk=torch.nn.Sequential(*list(d.children())[:-2]);head=torch.nn.Sequential(list(d.children())[-1])
q=export(trunk,cal,a.output/'trunk.onnx');
with torch.no_grad(): mid=trunk(torch.from_numpy(cal)).numpy().reshape(len(cal),-1)
export(head,mid,a.output/'head.onnx',input_quant=(np.float32(q[-1]['scale']),np.uint8(q[-1]['zero_point'])))
export(d,cal,a.output/'full.onnx')
o=ort.SessionOptions();o.intra_op_num_threads=2;o.graph_optimization_level=ort.GraphOptimizationLevel.ORT_DISABLE_ALL
session=ort.InferenceSession(str(a.output/'full.onnx'),o,providers=['CPUExecutionProvider'])
ref=np.concatenate([session.run(None,{'input':v[None]})[0] for v in probe]);ref.tofile(a.output/'reference.f32')
torch.save(m.state_dict(),a.output/'untrained.pt')
record={'untrained':True,'shape':list(probe.shape),'output':[1,22], 'gate_before_measurement':{'vendor_reference_max_lsb':1.01,'ort_max_lsb':12,'ort_mean_lsb':3,'repeat_exact':True},'sha256':{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in a.output.glob('*.onnx')}}
(a.output/'manifest.json').write_text(json.dumps(record,indent=2));print('Generated complete student pipeline gate: 16 frames')
