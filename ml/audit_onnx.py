"""Static evidence, not a vendor compiler or a declaration of NPU support."""
import argparse,hashlib,json
from collections import Counter
from pathlib import Path
import onnx

def inspect(path):
    m=onnx.load(path);onnx.checker.check_model(m)
    def tensor(v):return {'name':v.name,'dtype':onnx.TensorProto.DataType.Name(v.type.tensor_type.elem_type),
        'shape':[d.dim_value if d.HasField('dim_value') else d.dim_param for d in v.type.tensor_type.shape.dim]}
    return {'name':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'ir_version':m.ir_version,'opsets':{i.domain or 'ai.onnx':i.version for i in m.opset_import},
        'inputs':[tensor(x) for x in m.graph.input if x.name not in {t.name for t in m.graph.initializer}],
        'outputs':[tensor(x) for x in m.graph.output],
        'ops':dict(Counter((n.domain or 'ai.onnx')+':'+n.op_type for n in m.graph.node)),
        'conv_attributes':[{'name':n.name,**{a.name:onnx.helper.get_attribute_value(a)
            for a in n.attribute if a.name in ['kernel_shape','dilations','group','pads','strides']}}
            for n in m.graph.node if n.op_type in ['Conv','QLinearConv']],
        'initializer_dtypes':dict(Counter(onnx.TensorProto.DataType.Name(t.data_type) for t in m.graph.initializer))}

def main():
    p=argparse.ArgumentParser();p.add_argument('models',nargs='+',type=Path);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();r={'scope':'static ONNX graph inventory; vendor sample evidence does not prove full operator support',
        'models':[inspect(f) for f in a.models]}
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(r,indent=2)+'\n')
    for m in r['models']:print(m['name'],m['opsets'],m['inputs'],m['ops'],flush=True)

if __name__=='__main__':main()
