"""Evaluate already-frozen rules with saved real NPU predictions; no fitting."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ps.sensor_rules import SensorRules
from experiments.student.analyze_acceptance import metrics


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--rules", type=Path, required=True)
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    rules = SensorRules(a.rules)
    d = np.load(
        "/home/alan-william/xu2026/data/dataset_v4/dataset.npz", allow_pickle=False
    )
    baseline = json.loads(a.baseline.read_text())
    report = dict(
        config_sha256=rules.sha256,
        thresholds=rules.thresholds,
        evaluation_scope="E2 is a previously inspected event holdout; retrospective comparison after train/validation freeze, not a new blind test",
        splits={},
    )
    all_indices = []
    all_candidates = []
    all_means = []
    all_std = []
    all_labels = []
    for name, split in [("validation", 1), ("heldout", 2)]:
        ix = np.flatnonzero(d["split"] == split)
        y = d["y"][ix]
        b = baseline[name]
        assert np.array_equal(ix, b["indices"]) and np.array_equal(y, b["labels"])
        decisions = [rules.diagnose(x.reshape(-1).tolist()) for x in d["X"][ix]]
        candidate = np.array(
            [r["candidate_label"] if r["matched"] else -1 for r in decisions]
        )
        pred = np.array(b["predictions"])
        final = np.where(candidate >= 0, candidate, pred)
        report["splits"][name] = dict(
            npu=metrics(y, pred),
            hybrid=metrics(y, final),
            matches=int((candidate >= 0).sum()),
            changed=int((final != pred).sum()),
            improved=int(((pred != y) & (final == y)).sum()),
            harmed=int(((pred == y) & (final != y)).sum()),
            false_rule_matches=int(((candidate >= 0) & (candidate != y)).sum()),
            rule_source_run=b["run_id"],
        )
        all_indices.extend(ix.tolist())
        all_candidates.extend(candidate.tolist())
        all_labels.extend(y.tolist())
        all_means.extend(r["features"]["mean"] for r in decisions)
        all_std.extend(r["features"]["std"] for r in decisions)
    np.savez(
        a.output / "rule_reference.npz",
        indices=all_indices,
        candidates=all_candidates,
        labels=all_labels,
        mean=all_means,
        std=all_std,
    )
    report["reference_sha256"] = hashlib.sha256(
        (a.output / "rule_reference.npz").read_bytes()
    ).hexdigest()
    (a.output / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: {
                    f: v[f]
                    for f in [
                        "matches",
                        "changed",
                        "improved",
                        "harmed",
                        "false_rule_matches",
                    ]
                }
                | {"npu": v["npu"]["correct"], "hybrid": v["hybrid"]["correct"]}
                for k, v in report["splits"].items()
            }
        )
    )


if __name__ == "__main__":
    main()
