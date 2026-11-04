import csv
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pc_monitor.monitor import Monitor, REQUEST, decode
from pc_monitor.__main__ import make_server
from protocol.modbus import frame

ROOT = Path(__file__).resolve().parents[1]


class FakePort:
    def __init__(self, replies):
        self.replies = replies
        self.pending = b""
        self.closed = False
        self.requests = []
        self.timeout = 0.25

    @property
    def in_waiting(self):
        return 0

    def write(self, value):
        self.requests.append(value)
        self.pending = self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]
        return len(value)

    def read(self, n):
        result, self.pending = self.pending[:min(n, 2)], self.pending[min(n, 2):]
        return result

    def close(self):
        self.closed = True


def wait_until(predicate, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Condition did not become true")


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cases = json.loads((ROOT / "fixtures/pc_monitor_vectors.json").read_text())
        self.raw = bytes.fromhex(self.cases["active"]["response_hex"])
        self.port = FakePort([self.raw])
        self.open_count = 0

        def factory(_):
            self.open_count += 1
            return self.port

        self.monitor = Monitor(self.temp.name, factory, lambda: [dict(device="COM22", description="test fixture")])

    def tearDown(self):
        self.monitor.stop()
        self.temp.cleanup()

    def test_all_frozen_diagnosis_vectors_decode(self):
        data = json.loads((ROOT / "fixtures/student/wire_vectors.json").read_text())
        for case in data["result_cases"]:
            packet = case["packet"]
            row = decode(bytes.fromhex(packet["rtu_expected_response_hex"]))
            self.assertTrue(row["abi_ok"])
            self.assertEqual(row["result_valid"], bool(packet["registers"][1] & 1))
            self.assertEqual(row["registers"], packet["registers"])
            self.assertEqual(row["sequence"], packet["sequence"])
        row = decode(self.raw)
        self.assertEqual(row["label_name"], "A 相传感器卡死")
        self.assertEqual(row["label_code"], "Sensor A SF")

    def test_inactive_crc_exception_truncated_and_unknown_abi(self):
        for name, case in self.cases.items():
            if not isinstance(case, dict):
                continue
            with self.subTest(name=name):
                row = decode(bytes.fromhex(case["response_hex"]))
                for field, expected in case["expected"].items():
                    self.assertEqual(row[field], expected, field)
        wrong_address = frame(bytes([2]) + self.raw[1:-2])
        self.assertFalse(decode(wrong_address)["wire_ok"])
        self.assertFalse(decode(self.raw[:-3])["result_valid"])

    def test_polling_is_read_only_and_exports_full_records(self):
        self.monitor.start("COM22", 50)
        wait_until(lambda: self.monitor.snapshot()["counts"]["queries"] >= 3)
        with self.assertRaises(ValueError):
            self.monitor.start("COM22", 100)
        self.assertEqual(self.open_count, 1)
        self.monitor.stop()
        state = self.monitor.snapshot()
        self.assertEqual(state["link"], "disconnected")
        self.assertTrue(self.port.closed)
        self.assertTrue(all(r == REQUEST for r in self.port.requests))
        self.assertEqual(state["counts"]["updates"], 1)
        handle, _, size = self.monitor.export("jsonl", state["session"])
        with handle:
            events = [json.loads(line) for line in handle.read(size).splitlines()]
        self.assertEqual(events[-1]["event"], "complete")
        self.assertEqual(events[-1]["counts"]["queries"], state["counts"]["queries"])
        handle, _, size = self.monitor.export("csv", state["session"])
        with handle:
            records = list(csv.DictReader(io.StringIO(handle.read(size).decode("utf-8-sig"))))
        self.assertEqual(len(records), state["counts"]["queries"])
        self.assertEqual(bytes.fromhex(records[0]["response_hex"]), self.raw)
        with self.assertRaises(ValueError):
            self.monitor.export("jsonl", "old-session")

    def test_failed_response_replaces_live_result_and_counters(self):
        bad = self.raw[:-1] + bytes([self.raw[-1] ^ 1])
        self.port.replies = [self.raw, bad, b""]
        self.monitor.start("COM22", 50)
        wait_until(lambda: self.monitor.snapshot()["counts"]["timeouts"] >= 1)
        self.monitor.stop()
        state = self.monitor.snapshot()
        self.assertEqual(state["counts"]["crc_errors"], 1)
        self.assertEqual(state["counts"]["active_results"], 1)
        self.assertFalse(state["latest"]["result_valid"])
        self.assertEqual(state["latest"]["status"], "timeout")

    def test_live_export_size_is_frozen_at_a_complete_record(self):
        self.monitor.start("COM22", 50)
        wait_until(lambda: self.monitor.snapshot()["counts"]["queries"] >= 2)
        state = self.monitor.snapshot()
        handle, _, size = self.monitor.export("jsonl", state["session"])
        with handle:
            wait_until(lambda: self.monitor.snapshot()["counts"]["queries"] >= state["counts"]["queries"] + 2)
            snapshot = handle.read(size)
            self.assertTrue(snapshot.endswith(b"\n"))
            events = [json.loads(line) for line in snapshot.splitlines()]
            self.assertLess(events[-1]["id"], self.monitor.snapshot()["counts"]["queries"])
            self.assertTrue(handle.read())

    def test_disconnect_exception_releases_port(self):
        self.port.write = lambda _: (_ for _ in ()).throw(OSError("device unplugged"))
        self.monitor.start("COM22", 50)
        wait_until(lambda: self.monitor.snapshot()["link"] == "error")
        self.monitor.stop()
        self.assertTrue(self.port.closed)
        self.assertEqual(self.monitor.snapshot()["error"], "device unplugged")
        self.assertEqual(self.monitor.snapshot()["counts"]["queries"], 0)

    def test_bounded_history_and_session_cursor(self):
        with self.monitor.lock:
            self.monitor.session = "current"
            for i in range(650):
                self.monitor._accept(dict(id=i+1, stale_hex="", **decode(self.raw)))
        state = self.monitor.snapshot(after=0, session="current")
        self.assertEqual(len(self.monitor.rows), 600)
        self.assertEqual(len(state["rows"]), 200)
        self.assertEqual(state["omitted_rows"], 450)
        self.assertEqual(len(self.monitor.snapshot(after=649, session="current")["rows"]), 1)
        self.assertEqual(len(self.monitor.snapshot(after=10000, session="old")["rows"]), 200)

    def test_reject_bad_config_before_open(self):
        for port, period in [("COM10", 100), ("COM22", 0), ("COM22", True), ("COM22", 1.5), ("COM22", 5001), ("COM22/../../file", 100)]:
            with self.assertRaises(ValueError):
                self.monitor.start(port, period)
        self.assertEqual(self.open_count, 0)

    def test_http_controls_require_token_and_same_origin(self):
        server = make_server(self.monitor, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(url + "/api/state") as response:
                state = json.load(response)
            token = state["token"]
            def post(headers=None):
                return urlopen(Request(url + "/api/connect", data=b'{"port":"COM22","period_ms":50}', headers=headers or {}, method="POST"))
            for headers in [{}, {"X-PowerScope-Token": token, "Origin": "http://untrusted.example"}, {"X-PowerScope-Token": token, "Host": "untrusted.example"}]:
                with self.assertRaises(HTTPError) as error:
                    post(headers)
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
            self.assertEqual(self.open_count, 0)
            with post({"X-PowerScope-Token": token, "Origin": url}) as response:
                self.assertTrue(json.load(response)["ok"])
            wait_until(lambda: self.monitor.snapshot()["counts"]["queries"] > 0)
            with urlopen(Request(url + "/api/disconnect", data=b'{}', headers={"X-PowerScope-Token": token}, method="POST")) as response:
                self.assertTrue(json.load(response)["ok"])
            session = self.monitor.snapshot()["session"]
            with urlopen(url + "/api/export/csv?session=" + session) as response:
                self.assertIn("attachment", response.headers["Content-Disposition"])
                self.assertIn(self.raw.hex(" ").upper(), response.read().decode("utf-8-sig"))
            with self.assertRaises(HTTPError) as error:
                urlopen(url + "/api/export/csv?session=old")
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
