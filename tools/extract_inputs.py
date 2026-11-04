"""Pure NumPy fixture extraction; no model inference or training."""
import argparse,hashlib,json
from pathlib import Path
import numpy as np

root=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('dataset',type=Path);a=p.parse_args()
def sha(f): return hashlib.sha256(f.read_bytes()).hexdigest()
source=json.loads((root/'reports/manifest.json').read_text(encoding='utf-8'))
assert sha(a.dataset)==source['dataset_sha256'],'Unexpected dataset hash'
d=np.load(a.dataset,allow_pickle=False)
ids=np.array([np.flatnonzero((d['split']==1)&(d['y']==c))[0] for c in range(22)])
x=d['X'][ids]
assert x.shape==(22,3,500) and np.isfinite(x).all() and np.max(abs(x))<=1.00001
q=np.rint(x*32767).clip(-32767,32767).astype('<i2')
out=root/'fixtures';out.mkdir(exist_ok=True)
np.savez(out/'waveform_inputs.npz',X=x,q15=q,y=d['y'][ids],dataset_index=ids,
    source_id=d['source_id'][ids],start=d['start'][ids],event=d['event'][ids])
q.tofile(out/'golden_q15.bin');x.astype('<f4').tofile(out/'golden_f32.bin')
record={'scope':'input fixtures only; no model predictions','dataset_sha256':sha(a.dataset),
    'selection':'first validation row for each class in ascending label order',
    'count':22,'dataset_indices':ids.tolist(),'shape':[22,3,500],
    'q15_max_input_error':float(np.max(abs(x-q.astype(np.float32)/32767))),
    'files':{name:sha(out/name) for name in ['waveform_inputs.npz','golden_q15.bin','golden_f32.bin']}}
(root/'reports/input_fixture_manifest.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
print(json.dumps(record,indent=2))
