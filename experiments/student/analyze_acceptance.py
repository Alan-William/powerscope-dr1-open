"""Analyze saved acceptance evidence. Never train or change acceptance limits."""

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re

import numpy as np

LABELS = (
    ["Normal"]
    + [f"T{p}{n} OS" for p in "abc" for n in range(1, 5)]
    + [f"Sensor {p} {f}" for f in ("DF", "SF", "OF") for p in "ABC"]
)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def dump(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def metrics(y, pred):
    cm = np.zeros((22, 22), np.int64)
    np.add.at(cm, (y, pred), 1)
    tp = cm.diagonal()
    r = tp / np.maximum(cm.sum(1), 1)
    p = tp / np.maximum(cm.sum(0), 1)
    f1 = 2 * p * r / np.maximum(p + r, 1e-12)
    return dict(
        count=len(y),
        correct=int(tp.sum()),
        accuracy=float(tp.sum() / len(y)),
        macro_f1=float(f1.mean()),
        per_class=[
            dict(
                label=i,
                name=LABELS[i],
                count=int(cm[i].sum()),
                correct=int(tp[i]),
                recall=float(r[i]),
                precision=float(p[i]),
            )
            for i in range(22)
        ],
        confusion=cm.tolist(),
    )


def timing(x):
    return dict(
        p50=float(np.median(x)),
        p95=float(np.quantile(x, 0.95)),
        p99=float(np.quantile(x, 0.99)),
        max=float(np.max(x)),
    )


def backend_evidence(root):
    """Check both optimized graphs and actually executed profiling events."""
    folders = list(root.glob("worker-*"))
    assert len(folders) == 1, "Choose the worker evidence for this acceptance run"
    folder = folders[0]
    result = {}
    for name, expected in [("trunk", 3), ("head", 1)]:
        dot = folder / (name + ".dot")
        profile = folder / (name + ".profile.json")
        graph = dot.read_text()
        raw = profile.read_text()
        assert set(re.findall(r"BackendID : (\w+)", graph)) == {"Alnpu"}
        assert graph.count("LayerType : ALHardNPU") == expected
        assert not any(x in raw for x in ["CpuAcc", "CpuRef", "Neon", "ClWorkload"])
        events = []

        def visit(node):
            if not isinstance(node, dict):
                return
            for key, value in node.items():
                if key.startswith("AlnpuALHardNPUWorkload_Execute_"):
                    values = next(
                        v["raw"]
                        for k, v in value.items()
                        if k.startswith("Wall clock time_")
                    )
                    assert len(values) == 16 and all(v > 0 for v in values)
                    events.append(dict(event=key, samples=len(values)))
                visit(value)

        visit(json.loads(raw))
        assert len(events) == expected
        result[name] = dict(
            hardware_layers=expected,
            events=events,
            dot_sha256=sha(dot),
            profile_sha256=sha(profile),
        )
    return dict(
        scope="Optimized graphs plus first 16 inference profiles from the acceptance worker; not per-frame hardware traces",
        folder=folder.name,
        subgraphs=result,
        all_neural_layers_alnpu=True,
    )


def read_run(folder, npu_label_key="label"):
    assert npu_label_key in ("label", "npu_label")
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["status"] == "completed" and manifest["error"] is None
    fields = [
        "sequence",
        "dataset_index",
        "expected",
        "label",
        "cycle",
        "elapsed_ms",
        "inference_us",
        "worker_us",
        "ps_application_us",
    ]
    columns = {k: [] for k in fields}
    logits = []
    with (folder / "results.jsonl").open() as source, (folder / "results.csv").open(
        newline=""
    ) as csvfile:
        csvrows = iter(csv.DictReader(csvfile))
        for text in source:
            row = json.loads(text)
            other = next(csvrows)
            assert (
                row["backend"] == "Alnpu"
                and row["simulated"]
                and row["model_id"] == "kd-s1"
            )
            assert row["mailbox"]["flags"] == 7 and row["mailbox"]["error"] == 0
            assert row["axi"]["mode"] == "preview" and not row["axi"]["physical"]
            assert len(row["logits"]) == 22 and np.isfinite(row["logits"]).all()
            assert int(np.argmax(row["logits"])) == row[npu_label_key]
            assert row["correct"] == (row["expected"] == row["label"])
            for key in [
                "sequence",
                "dataset_index",
                "label",
                "expected",
                "inference_us",
                "worker_us",
                "ps_application_us",
            ]:
                assert int(other[key]) == row[key], key
            assert float(other["score"]) == row["score"]
            assert int(other["correct"]) == int(row["correct"])
            assert other["label_name"] == row["label_name"] == LABELS[row["label"]]
            assert other["axi_mode"] == "preview" and not other["ack_token"]
            for key in fields:
                columns[key].append(row[key])
            logits.append(row["logits"])
        assert next(csvrows, None) is None
    z = np.array(logits, dtype=np.float32)
    c = {k: np.array(v) for k, v in columns.items()}
    assert len(z) == manifest["processed"]
    assert ((np.diff(c["sequence"]) & 0xFFFFFFFF) == 1).all()
    assert (np.diff(c["elapsed_ms"]) > 0).all()
    assert int((c["label"] == c["expected"]).sum()) == manifest["correct"]
    return manifest, c, z


def heldout(root, run_id):
    folder = root / "runs" / run_id
    m, c, z = read_run(folder)
    ref = np.load(root / "reference.npz", allow_pickle=False)
    cpu = json.loads((root / "cpu-test.json").read_text())
    assert np.array_equal(c["dataset_index"], ref["indices"]) and np.array_equal(
        c["expected"], ref["labels"]
    )
    assert len(z) == 4400
    pred = z.argmax(1)
    qp = ref["quant"].argmax(1)
    diff = np.flatnonzero(pred != qp)
    scale = float(ref["output_scale"])
    report = dict(
        scope="Frozen kd-s1 on v4 event-E2 holdout, 4400 overlapping simulation windows; no retuning",
        run_id=run_id,
        npu=metrics(ref["labels"], pred),
        fp32=metrics(ref["labels"], ref["fp32"].argmax(1)),
        cpu_qdq=metrics(ref["labels"], qp),
        teacher=metrics(ref["labels"], ref["teacher"].argmax(1)),
        npu_vs_qdq=dict(
            top1_agreement_count=int((pred == qp).sum()),
            top1_disagreements=len(diff),
            max_lsb=float(np.abs(z - ref["quant"]).max() / scale),
            mean_lsb=float(np.abs(z - ref["quant"]).mean() / scale),
            disagreements=[
                dict(
                    index=int(c["dataset_index"][i]),
                    expected=int(ref["labels"][i]),
                    npu=int(pred[i]),
                    qdq=int(qp[i]),
                    qdq_top2_gap_lsb=float(
                        np.diff(np.sort(ref["quant"][i])[-2:])[0] / scale
                    ),
                )
                for i in diff
            ],
        ),
        by_condition=[
            dict(
                condition=int(k),
                count=int((ref["condition"] == k).sum()),
                correct=int(
                    (
                        pred[ref["condition"] == k]
                        == ref["labels"][ref["condition"] == k]
                    ).sum()
                ),
            )
            for k in np.unique(ref["condition"])
        ],
        by_source=[
            dict(
                source_id=int(k),
                condition=int(ref["condition"][ref["source_id"] == k][0]),
                label=int(ref["labels"][ref["source_id"] == k][0]),
                count=int((ref["source_id"] == k).sum()),
                correct=int(
                    (
                        pred[ref["source_id"] == k]
                        == ref["labels"][ref["source_id"] == k]
                    ).sum()
                ),
            )
            for k in np.unique(ref["source_id"])
        ],
        timing_us={
            k: timing(c[k]) for k in ["inference_us", "worker_us", "ps_application_us"]
        },
        manifest=m,
        split_audit=json.loads((root / "split-audit.json").read_text()),
        evidence_sha256={
            name: sha(folder / name)
            for name in ["manifest.json", "results.csv", "results.jsonl"]
        },
        reference_sha256=sha(root / "reference.npz"),
        cpu_runtime=cpu["runtime"],
    )
    assert (
        report["fp32"]["accuracy"] == cpu["fp32"]["accuracy"]
        and report["cpu_qdq"]["accuracy"] == cpu["qdq"]["accuracy"]
    )
    report["numerical_deployment_gate_passed"] = (
        report["npu_vs_qdq"]["max_lsb"] <= 12 and report["npu_vs_qdq"]["mean_lsb"] <= 3
    )
    return report


def endurance(root, status, reader=read_run):
    run = next(x for x in status["runs"] if x["stage"] == "endurance")
    rid = run["run_id"]
    folder = root / "runs" / rid
    m, c, z = reader(folder)
    assert len(z) == 72600 and m["repeat"] == 66
    blocks = z.reshape(66, 1100, 22)
    exact = np.all(blocks.view(np.uint32) == blocks[0].view(np.uint32), axis=(1, 2))
    assert np.array_equal(
        c["dataset_index"].reshape(66, 1100),
        np.tile(c["dataset_index"][:1100], (66, 1)),
    )
    assert np.array_equal(c["cycle"], np.repeat(np.arange(66), 1100))
    samples = [
        json.loads(s)
        for s in (root / "monitor/endurance-resources.jsonl").read_text().splitlines()
    ]
    assert len(samples) >= 2 and all(len(s["workers"]) == 1 for s in samples)
    assert status["status"] == "completed"
    assert status["server_pid"] == samples[0]["service"]["pid"]
    assert status["session"] == m["session"]
    assert run["processed"] == m["processed"] and run["correct"] == m["correct"]
    workers = [s["workers"][0] for s in samples if s["workers"]]
    assert len({(w["pid"], w["start_ticks"]) for w in workers}) == 1
    assert (
        len({(s["service"]["pid"], s["service"]["start_ticks"]) for s in samples}) == 1
    )
    assert len({s["boot_id"] for s in samples}) == 1
    before = json.loads((root / "monitor/resources-before.json").read_text())
    after = json.loads((root / "monitor/resources-after.json").read_text())
    assert before["boot_id"] == after["boot_id"] == samples[0]["boot_id"]
    assert (
        before["service"]["start_ticks"]
        == after["service"]["start_ticks"]
        == samples[0]["service"]["start_ticks"]
    )
    assert status["worker_identity"] == [workers[0]["pid"], workers[0]["start_ticks"]]
    assert len(after["workers"]) == 1
    assert [after["workers"][0]["pid"], after["workers"][0]["start_ticks"]] == status[
        "worker_identity"
    ]
    span = float((c["elapsed_ms"][-1] - c["elapsed_ms"][0]) / 1000)
    report = dict(
        run_id=rid,
        frames=len(z),
        cycles=66,
        unique_windows=1100,
        first_to_last_result_seconds=span,
        service_elapsed_seconds=m["elapsed_seconds"],
        result_rate_per_second=float((len(z) - 1) / span),
        repeated_logits_bitwise_equal=bool(exact.all()),
        matching_cycles=int(exact.sum()),
        correct=m["correct"],
        accuracy=m["accuracy"],
        accuracy_scope="Repeated validation windows, NOT additional independent test observations",
        http_errors=status["http_errors"],
        late_frames_reported=run["late_frames"],
        inter_result_gap_ms=timing(np.diff(c["elapsed_ms"])),
        gaps_over_50ms=int((np.diff(c["elapsed_ms"]) > 50).sum()),
        timing_us={
            k: dict(all=timing(c[k]), after_100=timing(c[k][100:]))
            for k in ["inference_us", "worker_us", "ps_application_us"]
        },
        resource_samples=len(samples),
        worker_pid=workers[0]["pid"],
        service_pid=samples[0]["service"]["pid"],
        boot_id=samples[0]["boot_id"],
        worker_rss_kib=dict(
            first=workers[0]["rss_kib"],
            peak=max(w["rss_kib"] for w in workers),
            last=workers[-1]["rss_kib"],
        ),
        service_rss_kib=dict(
            first=samples[0]["service"]["rss_kib"],
            peak=max(s["service"]["rss_kib"] for s in samples),
            last=samples[-1]["service"]["rss_kib"],
        ),
        worker_fd_counts=sorted({w["fd_count"] for w in workers}),
        service_fd_counts=sorted({s["service"]["fd_count"] for s in samples}),
        minimum_mem_available_kib=min(s["memory"]["MemAvailable_kib"] for s in samples),
        storage_bytes_used=samples[0]["free_bytes"] - samples[-1]["free_bytes"],
        minimum_free_bytes=min(s["free_bytes"] for s in samples),
        temperature_available=any(s["temperatures_c"] for s in samples),
        cpu_percent_one_core={},
        manifest=m,
        evidence_sha256={
            name: sha(folder / name)
            for name in ["manifest.json", "results.csv", "results.jsonl"]
        },
        monitor_sha256={
            name: sha(root / "monitor" / name)
            for name in ["status.json", "endurance-resources.jsonl", "protocol.json"]
        },
    )
    elapsed = np.array([s["monotonic"] - samples[0]["monotonic"] for s in samples])
    for who in ["service", "worker"]:
        processes = [
            s["service"] if who == "service" else s["workers"][0] for s in samples
        ]
        rss = np.array([p["rss_kib"] for p in processes])
        warm = np.flatnonzero(elapsed >= 60)[0]
        late = elapsed >= elapsed[-1] / 2
        report[who + "_rss_kib"].update(
            warm_60s=int(rss[warm]),
            change_since_warm=int(rss[-1] - rss[warm]),
            second_half_change=int(rss[-1] - rss[late][0]),
            second_half_slope_kib_per_minute=float(
                np.polyfit(elapsed[late] / 60, rss[late], 1)[0]
            ),
        )
        vals = []
        for a, b in zip(samples, samples[1:]):
            aa = a["service"] if who == "service" else a["workers"][0]
            bb = b["service"] if who == "service" else b["workers"][0]
            vals.append(
                100
                * (bb["cpu_ticks"] - aa["cpu_ticks"])
                / a["clock_ticks_per_second"]
                / (b["monotonic"] - a["monotonic"])
            )
        report["cpu_percent_one_core"][who] = timing(np.array(vals))
    report["acceptance_passed"] = bool(
        status["status"] == "completed"
        and span >= 1800
        and exact.all()
        and status["http_errors"] == 0
        and report["minimum_free_bytes"] >= 200 * 1024**2
    )
    return report


def system_evidence(root, status):
    """Separate observed kernel/IRQ evidence from application acceptance."""
    monitor = root / "monitor"
    before = (monitor / "kernel-before.txt").read_text(encoding="utf-8")
    after = (monitor / "kernel-after.txt").read_text(encoding="utf-8")
    assert before.startswith("[") and after.startswith(
        before
    ), "Kernel log unavailable or rolled over; review manually"
    added = after[len(before) :].splitlines()

    def irq_count(path):
        lines = path.read_text().splitlines()
        cpus = len(lines[0].split())
        line = next(s for s in lines if s.endswith("HARD_NPU_IRQ"))
        return sum(map(int, line.split()[1 : cpus + 1]))

    first = irq_count(monitor / "interrupts-before.txt")
    last = irq_count(monitor / "interrupts-after.txt")
    return dict(
        scope="Monitoring includes the 4400 heldout frames followed by 72600 endurance frames",
        new_kernel_lines=added,
        npu_kernel_error_lines=[
            s
            for s in added
            if "npu" in s.lower() and re.search(r"error|fail|timeout|fault", s, re.I)
        ],
        usb_disconnect_events=sum("USB disconnect" in s for s in added),
        hard_npu_irq_before=first,
        hard_npu_irq_after=last,
        hard_npu_irq_delta=last - first,
        total_inference_frames=sum(r["processed"] for r in status["runs"]),
        observed_hard_irqs_per_frame=(last - first)
        / sum(r["processed"] for r in status["runs"]),
        files_sha256={
            name: sha(monitor / name)
            for name in [
                "resources-before.json",
                "resources-after.json",
                "kernel-before.txt",
                "kernel-after.txt",
                "interrupts-before.txt",
                "interrupts-after.txt",
            ]
        },
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--evidence", type=Path, required=True)
    p.add_argument("--heldout-run", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--include-endurance", action="store_true")
    a = p.parse_args()
    report = dict(
        schema=1, host_date="2026-10-31", heldout=heldout(a.evidence, a.heldout_run)
    )
    protocol_path = Path(__file__).with_name("acceptance_protocol.json")
    protocol = json.loads(protocol_path.read_text())
    report["protocol_sha256"] = sha(protocol_path)
    report["backend_evidence"] = backend_evidence(a.evidence)
    for key in ["trunk_sha256", "head_sha256"]:
        assert report["heldout"]["manifest"][key] == protocol[key]
    if a.include_endurance:
        status = json.loads((a.evidence / "monitor/status.json").read_text())
        assert status["protocol_sha256"] == report["protocol_sha256"]
        report["endurance"] = endurance(a.evidence, status)
        report["system_evidence"] = system_evidence(a.evidence, status)
        for key in ["trunk_sha256", "head_sha256", "worker_sha256", "session"]:
            assert (
                report["endurance"]["manifest"][key]
                == report["heldout"]["manifest"][key]
            )
    dump(a.output, report)
    print(
        json.dumps(
            dict(
                heldout_correct=report["heldout"]["npu"]["correct"],
                heldout_accuracy=report["heldout"]["npu"]["accuracy"],
                npu_vs_qdq=report["heldout"]["npu_vs_qdq"],
                endurance=report.get("endurance", {}).get("acceptance_passed"),
            )
        )
    )


if __name__ == "__main__":
    main()
