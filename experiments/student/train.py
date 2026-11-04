"""Paired validation-only experiments after the full Alnpu pipeline gate.

Large checkpoints/predictions/exports go to a new artifacts directory. Never
changes the source dataset, teacher, historical experiments, or test split.
"""
import os
os.environ.setdefault('CUBLAS_WORKSPACE_CONFIG', ':4096:8')
import argparse,hashlib,json,time,sys
from pathlib import Path
import numpy as np
import torch
from torch import nn
import torch.nn.functional as F
import onnxruntime as ort
from network import Student,inputs
from export_qdq import export

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'ml'))
from model import ECARLNet
DATA_SHA='a9849da35f01595e201325295ffadd395e49dfa59ba3c1149e77b217ad6a5b2b'
CKPT_SHA='62bda590c58de1a3e5006ca2e221fd6a9b9aae06170600992f20aed4f387417a'
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def dump(p,d):Path(p).write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
def metrics(y,z):
    pred=z.argmax(1);cm=np.zeros((22,22),np.int64);np.add.at(cm,(y,pred),1)
    tp=cm.diagonal();rec=tp/np.maximum(cm.sum(1),1);pr=tp/np.maximum(cm.sum(0),1)
    f1=2*pr*rec/np.maximum(pr+rec,1e-12)
    return {'accuracy':float(np.mean(pred==y)),'macro_f1':float(f1.mean()),'recall':rec.tolist(),'confusion':cm.tolist()}
