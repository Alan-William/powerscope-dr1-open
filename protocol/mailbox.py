"""ABI 1 result contract; no physical address is assumed or opened here."""

import math
import time
from protocol.modbus import reply, read_request

ABI = 1
VALID, NPU_EXECUTED, SIMULATED = 1, 2, 4
ERROR_NONE, ERROR_NPU, ERROR_STORAGE, ERROR_AXI, ERROR_INPUT = 0, 1, 2, 3, 4


def packet(sequence, label=None, confidence=0.0, inference_us=0, error=0, npu=False):
    for value in (sequence, inference_us, error):
        if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
            raise ValueError("Expected uint32")
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("Invalid model score")
    valid = label is not None and error == 0
    if label is not None and (type(label) is not int or not 0 <= label < 22):
        raise ValueError("Invalid label")
    flags = SIMULATED | (VALID if valid else 0) | (NPU_EXECUTED if valid and npu else 0)
    label = label if valid else 0xFFFF
    confidence_q = int(round(confidence * 10000)) if valid else 0
    regs = [
        ABI,
        flags,
        label,
        confidence_q,
        sequence >> 16,
        sequence & 65535,
        inference_us >> 16,
        inference_us & 65535,
        error >> 16,
        error & 65535,
    ]
    writes = [
        (0x08, sequence),
        (0x0C, (label << 16) | flags),
        (0x10, confidence_q),
        (0x14, inference_us),
        (0x18, error),
    ]
    return {
        "sequence": sequence,
        "flags": flags,
        "label": label,
        "confidence_q": confidence_q,
        "inference_us": inference_us,
        "error": error,
        "registers": regs,
        "pending_writes": writes,
        "rtu_request_hex": read_request().hex(),
        "rtu_expected_response_hex": reply(read_request(), regs).hex(),
    }


class MailboxClient:
    """For a register backend with real device barriers, or a test-only fake.

    Commit token is derived from hardware ACK, including after a PS restart.
    A timeout leaves ownership unresolved and must not be retried by overwriting.
    """

    def __init__(self, io, timeout=0.05):
        self.io, self.timeout = io, timeout

    def commit(self, p):
        io = self.io
        if io.read(0) != ABI:
            raise RuntimeError("AXI ABI mismatch")
        ack = io.read(0x20)
        current = io.read(0x1C)
        if not io.read(4) & 1 or ack != current:
            raise TimeoutError("Previous AXI commit not acknowledged")
        token = (ack + 1) & 0xFFFFFFFF
        if token == 0:
            token = 1
        for offset, value in p["pending_writes"]:
            io.write(offset, value)
        io.barrier()
        io.write(0x1C, token)
        io.barrier()
        deadline = time.monotonic() + self.timeout
        while io.read(0x20) != token:
            if time.monotonic() >= deadline:
                raise TimeoutError("AXI ACK timeout")
            time.sleep(0.0002)
        return {"ack_token": token, "snapshot": self.snapshot()}

    def snapshot(self):
        io = self.io
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            first = io.read(0x30)
            io.barrier()
            values = [io.read(o) for o in (0x24, 0x28, 0x2C)]
            io.barrier()
            last = io.read(0x30)
            if first == last and not first & 1:
                return {
                    "token": last,
                    "rx_valid": values[0],
                    "rx_crc_errors": values[1],
                    "last_request": values[2],
                }
        raise TimeoutError("AXI snapshot unstable")
