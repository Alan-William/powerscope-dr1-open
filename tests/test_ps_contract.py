"""Contract/fault tests use fakes only; physical NPU acceptance is separate."""

import argparse, hashlib, json, struct, tempfile, unittest
import sys

if sys.platform == "win32":
    raise unittest.SkipTest("PS contract tests require Linux fcntl; run this group on Linux")
from pathlib import Path
from types import SimpleNamespace
import threading
import time
from unittest.mock import patch
from protocol.mailbox import packet, MailboxClient
from protocol.modbus import decode_response, reply
from ps.axi import UioRegisters
from ps.app import Replay, Worker, load_assets
from ps.sensor_rules import SensorRules
from tests.test_sensor_rules import CONFIG, wave
from protocol.replay_frame import encode as wave_encode, decode as wave_decode
from ps.sample_transport import SampleTransportError
from ps.result_store import ResultWriter


class FakeIO:
    def __init__(self, ack=True):
        self.reg = {0: 1, 4: 1, 0x1C: 0, 0x20: 0, 0x30: 2}
        self.events = []
        self.ack = ack

    def read(self, o):
        return self.reg.get(o, 0)

    def write(self, o, v):
        self.events.append((o, v))
        self.reg[o] = v
        if o == 0x1C and self.ack:
            self.reg[0x20] = v

    def barrier(self):
        self.events.append("barrier")


class Contracts(unittest.TestCase):
    def test_modbus_and_pending_atomic_layout(self):
        p = packet(0x12345678, 21, 0.9876, 900, npu=True)
        self.assertEqual(p["flags"], 7)
        self.assertEqual(p["pending_writes"][1], (12, (21 << 16) | 7))
        self.assertEqual(
            list(decode_response(bytes.fromhex(p["rtu_expected_response_hex"]))),
            p["registers"],
        )
        io = FakeIO()
        result = MailboxClient(io).commit(p)
        self.assertEqual(result["ack_token"], 1)
        self.assertEqual(io.events[:5], p["pending_writes"])
        self.assertEqual(io.events[5:8], ["barrier", (28, 1), "barrier"])

    def test_timeout_does_not_overwrite_unacknowledged_pending(self):
        io = FakeIO(False)
        client = MailboxClient(io, timeout=0.001)
        with self.assertRaises(TimeoutError):
            client.commit(packet(1, 0, 0.5, 1, npu=True))
        before = list(io.events)
        with self.assertRaises(TimeoutError):
            client.commit(packet(2, 1, 0.5, 1, npu=True))
        self.assertEqual(before, io.events)

    def test_restart_wrap_snapshot_and_invalidation(self):
        io = FakeIO()
        io.reg.update({0x20: 0xFFFFFFFF, 0x1C: 0xFFFFFFFF})
        self.assertEqual(MailboxClient(io).commit(packet(0, error=1))["ack_token"], 1)
        p = packet(2, error=1, npu=True)
        self.assertEqual(p["flags"], 4)
        self.assertEqual(p["label"], 65535)
        io.reg[0x30] = 3
        with self.assertRaises(TimeoutError):
            MailboxClient(io, timeout=0.001).snapshot()

    def test_reject_unverified_device_before_open(self):
        with self.assertRaises(ValueError):
            UioRegisters({"verified_design": False}, "/missing/library")

    def test_waveform_proposal_roundtrip_and_corruption(self):
        original = [0.0, 1.0, -1.0, 0.5, -0.5] * 300
        raw = wave_encode(0x12345678, original)
        seq, actual = wave_decode(raw)
        self.assertEqual(seq, 0x12345678)
        self.assertLessEqual(
            max(abs(a - b) for a, b in zip(actual, original)), 0.5 / 32767
        )
        for offset in [8, 32, len(raw) - 1]:
            damaged = bytearray(raw)
            damaged[offset] ^= 1
            with self.assertRaises(ValueError):
                wave_decode(damaged)
        with self.assertRaises(ValueError):
            wave_decode(raw[:-1])

    def test_frozen_wire_vectors(self):
        path = (
            Path(__file__).resolve().parents[1] / "fixtures/student/wire_vectors.json"
        )
        v = json.loads(path.read_text())
        for case in v["result_cases"]:
            p = case["packet"]
            self.assertEqual(
                list(decode_response(bytes.fromhex(p["rtu_expected_response_hex"]))),
                p["registers"],
            )
        for case in v["request_cases"]:
            result = reply(
                bytes.fromhex(case["request_hex"]),
                v["result_cases"][0]["packet"]["registers"],
            )
            self.assertEqual(
                None if result is None else result.hex(), case["response_hex"]
            )
        self.assertEqual(
            wave_decode(bytes.fromhex(v["waveform_proposal"]["hex"]))[0], 0x12345678
        )

    def test_measured_rule_wire_vectors(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "fixtures/student/rule_wire_vectors.json"
        )
        fixture = json.loads(path.read_text())
        self.assertEqual(
            fixture["config_sha256"], hashlib.sha256(CONFIG.read_bytes()).hexdigest()
        )
        changed = []
        for case in fixture["result_cases"]:
            p = case["packet"]
            expected = packet(
                p["sequence"],
                case["final_label"],
                case["score"],
                p["inference_us"],
                npu=True,
            )
            self.assertEqual(p, json.loads(json.dumps(expected)))
            self.assertEqual(
                list(decode_response(bytes.fromhex(p["rtu_expected_response_hex"]))),
                p["registers"],
            )
            self.assertEqual(p["flags"], 7)
            if case["rule"]["changed"]:
                self.assertNotEqual(case["npu_label"], p["label"])
                self.assertLess(case["score"], case["npu_score"])
                changed.append(case)
        self.assertTrue(changed)


