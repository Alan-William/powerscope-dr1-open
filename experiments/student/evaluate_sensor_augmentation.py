"""Evaluate only the frozen validation-selected candidate; E2 is retrospective."""

import argparse
import datetime
import hashlib
import io
import json
from pathlib import Path
import tarfile
import numpy as np
import onnxruntime as ort
import torch
from network import Student, inputs
from train_sensor_augmentation import DATA_SHA, sha, dump, infer, metrics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--training", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--old-package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert sha(args.dataset) == DATA_SHA
    selected = json.loads((args.training / "selected.json").read_text())
    complete = json.loads((args.training / "complete.json").read_text())
    assert (
        complete["runs"] == 9
        and not complete["test_evaluated"]
        and not selected["smoke"]
    )
    assert not selected["selection_uses_test"]
    assert sha(args.training / "protocol.json") == selected["protocol_sha256"]
    folder = args.training / selected["directory"]
    for name, digest in selected["artifacts_sha256"].items():
        assert sha(folder / name) == digest
    args.output.mkdir(parents=True, exist_ok=False)
    dump(
        args.output / "selection-freeze.json",
        dict(
            selected=selected,
            frozen_before_this_e2_evaluation_utc=datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(),
            limitation="E2 was already inspected in prior work. This is a retrospective comparison; no new independent simulation was generated.",
        ),
    )
    torch.set_num_threads(2)
    d = np.load(args.dataset, allow_pickle=False)
    vi, ti = [np.flatnonzero(d["split"] == s) for s in [1, 2]]
    indices = np.concatenate([vi, ti])
    raw = d["X"][indices].astype("f4")
    y = d["y"][indices].astype("i8")
    layout = inputs(raw)
    student = Student().eval()
    student.load_state_dict(
        torch.load(folder / "best.pt", map_location="cpu", weights_only=True)["state"]
    )
    fp = infer(student, torch.from_numpy(layout))
    so = ort.SessionOptions()
    so.intra_op_num_threads = 2
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    sess = ort.InferenceSession(
        str(folder / "full.onnx"), so, providers=["CPUExecutionProvider"]
    )
    quant = np.concatenate([sess.run(None, {"input": x[None]})[0] for x in layout])
    with tarfile.open(args.old_package) as archive:
        old_bytes = archive.extractfile("student-board-r1/best.pt").read()
        old_manifest = json.load(archive.extractfile("student-board-r1/manifest.json"))
    baseline = Student().eval()
    baseline.load_state_dict(
        torch.load(io.BytesIO(old_bytes), map_location="cpu", weights_only=True)[
            "state"
        ]
    )
    old = infer(baseline, torch.from_numpy(layout))
    layout.astype("<f4").tofile(args.output / "native-input.f32")
    np.savez(
        args.output / "reference.npz",
        indices=indices,
        labels=y,
        fp32=fp,
        qdq=quant,
        baseline_fp32=old,
    )
    report = dict(
        selected=selected["directory"],
        model_id="sfaug-r1",
        statistics=False,
        dataset_sha256=DATA_SHA,
        freeze_sha256=sha(args.output / "selection-freeze.json"),
        old_checkpoint_sha256=hashlib.sha256(old_bytes).hexdigest(),
        old_package_manifest=old_manifest,
        scope="Retrospective original E2; no new simulation, no PS rules in candidate inference",
        input_sha256=sha(args.output / "native-input.f32"),
        reference_sha256=sha(args.output / "reference.npz"),
        segments={},
    )
    for name, lo, hi in [("validation", 0, 1100), ("e2_retrospective", 1100, 5500)]:
        report["segments"][name] = dict(
            count=hi - lo,
            baseline_fp32=metrics(y[lo:hi], old[lo:hi]),
            candidate_fp32=metrics(y[lo:hi], fp[lo:hi]),
            candidate_qdq=metrics(y[lo:hi], quant[lo:hi]),
        )
    dump(args.output / "cpu-evaluation.json", report)
    print(
        json.dumps(
            {
                name: {
                    k: v[k]["accuracy"]
                    for k in ["baseline_fp32", "candidate_fp32", "candidate_qdq"]
                }
                for name, v in report["segments"].items()
            }
        )
    )


if __name__ == "__main__":
    main()
