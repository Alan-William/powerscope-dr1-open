"""Read-only Modbus function-03 polling with raw, monotonic, session-scoped evidence.

Requires an explicitly supplied COM/device name and pyserial. Does not select a
port, change board state, or claim NPU/AXI acceptance based on serial traffic alone.
"""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from protocol.modbus import crc16, decode_response, read_request  # noqa: E402


def decode_record(raw):
    result = {"response_hex": raw.hex(), "timeout": len(raw) == 0,
              "crc_ok": len(raw) >= 4 and crc16(raw[:-2]) == int.from_bytes(raw[-2:], "little")}
    try:
        registers = decode_response(raw)
        result.update(registers=list(registers), sequence=registers[4] * 65536 + registers[5],
                      flags=registers[1], label=registers[2], confidence_q=registers[3],
                      inference_us=registers[6] * 65536 + registers[7],
                      application_error=registers[8] * 65536 + registers[9], valid_response=True)
    except ValueError as error:
        result.update(valid_response=False, decode_error=str(error))
    return result


def read_response(port, timeout):
    deadline = time.monotonic() + timeout
    data = bytearray()
    target = 3
    while len(data) < target:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        port.timeout = remaining
        block = port.read(target - len(data))
        if not block:
            break
        data.extend(block)
        if len(data) == 3:
            target = 5 if data[1] & 0x80 else min(255, 5 + data[2])
    return bytes(data)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", required=True, help="Confirmed USB-RS485 adapter, not the board PS console")
    p.add_argument("--duration", type=float, default=30)
    p.add_argument("--period", type=float, default=0.1)
    p.add_argument("--timeout", type=float, default=0.2)
    p.add_argument("--write-timeout", type=float, default=1.0,
                   help="Host USB-driver write bound, separate from the Modbus response deadline")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--stop-file", type=Path, help="Stop cleanly after the current query when this file appears")
    args = p.parse_args()
    if args.duration <= 0 or args.period < 0.025 or args.timeout <= 0 or args.write_timeout <= 0:
        raise ValueError("Positive duration/timeout and polling period >= 25 ms required")
    if args.stop_file and args.stop_file.exists():
        raise ValueError("Stop file already exists; use a fresh capture session")
    import serial
    request = read_request()
    session = uuid.uuid4().hex
    args.output.parent.mkdir(parents=True, exist_ok=True)
    totals = {"queries": 0, "valid_responses": 0, "timeouts": 0, "invalid_responses": 0}
    with args.output.open("x", encoding="utf-8") as log:
        log.write(json.dumps({"event": "start", "session": session, "port": args.port,
                              "baud": 115200, "format": "8E1", "slave": 1,
                              "period_seconds": args.period, "response_timeout_seconds": args.timeout,
                              "write_timeout_seconds": args.write_timeout,
                              "started_monotonic_ns": time.monotonic_ns()}) + "\n")
        operation = "open"
        pending = None
        try:
            with serial.Serial(args.port, 115200, bytesize=8, parity=serial.PARITY_EVEN,
                               stopbits=serial.STOPBITS_ONE, timeout=args.timeout,
                               write_timeout=args.write_timeout) as port:
                start = time.monotonic()
                while (time.monotonic() - start < args.duration and
                       not (args.stop_file and args.stop_file.exists())):
                    before = time.monotonic_ns()
                    pending = {"start_monotonic_ns": before, "request_hex": request.hex()}
                    operation = "drain_stale"
                    # Keep late/noise bytes as evidence; do not silently reinterpret them as a new reply.
                    stale = port.read(port.in_waiting) if port.in_waiting else b""
                    operation = "write_request"
                    written = port.write(request)
                    wrote = time.monotonic_ns()
                    if written != len(request):
                        raise IOError("Incomplete serial request write")
                    operation = "flush_request"
                    port.flush()
                    operation = "read_response"
                    raw = read_response(port, args.timeout)
                    after = time.monotonic_ns()
                    result = decode_record(raw)
                    totals["queries"] += 1
                    totals["valid_responses"] += int(result["valid_response"])
                    totals["timeouts"] += int(result["timeout"])
                    totals["invalid_responses"] += int(not result["valid_response"] and not result["timeout"])
                    log.write(json.dumps({"event": "query", "session": session,
                                          "start_monotonic_ns": before, "end_monotonic_ns": after,
                                          "roundtrip_us": (after - before) / 1000,
                                          "write_us": (wrote - before) / 1000,
                                          "request_hex": request.hex(), "stale_hex": stale.hex(), **result}) + "\n")
                    log.flush()
                    operation, pending = "wait", None
                    time.sleep(max(0.002, args.period - (after - before) / 1e9))
        except Exception as error:
            log.write(json.dumps({"event": "failure", "error": str(error), "operation": operation,
                                  "pending_request": pending, "monotonic_ns": time.monotonic_ns(),
                                  **totals}) + "\n")
            raise
        log.write(json.dumps({"event": "complete", "ended_monotonic_ns": time.monotonic_ns(),
                              "stop_reason": "stop_file" if args.stop_file and args.stop_file.exists() else "duration",
                              **totals}) + "\n")
    print(json.dumps(totals))


if __name__ == "__main__":
    main()
