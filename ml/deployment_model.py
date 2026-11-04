"""Equivalent static 2-D graph with ordinary convolutions and QDQ-friendly ops."""
import copy
import torch
from torch import nn
from torch.nn import functional as F
from model import lift_to_2d

class ChannelAttention(nn.Module):
    def __init__(self,conv):
        super().__init__();self.conv=copy.deepcopy(conv)
    def forward(self,x):
        # Fixed batch=1 at export; reshape preserves the channel order.
        w=F.adaptive_avg_pool2d(x,(1,1)).reshape(x.shape[0],1,1,x.shape[1])
        return x*self.conv(w).sigmoid().reshape(x.shape[0],x.shape[1],1,1)

def ordinary_convolutions(parent):
    for name,m in list(parent.named_children()):
        if isinstance(m,nn.Conv2d) and (m.groups!=1 or m.dilation!=(1,1)):
            kh,kw=((k-1)*d+1 for k,d in zip(m.kernel_size,m.dilation))
            n=nn.Conv2d(m.in_channels,m.out_channels,(kh,kw),m.stride,m.padding,bias=m.bias is not None)
            n.weight.data.zero_()
            ci,co=m.in_channels//m.groups,m.out_channels//m.groups
            for group in range(m.groups):
                n.weight.data[group*co:(group+1)*co,group*ci:(group+1)*ci,
                    ::m.dilation[0],::m.dilation[1]].copy_(m.weight.data[group*co:(group+1)*co])
            if m.bias is not None:n.bias.data.copy_(m.bias.data)
            setattr(parent,name,n)
        else:ordinary_convolutions(m)

def pointwise(linear):
    conv=nn.Conv2d(linear.in_features,linear.out_features,1,bias=True)
    conv.weight.data.copy_(linear.weight.data[:,:,None,None]);conv.bias.data.copy_(linear.bias.data)
    return conv

class DeploymentNet(nn.Module):
    def __init__(self,baseline):
        super().__init__();m=lift_to_2d(baseline)
        for name in ['ds','bn_ds','p1','p2','r1','r2','gap']:setattr(self,name,getattr(m,name))
        for name in ['e1','e2','e3']:setattr(self,name,ChannelAttention(getattr(m,name).conv))
        self.fc1,self.fc2=pointwise(m.fc1),pointwise(m.fc2)
        ordinary_convolutions(self)
        self.eval()
    def forward(self,x):
        y=self.bn_ds(self.ds(x)).relu()
        y=self.r1(self.p1(self.e1(y)));y=self.r2(self.p2(self.e2(y)))
        y=self.gap(self.e3(y))
        return self.fc2(self.fc1(y).relu()).flatten(1)
