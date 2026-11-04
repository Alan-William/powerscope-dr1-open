"""Normalize static QDQ metadata for the supplied Arm NN parser.

Only lifts tensor-valued Constant nodes into initializers and infers shapes.
The quantization parameters and arithmetic graph remain unchanged.
"""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
import onnx
import onnxruntime as ort

def normalize(model):
    result=onnx.ModelProto(); result.CopyFrom(model)
    count=0
    for node in list(result.graph.node):
        if node.op_type != "Constant":
            continue
        values=[a for a in node.attribute if a.name=="value" and a.type==onnx.AttributeProto.TENSOR]
        if len(values)!=1 or len(node.output)!=1:
            raise ValueError("Only tensor-valued Constant nodes are supported")
        tensor=onnx.TensorProto(); tensor.CopyFrom(values[0].t); tensor.name=node.output[0]
        result.graph.initializer.append(tensor); result.graph.node.remove(node); count+=1
    result=onnx.shape_inference.infer_shapes(result,strict_mode=True)
    onnx.checker.check_model(result)
    return result,count

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("source",type=Path); ap.add_argument("output",type=Path)
    ap.add_argument("--dataset",type=Path,required=True)
    args=ap.parse_args()
    model,count=normalize(onnx.load(args.source))
    args.output.parent.mkdir(parents=True,exist_ok=True); onnx.save(model,args.output)
    options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
    options.graph_optimization_level=ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    sessions=[ort.InferenceSession(str(p),sess_options=options,providers=['CPUExecutionProvider']) for p in (args.source,args.output)]
    with np.load(args.dataset,allow_pickle=False) as d: x,y,split=d['X'],d['y'],d['split']
    ids=np.flatnonzero(split==1)
    if len(ids)!=1100:raise ValueError('Expected the existing 1100-window validation split')
    results=[np.concatenate([s.run(None,{s.get_inputs()[0].name:x[i:i+1].reshape(1,3,1,500)})[0] for i in ids]) for s in sessions]
    np.testing.assert_array_equal(results[0],results[1])
    report={'scope':'CPU equivalence only; vendor parsing and NPU execution are separate checks',
        'source_sha256':hashlib.sha256(args.source.read_bytes()).hexdigest(),
        'output_sha256':hashlib.sha256(args.output.read_bytes()).hexdigest(),
        'dataset_sha256':hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        'constants_lifted':count,'value_info_count':len(model.graph.value_info),'validation_count':len(ids),
        'max_abs_error':float(np.max(abs(results[0]-results[1]))),
        'top1_agreement':float(np.mean(results[0].argmax(1)==results[1].argmax(1))),
        'validation_accuracy':float(np.mean(results[1].argmax(1)==y[ids])),
        'onnx':onnx.__version__,'onnxruntime':ort.__version__}
    args.output.with_suffix('.normalization.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
