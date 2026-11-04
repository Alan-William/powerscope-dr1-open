"""Optional verified PL waveform round trip before native NPU inference."""
import struct
import time

from protocol.replay_frame import encode, decode
from protocol.waveform_mailbox import WaveformClient


class SampleTransportError(RuntimeError):
    """A hardware transfer failed; callers must not fall back to the input file."""


def transfer_for_inference(client, sequence, raw):
    if len(raw) != 6000:
        raise ValueError('Expected 1500 normalized float32 samples')
    start = time.monotonic_ns()
    record = encode(sequence, struct.unpack('<1500f', raw))
    try:
        returned, status = client.transfer(record)
        returned_sequence, samples = decode(returned)
        if returned != record or returned_sequence != sequence:
            raise ValueError('PL return identity mismatch')
    except Exception as error:
        raise SampleTransportError('PL waveform transfer failed: ' + str(error)) from error
    converted = struct.pack('<1500f', *samples)
    return converted, dict(mode='pl-q15', physical=True, bytes_equal=True,
                           transfer_us=(time.monotonic_ns() - start) // 1000, **status)


class SampleTransport:
    def __init__(self, config=None, library=None, recover_on_start=False):
        self.io = self.client = None
        self.startup_recovery = None
        self.mode = 'pl-q15' if config else 'direct-f32'
        if config:
            from ps.waveform import UioWaveformRegisters
            self.io = UioWaveformRegisters(config, library)
            self.client = WaveformClient(self.io)
            if recover_on_start:
                try:
                    self.startup_recovery = self.client.recover_interrupted()
                except Exception:
                    self.io.close()
                    self.io = self.client = None
                    raise

    def transfer(self, sequence, raw):
        if self.client is None:
            return raw, dict(mode='direct-f32', physical=False, transfer_us=0)
        return transfer_for_inference(self.client, sequence, raw)

    def close(self):
        if self.io:
            self.io.close()
