"""Explicit hardware-only waveform transport probe; needs a verified UIO contract."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from protocol.replay_frame import FRAME_BYTES, decode  # noqa: E402
from protocol.waveform_mailbox import WaveformClient  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--mmio-lib", type=Path, required=True)
    p.add_argument("--frames", type=Path, required=True, help="Concatenated 3032-byte PSW1 records")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    config = json.loads(args.config.read_text())
    records = args.frames.read_bytes()
    if not records or len(records) % FRAME_BYTES:
        raise ValueError("Input must contain complete PSW1 records")
    # Validate every input before opening a hardware device.
    for offset in range(0, len(records), FRAME_BYTES):
        decode(records[offset:offset + FRAME_BYTES])
    from ps.waveform import UioWaveformRegisters
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as log:
        log.write(json.dumps({"event": "start", "mode": "uio", "access": "not_opened", "config": config,
                              "input_sha256": hashlib.sha256(records).hexdigest(),
                              "npu_executed": False}) + "\n")
        io = None
        try:
            io = UioWaveformRegisters(config, args.mmio_lib)
            client = WaveformClient(io)
            for index, offset in enumerate(range(0, len(records), FRAME_BYTES)):
                start = time.monotonic_ns()
                returned, status = client.transfer(records[offset:offset + FRAME_BYTES])
                log.write(json.dumps({"event": "frame", "physical": True, "index": index, "start_monotonic_ns": start,
                                      "end_monotonic_ns": time.monotonic_ns(), "bytes_equal": True,
                                      "returned_sha256": hashlib.sha256(returned).hexdigest(), **status}) + "\n")
                log.flush()
        except Exception as error:
            log.write(json.dumps({"event": "failure", "error": str(error)}) + "\n")
            raise
        finally:
            if io is not None:
                io.close()
        log.write(json.dumps({"event": "complete", "frames": len(records) // FRAME_BYTES}) + "\n")


if __name__ == "__main__":
    main()
