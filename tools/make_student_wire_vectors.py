"""Produce small wire fixtures from a real PS validation run, never raw datasets."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from protocol.mailbox import packet
from protocol.modbus import frame, reply
from protocol.replay_frame import encode


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--results", type=Path, required=True)
    p.add_argument(
        "--output", type=Path, default=Path("fixtures/student/wire_vectors.json")
    )
    a = p.parse_args()
    rows = [json.loads(x) for x in a.results.read_text().splitlines()]
    selected = [next(r for r in rows if r["expected"] == c) for c in range(22)]
    cases = []
    for r in selected:
        assert r["backend"] == "Alnpu" and r["simulated"] and r["mailbox"]["flags"] == 7
        cases.append(
            dict(
                name="first_validation_class_" + str(r["expected"]),
                dataset_index=r["dataset_index"],
                expected=r["expected"],
                actual_npu_label=r["label"],
                packet=r["mailbox"],
            )
        )
    for name, spec in [
        ("idle", packet(0)),
        ("npu_error", packet(7, error=1)),
        ("sequence_uint32_max", packet(0xFFFFFFFF, 21, 1, 0xFFFFFFFF, npu=True)),
    ]:
        cases.append(dict(name=name, packet=spec))
    requests = [
        ("valid_read", "01030000000ac5cd"),
        ("bad_crc", "01030000000ac5cc"),
        ("wrong_slave", frame(bytes.fromhex("02030000000a")).hex()),
        ("broadcast_read", frame(bytes.fromhex("00030000000a")).hex()),
        ("zero_count", frame(bytes.fromhex("010300000000")).hex()),
        ("out_of_bounds", frame(bytes.fromhex("010300090002")).hex()),
        ("unsupported_function", frame(bytes.fromhex("01040000000a")).hex()),
    ]
    regs = cases[0]["packet"]["registers"]
    requests = [
        dict(
            name=n,
            request_hex=h,
            response_hex=(
                None
                if reply(bytes.fromhex(h), regs) is None
                else reply(bytes.fromhex(h), regs).hex()
            ),
        )
        for n, h in requests
    ]
    wave = encode(0x12345678, [0.0, 1.0, -1.0, 0.5, -0.5] * 300)
    result = dict(
        schema=1,
        result_mailbox_abi=1,
        model="kd-s1",
        statistics=False,
        source_results_sha256=hashlib.sha256(a.results.read_bytes()).hexdigest(),
        evidence="Measured NPU classifications; expected wire bytes only. Physical AXI/RS485 NOT tested.",
        result_cases=cases,
        request_cases=requests,
        waveform_proposal=dict(
            status="codec only; hardware map and transport not integrated",
            pattern="[0,1,-1,0.5,-0.5] repeated 300 times",
            bytes=len(wave),
            sha256=hashlib.sha256(wave).hexdigest(),
            hex=wave.hex(),
        ),
    )
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(a.output, a.output.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
