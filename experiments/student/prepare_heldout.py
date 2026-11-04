"""Audit the frozen v4 event holdout, export raw PS assets and CPU references.

Run only on the prepared Ubuntu host. Does not train, select, recalibrate or
overwrite a checkpoint. Full data/logits stay under a NEW ignored output dir.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

import numpy as np
import onnxruntime as ort
import torch

from network import Student, inputs

ROOT = Path("/home/alan-william/powerscope-dr1")
sys.path.insert(0, str(ROOT / "ml"))
from model import ECARLNet


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def dump(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def metrics(y, z):
    pred = z.argmax(1)
    cm = np.zeros((22, 22), np.int64)
    np.add.at(cm, (y, pred), 1)
    tp = cm.diagonal()
    recall = tp / np.maximum(cm.sum(1), 1)
    precision = tp / np.maximum(cm.sum(0), 1)
    f1 = 2 * precision * recall / np.maximum(precision + recall, 1e-12)
    return dict(
        count=len(y),
        correct=int(tp.sum()),
        accuracy=float(tp.sum() / len(y)),
        macro_f1=float(f1.mean()),
        recall=recall.tolist(),
        confusion=cm.tolist(),
    )


def infer(model, x):
    with torch.inference_mode():
        return np.concatenate(
            [model(t).numpy() for t in torch.from_numpy(x).split(128)]
        )


def audit(d, meta):
    ids = [np.flatnonzero(d["split"] == s) for s in range(3)]
    assert list(map(len, ids)) == [880, 1100, 4400]
    assert d["X"].shape == (6380, 3, 500) and np.isfinite(d["X"]).all()
    assert float(np.abs(d["X"]).max()) <= 1.00001
    sources = meta["sources"]
    for i in range(len(d["y"])):
        source = sources[int(d["source_id"][i])]
        assert (source["label"], source["condition"], source["event"]) == (
            int(d["y"][i]),
            int(d["cond"][i]),
            int(d["event"][i]),
        )
    split_info = {}
    for s, name in enumerate(["train", "validation", "test"]):
        ix = ids[s]
        split_info[name] = dict(
            count=len(ix),
            events=np.unique(d["event"][ix]).tolist(),
            sources=len(np.unique(d["source_id"][ix])),
            classes=np.bincount(d["y"][ix], minlength=22).tolist(),
            conditions={
                str(c): int((d["cond"][ix] == c).sum())
                for c in np.unique(d["cond"][ix])
            },
        )
    assert split_info["test"]["events"] == [2]
    assert split_info["train"]["events"] == split_info["validation"]["events"] == [1]
    hashes = [{hashlib.sha256(x.tobytes()).digest() for x in d["X"][ix]} for ix in ids]
    pairs = {}
    for a, b in [(0, 1), (0, 2), (1, 2)]:
        shared = set(d["source_id"][ids[a]]) & set(d["source_id"][ids[b]])
        overlap = 0
        for sid in shared:
            sa = d["start"][ids[a][d["source_id"][ids[a]] == sid]]
            sb = d["start"][ids[b][d["source_id"][ids[b]] == sid]]
            overlap += int((np.abs(sa[:, None] - sb[None, :]) < 500).sum())
        pairs[str(a) + "-" + str(b)] = dict(
            shared_source_count=len(shared),
            overlapping_window_pairs=overlap,
            exact_duplicate_windows=len(hashes[a] & hashes[b]),
        )
    assert all(v == 0 for k in ["0-2", "1-2"] for v in pairs[k].values())
    return dict(
        splits=split_info,
        cross_split=pairs,
        same_conditions_in_all_splits=True,
        controller_sha256=str(d["controller_sha256"]),
        simulator_sha256=str(d["model_sha256"]),
        nominal_within_source_overlap=meta["overlap_fraction"],
        limitation=meta["limitation"],
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    protocol_path = Path(__file__).with_name("acceptance_protocol.json")
    protocol = json.loads(protocol_path.read_text())
    dataset = Path("/home/alan-william/xu2026/data/dataset_v4/dataset.npz")
    model_dir = ROOT / "artifacts/student-train-r1/kd-s1"
    assert sha(dataset) == protocol["dataset_sha256"]
    original = json.loads((model_dir / "result.json").read_text())
    for name, digest in original["artifacts_sha256"].items():
        assert sha(model_dir / name) == digest, name
    for part in ["trunk", "head"]:
        assert sha(model_dir / (part + ".onnx")) == protocol[part + "_sha256"]
    a.output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(protocol_path, a.output / "protocol.json")
    d = np.load(dataset, allow_pickle=False)
    metadata_path = dataset.with_name("splits.json")
    meta = json.loads(metadata_path.read_text())
    check = audit(d, meta)
    check.update(
        dataset_sha256=sha(dataset),
        splits_manifest_sha256=sha(metadata_path),
        protocol_sha256=sha(protocol_path),
        checkpoint_sha256=sha(model_dir / "best.pt"),
    )
    dump(a.output / "split-audit.json", check)
    assets = a.output / "assets"
    assets.mkdir()
    manifest = dict(
        schema=1,
        model={"id": "kd-s1", "statistics": False},
        datasets={},
        dataset_sha256=sha(dataset),
        source="Frozen event-E2 test plus existing validation replay; no ADC or model retuning",
    )
    for part in ["trunk", "head"]:
        dest = assets / (part + ".onnx")
        shutil.copyfile(model_dir / dest.name, dest)
        manifest["model"][part] = {"path": dest.name, "sha256": sha(dest)}
    vi = np.flatnonzero(d["split"] == 1)
    ti = np.flatnonzero(d["split"] == 2)
    demo = np.array([vi[np.flatnonzero(d["y"][vi] == c)[0]] for c in range(22)])
    for name, ix, title in [
        ("demo22", demo, "22 类演示"),
        ("validation", vi, "1100 窗验证集"),
        ("heldout", ti, "4400 窗 E2 留出测试"),
    ]:
        dest = assets / (name + ".f32")
        d["X"][ix].astype("<f4").tofile(dest)
        manifest["datasets"][name] = dict(
            path=dest.name,
            sha256=sha(dest),
            count=len(ix),
            labels=d["y"][ix].tolist(),
            dataset_indices=ix.tolist(),
            title=title,
        )
    dump(assets / "manifest.json", manifest)
    print("Audit and PS assets ready:", a.output, flush=True)
    torch.set_num_threads(2)
    torch.use_deterministic_algorithms(True)
    checkpoint = torch.load(
        model_dir / "best.pt", map_location="cpu", weights_only=True
    )
    assert (
        checkpoint["features"] == "none"
        and not checkpoint["rep"]
        and checkpoint["seed"] == 1
    )
    student = Student(rep=False).eval()
    student.load_state_dict(checkpoint["state"], strict=True)
    x = inputs(d["X"][ti], "none")
    fp32 = infer(student, x)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    session = ort.InferenceSession(
        str(model_dir / "full.onnx"), options, providers=["CPUExecutionProvider"]
    )
    quant = np.concatenate([session.run(None, {"input": w[None]})[0] for w in x])
    teacher_path = Path(
        "/home/alan-william/xu2026/data/models_v4/clf_ckpt/ECA-RLNet_none_all_n40_s0.pt"
    )
    assert (
        sha(teacher_path)
        == "62bda590c58de1a3e5006ca2e221fd6a9b9aae06170600992f20aed4f387417a"
    )
    teacher = ECARLNet().eval()
    teacher.load_state_dict(
        torch.load(teacher_path, map_location="cpu", weights_only=False)["state"],
        strict=True,
    )
    teacher_logits = infer(teacher, d["X"][ti])
    scale = json.loads((model_dir / "head.quant.json").read_text())[-1]["scale"]
    np.savez(
        a.output / "reference.npz",
        indices=ti,
        labels=d["y"][ti],
        source_id=d["source_id"][ti],
        condition=d["cond"][ti],
        start=d["start"][ti],
        fp32=fp32,
        quant=quant,
        teacher=teacher_logits,
        output_scale=scale,
    )
    result = dict(
        model="kd-s1",
        test_count=len(ti),
        fp32=metrics(d["y"][ti], fp32),
        qdq=metrics(d["y"][ti], quant),
        teacher=metrics(d["y"][ti], teacher_logits),
        protocol_sha256=sha(protocol_path),
        reference_sha256=sha(a.output / "reference.npz"),
        model_sha256=original["artifacts_sha256"],
        no_training_recalibration_or_selection=True,
        source_script_sha256=sha(__file__),
        runtime=dict(torch=torch.__version__, onnxruntime=ort.__version__),
    )
    dump(a.output / "cpu-test.json", result)
    print(
        json.dumps({k: result[k]["accuracy"] for k in ["fp32", "qdq", "teacher"]}),
        flush=True,
    )


if __name__ == "__main__":
    main()
