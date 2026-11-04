import unittest
import struct
from protocol.replay_frame import encode
from protocol.waveform_mailbox import (
    WaveformClient, ABI, STATUS, LENGTH, COMMIT, ACK, CONSUME, COUNT, ERRORS, WORDS, BUFFER, ABORT,
)


class FakeWaveform:
    def __init__(self, acknowledge=True, consume=True, corrupt=False, token=0):
        self.reg = {ABI: 1, STATUS: 1, LENGTH: 3032, COMMIT: token, ACK: token,
                    CONSUME: token, COUNT: 0, ERRORS: 0, WORDS: 0}
        self.acknowledge, self.consume, self.corrupt = acknowledge, consume, corrupt
        self.writes = []

    def read(self, offset):
        value = self.reg[offset]
        return value ^ 1 if self.corrupt and offset == BUFFER + 40 else value

    def write(self, offset, value):
        self.writes.append((offset, value))
        self.reg[offset] = value
        if offset >= BUFFER:
            self.reg[WORDS] += 1
        if offset == COMMIT and self.acknowledge:
            self.reg[ACK] = value
            self.reg[STATUS] = 2
            self.reg[COUNT] += 1
        if offset == CONSUME and self.consume:
            self.reg[STATUS] = 1
            self.reg[WORDS] = 0
        if offset == ABORT:
            self.reg[WORDS] = 0

    def barrier(self):
        self.writes.append("barrier")


class WaveformTests(unittest.TestCase):
    record = encode(0xFFFFFFFF, [0, 1, -1, 0.5, -0.5] * 300)

    def test_roundtrip_wrap_and_barrier_order(self):
        io = FakeWaveform(token=0xFFFFFFFF)
        returned, evidence = WaveformClient(io).transfer(self.record)
        self.assertEqual(returned, self.record)
        self.assertEqual(evidence["ack_token"], 1)
        self.assertEqual(io.writes[759:762], ["barrier", (COMMIT, 1), "barrier"])
        self.assertEqual(io.writes[-3:], ["barrier", (CONSUME, 1), "barrier"])

    def test_ack_timeout_does_not_overwrite(self):
        io = FakeWaveform(acknowledge=False)
        client = WaveformClient(io, timeout=0.001)
        with self.assertRaises(TimeoutError):
            client.transfer(self.record)
        before = list(io.writes)
        with self.assertRaises(RuntimeError):
            client.transfer(self.record)
        self.assertEqual(before, io.writes)

    def test_corruption_preserves_unconsumed_buffer(self):
        io = FakeWaveform(corrupt=True)
        with self.assertRaises(ValueError):
            WaveformClient(io).transfer(self.record)
        self.assertEqual(io.reg[CONSUME], 0)
        self.assertEqual(io.reg[STATUS], 2)

    def test_consume_timeout_and_partial_prior_input(self):
        io = FakeWaveform(consume=False)
        with self.assertRaises(TimeoutError):
            WaveformClient(io, timeout=0.001).transfer(self.record)
        before = list(io.writes)
        with self.assertRaises(RuntimeError):
            WaveformClient(io).transfer(self.record)
        self.assertEqual(before, io.writes)
        io = FakeWaveform()
        io.reg[WORDS] = 1
        with self.assertRaises(RuntimeError):
            WaveformClient(io).transfer(self.record)
        self.assertEqual(io.writes, [])

    def test_invalid_input_before_hardware_writes(self):
        io = FakeWaveform()
        with self.assertRaises(ValueError):
            WaveformClient(io).transfer(self.record[:-1])
        self.assertEqual(io.writes, [])

    def test_startup_recovery_discards_only_uncommitted_input(self):
        io = FakeWaveform(token=11)
        io.reg[WORDS] = 209
        result = WaveformClient(io).recover_interrupted()
        self.assertEqual(result['action'], 'discarded_uncommitted_input')
        self.assertEqual([w for w in io.writes if w != 'barrier'], [(ABORT, 1)])
        self.assertEqual(io.reg[WORDS], 0)
        returned, _ = WaveformClient(io).transfer(self.record)
        self.assertEqual(returned, self.record)

    def test_startup_recovery_validates_completed_orphan_and_wrap(self):
        for corrupt in [False, True]:
            io = FakeWaveform(token=0xFFFFFFFF, corrupt=corrupt)
            for i, word in enumerate(struct.unpack('<758I', self.record)):
                io.write(BUFFER + i * 4, word)
            io.write(COMMIT, 1)
            io.writes.clear()
            if corrupt:
                with self.assertRaises(ValueError):
                    WaveformClient(io).recover_interrupted()
                self.assertEqual([w for w in io.writes if w != 'barrier'], [])
                self.assertEqual(io.reg[STATUS], 2)
            else:
                result = WaveformClient(io).recover_interrupted()
                self.assertEqual(result['action'], 'consumed_valid_orphan')
                self.assertEqual(result['sequence'], 0xFFFFFFFF)
                self.assertEqual([w for w in io.writes if w != 'barrier'], [(CONSUME, 1)])

    def test_startup_recovery_refuses_ambiguous_or_faulted_ownership(self):
        for changes in [{COMMIT: 2}, {STATUS: 3}, {ERRORS: 1}, {WORDS: 759},
                        {STATUS: 2, WORDS: 758, ACK: 7, COMMIT: 7}]:
            io = FakeWaveform()
            io.reg.update(changes)
            with self.assertRaises(RuntimeError):
                WaveformClient(io).recover_interrupted()
            self.assertEqual([w for w in io.writes if w != 'barrier'], [])

    def test_startup_recovery_timeout_does_not_abort_completed_buffer(self):
        io = FakeWaveform(consume=False)
        for i, word in enumerate(struct.unpack('<758I', self.record)):
            io.write(BUFFER + i * 4, word)
        io.write(COMMIT, 1)
        io.writes.clear()
        with self.assertRaises(TimeoutError):
            WaveformClient(io, timeout=.001).recover_interrupted()
        self.assertNotIn((ABORT, 1), io.writes)


if __name__ == "__main__":
    unittest.main()
