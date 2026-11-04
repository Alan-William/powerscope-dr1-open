"""Engineering implementation matching the user's xu2026 v4 checkpoint keys.

Architecture reference: xu2026-repro/repro/models/eca_rlnet.py.
This is the existing engineering variant, not a claim of exact paper fidelity.
"""
import copy
import torch
from torch import nn


class ECA(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv = nn.Conv1d(1, 1, 3, padding=1, bias=False)

    def forward(self, x):
        if x.ndim == 4:
            w = x.mean((2, 3), keepdim=True).transpose(1, 3)
            return x * self.conv(w).sigmoid().transpose(1, 3)
        w = x.mean(2, keepdim=True).transpose(1, 2)
        return x * self.conv(w).sigmoid().transpose(1, 2)


class DSConv(nn.Module):
    def __init__(self):
        super().__init__()
        self.dw = nn.Conv1d(3, 3, 7, padding=3, groups=3, bias=False)
        self.pw = nn.Conv1d(3, 32, 1, bias=False)

    def forward(self, x):
        return self.pw(self.dw(x))


class Residual(nn.Module):
    def __init__(self, ci, co, dilation):
        super().__init__()
        self.c1 = nn.Conv1d(ci, co, 3, padding=dilation, dilation=dilation, bias=False)
        self.b1 = nn.BatchNorm1d(co)
        self.c2 = nn.Conv1d(co, co, 3, padding=dilation, dilation=dilation, bias=False)
        self.b2 = nn.BatchNorm1d(co)
        self.skip = nn.Identity() if ci == co else nn.Sequential(
            nn.Conv1d(ci, co, 1, bias=False), nn.BatchNorm1d(co))

    def forward(self, x):
        y = self.b2(self.c2(self.b1(self.c1(x)).relu()))
        return (y + self.skip(x)).relu()


class ECARLNet(nn.Module):
    def __init__(self):
        super().__init__()
        self.ds, self.bn_ds = DSConv(), nn.BatchNorm1d(32)
        self.e1, self.e2, self.e3 = ECA(), ECA(), ECA()
        self.p1, self.p2 = nn.MaxPool1d(2), nn.MaxPool1d(2)
        self.r1, self.r2 = Residual(32, 32, 1), Residual(32, 64, 2)
        self.gap = nn.AdaptiveAvgPool1d(1)
        self.fc1, self.fc2 = nn.Linear(64, 160), nn.Linear(160, 22)

    def forward(self, x):
        y = self.bn_ds(self.ds(x)).relu()
        y = self.r1(self.p1(self.e1(y)))
        y = self.r2(self.p2(self.e2(y)))
        y = self.gap(self.e3(y)).flatten(1)
        return self.fc2(self.fc1(y).relu())


def lift_to_2d(model):
    """Preserve learned parameters and receptive fields with height=1."""
    result = copy.deepcopy(model)
    def replace(parent):
        for name, layer in list(parent.named_children()):
            new = None
            if isinstance(layer, nn.Conv1d):
                new = nn.Conv2d(layer.in_channels, layer.out_channels,
                    (1, layer.kernel_size[0]), (1, layer.stride[0]),
                    (0, layer.padding[0]), (1, layer.dilation[0]), layer.groups,
                    layer.bias is not None)
                new.weight.data.copy_(layer.weight.data.unsqueeze(2))
                if layer.bias is not None: new.bias.data.copy_(layer.bias.data)
            elif isinstance(layer, nn.BatchNorm1d):
                new = nn.BatchNorm2d(layer.num_features, eps=layer.eps, momentum=layer.momentum)
                new.load_state_dict(layer.state_dict())
            elif isinstance(layer, nn.MaxPool1d): new = nn.MaxPool2d((1, 2))
            elif isinstance(layer, nn.AdaptiveAvgPool1d): new = nn.AdaptiveAvgPool2d((1, 1))
            if new is not None: setattr(parent, name, new)
            else: replace(layer)
    replace(result)
    return result.eval()
