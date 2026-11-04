"""Audit an actual 21 s automatic Simulink capture without rerunning a model."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pc_monitor.simulink import HEADER, SAMPLES, decode_packet, verify_build
from pc_monitor.monitor import utc_now


def audit(session, runtime, research):
    folder = ROOT / "artifacts/simulink-live-20261103"
    records = folder / "sessions"
    build = verify_build(runtime)
    for filename in ("npc_diag.slx", "ctrl_npc.m", "params_npc.m", "sensor_fault.m"):
        digest = hashlib.sha256((research / "sim" / filename).read_bytes()).hexdigest()
        assert digest == build["source"][filename.replace(".", "_")], filename
    result = json.loads((records / (session + ".result.json")).read_text(encoding="utf-8"))
    matlab = json.loads((folder / (session + ".matlab.json")).read_text(encoding="utf-8"))
    assert result["status"] == "stopped" and not result["error"]
    assert result["dropped_windows"] == result["invalid_packets"] == 0
    assert result["received_end_packet"] and matlab["success"]
    assert matlab["measured_export_max_abs"] == 0
    raw = (records / (session + ".windows.bin")).read_bytes()
    size = HEADER.size + SAMPLES.size
    assert len(raw) == 840 * size
    kcl = passthrough = offset_error = 0.0
    stuck = []
    compute = []
    transitions = []
    boundary = []
    previous = None
    schedule = [0, 2, 0, 16, 0, 21, 0]
    for index in range(840):
        f = decode_packet(raw[index*size:(index+1)*size], bytes.fromhex(session))
        assert f["window_id"] == index + 1
        assert f["label"] == schedule[index // 120]
        label = f["label"]
        changed = label != previous
        if changed:
            transitions.append({"label": label, "at_s": f["applied_at_s"], "window": index+1})
        for point in range(500):
            kcl = max(kcl, abs(sum(f["actual"][p][point] for p in range(3))))
            for phase in range(3):
                measured, actual = f["measured"][phase][point], f["actual"][phase][point]
                if label == 16 and phase == 0:
                    if not (changed and point == 0):
                        stuck.append(measured)
                elif label == 21 and phase == 2:
                    error = abs(measured - actual - .2*f["nominal_peak_a"])
                    if changed and point == 0:
                        # The 50 us control update is applied after signal evaluation
                        # at the boundary. Preserve the genuine transition sample.
                        boundary.append({"window": index+1, "sample": point,
                                         "measured_minus_actual_A": measured-actual})
                    else:
                        offset_error = max(offset_error, error)
                else:
                    passthrough = max(passthrough, abs(measured-actual))
        compute.append(f["compute_ms"])
        previous = label
    assert kcl < 1e-4 and passthrough < 1e-6
    assert max(stuck)-min(stuck) < 1e-6 and offset_error < 1e-6
    return {"checked_at": utc_now(), "session": session, "success": True,
            "scope": "Actual Simulink plant, local loopback; no board connection",
            "windows": 840, "samples_per_phase": 420000,
            "simulation_s": 21, "stream_wall_s": f["wall_elapsed_s"],
            "stream_pace_ratio": 21/f["wall_elapsed_s"],
            "window_compute_median_ms": statistics.median(compute),
            "window_compute_p95_ms": sorted(compute)[int(.95*len(compute))],
            "all_window_kcl_max_A": kcl,
            "healthy_sensor_passthrough_max_A": passthrough,
            "stuck_current_range_A": max(stuck)-min(stuck),
            "stable_offset_max_error_A": offset_error,
            "offset_transition_samples": boundary, "transitions": transitions,
            "matlab_logging_comparison": {"samples_per_phase": 500,
                "location": "last complete window", "max_abs_A": matlab["measured_export_max_abs"]},
            "packet_sha256": hashlib.sha256(raw).hexdigest(),
            "build": build, "research_sources_unchanged": True, "receiver": result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session")
    parser.add_argument("--runtime", type=Path,
                        default=Path("E:/fpga_workflow_runs/powerscope-simulink-runtime"))
    parser.add_argument("--research", type=Path, default=Path("E:/电力电子故障检测/Xu2026-repro"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.session, args.runtime, args.research)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(output+"\n", encoding="utf-8")
    print(output)
