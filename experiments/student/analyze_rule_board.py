"""Check native hybrid rows against frozen rules and previous raw Alnpu logits."""

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import sys
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from experiments.student.analyze_acceptance import metrics, timing, LABELS
from protocol.mailbox import packet


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--evidence", type=Path, required=True)
    p.add_argument("--previous", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--wire-output", type=Path)
    a = p.parse_args()
    status = json.loads((a.evidence / "monitor/status.json").read_text())
    assert status["status"] == "completed" and status["http_errors"] == 0
    config = ROOT / "configs/sensor_rules_v1.json"
    assert status["rules"]["sha256"] == sha(config)
    ref = np.load(a.evidence / "evaluation/rule_reference.npz", allow_pickle=False)
    lookup = {int(v): i for i, v in enumerate(ref["indices"])}
    old = {}
    for rid in ["2f053b53c2386e8cbff12743", "80fabdaf124fb6368438fe10"]:
        with (a.previous / "runs" / rid / "results.jsonl").open() as source:
            for text in source:
                row = json.loads(text)
                if row["cycle"] > 0:
                    break
                old[row["dataset_index"]] = np.array(row["logits"], dtype=np.float32)
    out = dict(
        config_sha256=sha(config),
        frozen_rules=json.loads(config.read_text()),
        runs={},
        calibration=json.loads((a.evidence / "calibration.json").read_text()),
        freeze=json.loads((a.evidence / "freeze.json").read_text()),
        scope="Same frozen NPU and inputs. E2 is retrospective, not a new blind test. PS rules do not change NPU statistical channels or PL.",
    )
    identities = set()
    services = set()
    boots = set()
    wire_cases = {}
    for run in status["runs"]:
        stage = run["stage"]
        folder = a.evidence / "runs" / run["run_id"]
        m = json.loads((folder / "manifest.json").read_text())
        assert (
            m == run["manifest"] and m["status"] == "completed" and m["error"] is None
        )
        assert m["sensor_rules"]["sha256"] == sha(config) and m["statistics"] is False
        assert (
            m["trunk_sha256"]
            == "75d726be16abbc4fb74238cec880beeb51bef0a984918a81daedb65ce2404a2c"
        )
        assert (
            m["head_sha256"]
            == "b5edd4aa73b92b3dc008387cc27d766f4752b7864d6eea7858fef679f538e267"
        )
        ys = []
        npu = []
        final = []
        timestamps = []
        sequences = []
        cycles = []
        indices = []
        costs = {
            k: [] for k in ["inference_us", "worker_us", "rule_us", "ps_application_us"]
        }
        matches = changes = 0
        with (folder / "results.jsonl").open() as js, (folder / "results.csv").open(
            newline=""
        ) as cf:
            cr = iter(csv.DictReader(cf))
            for line in js:
                row = json.loads(line)
                c = next(cr)
                i = lookup[row["dataset_index"]]
                assert (
                    row["backend"] == "Alnpu"
                    and row["run_id"] == run["run_id"]
                    and row["simulated"]
                )
                z = np.array(row["logits"], dtype=np.float32)
                assert np.array_equal(
                    z.view(np.uint32), old[row["dataset_index"]].view(np.uint32)
                )
                assert row["npu_label"] == int(z.argmax())
                candidate = int(ref["candidates"][i])
                matched = candidate >= 0
                assert row["rule"]["matched"] == matched and row["rule"][
                    "candidate_label"
                ] == (candidate if matched else None)
                assert np.allclose(
                    row["rule"]["features"]["mean"], ref["mean"][i], rtol=0, atol=1e-12
                )
                assert np.allclose(
                    row["rule"]["features"]["std"], ref["std"][i], rtol=0, atol=1e-12
                )
                label = candidate if matched else row["npu_label"]
                assert row["label"] == label and row["expected"] == int(
                    ref["labels"][i]
                )
                assert row["correct"] == (label == row["expected"]) and row[
                    "npu_correct"
                ] == (row["npu_label"] == row["expected"])
                changed = label != row["npu_label"]
                assert row["rule"]["changed"] == changed and row[
                    "diagnosis_source"
                ] == ("ps_rule" if matched else "npu")
                exp = [math.exp(v - max(row["logits"])) for v in row["logits"]]
                assert abs(row["score"] - exp[label] / sum(exp)) < 1e-12
                assert abs(row["npu_score"] - exp[row["npu_label"]] / sum(exp)) < 1e-12
                expected_packet = packet(
                    row["sequence"], label, row["score"], row["inference_us"], npu=True
                )
                assert row["mailbox"] == json.loads(json.dumps(expected_packet))
                if (
                    stage == "heldout"
                    and row["expected"] in [0, 13, 14, 15, 16, 17, 18]
                    and row["expected"] not in wire_cases
                ):
                    wire_cases[row["expected"]] = dict(
                        name="first_heldout_class_" + str(row["expected"]),
                        dataset_index=row["dataset_index"],
                        expected=row["expected"],
                        npu_label=row["npu_label"],
                        npu_score=row["npu_score"],
                        final_label=row["label"],
                        score=row["score"],
                        diagnosis_source=row["diagnosis_source"],
                        rule=row["rule"],
                        packet=row["mailbox"],
                    )
                assert row["axi"]["mode"] == "preview" and not row["axi"]["physical"]
                for k in [
                    "sequence",
                    "dataset_index",
                    "label",
                    "expected",
                    "inference_us",
                    "worker_us",
                    "ps_application_us",
                    "npu_label",
                    "rule_us",
                ]:
                    assert int(c[k]) == row[k]
                for k in ["correct", "npu_correct"]:
                    assert int(c[k]) == int(row[k])
                for k in ["score", "npu_score"]:
                    assert float(c[k]) == row[k]
                for k in ["label_name", "npu_label_name", "diagnosis_source"]:
                    assert c[k] == row[k]
                assert (
                    row["label_name"] == LABELS[label]
                    and row["npu_label_name"] == LABELS[row["npu_label"]]
                )
                assert int(c["rule_matched"]) == int(matched) and int(
                    c["rule_changed"]
                ) == int(changed)
                assert (
                    c["rule_reason"] == row["rule"]["reason"]
                    and c["axi_mode"] == "preview"
                    and not c["ack_token"]
                )
                ys.append(row["expected"])
                npu.append(row["npu_label"])
                final.append(label)
                timestamps.append(row["elapsed_ms"])
                sequences.append(row["sequence"])
                cycles.append(row["cycle"])
                indices.append(row["dataset_index"])
                matches += int(matched)
                changes += int(changed)
                for k in costs:
                    costs[k].append(row[k])
            assert next(cr, None) is None
        expected_count = {
            "validation": 1100,
            "heldout": 4400,
            "repeat_validation": 3300,
        }[stage]
        assert (
            len(ys) == m["processed"] == expected_count
            and (np.diff(timestamps) > 0).all()
        )
        assert ((np.diff(sequences) & 0xFFFFFFFF) == 1).all()
        expected_indices = (
            ref["indices"][:1100] if stage != "heldout" else ref["indices"][1100:]
        )
        assert np.array_equal(indices, np.tile(expected_indices, m["repeat"]))
        assert np.array_equal(
            cycles, np.repeat(np.arange(m["repeat"]), len(expected_indices))
        )
        ys = np.array(ys)
        npu = np.array(npu)
        final = np.array(final)
        improved = int(((npu != ys) & (final == ys)).sum())
        harmed = int(((npu == ys) & (final != ys)).sum())
        assert (
            int((final == ys).sum()),
            int((npu == ys).sum()),
            matches,
            changes,
            improved,
            harmed,
        ) == (
            m["correct"],
            m["npu_correct"],
            m["rule_matches"],
            m["rule_changes"],
            m["rule_improvements"],
            m["rule_regressions"],
        )
        resource = [
            json.loads(s)
            for s in (a.evidence / "monitor" / (stage + "-resources.jsonl"))
            .read_text()
            .splitlines()
        ]
        for s in resource:
            services.add((s["service"]["pid"], s["service"]["start_ticks"]))
            boots.add(s["boot_id"])
            for w in s["workers"]:
                identities.add((w["pid"], w["start_ticks"]))
        out["runs"][stage] = dict(
            run_id=run["run_id"],
            manifest=m,
            npu=metrics(ys, npu),
            hybrid=metrics(ys, final),
            rule_matches=matches,
            changed=changes,
            improved=improved,
            harmed=harmed,
            first_to_last_seconds=(timestamps[-1] - timestamps[0]) / 1000,
            timing_us={k: timing(np.array(v)) for k, v in costs.items()},
            inter_result_gap_ms=timing(np.diff(timestamps)),
            late_frames=run["late_frames"],
            old_npu_logits_bitwise_equal=True,
            offline_rule_decisions_equal=True,
            evidence_sha256={
                name: sha(folder / name)
                for name in ["manifest.json", "results.csv", "results.jsonl"]
            },
        )
    assert len(services) == len(identities) == len(boots) == 1
    assert out["runs"]["repeat_validation"]["first_to_last_seconds"] >= 80
    out["board_regression_passed"] = all(
        r["harmed"] == 0 and r["hybrid"]["correct"] >= r["npu"]["correct"]
        for r in out["runs"].values()
    )
    out["same_worker_all_runs"] = True
    out["same_service_all_runs"] = True
    out["http_errors"] = status["http_errors"]
    out["deployment_hashes"] = json.loads(
        (a.evidence / "deployment-hashes.json").read_text()
    )
    for name in ["transfer-verification", "frontend-after-validation", "ui-smoke"]:
        path = a.evidence / (name + ".json")
        if path.exists():
            out[name.replace("-", "_")] = json.loads(path.read_text())
    if a.wire_output:
        assert set(wire_cases) == {0, 13, 14, 15, 16, 17, 18}
        fixture = dict(
            schema=1,
            result_mailbox_abi=1,
            config_sha256=sha(config),
            source_run_id=out["runs"]["heldout"]["run_id"],
            source_results_sha256=out["runs"]["heldout"]["evidence_sha256"][
                "results.jsonl"
            ],
            worker_sha256=out["runs"]["heldout"]["manifest"]["worker_sha256"],
            selection="First occurrence of each listed reference class, without filtering predictions",
            evidence="Measured Alnpu plus PS rules; expected wire bytes only. Physical AXI/RS485 NOT tested.",
            score_semantics="Original NPU softmax at final label; not hybrid confidence. May be low or round to zero even with flags=7.",
            result_cases=[wire_cases[c] for c in sorted(wire_cases)],
        )
        a.wire_output.write_text(
            json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    a.output.write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                k: dict(
                    npu=v["npu"]["correct"],
                    hybrid=v["hybrid"]["correct"],
                    changed=v["changed"],
                    harmed=v["harmed"],
                    seconds=v["first_to_last_seconds"],
                    rule_us=v["timing_us"]["rule_us"],
                )
                for k, v in out["runs"].items()
            }
        )
    )


if __name__ == "__main__":
    main()
