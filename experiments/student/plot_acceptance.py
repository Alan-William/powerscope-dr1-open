"""Export a source-backed heldout recall figure; output belongs outside Git."""

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

p = argparse.ArgumentParser()
p.add_argument("--report", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
d = json.loads(a.report.read_text(encoding="utf-8"))["heldout"]
names = [x["name"] for x in d["npu"]["per_class"]]
recall = np.array([x["recall"] for x in d["npu"]["per_class"]]) * 100
teacher = np.array([x["recall"] for x in d["teacher"]["per_class"]]) * 100
fig, ax = plt.subplots(figsize=(11, 8))
fig.patch.set_facecolor("#fafbf8")
ax.set_facecolor("#fafbf8")
y = np.arange(22)
ax.barh(
    y,
    recall,
    color=["#ba452e" if r < 90 else "#247c68" for r in recall],
    height=0.65,
    label="kd-s1 / board Alnpu",
)
ax.scatter(
    teacher,
    y,
    s=22,
    facecolors="white",
    edgecolors="#344650",
    zorder=3,
    label="Teacher / CPU FP32",
)
for i, r in enumerate(recall):
    if r < 99:
        ax.text(r + 1.5, i, f"{r:.1f}%", va="center", fontsize=10, color="#923723")
ax.set_yticks(y, names)
ax.invert_yaxis()
ax.set_xlim(-1, 105)
ax.set_xlabel("Recall (%)")
ax.spines[["top", "right", "left"]].set_visible(False)
ax.tick_params(axis="y", length=0)
ax.grid(axis="x", alpha=0.15)
ax.set_axisbelow(True)
ax.legend(
    loc="lower left", bbox_to_anchor=(0, 1.005), ncol=2, frameon=False, borderaxespad=0
)
fig.suptitle(
    "Event-E2 holdout exposes two SF failure modes",
    x=0.18,
    ha="left",
    fontsize=17,
    fontweight="bold",
    y=0.97,
)
fig.text(
    0.18,
    0.925,
    f"Board: {d['npu']['correct']}/4400 = {100*d['npu']['accuracy']:.2f}%  |  Macro F1: {100*d['npu']['macro_f1']:.2f}%",
    fontsize=12,
)
fig.text(
    0.18,
    0.02,
    "200 windows per class; overlapping simulation windows, not independent physical trials.\nFrozen kd-s1; no retraining or calibration on this test set.",
    fontsize=9,
    color="#52615e",
)
fig.subplots_adjust(left=0.18, right=0.96, top=0.86, bottom=0.10)
a.output.parent.mkdir(parents=True, exist_ok=True)
fig.savefig(a.output, dpi=160)
plt.close(fig)
print(a.output)
