"""Independent audit of rules-enabled continuous execution, preserving NPU scores."""

import argparse
import csv
import json
import math
from pathlib import Path
import sys
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from experiments.student.analyze_acceptance import (
    read_run,
    endurance,
    system_evidence,
    sha,
    timing,
    backend_evidence,
)
from protocol.mailbox import packet


def verify_rows(folder, reference):
    manifest = json.loads((folder / "manifest.json").read_text())
    npu_correct = matches = changes = improvements = regressions = count = 0
    times = []
    with (folder / "results.jsonl").open() as js, (folder / "results.csv").open(
        newline=""
    ) as cf:
        rows = iter(csv.DictReader(cf))
        for line in js:
            r = json.loads(line)
            c = next(rows)
            old = reference[r["dataset_index"]]
            assert np.array_equal(
                np.array(r["logits"], dtype="f4").view("u4"),
                np.array(old["logits"], dtype="f4").view("u4"),
            )
            for key in [
                "label",
                "npu_label",
                "expected",
                "correct",
                "npu_correct",
                "score",
                "npu_score",
                "rule",
                "diagnosis_source",
            ]:
                assert r[key] == old[key], key
            npu = int(np.argmax(r["logits"]))
            assert npu == r["npu_label"]
            expected = r["rule"]["candidate_label"] if r["rule"]["matched"] else npu
            assert expected == r["label"]
            p = [math.exp(v - max(r["logits"])) for v in r["logits"]]
            assert abs(r["score"] - p[expected] / sum(p)) < 1e-12
            assert abs(r["npu_score"] - p[npu] / sum(p)) < 1e-12
            assert r["mailbox"] == json.loads(
                json.dumps(
                    packet(
                        r["sequence"], expected, r["score"], r["inference_us"], npu=True
                    )
                )
            )
            for key in ["npu_label", "rule_us"]:
                assert int(c[key]) == r[key]
            for key in ["npu_score", "score"]:
                assert float(c[key]) == r[key]
            for key in ["npu_label_name", "diagnosis_source"]:
                assert c[key] == r[key]
            assert int(c["npu_correct"]) == int(r["npu_correct"])
            assert int(c["rule_matched"]) == int(r["rule"]["matched"])
            assert int(c["rule_changed"]) == int(r["rule"]["changed"])
            assert c["rule_reason"] == r["rule"]["reason"]
            npu_correct += int(r["npu_correct"])
            matches += int(r["rule"]["matched"])
            changes += int(r["rule"]["changed"])
            improvements += int(r["correct"] and not r["npu_correct"])
            regressions += int(r["npu_correct"] and not r["correct"])
            times.append(r["rule_us"])
            count += 1
        assert next(rows, None) is None
    for key, value in [
        ("npu_correct", npu_correct),
        ("rule_matches", matches),
        ("rule_changes", changes),
        ("rule_improvements", improvements),
        ("rule_regressions", regressions),
    ]:
        assert manifest[key] == value
    assert count == manifest["processed"]
    return dict(
        npu_correct=npu_correct,
        rule_matches=matches,
        rule_changes=changes,
        rule_improvements=improvements,
        rule_regressions=regressions,
        raw_logits_equal_previous=True,
        decisions_equal_frozen_reference=True,
        csv_json_packet_agree=True,
        rule_us=timing(np.array(times)),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--previous-rules", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    status = json.loads((args.evidence / "monitor/status.json").read_text())
    protocol = json.loads((args.evidence / "monitor/protocol.json").read_text())
    digest = sha(ROOT / "configs/sensor_rules_v1.json")
    assert status["status"] == "completed" and status["http_errors"] == 0
    assert (
        status["sensor_rules"]["enabled"] and status["sensor_rules"]["sha256"] == digest
    )
    assert (
        protocol["sensor_rules_sha256"] == digest
        and sha(args.evidence / "monitor/protocol.json") == status["protocol_sha256"]
    )
    reference = {}
    for rid in ["f49d408b4bba3a5f31b9d65a", "438e4a4a000048616475ef98"]:
        with (args.previous_rules / "runs" / rid / "results.jsonl").open() as source:
            for line in source:
                row = json.loads(line)
                reference[row["dataset_index"]] = row
    report = dict(
        scope="Frozen kd-s1 plus frozen sensor-flat-v1; actual PS/NPU software endurance. Physical AXI/RS485 excluded.",
        rule_sha256=digest,
        runs={},
    )
    for run in status["runs"]:
        folder = args.evidence / "runs" / run["run_id"]
        manifest = json.loads((folder / "manifest.json").read_text())
        assert (
            manifest["sensor_rules"]["sha256"] == digest
            and manifest["statistics"] is False
        )
        assert manifest["session"] == status["session"] and manifest == run["manifest"]
        report["runs"][run["stage"]] = verify_rows(folder, reference)
    report["endurance"] = endurance(
        args.evidence,
        status,
        reader=lambda folder: read_run(folder, npu_label_key="npu_label"),
    )
    heldout = next(r for r in status["runs"] if r["stage"] == "heldout")
    m, _, _ = read_run(
        args.evidence / "runs" / heldout["run_id"], npu_label_key="npu_label"
    )
    assert m["processed"] == 4400
    report["heldout_manifest"] = m
    report["system"] = system_evidence(args.evidence, status)
    report["backend"] = backend_evidence(args.evidence)
    assert report["system"]["hard_npu_irq_delta"] == 4 * 77000
    assert not report["system"]["npu_kernel_error_lines"]
    assert report["endurance"]["acceptance_passed"]
    report["passed"] = True
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    e = report["endurance"]
    print(
        json.dumps(
            dict(
                passed=True,
                seconds=e["first_to_last_result_seconds"],
                frames=e["frames"],
                correct=e["correct"],
                npu_correct=report["runs"]["endurance"]["npu_correct"],
                late=e["late_frames_reported"],
                max_gap_ms=e["inter_result_gap_ms"]["max"],
                service_rss=e["service_rss_kib"],
            )
        )
    )


if __name__ == "__main__":
    main()