class FakeWorker:
    def __init__(self, fail=False):
        self.fail = fail
        self.proc = SimpleNamespace(poll=lambda: None)

    def infer(self, sequence, raw):
        if self.fail:
            raise RuntimeError("Injected NPU failure")
        return {
            "type": "result",
            "backend": "Alnpu",
            "sequence": sequence,
            "logits": [1.0] + [0.0] * 21,
            "inference_us": 500,
            "worker_us": 2500,
        }

    def close(self):
        pass


class ReplayTests(unittest.TestCase):
    def setup_app(self, root, fail=False, labels=None):
        labels = [0, 0] if labels is None else labels
        assets = root / "assets"
        assets.mkdir()
        (assets / "trunk").write_bytes(b"trunk")
        (assets / "head").write_bytes(b"head")
        (assets / "raw").write_bytes(struct.pack("<1500f", *([0.0] * 1500)) * len(labels))

        def item(n):
            return {
                "path": n,
                "sha256": hashlib.sha256((assets / n).read_bytes()).hexdigest(),
            }

        m = {
            "schema": 1,
            "model": {
                "id": "kd-s1",
                "statistics": False,
                "trunk": item("trunk"),
                "head": item("head"),
            },
            "datasets": {
                "demo22": dict(
                    item("raw"),
                    labels=labels,
                    dataset_indices=[4, 9] if len(labels) == 2 else list(range(len(labels))),
                    count=len(labels),
                    title="unit fake",
                )
            },
        }
        (assets / "manifest.json").write_text(json.dumps(m))
        args = argparse.Namespace(
            assets=assets / "manifest.json",
            store=root / "storage",
            worker=assets / "trunk",
            vendor_lib=root,
            axi_config=None,
            mmio_lib=None,
            revision="unit-fake",
        )
        app = Replay(args)
        app.worker = FakeWorker(fail)
        return app

    def test_operating_scenario_preserves_predictions_during_input_switch(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.setup_app(Path(temp), labels=list(range(22)))
            try:
                rid = app.start('demo22', 1, 75, monitor=True)
                deadline = time.monotonic() + 3
                while app.public()['processed'] < 1 and time.monotonic() < deadline:
                    time.sleep(.01)
                request = app.inject(8)
                while app.public()['processed'] < 4 and time.monotonic() < deadline:
                    time.sleep(.01)
                app.stop()
                app.thread.join(3)
                saved = json.loads((app.store / rid / 'manifest.json').read_text())
                rows = [json.loads(x) for x in (app.store / rid / 'results.jsonl').read_text().splitlines()]
                switched = [r for r in rows if r['scenario_input']['request_id'] == request['id']]
                self.assertTrue(switched)
                self.assertTrue(all(r['expected'] == 8 and r['label'] == r['npu_label'] == 0 for r in switched))
                self.assertIsNone(saved['scenario']['requests'][-1]['first_match_sequence'])
                self.assertEqual(saved['scenario']['event_count'], 1)
                self.assertEqual(saved['status'], 'stopped')
                self.assertFalse(app.public()['mailbox']['flags'] & 1)
                with self.assertRaises(RuntimeError):
                    app.inject(0)
            finally:
                app.close()

    def test_sfaug_identity_is_saved_and_unknown_models_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = self.setup_app(root)
            args = old.args
            old.close()
            path = args.assets
            manifest = json.loads(path.read_text())
            manifest['model']['id'] = 'sfaug-r1'
            path.write_text(json.dumps(manifest))
            app = Replay(args)
            app.worker = FakeWorker()
            try:
                rid = app.start('demo22', 1, 0)
                app.thread.join(4)
                self.assertEqual(app.public()['status'], 'completed')
                self.assertEqual(app.public()['model'], 'sfaug-r1')
                saved = json.loads((app.store / rid / 'manifest.json').read_text())
                self.assertEqual(saved['model_id'], 'sfaug-r1')
                rows = [json.loads(line) for line in (app.store / rid / 'results.jsonl').read_text().splitlines()]
                self.assertTrue(all(row['model_id'] == 'sfaug-r1' for row in rows))
            finally:
                app.close()
            manifest['model']['id'] = 'unverified-model'
            path.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                load_assets(path)

    def test_worker_rejects_old_identity_for_new_model(self):
        with tempfile.TemporaryDirectory() as temp:
            model = {'id': 'sfaug-r1', 'trunk': {'absolute_path': 'trunk'}, 'head': {'absolute_path': 'head'}}
            ready = dict(type='ready', backend='Alnpu', statistics=False, model='kd-s1',
                         input_samples=1500, output_classes=22)
            with patch('ps.app.subprocess.Popen') as popen, patch.object(Worker, 'read', return_value=ready):
                with self.assertRaisesRegex(RuntimeError, 'identity mismatch'):
                    Worker('worker', 'libs', model, Path(temp) / 'evidence')
                self.assertEqual(popen.call_args.args[0][-1], 'sfaug-r1')

    def test_pl_return_is_the_only_input_sent_to_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.setup_app(Path(temp))
            returned = struct.pack("<1500f", *([0.25] * 1500))
            inputs = []
            worker = app.worker
            original_infer = worker.infer
            worker.infer = lambda seq, raw: (inputs.append(raw), original_infer(seq, raw))[1]
            app.sample_transport = SimpleNamespace(
                transfer=lambda seq, raw: (returned, dict(mode="pl-q15", physical=True)),
                close=lambda: None,
            )
            app.start("demo22", 1, 0)
            app.thread.join(4)
            self.assertEqual(app.public()["status"], "completed")
            self.assertEqual(inputs, [returned, returned])
            app.close()

    def test_slow_storage_keeps_publishing_and_invalidates_before_final_drain(self):
        entered, release = threading.Event(), threading.Event()

        def slow_sync(fd):
            entered.set()
            if not release.wait(3):
                raise OSError("Injected storage wait expired")

        with tempfile.TemporaryDirectory() as temp:
            app = self.setup_app(Path(temp))
            with patch("ps.app.ResultWriter", side_effect=lambda folder, header:
                       ResultWriter(folder, header, sync_every=1, sync=slow_sync)):
                rid = app.start("demo22", 4, 20)
                try:
                    self.assertTrue(entered.wait(1))
                    deadline = time.monotonic() + 1
                    while app.public()["status"] != "finalizing" and time.monotonic() < deadline:
                        time.sleep(.005)
                    state = app.public()
                    self.assertEqual(state["processed"], 8)
                    self.assertEqual(state["status"], "finalizing")
                    self.assertEqual(state["storage_io"]["durable"], 0)
                    self.assertEqual(state["mailbox"]["flags"], 4)
                finally:
                    release.set()
                    app.thread.join(4)
            self.assertEqual(app.public()["status"], "completed")
            self.assertEqual(app.public()["storage_io"]["durable"], 8)
            self.assertEqual(len((app.store / rid / "results.jsonl").read_text().splitlines()), 8)
            app.close()

    def test_storage_error_prevents_next_inference_and_invalidates(self):
        def fail_sync(fd):
            raise OSError("Injected storage sync failure")

        with tempfile.TemporaryDirectory() as temp:
            app = self.setup_app(Path(temp))
            with patch("ps.app.ResultWriter", side_effect=lambda folder, header:
                       ResultWriter(folder, header, sync_every=1, sync=fail_sync)):
                app.start("demo22", 1, 100)
                app.thread.join(4)
            state = app.public()
            self.assertEqual(state["processed"], 1)
            self.assertEqual(state["status"], "error")
            self.assertEqual(state["mailbox"]["flags"], 4)
            self.assertEqual(state["mailbox"]["error"], 2)
            self.assertFalse(state["worker_ready"])
            app.close()

    def test_pl_failure_prevents_inference_and_publishes_axi_error(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.setup_app(Path(temp))
            inputs = []
            app.worker.infer = lambda seq, raw: inputs.append(raw)

            def fail(sequence, raw):
                raise SampleTransportError("Injected PL ACK failure")

            app.sample_transport = SimpleNamespace(transfer=fail, close=lambda: None)
            app.start("demo22", 1, 0)
            app.thread.join(4)
            state = app.public()
            self.assertEqual(inputs, [])
            self.assertEqual(state["status"], "error")
            self.assertEqual(state["mailbox"]["flags"], 4)
            self.assertEqual(state["mailbox"]["error"], 3)
            app.close()

    def test_success_persists_and_invalidates_after_finish(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.setup_app(Path(temp))
            rid = app.start("demo22", 1, 0)
            app.thread.join(4)
            state = app.public()
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["correct"], 2)
            self.assertEqual(state["mailbox"]["flags"], 4)
            self.assertFalse(state["axi"]["physical"])
            rows = [
                json.loads(s)
                for s in (app.store / rid / "results.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["mailbox"]["flags"], 7)
            self.assertEqual(
                json.loads((app.store / rid / "manifest.json").read_text())["status"],
                "completed",
            )
            app.close()

    def test_worker_failure_invalidates_and_marks_run_error(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.setup_app(Path(temp), True)
            rid = app.start("demo22", 1, 0)
            app.thread.join(4)
            state = app.public()
            self.assertEqual(state["status"], "error")
            self.assertEqual(state["processed"], 0)
            self.assertEqual(state["mailbox"]["error"], 1)
            self.assertEqual(state["mailbox"]["flags"], 4)
            saved = json.loads((app.store / rid / "manifest.json").read_text())
            self.assertEqual(saved["status"], "error")
            self.assertEqual(saved["error"], "Injected NPU failure")
            app.close()

    def test_asset_mutation_and_bad_controls_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.setup_app(Path(temp))
            for repeat, period in [(0, 25), (1, float("nan")), (True, 25), (1, -1)]:
                with self.assertRaises(ValueError):
                    app.start("demo22", repeat, period)
            with self.assertRaises(ValueError):
                app.start([])
            (Path(temp) / "assets/raw").write_bytes(b"changed")
            with self.assertRaises(ValueError):
                load_assets(app.args.assets)
            app.close()

    def test_endurance_limit_is_explicit_and_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.setup_app(Path(temp))
            self.assertEqual(app.public()["max_frames"], 10000)
            app.max_frames = 3
            with self.assertRaises(ValueError):
                app.start("demo22", 2, 0)
            app.max_frames = 100000
            rid = app.start("demo22", 2, 0)
            app.thread.join(4)
            self.assertEqual(app.public()["processed"], 4)
            self.assertEqual(
                json.loads((app.store / rid / "manifest.json").read_text())["status"],
                "completed",
            )
            app.close()
            app.args.max_frames = 100001
            with self.assertRaises(ValueError):
                Replay(app.args)

    def test_rule_override_keeps_npu_and_wire_score_honest(self):
        for enabled in [False, True]:
            with tempfile.TemporaryDirectory() as temp:
                app = self.setup_app(Path(temp))
                data = struct.pack("<1500f", *wave(1, 0.25)) * 2
                Path(app.assets["datasets"]["demo22"]["absolute_path"]).write_bytes(
                    data
                )
                # Fake metadata is installed before any run; the classifier only sees bytes.
                app.assets["datasets"]["demo22"]["labels"] = [17, 17]
                if enabled:
                    app.sensor_rules = SensorRules(CONFIG)
                    app.rule_policy = app.sensor_rules.public()
                    app.audit["sensor_rules"] = app.rule_policy
                rid = app.start("demo22", 1, 0)
                app.thread.join(4)
                rows = [
                    json.loads(s)
                    for s in (app.store / rid / "results.jsonl")
                    .read_text()
                    .splitlines()
                ]
                self.assertEqual(app.public()["npu_correct"], 0)
                self.assertEqual(app.public()["correct"], 2 if enabled else 0)
                for row in rows:
                    self.assertEqual(row["npu_label"], 0)
                    self.assertEqual(row["label"], 17 if enabled else 0)
                    self.assertEqual(row["logits"], [1.0] + [0.0] * 21)
                    self.assertEqual(row["mailbox"]["flags"], 7)
                    self.assertEqual(row["mailbox"]["label"], row["label"])
                    self.assertEqual(
                        row["mailbox"]["confidence_q"], round(row["score"] * 10000)
                    )
                    self.assertEqual(row["rule"]["changed"], enabled)
                    if enabled:
                        self.assertLess(row["score"], row["npu_score"])
                self.assertEqual(app.public()["mailbox"]["flags"], 4)
                app.close()

    def test_matching_rule_cannot_hide_npu_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.setup_app(Path(temp), True)
            app.sensor_rules = SensorRules(CONFIG)
            app.rule_policy = app.sensor_rules.public()
            app.audit["sensor_rules"] = app.rule_policy
            waveform = wave(1, 0.25)
            self.assertTrue(app.sensor_rules.diagnose(waveform)["matched"])
            Path(app.assets["datasets"]["demo22"]["absolute_path"]).write_bytes(
                struct.pack("<1500f", *waveform) * 2
            )
            rid = app.start("demo22", 1, 0)
            app.thread.join(4)
            state = app.public()
            self.assertEqual(state["status"], "error")
            self.assertEqual(state["processed"], 0)
            self.assertEqual(state["rule_matches"], 0)
            self.assertEqual(state["mailbox"]["error"], 1)
            self.assertEqual(state["mailbox"]["label"], 65535)
            self.assertEqual((app.store / rid / "results.jsonl").read_text(), "")
            app.close()


if __name__ == "__main__":
    unittest.main()
