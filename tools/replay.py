"""Offline CPU replay with explicit backend identity and real model outputs."""
import argparse,hashlib,json,sys,time
from pathlib import Path
import numpy as np
import onnxruntime as ort
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from protocol.modbus import read_request,reply

def main():
    p=argparse.ArgumentParser(); p.add_argument('--artifacts',type=Path,default=Path('artifacts/baseline-v4'))
    p.add_argument('--output',type=Path,default=Path('artifacts/replay.jsonl'))
    p.add_argument('--model',type=Path,help='Explicit 2-D FP32 or QDQ model; default is baseline FP32')
    a=p.parse_args(); data=np.load(a.artifacts/'golden.npz',allow_pickle=False)
    opts=ort.SessionOptions(); opts.intra_op_num_threads=4
    opts.graph_optimization_level=ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    model_path=a.model or a.artifacts/'eca_2d_fp32.onnx'
    model_sha=hashlib.sha256(model_path.read_bytes()).hexdigest()
    model=ort.InferenceSession(str(model_path),sess_options=opts,providers=['CPUExecutionProvider'])
    assert model.get_inputs()[0].shape==[1,3,1,500] and model.get_outputs()[0].shape==[1,22]
    waves,labels=data['q15'],data['y'];data.close()
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open('w',encoding='utf-8') as f:
        for seq,x in enumerate(waves,1):
            x=x.astype(np.float32).reshape(1,3,1,500)/32767
            start=time.perf_counter_ns(); logits=model.run(None,{'waveform':x})[0][0]
            if not np.isfinite(logits).all():raise ValueError('Non-finite inference output')
            us=min((time.perf_counter_ns()-start)//1000,0xffffffff)
            prob=np.exp(logits-logits.max()); prob/=prob.sum(); label=int(prob.argmax())
            # flags: VALID|SIMULATED, NPU bit remains clear.
            regs=[1,5,label,int(round(float(prob[label])*10000)),seq>>16,seq&65535,us>>16,us&65535,0,0]
            row={'backend':'onnxruntime-cpu','optimization':'ORT_DISABLE_ALL','model_sha256':model_sha,'simulated':True,'sequence':seq,'label':label,
                'expected':int(labels[seq-1]),'confidence':float(prob[label]),'host_inference_us':us,
                'registers':regs,'modbus_response_hex':reply(read_request(),regs).hex()}
            f.write(json.dumps(row)+'\n')
    print(f'Wrote {len(labels)} offline predictions to {a.output}')

if __name__=='__main__': main()
