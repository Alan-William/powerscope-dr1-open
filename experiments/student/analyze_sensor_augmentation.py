"""Audit the frozen local candidate and its explicit Alnpu execution evidence."""

import argparse
import csv
import json
from pathlib import Path
import re
import numpy as np
from analyze_acceptance import backend_evidence, dump, metrics, sha, timing


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--training", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--previous-rules", type=Path, required=True)
    parser.add_argument("--previous-acceptance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    native = args.evaluation / "native"
    selected = json.loads((args.training / "selected.json").read_text())
    protocol = json.loads((args.training / "protocol.json").read_text())
    runs = json.loads((args.training / "results.json").read_text())
    assert len(runs) == 9 and not protocol["smoke"]
    assert not selected["selection_uses_test"]
    assert selected["protocol_sha256"] == sha(args.training / "protocol.json")
    for name, digest in protocol["source_sha256"].items():
        assert sha(Path(__file__).parent / name) == digest, name
    folder = args.training / selected["directory"]
    for name, digest in selected["artifacts_sha256"].items():
        assert sha(folder / name) == digest, name
    transfer = json.loads((native / "transfer-manifest.json").read_text())
    for name, digest in transfer.items():
        assert sha(native / name) == digest, name
    prepared = json.loads((native / "prepared.json").read_text())
    status = json.loads((native / "probe-status.json").read_text())
    assert prepared == status["candidate"]
    assert status["status"] == "completed" and status["returncode"] == 0
    assert status["restoration"]["correct"] == status["restoration"]["processed"] == 22
    assert status["restoration"]["max_frames"] == 10000
    assert not status["ps_rules_in_candidate"] and not prepared["statistics"]
    assert prepared["selected"] == selected["directory"]
    assert prepared["selection_sha256"] == sha(
        args.evaluation / "selection-freeze.json"
    )
    assert prepared["protocol_sha256"] == selected["protocol_sha256"]
    for name in ["trunk.onnx", "head.onnx"]:
        assert prepared["files_sha256"][name] == selected["artifacts_sha256"][name]
    assert prepared["files_sha256"]["native-input.f32"] == sha(
        args.evaluation / "native-input.f32"
    )
    log = (native / "native.log").read_text()
    assert "requested_backend=Alnpu fallback=disabled" in log
    assert (
        "complete frames=5500 backend=Alnpu all_neural_ops_on_requested_backend" in log
    )
    evidence = backend_evidence(native)
    ref = np.load(args.evaluation / "reference.npz", allow_pickle=False)
    z = np.fromfile(native / "native.f32", dtype="<f4").reshape(5500, 22)
    assert np.isfinite(z).all()
    labels, indices = ref["labels"], ref["indices"]
    assert len(labels) == len(indices) == 5500
    with (native / "native.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 5500
    assert all(
        int(r["frame"]) == i and r["backend"] == "Alnpu" for i, r in enumerate(rows)
    )
    scale = json.loads((folder / "head.quant.json").read_text())[-1]["scale"]
    with (native / "native.bindings.tsv").open(newline="") as f:
        binding = {r["stage"]: r for r in csv.DictReader(f, delimiter="\t")}
    assert int(binding["output"]["elements"]) == 22
    assert np.isclose(float(binding["output"]["scale"]), scale, rtol=2e-6)
    before, after = [
        (native / name).read_text(encoding="utf-8")
        for name in ["kernel-before.txt", "kernel-after.txt"]
    ]
    assert before.startswith("[") and after.startswith(before)
    added = after[len(before) :].splitlines()
    errors = [
        line
        for line in added
        if "npu" in line.lower() and re.search("error|fail|timeout|fault", line, re.I)
    ]
    assert not errors

    def irq(name):
        lines = (native / name).read_text().splitlines()
        ncpu = len(lines[0].split())
        row = next(line for line in lines if line.endswith("HARD_NPU_IRQ"))
        return sum(map(int, row.split()[1 : 1 + ncpu]))

    delta = irq("interrupts-after.txt") - irq("interrupts-before.txt")
    assert delta == 5500 * 4
    old = json.loads(args.previous_acceptance.read_text())["heldout"]
    previous = {}
    with (
        args.previous_rules / "runs/438e4a4a000048616475ef98/results.jsonl"
    ).open() as f:
        for line in f:
            row = json.loads(line)
            previous[row["dataset_index"]] = row
    old_pred = np.array([previous[int(i)]["npu_label"] for i in indices[1100:]])
    assert np.array_equal(
        labels[1100:], [previous[int(i)]["expected"] for i in indices[1100:]]
    )
    assert metrics(labels[1100:], old_pred) == old["npu"]
    cpu = json.loads((args.evaluation / "cpu-evaluation.json").read_text())
    assert cpu["reference_sha256"] == sha(args.evaluation / "reference.npz")
    chosen = next(
        r for r in runs if f"{r['arm']}-s{r['seed']}" == selected["directory"]
    )
    report = dict(
        schema=1,
        host_date="2026-11-01",
        model_id="sfaug-r1",
        scope="Existing v4 simulation data; no new simulation or Ubuntu training. Original E2 is retrospective, not a new blind event test.",
        selected=selected,
        training_protocol=protocol,
        parameters=chosen["deploy_parameters"],
        training_seconds=json.loads((args.training / "complete.json").read_text())[
            "seconds"
        ],
        candidate_ps_rules=False,
        statistics=False,
        backend=evidence,
        native_status=status,
        npu_irq_delta=delta,
        new_kernel_lines=added,
        old_native_e2=old["npu"],
        segments={},
        development_results=[
            dict(
                arm=r["arm"],
                seed=r["seed"],
                best_epoch=r["best_epoch"],
                fp32_val=r["fp32"]["accuracy"],
                qdq_val=r["qdq"]["accuracy"],
                fp32_synthetic_sf=r["fp32_synthetic_sf"]["accuracy"],
                qdq_synthetic_sf=r["qdq_synthetic_sf"]["accuracy"],
            )
            for r in runs
        ],
        evidence_sha256=transfer,
        cpu_evaluation_sha256=sha(args.evaluation / "cpu-evaluation.json"),
    )
    for name, lo, hi in [("validation", 0, 1100), ("e2_retrospective", 1100, 5500)]:
        pred = z[lo:hi].argmax(1)
        qp = ref["qdq"][lo:hi].argmax(1)
        diff = np.abs(z[lo:hi] - ref["qdq"][lo:hi]) / scale
        values = dict(
            npu=metrics(labels[lo:hi], pred),
            fp32=metrics(labels[lo:hi], ref["fp32"][lo:hi].argmax(1)),
            cpu_qdq=metrics(labels[lo:hi], qp),
            npu_vs_qdq=dict(
                agreement_count=int((pred == qp).sum()),
                disagreements=int((pred != qp).sum()),
                max_lsb=float(diff.max()),
                mean_lsb=float(diff.mean()),
            ),
            timing_us={
                k: timing(np.array([int(r[k]) for r in rows[lo:hi]]))
                for k in [
                    "trunk_us",
                    "head_us",
                    "total_us",
                    "input_encode_copy_us",
                    "output_decode_us",
                    "frame_us",
                ]
            },
        )
        values["numerical_deployment_gate_passed"] = bool(
            diff.max() <= 12 and diff.mean() <= 3
        )
        assert values["numerical_deployment_gate_passed"]
        assert values["fp32"] == cpu["segments"][name]["candidate_fp32"]
        assert values["cpu_qdq"] == cpu["segments"][name]["candidate_qdq"]
        report["segments"][name] = values
    pred = z[1100:].argmax(1)
    truth = labels[1100:]
    report["e2_changes_vs_old_npu"] = dict(
        improvements=int(((pred == truth) & (old_pred != truth)).sum()),
        regressions=int(((pred != truth) & (old_pred == truth)).sum()),
        same_mistake_count=int(((pred != truth) & (old_pred != truth)).sum()),
    )
    report["limitations"] = [
        "E2 inspected previously; no claim of independent new-event generalization.",
        "Synthetic SF observations retain other phases and do not re-simulate controller response or fault onset.",
        "Train/validation windows share E1 sources; synthetic validation derivatives are not independent events.",
        "30-minute endurance belongs to kd-s1 plus PS rules, not this new candidate.",
        "Candidate native timing excludes input file I/O, PS service/UI, AXI and RS485.",
        "Existing kd-s1 plus PS rules restored; candidate is not yet the production PS model.",
    ]
    dump(args.output, report)
    print(
        json.dumps(
            dict(
                selected=selected["directory"],
                native={k: v["npu"]["accuracy"] for k, v in report["segments"].items()},
                changes=report["e2_changes_vs_old_npu"],
                hardware_verified=True,
            )
        )
    )


if __name__ == "__main__":
    main()
