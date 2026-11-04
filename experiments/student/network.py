"""DR1 student: explicit spatial head; no mean/attention/flatten inside NPU graph."""
import copy
import numpy as np
import torch
from torch import nn


def inputs(x, features="none", seed=703):
    """Fixed 32x32 per-phase layout, 500 samples + 524 zeros (not a 1D equivalence).

    All ablation arms have nine input channels. Six constant planes are either
    zero, physical Q15 statistics, or independent nuisance values. Statistics
    describe normalized currents, not absolute amperes. Integer intermediate
    ranges fit signed 64 bits for exactly 500 samples.
    """
    x = np.asarray(x, dtype=np.float32)
    assert x.ndim == 3 and x.shape[1:] == (3, 500) and np.isfinite(x).all()
    y = np.zeros((len(x), 9, 32, 32), np.float32)
    y[:, :3].reshape(len(x), 3, 1024)[:, :, :500] = x
    if features == "stats":
        q = np.rint(np.clip(x, -1, 1)*32767).astype(np.int64)
        sums = q.sum(-1)
        # Keep exact sums before converting the software reference to float.
        mean = sums / (500*32767)
        ac_energy = (q*q).sum(-1)/(500*32767**2)-mean**2
        f = np.concatenate([mean, np.maximum(ac_energy, 0)], axis=1)
        y[:, 3:] = f[:, :, None, None]
    elif features == "noise":
        rng = np.random.default_rng(seed)
        f = np.concatenate([rng.uniform(-1, 1, (len(x), 3)),
                            rng.uniform(0, 1, (len(x), 3))], axis=1)
        y[:, 3:] = f[:, :, None, None]
    elif features != "none":
        raise ValueError(features)
    return y


class Block(nn.Module):
    def __init__(self, ci, co, rep=False):
        super().__init__()
        self.conv = nn.Conv2d(ci, co, 3, padding=1, bias=False)
        self.bn = nn.BatchNorm2d(co)
        self.extra = nn.Sequential(nn.Conv2d(ci, co, 1, bias=False), nn.BatchNorm2d(co)) if rep else None

    def forward(self, x):
        z = self.bn(self.conv(x))
        if self.extra is not None:
            z = z+self.extra(x)
        return torch.relu(z)

    def fused(self):
        c = torch.nn.utils.fuse_conv_bn_eval(self.conv, self.bn)
        if self.extra is not None:
            e = torch.nn.utils.fuse_conv_bn_eval(*self.extra)
            c.weight.data[:, :, 1:2, 1:2] += e.weight.data
            c.bias.data += e.bias.data
        return c


class Student(nn.Module):
    def __init__(self, widths=(16, 32, 32), rep=False, head="fc"):
        super().__init__()
        self.blocks = nn.ModuleList([Block(ci, co, rep) for ci, co in zip((9,)+tuple(widths[:-1]), widths)])
        self.head_kind = head
        self.head = nn.Linear(widths[-1]*4*4, 22) if head == "fc" else nn.Conv2d(widths[-1], 22, 3 if head == "conv" else 1)

    def forward(self, x):
        for b in self.blocks:
            x = torch.nn.functional.max_pool2d(b(x), 2)
        if self.head_kind == "fc":
            return self.head(x.flatten(1))
        x = self.head(x)
        x = torch.nn.functional.max_pool2d(x, 2)
        if self.head_kind == "max":
            x = torch.nn.functional.max_pool2d(x, 2)
        return x[:, :, 0, 0]  # training convenience; exported graph binds [N,22,1,1]

    def deploy(self):
        model = copy.deepcopy(self).cpu().eval()
        ops = []
        for block in model.blocks:
            ops += [block.fused(), nn.ReLU(), nn.MaxPool2d(2)]
        if model.head_kind == "fc":
            return nn.Sequential(*ops, nn.Flatten(), model.head).eval()
        ops += [model.head]
        ops += [nn.MaxPool2d(2)]
        if model.head_kind == "max":
            ops += [nn.MaxPool2d(2)]
        return nn.Sequential(*ops).eval()
