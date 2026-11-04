"""Build a new ignored PS asset bundle from the validated student and dataset."""

import argparse, hashlib, json
from pathlib import Path
import numpy as np

p = argparse.ArgumentParser()
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
root = Path("/home/alan-william/powerscope-dr1")
dataset = Path("/home/alan-william/xu2026/data/dataset_v4/dataset.npz")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


assert (
    sha(dataset) == "a9849da35f01595e201325295ffadd395e49dfa59ba3c1149e77b217ad6a5b2b"
)
a.output.mkdir(parents=True, exist_ok=False)
model = {"id": "kd-s1", "statistics": False}
for part, digest in [
    ("trunk", "75d726be16abbc4fb74238cec880beeb51bef0a984918a81daedb65ce2404a2c"),
    ("head", "b5edd4aa73b92b3dc008387cc27d766f4752b7864d6eea7858fef679f538e267"),
]:
    source = root / "artifacts/student-train-r1/kd-s1" / (part + ".onnx")
    assert sha(source) == digest
    dest = a.output / source.name
    dest.write_bytes(source.read_bytes())
    model[part] = {"path": dest.name, "sha256": digest}
d = np.load(dataset, allow_pickle=False)
ids = np.flatnonzero(d["split"] == 1)
assert len(ids) == 1100
demo = np.array([ids[np.flatnonzero(d["y"][ids] == c)[0]] for c in range(22)])
manifest = {
    "schema": 1,
    "dataset_sha256": sha(dataset),
    "model": model,
    "source": "existing v4 simulation validation split; no ADC; no new test claims",
    "format": "little-endian float32, A500 then B500 then C500, normalized [-1,1], 6000 bytes per frame",
    "datasets": {},
}
for name, selected, title in [
    ("demo22", demo, "22 类演示"),
    ("validation", ids, "1100 窗验证集"),
]:
    dest = a.output / (name + ".f32")
    d["X"][selected].astype("<f4").tofile(dest)
    manifest["datasets"][name] = {
        "path": dest.name,
        "sha256": sha(dest),
        "count": len(selected),
        "labels": d["y"][selected].tolist(),
        "dataset_indices": selected.tolist(),
        "title": title,
    }
(a.output / "manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
)
print("Wrote", a.output, "without modifying upstream data or weights")
