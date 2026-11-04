"""Run real RTL simulation against frozen Python wire references; never accesses hardware."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from protocol.modbus import frame, reply  # noqa: E402
from protocol.replay_frame import decode  # noqa: E402


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def packed(data, bits):
    return f"{bits}'h{int.from_bytes(data, 'little'):x}"


def vectors(work):
    base = json.loads((ROOT / "fixtures/student/wire_vectors.json").read_text())
    rules = json.loads((ROOT / "fixtures/student/rule_wire_vectors.json").read_text())
    packets = [case["packet"] for d in (base, rules) for case in d["result_cases"]]
    lines = []

    def publish(p):
        result = [f"write_reg(16'h{a:04x},32'h{v:08x});" for a, v in p["pending_writes"]]
        return result + ["token=token+1; write_reg('h1c,token);"]

    def transaction(req, response):
        response = response or b""
        return f"transaction({packed(req, 2048)},{len(req)},{packed(response, 200)},{len(response)});"

    for p in packets:
        lines += publish(p)
        lines.append(transaction(bytes.fromhex(p["rtu_request_hex"]), bytes.fromhex(p["rtu_expected_response_hex"])))
    p = packets[0]
    lines += publish(p)
    for case in base["request_cases"]:
        lines.append(transaction(bytes.fromhex(case["request_hex"]), bytes.fromhex(case["response_hex"]) if case["response_hex"] else None))
    # Additional independently encoded requests: subranges, overflow, maximum quantity,
    # short frames, a 256-byte unsupported-function ADU and a wrong-address bad CRC.
    additional = [frame(struct.pack(">BBHH", 1, 3, start, count)) for start, count in [(9, 1), (2, 4), (65535, 2), (0, 126), (0, 125)]]
    additional += [b"\x01\x03", frame(b"\x01\x03\x00"), frame(bytes([1, 4]) + bytes(252)), bytes.fromhex("02030000000ac5ff")]
    for req in additional:
        lines.append(transaction(req, reply(req, p["registers"])))
    # Check the documented counter policy before transport framing-error tests.
    all_requests = [(bytes.fromhex(x["request_hex"]), bytes.fromhex(x["response_hex"]) if x["response_hex"] else None) for x in base["request_cases"]]
    all_requests += [(req, reply(req, p["registers"])) for req in additional]
    valid_count = len(packets) + sum(response is not None for _, response in all_requests)
    lines += [f"if(dut.mailbox.snap_valid!={valid_count} || dut.mailbox.snap_crc!=1) $fatal(1,\"RX counters\");"]
    (work / "rtu_vectors.svh").write_text("\n".join(lines))
    atomic = publish(p) + ["captured_count=0;"]
    atomic += [f"send_byte(8'h{x:02x},0,0);" for x in bytes.fromhex(p["rtu_request_hex"])]
    atomic += ["wait(de);", *publish(packets[1]), "wait_reply(25);",
               f"check_reply({packed(bytes.fromhex(p['rtu_expected_response_hex']),200)},25);",
               "repeat(T35+30) @(negedge clk);",
               transaction(bytes.fromhex(packets[1]["rtu_request_hex"]), bytes.fromhex(packets[1]["rtu_expected_response_hex"]))]
    (work / "rtu_atomic.svh").write_text("\n".join(atomic))
    template = bytes.fromhex(base["waveform_proposal"]["hex"])
    with (work / "waveforms.hex").open("w") as output:
        for i in range(1100):
            record = bytearray(template)
            struct.pack_into("<I", record, 8, (i - 2) & 0xFFFFFFFF)
            struct.pack_into("<I", record, 28, zlib.crc32(record[:28] + record[32:]))
            # One intentionally corrupted CRC must be returned unchanged and rejected by PS.
            if i == 1099:
                record[28] ^= 1
            output.write("".join(f"{word:08x}\n" for word in struct.unpack("<758I", record)))
    return {"golden_result_cases": len(packets), "golden_request_cases": len(base["request_cases"]),
            "extra_request_cases": len(additional), "waveform_frames": 1100}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iverilog", default="iverilog")
    parser.add_argument("--vvp", default=None)
    parser.add_argument("--work-root", type=Path, default=Path("E:/fpga_workflow_runs"))
    parser.add_argument("--bench", choices=["all", "core", "rtu", "router", "headless"], default="all")
    args = parser.parse_args()
    ivl = Path(shutil.which(args.iverilog) or args.iverilog).resolve(strict=True)
    vvp = Path(args.vvp) if args.vvp else ivl.with_name("vvp.exe" if os.name == "nt" else "vvp")
    args.work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="powerscope-rtl-", dir=args.work_root))
    if not str(work).isascii():
        raise ValueError("Choose an ASCII simulator working directory")
    shutil.copytree(ROOT / "pl", work / "pl")
    metadata = vectors(work)
    env = dict(os.environ, PATH=str(ivl.parent) + os.pathsep + os.environ["PATH"])
    report = {"schema": "powerscope-rtl-tests-v1", "status": "running", "hardware_accessed": False,
              "work": str(work), "vector_inventory": metadata, "benches": {},
              "sources": {str(p.relative_to(ROOT)): sha(p) for p in (ROOT / "pl").rglob("*") if p.is_file()},
              "fixtures": {str(p.relative_to(ROOT)): sha(p) for p in (ROOT / "fixtures/student").glob("*wire_vectors.json")},
              "limitations": ["RTL simulation only; no physical AXI, RS485, NPU coexistence or 30-minute joint acceptance.",
                              "Core watchdog test uses accelerated 1 MHz / 5 ms parameters; RTU uses production 75 MHz divisors."]}
    start = time.monotonic()
    try:
        for bench in (["core", "rtu", "router", "headless"] if args.bench == "all" else [args.bench]):
            source_bench = "router" if bench == "headless" else bench
            tb = work / "pl/sim" / f"tb_{source_bench}.sv"
            if not tb.is_file():
                raise FileNotFoundError(tb)
            output = work / f"tb_{bench}.vvp"
            command = [str(ivl), "-g2012", "-Wall", "-s", f"tb_{source_bench}", "-I", str(work), "-o", str(output)]
            if bench == "headless":
                command += ["-Ptb_router.HEADLESS=1"]
            command += [str(p) for p in sorted((work / "pl/rtl").glob("*.v"))] + [str(tb)]
            result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=120)
            (work / f"{bench}-compile.log").write_text(result.stdout + result.stderr)
            if result.returncode:
                raise RuntimeError(f"Compile {bench} (exit {result.returncode}): {result.stdout}{result.stderr}")
            begin = time.monotonic()
            result = subprocess.run([str(vvp), str(output)], cwd=work, env=env, capture_output=True, text=True, timeout=600)
            (work / f"{bench}-run.log").write_text(result.stdout + result.stderr)
            print(result.stdout, flush=True)
            if result.returncode or "PASS" not in result.stdout:
                raise RuntimeError(f"Simulation {bench}: {result.stdout}{result.stderr}")
            report["benches"][bench] = {"passed": True, "seconds": time.monotonic() - begin, "log_sha256": sha(work / f"{bench}-run.log")}
            if bench == "core":
                if (work / "waveforms.hex").read_bytes() != (work / "waveforms-returned.hex").read_bytes():
                    raise AssertionError("RTL waveform return bytes differ")
                words = [int(w, 16) for w in (work / "waveforms-returned.hex").read_text().split()]
                for i in range(1100):
                    record = struct.pack("<758I", *words[i * 758:(i + 1) * 758])
                    try:
                        sequence, _ = decode(record)
                    except ValueError:
                        if i != 1099:
                            raise
                    else:
                        if i == 1099 or sequence != (i - 2) & 0xFFFFFFFF:
                            raise AssertionError("CRC rejection / order mismatch")
                report["waveform_bytes_equal"] = True
                report["corrupt_crc_returned_unchanged_and_rejected_by_ps"] = True
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error=str(exc))
        raise
    finally:
        report["seconds"] = time.monotonic() - start
        (work / "result.json").write_text(json.dumps(report, indent=2))
        print(f"Result: {work / 'result.json'}", flush=True)


if __name__ == "__main__":
    main()
