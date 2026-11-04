"""Acceptance scoring must expose a missing class and reject corrupt evidence."""

import csv
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from experiments.student.analyze_acceptance import LABELS, metrics, read_run


class AcceptanceEvidence(unittest.TestCase):
    def test_accuracy_does_not_hide_zero_recall(self):
        y = np.arange(22)
        pred = y.copy()
        pred[17] = 14
        result = metrics(y, pred)
        self.assertEqual(result["correct"], 21)
        self.assertEqual(result["per_class"][17]["recall"], 0)
        self.assertEqual(result["confusion"][17][14], 1)
        self.assertAlmostEqual(result["macro_f1"], (20 + 2 / 3) / 22)

    def test_corrupt_csv_or_sequence_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = dict(status="completed", error=None, processed=2, correct=2)
            (root / "manifest.json").write_text(json.dumps(manifest))
            rows = []
            for i in range(2):
                z = [0.0] * 22
                z[i] = 1.0
                rows.append(
                    dict(
                        sequence=i + 1,
                        dataset_index=10 + i,
                        expected=i,
                        label=i,
                        label_name=LABELS[i],
                        cycle=0,
                        elapsed_ms=10 + i * 25,
                        inference_us=500,
                        worker_us=2500,
                        ps_application_us=5000,
                        backend="Alnpu",
                        simulated=True,
                        model_id="kd-s1",
                        logits=z,
                        score=0.5,
                        correct=True,
                        mailbox=dict(flags=7, error=0),
                        axi=dict(mode="preview", physical=False),
                    )
                )

            def save():
                (root / "results.jsonl").write_text(
                    "".join(json.dumps(r) + "\n" for r in rows)
                )
                keys = [
                    "sequence",
                    "dataset_index",
                    "label",
                    "label_name",
                    "expected",
                    "correct",
                    "inference_us",
                    "worker_us",
                    "ps_application_us",
                    "score",
                    "axi_mode",
                    "ack_token",
                ]
                with (root / "results.csv").open("w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=keys)
                    w.writeheader()
                    for row in rows:
                        w.writerow(
                            {
                                k: (
                                    int(row[k])
                                    if k == "correct"
                                    else (
                                        row[k]
                                        if k in row
                                        else "preview" if k == "axi_mode" else ""
                                    )
                                )
                                for k in keys
                            }
                        )

            save()
            self.assertEqual(len(read_run(root)[2]), 2)
            for row in rows:
                row["npu_label"] = row["label"]
                row["label"] = row["expected"] = 1 - row["label"]
                row["label_name"] = LABELS[row["label"]]
            save()
            with self.assertRaises(AssertionError):
                read_run(root)
            self.assertEqual(len(read_run(root, npu_label_key="npu_label")[2]), 2)
            rows[0]["npu_label"] = 1
            save()
            with self.assertRaises(AssertionError):
                read_run(root, npu_label_key="npu_label")
            for i, row in enumerate(rows):
                row["label"] = row["expected"] = row["npu_label"] = i
                row["label_name"] = LABELS[i]
            save()
            path = root / "results.csv"
            path.write_text(path.read_text().replace("Normal", "Wrong name"))
            with self.assertRaises(AssertionError):
                read_run(root)
            save()
            rows[1]["sequence"] = 4
            save()
            with self.assertRaises(AssertionError):
                read_run(root)
            rows[1]["sequence"] = 2
            save()
            lines = (root / "results.csv").read_text().splitlines()
            (root / "results.csv").write_text("\n".join(lines[:-1]) + "\n")
            with self.assertRaises(StopIteration):
                read_run(root)


if __name__ == "__main__":
    unittest.main()
