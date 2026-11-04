"""Freeze PS rules from train; validation is a gate. Never inspect split 2 here."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ps.sensor_rules import decide, features


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, data):
    Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--validation-npu", type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    path = Path("/home/alan-william/xu2026/data/dataset_v4/dataset.npz")
    assert (
        digest(path)
        == "a9849da35f01595e201325295ffadd395e49dfa59ba3c1149e77b217ad6a5b2b"
    )
    d = np.load(path, allow_pickle=False)
    ix = np.flatnonzero(d["split"] == 0)
    x, y = d["X"][ix], d["y"][ix]
    assert len(ix) == 880
    fs = [features(w.reshape(-1).tolist()) for w in x]
    means = np.array([f["mean"] for f in fs])
    std = np.array([f["std"] for f in fs])
    targets = np.stack([np.isin(y, [13 + p, 16 + p]) for p in range(3)], axis=1)
    ratio = np.stack(
        [std[:, p] / std[:, [q for q in range(3) if q != p]].min(1) for p in range(3)],
        axis=1,
    )
    df_means = np.concatenate([abs(means[y == 13 + p, p]) for p in range(3)])
    # Shared across all phases. Fixed margins, no search against E2.
    thresholds = dict(
        std_max=3 * float(std[targets].max()),
        ratio_max=3 * float(ratio[targets].max()),
        other_std_min=0.5 * float(std[~targets].min()),
        zero_mean_max=3 * float(df_means.max()),
        stuck_mean_min=9 * float(df_means.max()),
    )
    config = dict(
        schema=1,
        id="sensor-flat-v1",
        input="normalized_f32_3x500",
        thresholds=thresholds,
        dataset_sha256=digest(path),
        calibration_splits=[0],
        validation_split=1,
        threshold_recipe="3x maximum training target std and ratio; half minimum training non-target std; DF mean <=3x max training DF abs mean; SF mean >=9x; middle band abstains",
        decision_scope="One 500-sample window; no claimed multi-window persistence. Other phase activity is a waveform proxy, not a verified machine RUN signal.",
        zero_ambiguity="Near-zero flat output maps to DF by simulation convention; a sensor stuck exactly at zero is not physically distinguishable from dropout here.",
        source_sha256={
            str(Path(__file__).relative_to(ROOT)): digest(__file__),
            "ps/sensor_rules.py": digest(ROOT / "ps/sensor_rules.py"),
        },
        test_policy="E2 previously observed in earlier audit; no E2 threshold selection. Any later E2 result is a retrospective comparison, not new blind validation.",
    )
    results = {}
    for split, name in [(0, "train"), (1, "validation")]:
        selected = np.flatnonzero(d["split"] == split)
        labels = d["y"][selected]
        decisions = [
            decide(features(w.reshape(-1).tolist()), thresholds)
            for w in d["X"][selected]
        ]
        rule = np.array(
            [r["candidate_label"] if r["matched"] else -1 for r in decisions]
        )
        matched = rule >= 0
        target = np.isin(labels, range(13, 19))
        wrong = int((rule[matched] != labels[matched]).sum())
        coverage = float(matched[target].mean())
        assert (
            wrong == 0 and coverage >= 0.95
        ), "Training/validation gate failed; do not deploy"
        results[name] = dict(
            count=len(selected),
            rule_matches=int(matched.sum()),
            wrong_rule_matches=wrong,
            target_count=int(target.sum()),
            target_coverage=coverage,
            per_class=[
                dict(
                    label=c,
                    count=int((labels == c).sum()),
                    matched=int((matched & (labels == c)).sum()),
                )
                for c in range(22)
            ],
        )
        if split == 1:
            baseline = json.loads(a.validation_npu.read_text())
            assert baseline["backend"] == "Alnpu" and baseline["model_id"] == "kd-s1"
            assert (
                baseline["trunk_sha256"]
                == "75d726be16abbc4fb74238cec880beeb51bef0a984918a81daedb65ce2404a2c"
            )
            assert (
                baseline["head_sha256"]
                == "b5edd4aa73b92b3dc008387cc27d766f4752b7864d6eea7858fef679f538e267"
            )
            assert np.array_equal(selected, baseline["indices"]) and np.array_equal(
                labels, baseline["labels"]
            )
            npu = np.array(baseline["predictions"])
            final = np.where(matched, rule, npu)
            harmed = int(((npu == labels) & (final != labels)).sum())
            assert harmed == 0 and int((final == labels).sum()) > int(
                (npu == labels).sum()
            )
            results[name].update(
                npu_correct=int((npu == labels).sum()),
                hybrid_correct=int((final == labels).sum()),
                changed=int((final != npu).sum()),
                harmed=harmed,
                baseline_run=baseline["run_id"],
                baseline_sha256=digest(a.validation_npu),
            )
    save(a.output / "sensor_rules_v1.json", config)
    save(
        a.output / "calibration.json",
        dict(
            config_sha256=digest(a.output / "sensor_rules_v1.json"),
            thresholds=thresholds,
            train_validation_only=True,
            validation_gate_passed=True,
            results=results,
        ),
    )
    print(json.dumps(dict(thresholds=thresholds, results=results["validation"])))


if __name__ == "__main__":
    main()
