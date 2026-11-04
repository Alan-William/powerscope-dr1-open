"""Prepare an offline boot candidate for the one verified DR1 factory image.

Never accesses a board. Keep the existing FSBL/U-Boot, boot headers and padding;
replace only the fixed-size PL payload and its SHA256 field. The format was
checked against the supplied alfsbl_data.h / alfsbl_secure.c / partition loader
and every header/data checksum in the original image. Other image layouts,
authentication/encryption modes and failing timing results are rejected.
"""
from __future__ import annotations

import argparse
from array import array
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import tempfile
import zipfile
import zlib

try:
    from .build_pl_soc import verify_timing_clocks
    from .prepare_display_dtb import validate_display_dtb
except ImportError:
    from build_pl_soc import verify_timing_clocks
    from prepare_display_dtb import validate_display_dtb


ARCHIVE_SHA = "f754663eded613cf15ac2ceb241ac91533792d360c4fdb49ba4105d887ba70ae"
BOOT_SHA = "567e14124fa17137e964ebcadd4680d8349058beacd4070377ba0ac3fc8c2585"
DTB_SHA = "6dfa8303f5b06f101414adfae5597edd9e37002b15ee5476d2ae87e62f4dbf40"
DISPLAY_REVIEW_SHA = "4b37f1e1c0fd26b52939ab085fdf98a7ab021b7528952d33e9352fc17674b173"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def swap32(data):
    if len(data) % 4:
        raise ValueError("Expected complete 32-bit words")
    words = array("I", data)
    if words.itemsize != 4:
        raise ValueError("Unsupported native word size")
    words.byteswap()
    return words.tobytes()


def bit_payload(bit):
    end = bit.find(b"\n\n", 0, 4096)
    if end < 0:
        raise ValueError("Missing Anlogic bitstream header")
    header = bit[:end].decode("ascii")
    for expected in ("# Anlogic ASCII Bitstream", "# Architecture: dr1_90",
                     "# Package: DR1M90GEG400", "# File Format: Bitstream"):
        if expected not in header:
            raise ValueError(f"Unexpected bitstream format: {expected}")
    payload = bit[end + 2:]
    if len(payload) != 4051808:
        raise ValueError("Only the verified uncompressed DR1 payload size is supported")
    return payload


def partition_hash(data):
    # The verified image uses word-swapped input and output for hardware SHA256.
    return swap32(hashlib.sha256(swap32(data)).digest())


def validate_boot(boot):
    if len(boot) != 5179264:
        raise ValueError("Unexpected factory boot image length")
    def u32(offset):
        return struct.unpack_from("<I", boot, offset)[0]
    if u32(0x64) != 0x43474C41 or u32(0x68) or u32(0x8C) or u32(0x90):
        raise ValueError("Unexpected or protected boot image format")
    if u32(0x94) != 0x300 or u32(0x98) != 3:
        raise ValueError("Unexpected partition table")
    if zlib.crc32(boot[0x60:0x9C]) != u32(0x9C):
        raise ValueError("Boot header CRC mismatch")
    expected = ((960, 199936, 199888, 0x820),
                (200960, 4051840, 4051808, 0x8A0),
                (4252864, 926336, 926280, 0x820))
    partitions = []
    for index, (start, padded, extracted, attributes) in enumerate(expected):
        header = 0x300 + 64 * index
        if zlib.crc32(boot[header:header + 60]) != u32(header + 60):
            raise ValueError(f"Partition {index} header CRC mismatch")
        actual = (u32(header + 0x20), u32(header), u32(header + 4), u32(header + 0x24))
        if actual != (start, padded, extracted, attributes):
            raise ValueError(f"Unexpected partition {index} layout or security flags")
        if u32(header + 8) != padded + 64 or u32(header + 0x34):
            raise ValueError("Unexpected authentication/trailer layout")
        if u32(header + 12) != (header + 64 if index < 2 else 0):
            raise ValueError("Unexpected next partition header")
        hash_offset = u32(header + 0x2C)
        if hash_offset != start + padded:
            raise ValueError("Unexpected hash offset")
        if partition_hash(boot[start:start + padded]) != boot[hash_offset:hash_offset + 32]:
            raise ValueError(f"Partition {index} data hash mismatch")
        if boot[start + extracted:start + padded] != b"\xff" * (padded - extracted):
            raise ValueError("Unexpected partition padding")
        partitions.append({"index": index, "offset": start, "padded_bytes": padded,
                           "extracted_bytes": extracted, "hash_offset": hash_offset,
                           "payload_sha256": digest(boot[start:start + extracted])})
    return partitions


