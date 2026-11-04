import base64
import hashlib
import json
from pathlib import Path
import struct
import threading
import time
import unittest

from ps.network_input import WindowStream, parse_window, display_metadata


def payload(value=.25, input_id=1, run_id="test"):
    raw = struct.pack("<1500f", *([value] * 1500))
    return dict(run_id=run_id, input_id=input_id, sha256=hashlib.sha256(raw).hexdigest(),
                samples_b64=base64.b64encode(raw).decode())


class NetworkInputTests(unittest.TestCase):
    def test_waveform_hash_bounds_and_no_label_channel(self):
        fixture = json.loads((Path(__file__).resolve().parents[1] / "fixtures/network_input_contract.json").read_text())
        self.assertEqual(payload()["sha256"], fixture["unit_vector"]["sha256"])
        self.assertEqual(set(payload()), set(fixture["request_fields"]))
        self.assertEqual(len(parse_window(payload())), 6000)
        for value in [float("nan"), float("inf"), 1.01, -1.01]:
            with self.assertRaises(ValueError):
                parse_window(payload(value))
        for updates in [dict(label=2), dict(expected=0), dict(input_id=True), dict(samples_b64="x"), dict(sha256="bad")]:
            with self.assertRaises(ValueError):
                parse_window(dict(payload(), **updates))

    def test_single_outstanding_and_strict_ids(self):
        stream, result = WindowStream(), []
        thread = threading.Thread(target=lambda: result.append(stream.submit(payload())))
        thread.start()
        item = stream.take(threading.Event())
        with self.assertRaises(RuntimeError):
            stream.submit(payload(input_id=2))
        self.assertEqual(item["raw"], parse_window(payload()))
        stream.complete(item, {"sequence": 42})
        thread.join(2)
        self.assertEqual(result, [{"sequence": 42}])
        with self.assertRaises(RuntimeError):
            stream.submit(payload())
        stream.close()

    def test_display_metadata_is_strict_and_does_not_change_inference_bytes(self):
        original = payload()
        display = dict(scale_a=8.0, sample_rate_hz=20000, sim_time_s=1.025)
        request = dict(original, display=display)
        self.assertEqual(parse_window(request), parse_window(original))
        self.assertEqual(display_metadata(request), display)
        for value in [None, {}, dict(display, label=2), dict(display, scale_a=0),
                      dict(display, sim_time_s=float('nan')), dict(display, sample_rate_hz=1000),
                      dict(display, scale_a=True), dict(display, sim_time_s=0)]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_window(dict(original, display=value))

    def test_stop_unblocks_sender_and_waiting_consumer(self):
        stream, errors = WindowStream(), []
        def send():
            try:
                stream.submit(payload())
            except RuntimeError as error:
                errors.append(str(error))
        thread = threading.Thread(target=send)
        thread.start()
        item = stream.take(threading.Event())
        stream.close("stopped by user")
        stream.complete(item, {"sequence": 1})
        thread.join(2)
        self.assertEqual(errors, ["stopped by user"])
        event = threading.Event()
        event.set()
        self.assertIsNone(stream.take(event))

    def test_idle_expiry_and_processing_timeout_do_not_reuse_input(self):
        stream = WindowStream(idle_seconds=.02)
        with self.assertRaisesRegex(RuntimeError, "expired"):
            stream.take(threading.Event())
        begin = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            stream.submit(payload(), timeout=.02)
        self.assertLess(time.monotonic() - begin, 1)
        with self.assertRaises(RuntimeError):
            stream.take(threading.Event())


if __name__ == "__main__":
    unittest.main()
