"""Reload every saved checkpoint and independently recount classification metrics."""
import argparse,hashlib,json
from pathlib import Path
import numpy as np
import torch
from network import Student,inputs

p=argparse.ArgumentParser();p.add_argument('--run-dir',type=Path,required=True);a=p.parse_args()
protocol=json.loads((a.run_dir/'protocol.json').read_text())
dataset=Path('/home/alan-william/xu2026/data/dataset_v4/dataset.npz')
assert hashlib.sha256(dataset.read_bytes()).hexdigest()==protocol['dataset_sha256']
d=np.load(dataset,allow_pickle=False);vi=np.flatnonzero(d['split']==1);x=d['X'][vi];y=d['y'][vi]
torch.set_num_threads(2);audit=[]
for row in json.loads((a.run_dir/'results.json').read_text()):
    q=a.run_dir/(row['arm']+'-s'+str(row['seed']))
    cp=torch.load(q/'best.pt',weights_only=True,map_location='cpu');m=Student(rep=cp['rep']).eval();m.load_state_dict(cp['state'])
    xx=inputs(x,cp['features'],8400+cp['seed'])
    with torch.no_grad():z=np.concatenate([m(t).numpy() for t in torch.from_numpy(xx).split(256)])
    saved=np.load(q/'predictions.npz',allow_pickle=False)
    assert np.array_equal(saved['val_indices'],vi) and np.array_equal(saved['labels'],y)
    delta=float(np.max(abs(z-saved['fp32'])));assert delta<.001
    for kind,key in [('fp32','fp32'),('qdq','quant')]:
        pred=saved[key].argmax(1);cm=np.bincount(y*22+pred,minlength=22*22).reshape(22,22)
        counts=cm.sum(1)+cm.sum(0);f1=np.divide(2*np.diag(cm),counts,out=np.zeros(22),where=counts!=0)
        assert abs(float(np.mean(y==pred))-row[kind]['accuracy'])<1e-12
        assert abs(float(f1.mean())-row[kind]['macro_f1'])<1e-12
        assert np.array_equal(cm,row[kind]['confusion'])
        np.testing.assert_allclose(np.diag(cm)/cm.sum(1),row[kind]['recall'],atol=1e-12)
    hist=json.loads((q/'history.json').read_text());assert len(hist)==protocol['epochs']
    assert cp['epoch']==max(hist,key=lambda h:(h[2],-h[3]))[0]
    for name,digest in row['artifacts_sha256'].items():assert hashlib.sha256((q/name).read_bytes()).hexdigest()==digest
    audit.append({'run':q.name,'checkpoint_replay_max_abs':delta,'independent_metrics_match':True,'checkpoint_selection_matches_history':True,'artifact_hashes_match':True})
result={'runs':audit,'test_evaluated':False,'audit_script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
(a.run_dir/'independent_audit.json').write_text(json.dumps(result,indent=2)+'\n');print('Verified',len(audit),'checkpoints and independent metrics')