def replace_pl(boot, bit):
    parts = validate_boot(boot)
    pl = parts[1]
    payload = swap32(bit_payload(bit))
    changed = bytearray(boot)
    start, length, hash_offset = pl["offset"], pl["extracted_bytes"], pl["hash_offset"]
    changed[start:start + length] = payload
    changed[hash_offset:hash_offset + 32] = partition_hash(changed[start:start + pl["padded_bytes"]])
    changed = bytes(changed)
    validate_boot(changed)
    for begin, end in ((0, start), (start + length, hash_offset), (hash_offset + 32, len(boot))):
        if changed[begin:end] != boot[begin:end]:
            raise ValueError("Bytes outside the PL payload/hash changed")
    return changed, parts


def check_build(result_path, bit):
    result = json.loads(result_path.read_text(encoding="utf-8"))
    integration = result.get("integration", {})
    profile = integration.get("profile")
    if (result.get("status") != "compiled" or profile not in ("headless", "display")
            or integration.get("clock_plan") != "shared-axi"
            or integration.get("vendor_archive_sha256") != ARCHIVE_SHA):
        raise ValueError("Requires a compiled, verified headless/display shared-axi design")
    expected = result.get("bit_sha256") or result.get("files_sha256", {}).get("image.bit")
    if not expected or digest(bit) != expected:
        raise ValueError("Bitstream hash does not match the completed build")
    timing_path = result_path.parent / "products/timing.rpt"
    timing = timing_path.read_text(encoding="utf-8", errors="strict")
    metrics = {}
    for name, pattern in {"setup_wns_ns": r"SWNS:\s*([-+\d.]+)ns",
                          "hold_wns_ns": r"HWNS:\s*([-+\d.]+)ns",
                          "sta_coverage_percent": r"STA coverage\s*:\s*([\d.]+)%"}.items():
        match = re.search(pattern, timing)
        if not match:
            raise ValueError(f"Missing routed timing metric: {name}")
        metrics[name] = float(match[1])
    if "Confidence          : Routed" not in timing:
        raise ValueError("Requires final routed timing")
    if metrics["setup_wns_ns"] < 0 or metrics["hold_wns_ns"] < 0:
        raise ValueError(f"Timing is not closed: {metrics}")
    review_sha = None
    if metrics["sta_coverage_percent"] != 100:
        # One explicit engineering review, pinned to the bit, build and reports.
        # Never turn this into a generic percentage threshold for future builds.
        review_path = Path(__file__).resolve().parents[1]/"reports/hdmi-timing-review-20261104.json"
        if profile != "display" or digest(review_path.read_bytes()) != DISPLAY_REVIEW_SHA:
            raise ValueError("Unreviewed STA coverage")
        review = json.loads(review_path.read_text())
        if (review["bit_sha256"] != digest(bit) or review["result_sha256"] != digest(result_path.read_bytes())
                or review["timing_report_sha256"] != digest(timing_path.read_bytes())
                or review["timing_metrics"] != metrics):
            raise ValueError("Coverage review belongs to a different build")
        for path, expected_sha in review["evidence"].items():
            if digest(Path(path).read_bytes()) != expected_sha:
                raise ValueError("Coverage review evidence changed: " + path)
        review_sha = DISPLAY_REVIEW_SHA
    clocks = verify_timing_clocks(timing, "shared-axi", profile)
    # Guard against editing a staged SDC after the routed build was recorded.
    inputs = {name.replace("\\", "/"): value for name, value in integration.get("project_inputs_sha256", {}).items()}
    project = Path(integration["project"]).parent
    for name in ("src/constraint/timing.sdc", "src/constraint/io.adc", "design_0/hdl/design_0.v"):
        if digest((project/name).read_bytes()) != inputs.get(name):
            raise ValueError("A checked build input changed: " + name)
    return {"profile": profile, "clock_plan": "shared-axi",
            "result": str(result_path.resolve()), "result_sha256": digest(result_path.read_bytes()),
            "timing_report_sha256": digest(timing_path.read_bytes()), "timing_metrics": metrics,
            "timing_clocks": clocks, "bit_sha256": digest(bit), "timing_review_sha256": review_sha}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor-zip", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--build-result", type=Path)
    parser.add_argument("--bit", type=Path)
    parser.add_argument("--dtb", type=Path)
    parser.add_argument("--factory-dtb", type=Path, help="Verified source DTB, required for a display candidate")
    parser.add_argument("--work-root", type=Path, default=Path("E:/fpga_workflow_runs"))
    args = parser.parse_args()
    if not args.verify_only and not all((args.build_result, args.bit, args.dtb)):
        parser.error("Packing requires --build-result, --bit and --dtb")
    with args.vendor_zip.open("rb") as stream:
        archive_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    if archive_sha != ARCHIVE_SHA:
        raise ValueError("Unverified vendor archive")
    with zipfile.ZipFile(args.vendor_zip) as archive:
        boot = archive.read("boot/BOOT.bin")
        old_bit = archive.read("soc_prj/soc_prj_Runs/phy_1/soc_prj.bit")
    if digest(boot) != BOOT_SHA:
        raise ValueError("Factory image differs from the board's verified backup")
    reconstructed, parts = replace_pl(boot, old_bit)
    if reconstructed != boot:
        raise ValueError("Original-bit round trip did not reproduce the exact factory image")
    if args.verify_only:
        print(json.dumps({"status": "verified", "boot_sha256": BOOT_SHA,
                          "original_bit_roundtrip_byte_identical": True,
                          "partitions": parts, "hardware_accessed": False}, indent=2))
        return
    bit = args.bit.read_bytes()
    build = check_build(args.build_result, bit)
    dtb = args.dtb.read_bytes()
    if build["profile"] == "headless":
        if digest(dtb) != DTB_SHA:
            raise ValueError("Requires the independently validated headless DTB candidate")
        dtb_validation = {"profile": "headless", "candidate_sha256": DTB_SHA}
    else:
        if not args.factory_dtb:
            raise ValueError("A display image requires --factory-dtb for independent binary comparison")
        dtb_validation = validate_display_dtb(args.factory_dtb.read_bytes(), dtb)
    candidate, _ = replace_pl(boot, bit)
    args.work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="powerscope-boot-candidate-", dir=args.work_root))
    (work / "BOOT.bin").write_bytes(candidate)
    shutil.copy2(args.dtb, work / "system.dtb")
    manifest = {"status": "prepared_offline_only", "build": build, "base_boot_sha256": BOOT_SHA,
                "device_tree_validation": dtb_validation,
                "base_partitions": parts, "candidate_partitions": validate_boot(candidate),
                "files_sha256": {"BOOT.bin": digest(candidate), "system.dtb": digest(dtb)},
                "only_pl_payload_and_its_hash_changed": True, "fsbl_and_uboot_byte_identical": True,
                "boot_headers_and_padding_byte_identical": True, "hardware_accessed": False,
                "hardware_verified": False,
                "note": "Keep existing kernel and boot script. Requires deployment review, UIO binding and NPU/network/AXI/RS485 hardware acceptance."}
    (work / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Prepared: {work / 'manifest.json'}")


if __name__ == "__main__":
    main()
