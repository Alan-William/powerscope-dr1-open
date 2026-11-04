"""Build and compare the HDMI DTB against this DR1's verified factory tree.

Offline only. Rejects changes beyond camera status, the two UIO windows and
the explicitly selected HDMI clock. Parses the binary result independently of
the source text, including memory reservations and the boot CPU identifier.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FACTORY_DTB_SHA = "e1ddd7405245d7b9f644c64066bda64369edbac0d8e8676564261d9cfc22b979"


def fdt_contents(data):
    header = struct.unpack_from(">10I", data)
    magic, total, off_struct, off_strings, off_rsv, version, compatible, cpu, nstrings, nstruct = header
    if magic != 0xD00DFEED or total != len(data) or version != 17 or compatible != 16:
        raise ValueError("Unexpected FDT header")
    if off_struct + nstruct > total or off_strings + nstrings > total or off_rsv < 40:
        raise ValueError("FDT section out of bounds")
    strings = data[off_strings:off_strings+nstrings]
    reserve, pos = [], off_rsv
    while True:
        pair = struct.unpack_from(">QQ", data, pos)
        pos += 16
        if pair == (0, 0):
            break
        reserve.append(pair)
        if pos >= off_struct:
            raise ValueError("Unterminated memory reservations")
    tree, stack = {}, []
    pos, end = off_struct, off_struct+nstruct
    while pos < end:
        token = struct.unpack_from(">I", data, pos)[0]
        pos += 4
        if token == 1:
            stop = data.index(b"\0", pos, end)
            name = data[pos:stop].decode("ascii")
            stack.append(name)
            path = "/" + "/".join(stack[1:])
            if path in tree:
                raise ValueError("Duplicate FDT node")
            tree[path] = {}
            pos = (stop+4) & ~3
        elif token == 2:
            if not stack:
                raise ValueError("Unbalanced FDT node")
            stack.pop()
        elif token == 3:
            size, offset = struct.unpack_from(">II", data, pos)
            pos += 8
            name = strings[offset:strings.index(b"\0", offset)].decode("ascii")
            path = "/" + "/".join(stack[1:])
            if not stack or pos+size > end or name in tree[path]:
                raise ValueError("Invalid FDT property")
            tree[path][name] = data[pos:pos+size]
            pos = (pos+size+3) & ~3
        elif token == 4:
            continue
        elif token == 9:
            if stack:
                raise ValueError("Unclosed FDT node")
            return tree, reserve, cpu
        else:
            raise ValueError("Unknown FDT token")
    raise ValueError("Missing FDT terminator")


def validate_display_dtb(source, candidate):
    if hashlib.sha256(source).hexdigest() != FACTORY_DTB_SHA:
        raise ValueError("Requires this board's verified factory DTB")
    before, reserved, cpu = fdt_contents(source)
    after, after_reserved, after_cpu = fdt_contents(candidate)
    expected = copy.deepcopy(before)
    expected["/soc/mipicam@80300000"]["status"] = b"disabled\0"
    expected["/soc/axi_dynclk@80101000"]["status"] = b"disabled\0"
    expected["/soc/axi_dynclk@80101000"]["expclk"] = struct.pack(">I", 148500000)
    expected["/soc/vdmafb@80100000/display-timings/timing1"]["clock-frequency"] = struct.pack(">I", 148500000)
    for suffix, short, addr, size in (("mailbox", "powerscope_mailbox", 0x80400000, 0x1000),
                                       ("waveform", "powerscope_waveform", 0x80401000, 0x2000)):
        expected[f"/soc/powerscope-{suffix}@{addr:x}"] = {
            "compatible": b"generic-uio\0", "linux,uio-name": short.encode()+b"\0",
            "reg": struct.pack(">4I", 0, addr, 0, size), "status": b"okay\0"}
    if after != expected or reserved != after_reserved or cpu != after_cpu:
        changed = [p for p in set(after)|set(expected) if after.get(p) != expected.get(p)]
        raise ValueError(f"Unexpected DTB changes: {changed}")
    for node in ("vdma@80200000", "vdmafb@80100000"):
        if after[f"/soc/{node}"]["status"] != b"okay\0":
            raise ValueError("Display node not enabled")
    return {"source_sha256": FACTORY_DTB_SHA, "candidate_sha256": hashlib.sha256(candidate).hexdigest(),
            "profile": "display", "hdmi_pixel_hz": 148500000,
            "hdmi_clock_mode": "compiled-pll-output-gate",
            "compiled_pixel_hz_ratio": [4900000000, 33],
            "all_other_node_properties_byte_identical": True,
            "memory_reservations_and_boot_cpu_preserved": True,
            "changed_properties": ["/soc/mipicam@80300000/status", "/soc/axi_dynclk@80101000/status",
                                   "/soc/axi_dynclk@80101000/expclk", "/soc/vdmafb@80100000/display-timings/timing1/clock-frequency"],
            "added_nodes": ["/soc/powerscope-mailbox@80400000", "/soc/powerscope-waveform@80401000"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--dtc", type=Path, default=Path("E:/fpga_workflow_runs/tools/dtc-1.8.1/ucrt64/bin/dtc.exe"))
    parser.add_argument("--work-root", type=Path, default=Path("E:/fpga_workflow_runs"))
    args = parser.parse_args()
    source = args.source.read_bytes()
    if hashlib.sha256(source).hexdigest() != FACTORY_DTB_SHA:
        raise ValueError("Unexpected source DTB")
    work = Path(tempfile.mkdtemp(prefix="powerscope-display-dtb-", dir=args.work_root))
    shutil.copy2(args.source, work/"source.dtb")
    for name in ("powerscope-uio.dtsi", "powerscope-display.dtsi"):
        shutil.copy2(ROOT/"pl/integration"/name, work/name)
    commands = [[str(args.dtc), "-I", "dtb", "-O", "dts", "-o", "source.dts", "source.dtb"],
                [str(args.dtc), "-I", "dts", "-O", "dtb", "-b", "0", "-i", str(work),
                 "-o", "display.dtb", "display.dts"]]
    with (work/"dtc.log").open("wb") as log:
        subprocess.run(commands[0], cwd=work, stdout=log, stderr=subprocess.STDOUT, check=True)
        (work/"display.dts").write_text((work/"source.dts").read_text()+'\n/include/ "powerscope-display.dtsi"\n')
        subprocess.run(commands[1], cwd=work, stdout=log, stderr=subprocess.STDOUT, check=True)
    report = validate_display_dtb(source, (work/"display.dtb").read_bytes())
    report.update(status="passed_offline_only", hardware_accessed=False, commands=commands,
                  source=str(args.source.resolve()), candidate=str(work/"display.dtb"))
    (work/"validation.json").write_text(json.dumps(report, indent=2)+"\n")
    print(work/"validation.json")


if __name__ == "__main__":
    main()
