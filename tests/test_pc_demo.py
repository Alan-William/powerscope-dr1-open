import hashlib
import json
from pathlib import Path
import struct
import tempfile
import time
import unittest

from pc_monitor.demo import Demo, WindowLibrary
from protocol.mailbox import packet
from tests.test_pc_monitor import wait_until


class FakeMonitor:
    def __init__(self):
        self.link = "disconnected"
        self.rows = []
        self.stop_calls = 0

    def start(self, port, period):
        self.link = "connected"

    def stop(self):
        self.link = "disconnected"
        self.stop_calls += 1

    def snapshot(self):
        return dict(link=self.link, session="test-serial", config={"period_ms": 100}, rows=list(self.rows))


class FakeBoard:
    def __init__(self, monitor):
        self.monitor = monitor
        self.calls = []
        self.running = False
        self.bad_ack = False

    def state(self):
        return dict(model="sfaug-r1", sensor_rules={"enabled": False}, session="test-board",
                    external_input={"supported": True}, run_id="test-run",
                    status="running" if self.running else "stopped")

    def request(self, path, body, **kwargs):
        self.calls.append((path, body))
        if path == "/api/stream/start":
            self.running = True
            return {"run_id": "test-run"}
        if path == "/api/stream/stop":
            self.running = False
            return {}
        if path != "/api/stream/window":
            raise AssertionError("Unexpected board control")
        p = packet(body["input_id"], 0, .8, 1000, npu=True)
        self.monitor.rows.append(dict(result_valid=True, sequence=p["sequence"],
            registers=p["registers"], npu_executed=True, label=0, label_name="正常运行",
            response_hex=p["rtu_expected_response_hex"], start_monotonic_ns=time.monotonic_ns(),
            end_monotonic_ns=time.monotonic_ns(), inference_us=1000, received_at="test",
            id=body["input_id"]))
        return dict(run_id="test-run", input_id=body["input_id"], sequence=p["sequence"],
                    sha256="bad" if self.bad_ack else body["sha256"], bytes=6000,
                    model="sfaug-r1", sample_transport={"physical": True}, axi={"physical": True},
                    registers=p["registers"])


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        raw = b"".join(struct.pack("<1500f", *([label / 100] * 1500)) for label in range(22))
        (self.root / "test.f32").write_bytes(raw)
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps(dict(schema=1, format="float32-le-phase-major-normalized",
            channels=3, samples_per_channel=500, sample_rate_hz=20000, simulated=True,
            path="test.f32", sha256=hashlib.sha256(raw).hexdigest(), count=22, title="synthetic unit fixture",
            labels=list(range(22)), dataset_indices=list(range(22)))), encoding="utf-8")
        self.monitor = FakeMonitor()
        self.board = FakeBoard(self.monitor)
        self.demo = Demo(self.monitor, self.manifest, self.root / "logs", self.board)

    def tearDown(self):
        self.demo.stop(wait=True)
        self.temp.cleanup()

    def test_reference_label_never_sent_and_wire_prediction_preserved(self):
        self.demo.start("COM22")
        wait_until(lambda: self.demo.snapshot()["matched"] >= 1)
        self.demo.select(2)
        wait_until(lambda: (self.demo.snapshot()["latest_match"] or {}).get("source_label") == 2)
        self.demo.stop(wait=True)
        state = self.demo.snapshot()
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["latest_match"]["label"], 0)
        self.assertFalse(state["latest_match"]["matches_reference"])
        sent = [b for path, b in self.board.calls if path == "/api/stream/window"]
        self.assertGreaterEqual(len(sent), 2)
        self.assertTrue(all(set(b) == {"run_id", "input_id", "samples_b64", "sha256"} for b in sent))
        self.assertNotEqual(sent[0]["sha256"], sent[-1]["sha256"])
        self.assertEqual(self.monitor.stop_calls, 1)
        stream, name, size = self.demo.export(state["session"])
        with stream:
            content = stream.read(size).decode()
        self.assertTrue(content.endswith("\n"))
        self.assertEqual(json.loads(content.splitlines()[-1])["event"], "complete")

    def test_bad_ack_stops_owned_run_and_preserves_preexisting_serial(self):
        self.board.bad_ack = True
        self.monitor.link = "connected"
        self.demo.start("COM22")
        wait_until(lambda: self.demo.snapshot()["status"] == "error")
        self.assertEqual(self.demo.snapshot()["matched"], 0)
        self.assertFalse(self.board.running)
        self.assertEqual(self.monitor.link, "connected")
        self.assertEqual(self.monitor.stop_calls, 0)

    def test_other_board_run_is_not_stopped(self):
        self.board.running = True
        self.demo.start("COM22")
        wait_until(lambda: self.demo.snapshot()["status"] == "error")
        self.assertEqual(self.board.calls, [])
        self.assertEqual(self.monitor.link, "disconnected")

    def test_data_corruption_rejected(self):
        (self.root / "test.f32").write_bytes(b"corrupted")
        with self.assertRaises(ValueError):
            WindowLibrary(self.manifest)

    def test_stop_drains_the_published_window(self):
        self.monitor.link = "connected"
        sent = time.monotonic_ns()
        ack = self.board.request("/api/stream/window", dict(input_id=1, sha256="test"))
        self.demo.stop_event.set()
        result = self.demo._wait_wire(ack, sent, "test-serial")
        self.assertEqual(result["sequence"], 1)


if __name__ == "__main__":
    unittest.main()
