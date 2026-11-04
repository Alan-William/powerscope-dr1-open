"""Independently score saved board outputs; never changes evaluation thresholds."""
import argparse,csv,hashlib,json,re
from pathlib import Path
import numpy as np
p=argparse.ArgumentParser();p.add_argument('--results',type=Path,required=True);p.add_argument('--report',type=Path,required=True);a=p.parse_args();d=a.results
reference=np.load(d/'reference.npz',allow_pickle=False);y=reference['labels'];q=reference['quant']
z=np.fromfile(d/'hardware-r3.f32',dtype='f4').reshape(-1,22)
assert z.shape==q.shape==(1100,22) and np.isfinite(z).all()
scale=float((d/'hardware-r3.bindings.tsv').read_text().splitlines()[-1].split('\t')[2])
s=np.fromfile(d/'smoke-npu.f32',dtype='f4').reshape(-1,22);r=np.fromfile(d/'smoke-alref.f32',dtype='f4').reshape(-1,22)
assert s.shape==r.shape==(45,22)
cm=np.zeros((22,22),np.int64);np.add.at(cm,(y,z.argmax(1)),1)
tp=cm.diagonal();rec=tp/np.maximum(cm.sum(1),1);pr=tp/np.maximum(cm.sum(0),1);f1=2*pr*rec/np.maximum(pr+rec,1e-12)
timing=list(csv.DictReader((d/'hardware-r3.csv').open()));assert len(timing)==1100 and [int(t['frame']) for t in timing]==list(range(1100))
assert {t['backend'] for t in timing}=={'Alnpu'}
graphs={}
for stage in ['trunk','head']:
    text=(d/f'hardware-r3.{stage}.dot').read_text();backends=set(re.findall(r'BackendID : (\w+)',text));assert backends=={'Alnpu'}
    profile=json.loads((d/f'hardware-r3.{stage}.profile.json').read_text());assert 'Alnpu' in json.dumps(profile)
    graphs[stage]={'backends':sorted(backends),'hard_npu_layers':text.count('LayerType : ALHardNPU')}
result={'selected':'kd-s1','scope':'existing simulation validation set; selected with this set; no new blind-test claim',
        'frames':1100,'correct':int(tp.sum()),'accuracy':float(tp.sum()/len(y)),'macro_f1':float(f1.mean()),'confusion':cm.tolist(),
        'quant_reference_top1_agreement':float(np.mean(z.argmax(1)==q.argmax(1))),
        'npu_vs_ort_max_lsb':float(np.max(abs(z-q))/scale),'npu_vs_ort_mean_lsb':float(np.mean(abs(z-q))/scale),
        'smoke_frames':45,'npu_vs_alref_max_lsb':float(np.max(abs(s-r))/scale),'repeat_exact':bool(np.array_equal(s[:22],s[22:44])),
        'graphs':graphs,'profiling':'first 16 frames only; timing statistics exclude first 20',
        'timing_scope':'prepacked input quantization + CMA copy + two Alnpu enqueues + output decode; excludes file read/layout preparation, window acquisition, PL/AXI and RS485',
        'timing_us':{},'evidence_sha256':{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in d.iterdir() if f.is_file()}}
for key in ['input_encode_copy_us','trunk_us','head_us','total_us','output_decode_us','frame_us']:
    vals=np.array([int(t[key]) for t in timing[20:]])
    result['timing_us'][key]={'p50':float(np.median(vals)),'p95':float(np.quantile(vals,.95)),'max':int(vals.max())}
result['accepted']=result['accuracy']>=.9872727272 and result['npu_vs_alref_max_lsb']<=1.01 and result['npu_vs_ort_max_lsb']<=12 and result['npu_vs_ort_mean_lsb']<=3 and result['repeat_exact']
a.report.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:result[k] for k in ['correct','frames','accuracy','quant_reference_top1_agreement','accepted']}))