def infer(model,x):
    with torch.no_grad():return torch.cat([model(a) for a in x.split(256)]).cpu().numpy()

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--gate',type=Path,required=True)
    p.add_argument('--epochs',type=int,default=120);a=p.parse_args()
    gate=json.loads(a.gate.read_text());assert gate['passed'] and gate['frames']>=16
    a.output.mkdir(parents=True,exist_ok=False)
    torch.set_num_threads(2);torch.use_deterministic_algorithms(True);torch.backends.cudnn.benchmark=False
    torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
    device='cuda';torch.cuda.set_per_process_memory_fraction(.25)
    ds=Path('/home/alan-william/xu2026/data/dataset_v4/dataset.npz')
    ck=Path('/home/alan-william/xu2026/data/models_v4/clf_ckpt/ECA-RLNet_none_all_n40_s0.pt')
    assert sha(ds)==DATA_SHA and sha(ck)==CKPT_SHA
    data=np.load(ds,allow_pickle=False);ti=np.flatnonzero(data['split']==0);vi=np.flatnonzero(data['split']==1)
    assert len(ti)==880 and len(vi)==1100
    xtr,ytr=data['X'][ti].astype('f4'),data['y'][ti].astype('i8');xv,yv=data['X'][vi].astype('f4'),data['y'][vi].astype('i8')
    state=torch.load(ck,map_location='cpu',weights_only=False);assert state['dataset_sha256']==DATA_SHA
    teacher=ECARLNet().to(device).eval();teacher.load_state_dict(state['state'],strict=True)
    teacher_train=infer(teacher,torch.from_numpy(xtr).to(device));teacher_val=infer(teacher,torch.from_numpy(xv).to(device))
    tlog=torch.from_numpy(teacher_train).to(device);yt=torch.from_numpy(ytr).to(device)
    arms=[('ce',False,False,'none'),('kd',False,True,'none'),('rep_ce',True,False,'none'),('rep_kd',True,True,'none'),('rep_kd_stats',True,True,'stats'),('rep_kd_noise',True,True,'noise')]
    protocol={'dataset_sha256':DATA_SHA,'teacher_sha256':CKPT_SHA,'gate_report_sha256':sha(a.gate),'epochs':a.epochs,'seeds':[0,1,2],
              'arms':[list(row) for row in arms],'split':'train for fitting/calibration, val for checkpoint/model selection; test untouched',
              'batch_size':128,'optimizer':'AdamW lr=.003 weight_decay=.0001; cosine to .00003',
              'KD':'0.5*CE + 0.5*T^2*KL(teacher||student), T=2',
              'rep_init':'same base Conv/BN/FC; added 1x1 branch BN gamma=0',
              'selection':'highest val accuracy, then lowest val CE, within fixed epoch budget',
              'features':'all arms 9 channels; raw three-phase 500+524 zero padding; six constant planes none/stats/independent noise',
              'source_sha256':{p.name:sha(p) for p in Path(__file__).parent.glob('*.py')},'runtime':{'torch':torch.__version__,'device':torch.cuda.get_device_name(0)}}
    dump(a.output/'protocol.json',protocol);dump(a.output/'teacher.json',metrics(yv,teacher_val));np.savez(a.output/'indices.npz',train=ti,val=vi)
    np.savez(a.output/'teacher_predictions.npz',train=teacher_train,val=teacher_val)
    all_results=[];start=time.monotonic()
    for name,rep,kd,features in arms:
      for seed in [0,1,2]:
        out=a.output/f'{name}-s{seed}';out.mkdir()
        torch.manual_seed(1000+seed);np.random.seed(1000+seed)
        base=Student(rep=False).eval();m=Student(rep=rep)
        for target,source in zip(m.blocks,base.blocks):
            target.conv.load_state_dict(source.conv.state_dict());target.bn.load_state_dict(source.bn.state_dict())
            if target.extra is not None:nn.init.zeros_(target.extra[1].weight)
        m.head.load_state_dict(base.head.state_dict());m=m.to(device)
        train_np=inputs(xtr,features,seed=7300+seed);val_np=inputs(xv,features,seed=8400+seed)
        train=torch.from_numpy(train_np).to(device);val=torch.from_numpy(val_np).to(device)
        optimizer=torch.optim.AdamW(m.parameters(),lr=.003,weight_decay=.0001)
        scheduler=torch.optim.lr_scheduler.CosineAnnealingLR(optimizer,a.epochs,eta_min=.00003)
        rng=np.random.default_rng(1000+seed);best=(-1.,-1e30);best_epoch=0;history=[];run_start=time.monotonic()
        for epoch in range(a.epochs):
            m.train();order=rng.permutation(len(train));loss_sum=0.
            for ids in np.array_split(order,(len(order)+127)//128):
                ids=torch.as_tensor(ids,device=device);optimizer.zero_grad(set_to_none=True);z=m(train[ids]);ce=F.cross_entropy(z,yt[ids])
                loss=ce
                if kd:loss=.5*ce+.5*4*F.kl_div(F.log_softmax(z/2,dim=1),F.softmax(tlog[ids]/2,dim=1),reduction='batchmean')
                loss.backward();optimizer.step();loss_sum+=float(loss.detach())*len(ids)
            scheduler.step();m.eval();z=infer(m,val);acc=float(np.mean(z.argmax(1)==yv));vce=float(F.cross_entropy(torch.from_numpy(z),torch.from_numpy(yv)))
            history.append([epoch+1,loss_sum/len(train),acc,vce])
            if (acc,-vce)>best:
                best=(acc,-vce);best_epoch=epoch+1
                torch.save({'state':{k:v.detach().cpu() for k,v in m.state_dict().items()},'rep':rep,'features':features,'seed':seed,'epoch':best_epoch},out/'best.pt')
        m.load_state_dict(torch.load(out/'best.pt',map_location=device,weights_only=True)['state']);m.eval();z=infer(m,val)
        d=m.deploy();fused=infer(d,torch.from_numpy(val_np));fusion_err=float(np.max(np.abs(fused-z)));assert fusion_err<.001
        trunk=nn.Sequential(*list(d.children())[:-2]);head=nn.Sequential(list(d.children())[-1])
        q=export(trunk,train_np,out/'trunk.onnx')
        mid=infer(trunk,torch.from_numpy(train_np)).reshape(len(train_np),-1)
        export(head,mid,out/'head.onnx',input_quant=(np.float32(q[-1]['scale']),np.uint8(q[-1]['zero_point'])))
        export(d,train_np,out/'full.onnx')
        so=ort.SessionOptions();so.intra_op_num_threads=2;so.graph_optimization_level=ort.GraphOptimizationLevel.ORT_DISABLE_ALL
        sess=ort.InferenceSession(str(out/'full.onnx'),so,providers=['CPUExecutionProvider'])
        qz=np.concatenate([sess.run(None,{'input':x[None]})[0] for x in val_np])
        np.savez(out/'predictions.npz',val_indices=vi,labels=yv,fp32=z,quant=qz);dump(out/'history.json',history)
        result={'arm':name,'seed':seed,'features':features,'rep':rep,'kd':kd,'best_epoch':best_epoch,'epochs':a.epochs,'updates':a.epochs*7,
                'fp32':metrics(yv,z),'qdq':metrics(yv,qz),'fp32_vs_qdq_agreement':float(np.mean(z.argmax(1)==qz.argmax(1))),
                'fusion_max_abs':fusion_err,'deploy_parameters':sum(v.numel() for v in d.parameters()),'seconds':time.monotonic()-run_start,
                'artifacts_sha256':{p.name:sha(p) for p in out.iterdir() if p.suffix in ['.onnx','.pt','.npz']}}
        dump(out/'result.json',result);all_results.append(result);dump(a.output/'results.json',all_results)
        print(json.dumps({'arm':name,'seed':seed,'fp32':result['fp32']['accuracy'],'qdq':result['qdq']['accuracy'],'best_epoch':best_epoch,'seconds':round(result['seconds'],1)}),flush=True)
        del m,train,val,d,trunk,head;torch.cuda.empty_cache()
    dump(a.output/'complete.json',{'runs':len(all_results),'seconds':time.monotonic()-start,'test_evaluated':False})
if __name__=='__main__':main()
