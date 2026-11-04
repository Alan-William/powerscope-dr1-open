import json
from pathlib import Path
import unittest
from tools.rs485_capture import decode_record, read_response


class ChunkPort:
    def __init__(self, data):
        self.data = data
        self.timeout = None

    def read(self, n):
        n = min(n, 2)
        result, self.data = self.data[:n], self.data[n:]
        return result


class CaptureTests(unittest.TestCase):
    def test_partial_serial_reads_and_all_frozen_results(self):
        for name in ("wire_vectors.json", "rule_wire_vectors.json"):
            data = json.loads((Path(__file__).resolve().parents[1] / "fixtures/student" / name).read_text())
            for case in data["result_cases"]:
                p = case["packet"]
                raw = bytes.fromhex(p["rtu_expected_response_hex"])
                observed = read_response(ChunkPort(raw), 0.1)
                result = decode_record(observed)
                self.assertEqual(observed, raw)
                self.assertTrue(result["crc_ok"] and result["valid_response"])
                self.assertEqual(result["registers"], p["registers"])
                self.assertEqual(result["sequence"], p["sequence"])

    def test_exception_crc_error_and_timeout(self):
        result = decode_record(read_response(ChunkPort(bytes.fromhex("0183030131")), 0.1))
        self.assertTrue(result["crc_ok"])
        self.assertFalse(result["valid_response"])
        self.assertFalse(decode_record(bytes.fromhex("0183030130"))["crc_ok"])
        self.assertTrue(decode_record(b"")["timeout"])
        self.assertFalse(decode_record(b"\x01\x03")["valid_response"])


if __name__ == "__main__":
    unittest.main()
