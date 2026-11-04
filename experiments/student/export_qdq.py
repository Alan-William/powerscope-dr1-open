"""Small explicit per-tensor UINT8 QDQ exporter, calibrated on training only.

Writes only standard ONNX. Alnpu parser removes endpoint Q/DQ and exposes
quantized bindings; actual bindings must be read from the runtime, not guessed.
"""
import json
from pathlib import Path
import numpy as np
import onnx
from onnx import helper as h, numpy_helper as nh, TensorProto as T
import torch
from torch import nn


def qparams(lo, hi):
    lo, hi = min(0., float(lo)), max(0., float(hi))
    scale = np.float32(max((hi-lo)/255, 1e-7))
    return scale, np.uint8(np.clip(np.rint(-lo/scale), 0, 255))


def export(model, calibration, output, input_quant=None):
    model = model.cpu().eval()
    ranges = [[float(calibration.min()), float(calibration.max())]]
    for _ in model:
        ranges.append([float('inf'), float('-inf')])
    with torch.no_grad():
        for chunk in np.array_split(calibration, max(1, (len(calibration)+63)//64)):
            v = torch.from_numpy(chunk)
            for j, op in enumerate(model):
                v = op(v)
                ranges[j+1][0] = min(ranges[j+1][0], float(v.min()))
                ranges[j+1][1] = max(ranges[j+1][1], float(v.max()))
    nodes, initializers, record = [], [], []
    def init(name, val):
        initializers.append(nh.from_array(np.asarray(val), name)); return name
    def qdq(source, name, scale, zp):
        sn, zn = init(name+'s', np.float32(scale)), init(name+'z', np.uint8(zp))
        nodes.extend([h.make_node('QuantizeLinear', [source,sn,zn], [name+'q']),
                      h.make_node('DequantizeLinear', [name+'q',sn,zn], [name+'d'])])
        return name+'d'
    scale, zp = qparams(*ranges[0]) if input_quant is None else input_quant; current = qdq('input', 'input_', scale, zp)
    spatial = calibration.shape[-1]
    channels = calibration.shape[1]
    for j, op in enumerate(model):
        name = 'layer'+str(j); raw = name+'raw'
        if isinstance(op, (nn.Conv2d, nn.Linear)):
            w = op.weight.detach().numpy(); b = op.bias.detach().numpy(); channels = op.out_channels if isinstance(op,nn.Conv2d) else op.out_features
            ws = np.float32(max(float(np.abs(w).max())/127, 1e-8)); wz = np.uint8(128)
            wq = np.clip(np.rint(w/ws)+128,0,255).astype(np.uint8)
            bs = np.float32(scale*ws); bq64 = np.rint(b/bs)
            if np.abs(bq64).max() > np.iinfo(np.int32).max: raise ValueError('Bias overflow')
            for tag, arr, sc, z in [('w',wq,ws,wz),('b',bq64.astype(np.int32),bs,np.int32(0))]:
                names = [init(name+tag+'q', arr), init(name+tag+'s',sc), init(name+tag+'z',z)]
                nodes.append(h.make_node('DequantizeLinear', names, [name+tag+'d']))
            if isinstance(op, nn.Conv2d):
                k, pad, stride = op.kernel_size[0], op.padding[0], op.stride[0]
                nodes.append(h.make_node('Conv',[current,name+'wd',name+'bd'],[raw],kernel_shape=[k,k],pads=[pad]*4,strides=[stride]*2))
                spatial = (spatial+2*pad-k)//stride+1
            else:
                nodes.append(h.make_node('Gemm',[current,name+'wd',name+'bd'],[raw],transB=1))
        elif isinstance(op, nn.Flatten):
            nodes.append(h.make_node('Flatten',[current],[raw],axis=1))
        elif isinstance(op, nn.ReLU):
            nodes.append(h.make_node('Relu',[current],[raw]))
        elif isinstance(op, nn.MaxPool2d):
            k=op.kernel_size;nodes.append(h.make_node('MaxPool',[current],[raw],kernel_shape=[k,k],strides=[k,k]))
            spatial //= k
        else: raise TypeError(type(op))
        if isinstance(op, (nn.Conv2d, nn.Linear, nn.ReLU)):
            scale,zp=qparams(*ranges[j+1])
        # MaxPool/Flatten preserve quantization. ReLU uses its nonnegative range.
        current=qdq(raw,name,scale,zp)
        record.append({'layer':j,'type':type(op).__name__,'range':ranges[j+1], 'scale':float(scale),'zero_point':int(zp)})
    nodes[-1].output[0]='logits'
    with torch.no_grad(): output_shape=list(model(torch.from_numpy(calibration[:1])).shape)
    graph=h.make_graph(nodes,'dr1_student',[h.make_tensor_value_info('input',T.FLOAT,[1,*calibration.shape[1:]])],
                       [h.make_tensor_value_info('logits',T.FLOAT,output_shape)],initializers)
    result=h.make_model(graph,opset_imports=[h.make_opsetid('',13)],ir_version=7)
    result=onnx.shape_inference.infer_shapes(result);onnx.checker.check_model(result);onnx.save(result,str(output))
    Path(output).with_suffix('.quant.json').write_text(json.dumps(record,indent=2))
    return record
