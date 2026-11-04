"""Native PS contracts; fake worker and UIO, no physical-device access."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from tests import test_ps_contract as contracts
from tests.test_network_input import payload


class ExternalReplayTests(unittest.TestCase):
    def test_only_received_samples_reach_pl_then_worker_without_reference_label(self):
        with tempfile.TemporaryDirectory() as temp:
            app = contracts.ReplayTests().setup_app(Path(temp))
            seen, inferred = [], []
            original = app.worker.infer
            app.worker.infer = lambda seq, raw: (inferred.append(raw), original(seq, raw))[1]
            returned = b"\0" * 6000
            app.sample_transport = SimpleNamespace(mode="pl-q15", close=lambda: None,
                transfer=lambda seq, raw: (seen.append(raw), (returned, dict(mode="pl-q15", physical=True)))[1])
            app.args.axi_config = "verified-fixture"
            try:
                rid = app.start_external()
                data = payload(run_id=rid)
                data["display"] = dict(scale_a=8.0, sample_rate_hz=20000, sim_time_s=1.025)
                with self.assertRaises(RuntimeError):
                    app.submit_external(dict(data, run_id="stale-session"))
                with self.assertRaises(ValueError):
                    app.submit_external(dict(data, expected=0))
                ack = app.submit_external(data)
                app.stop_external(rid)
                app.thread.join(3)
                self.assertEqual(len(seen), 1)
                self.assertNotEqual(seen[0], returned)
                self.assertEqual(inferred, [returned])
                self.assertEqual(ack["sha256"], data["sha256"])
                self.assertEqual(ack["display"], data["display"])
                latest = app.public()["latest"]
                self.assertEqual(latest["network_input"]["display"], data["display"])
                self.assertEqual(latest["waveform"], [[0.0] * 500 for _ in range(3)])
                rows = [json.loads(x) for x in (app.store / rid / "results.jsonl").read_text().splitlines()]
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertIsNone(row["expected"])
                self.assertIsNone(row["correct"])
                self.assertEqual(row["diagnosis_source"], "npu")
                self.assertEqual(row["label"], 0)
                manifest = json.loads((app.store / rid / "manifest.json").read_text())
                self.assertIsNone(manifest["accuracy"])
                self.assertEqual(manifest["mode"], "ethernet-waveform")
                self.assertFalse(app.public()["mailbox"]["flags"] & 1)
                with self.assertRaises(RuntimeError):
                    app.submit_external(payload(run_id=rid, input_id=2))
            finally:
                app.close()

    def test_physical_configuration_required_before_start(self):
        with tempfile.TemporaryDirectory() as temp:
            app = contracts.ReplayTests().setup_app(Path(temp))
            try:
                with self.assertRaises(ValueError):
                    app.start_external()
                self.assertIsNone(app.thread)
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
