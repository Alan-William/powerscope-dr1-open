import struct
import unittest

from protocol.waveform_mailbox import WaveformClient
from ps.sample_transport import SampleTransport, SampleTransportError, transfer_for_inference
from tests.test_waveform_mailbox import FakeWaveform


class SampleTransportTests(unittest.TestCase):
    def test_npu_input_is_quantized_return_not_original_float_bytes(self):
        values = [.123456, -.234567, 1.0, -1.0, 0.0] * 300
        raw = struct.pack('<1500f', *values)
        returned, evidence = transfer_for_inference(WaveformClient(FakeWaveform()), 42, raw)
        self.assertNotEqual(returned, raw)
        actual = struct.unpack('<1500f', returned)
        self.assertLessEqual(max(abs(a - b) for a, b in zip(actual, values)), .5 / 32767 + 1e-7)
        self.assertEqual(evidence['sequence'], 42)
        self.assertTrue(evidence['bytes_equal'])

    def test_failure_never_returns_original_input_as_fallback(self):
        raw = struct.pack('<1500f', *([.2] * 1500))
        for io in [FakeWaveform(acknowledge=False), FakeWaveform(corrupt=True)]:
            with self.assertRaises(SampleTransportError):
                transfer_for_inference(WaveformClient(io, timeout=.001), 1, raw)

    def test_bad_input_does_not_touch_hardware(self):
        io = FakeWaveform()
        with self.assertRaises(ValueError):
            transfer_for_inference(WaveformClient(io), 1, struct.pack('<1500f', *([2.0] * 1500)))
        self.assertEqual(io.writes, [])

    def test_default_direct_path_is_byte_identical(self):
        raw = struct.pack('<1500f', *([.123456] * 1500))
        converted, evidence = SampleTransport().transfer(1, raw)
        self.assertEqual(converted, raw)
        self.assertFalse(evidence['physical'])
        self.assertEqual(evidence['mode'], 'direct-f32')


if __name__ == '__main__':
    unittest.main()
