"""Read the real board input binary, run CPU ORT, and validate wire fixtures."""
import argparse,hashlib,json,sys
from pathlib import Path
import numpy as np
import onnxruntime as ort
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.check_logits import compare
from protocol.modbus import decode_response

p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path,default=Path('artifacts/baseline-v4'))
p.add_argument('--package',type=Path,default=Path('artifacts/npu-qdq-v2'));a=p.parse_args()
manifest=json.loads((a.package/'manifest.json').read_text())
model=a.package/'eca_deploy_uint8_qdq.onnx'
assert hashlib.sha256(model.read_bytes()).hexdigest()==manifest['files'][model.name]
baseline=json.loads((a.baseline/'manifest.json').read_text())
raw=(a.baseline/'golden_f32.bin').read_bytes()
assert hashlib.sha256(raw).hexdigest()==baseline['files']['golden_f32.bin']
assert len(raw)==22*1500*4
x=np.frombuffer(raw,dtype='<f4').reshape(22,3,1,500)
opts=ort.SessionOptions();opts.intra_op_num_threads=2;opts.inter_op_num_threads=1
opts.graph_optimization_level=ort.GraphOptimizationLevel.ORT_DISABLE_ALL
session=ort.InferenceSession(str(model),sess_options=opts,providers=['CPUExecutionProvider'])
actual=np.concatenate([session.run(None,{'waveform':v[None]})[0] for v in x])
reference=np.load(a.package/'golden_qdq_logits.npy',allow_pickle=False)
numeric=compare(actual,reference,1e-4,1.0)
assert numeric['numeric_match'],numeric
actual.astype('<f4').tofile(a.package/'cpu_probe.logits.bin')
rows=[json.loads(s) for s in (a.package/'replay.jsonl').read_text().splitlines()]
assert len(rows)==22
for row in rows:
    assert tuple(row['registers'])==decode_response(bytes.fromhex(row['modbus_response_hex']))
    assert row['registers'][1]==5 and row['backend']=='onnxruntime-cpu'
    assert row['registers'][2]==row['label'] and row['registers'][4:6]==[0,row['sequence']]
report={'scope':'offline CPU acceptance; no NPU execution or physical RS485',
    'model_sha256':manifest['files'][model.name],'binary_input_bytes':len(raw),
    'ort_graph_optimization':'ORT_DISABLE_ALL','float32_binary_vs_qdq_reference':numeric,
    'q15_replay_count':len(rows),'q15_replay_accuracy':sum(r['label']==r['expected'] for r in rows)/len(rows),
    'modbus_roundtrips_verified':len(rows),'npu_flag_clear_for_all_cpu_frames':True}
(a.package/'offline_acceptance.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
