"""PSW1 single-buffer ownership protocol; offsets relative to the waveform UIO map.

The physical map is assigned by the compiled SoC and verified separately. No
CPU/model fallback is involved: this client only validates transport bytes.
"""
import hashlib
import struct
import time

from protocol.replay_frame import FRAME_BYTES, decode

ABI, STATUS, LENGTH, COMMIT, ACK, CONSUME, COUNT, ERRORS, ABORT, WORDS = range(0, 0x28, 4)
BUFFER = 0x1000


class WaveformClient:
    def __init__(self, io, timeout=0.05):
        self.io, self.timeout = io, timeout

    def _wait(self, predicate, message):
        deadline = time.monotonic() + self.timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise TimeoutError(message)
            time.sleep(0.0002)

    def recover_interrupted(self):
        """Explicit startup recovery, only while holding the exclusive UIO lock.

        Discard an uncommitted partial input, or validate and consume a completed
        orphan. Never call this after a transfer error in the same process.
        Unknown ownership, error counts and corrupt frames remain fail-closed.
        """
        io = self.io
        offsets = (ABI, STATUS, LENGTH, COMMIT, ACK, CONSUME, COUNT, ERRORS, WORDS)

        def snapshot():
            io.barrier()
            values = {offset: io.read(offset) for offset in offsets}
            io.barrier()
            return values

        before = snapshot()
        if snapshot() != before or before[ABI] != 1 or before[ERRORS] != 0:
            raise RuntimeError("Waveform startup recovery requires a stable error-free ABI")
        ack = before[ACK]
        if before[COMMIT] != ack or before[LENGTH] != FRAME_BYTES:
            raise RuntimeError("Waveform startup ownership is unresolved")
        evidence = {"action": "none", "before": before}
        if before[STATUS] == 1 and before[CONSUME] == ack:
            words = before[WORDS]
            if not 0 <= words <= FRAME_BYTES // 4:
                raise RuntimeError("Invalid incomplete waveform length")
            if words == 0:
                return evidence
            # RTL permits ABORT only for an uncommitted, unoccupied buffer.
            io.write(ABORT, 1)
            evidence["action"] = "discarded_uncommitted_input"
        elif (before[STATUS] == 2 and before[WORDS] == FRAME_BYTES // 4 and
              ack == (((before[CONSUME] + 1) & 0xFFFFFFFF) or 1)):
            record = struct.pack("<758I", *(io.read(BUFFER + 4 * i) for i in range(758)))
            sequence, _ = decode(record)
            if snapshot() != before:
                raise RuntimeError("Waveform orphan changed during validation")
            io.write(CONSUME, ack)
            evidence.update(action="consumed_valid_orphan", sequence=sequence,
                            frame_sha256=hashlib.sha256(record).hexdigest())
        else:
            raise RuntimeError("Waveform startup ownership is unresolved")
        io.barrier()
        self._wait(lambda: io.read(STATUS) == 1 and io.read(WORDS) == 0 and
                   io.read(CONSUME) == ack, "Waveform startup recovery was not acknowledged")
        after = snapshot()
        if (any(after[k] != before[k] for k in (ABI, LENGTH, COMMIT, ACK, COUNT, ERRORS))
                or snapshot() != after):
            raise RuntimeError("Waveform startup recovery changed unexpected state")
        evidence["after"] = after
        return evidence

    def transfer(self, record):
        sequence, _ = decode(record)
        io = self.io
        if io.read(ABI) != 1:
            raise RuntimeError("Waveform ABI mismatch")
        ack = io.read(ACK)
        if (io.read(STATUS) != 1 or io.read(COMMIT) != ack or
                io.read(CONSUME) != ack or io.read(WORDS) != 0):
            raise RuntimeError("Waveform buffer ownership unresolved; no automatic overwrite/abort")
        token = ((ack + 1) & 0xFFFFFFFF) or 1
        io.write(LENGTH, FRAME_BYTES)
        for index, word in enumerate(struct.unpack("<758I", record)):
            io.write(BUFFER + 4 * index, word)
        io.barrier()
        io.write(COMMIT, token)
        io.barrier()
        self._wait(lambda: io.read(ACK) == token and io.read(STATUS) == 2,
                   "Waveform return ACK timeout; buffer ownership retained")
        io.barrier()
        returned = struct.pack("<758I", *(io.read(BUFFER + 4 * i) for i in range(758)))
        io.barrier()
        if returned != record:
            raise ValueError("Waveform return bytes differ; buffer not consumed")
        returned_sequence, _ = decode(returned)
        if returned_sequence != sequence:
            raise ValueError("Waveform sequence differs; buffer not consumed")
        io.write(CONSUME, token)
        io.barrier()
        self._wait(lambda: io.read(CONSUME) == token and io.read(STATUS) == 1,
                   "Waveform consume ACK timeout; do not reuse buffer")
        return returned, {"sequence": sequence, "ack_token": token,
                          "accepted_count": io.read(COUNT), "error_count": io.read(ERRORS)}
