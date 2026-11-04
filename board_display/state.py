"""Qt-independent display validation. Compatible with board Python 3.8."""
import math

from ps.network_input import display_metadata
from protocol.labels import LABELS


def channels(value, normalized=False):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("Expected three current channels")
    size = len(value[0]) if isinstance(value[0], list) else 0
    if size not in (100, 125, 500):
        raise ValueError("Unexpected waveform length")
    result = []
    for row in value:
        if not isinstance(row, list) or len(row) != size:
            raise ValueError("Current channel lengths differ")
        if any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > (1.0001 if normalized else 1e6) for v in row):
            raise ValueError("Invalid current samples")
        result.append(list(row))
    return result


def current_rms(currents):
    # Match the MATLAB console: most recent 20 ms (one 50 Hz period),
    # avoiding phase-dependent RMS over the 25 ms classification window.
    count = len(currents[0]) * 4 // 5
    return [math.sqrt(sum(v*v for v in p[-count:])/count) for p in currents]


def from_board(state):
    """Only consume an atomic latest record, never join separate frame/result fields."""
    if state.get("model") != "sfaug-r1" or state.get("sensor_rules", {}).get("enabled") is not False:
        raise ValueError("显示需要 sfaug-r1 纯 NPU 服务")
    row = state.get("latest")
    if not row:
        return None
    seq, label = row.get("sequence"), row.get("npu_label")
    if (type(seq) is not int or not 0 <= seq <= 0xFFFFFFFF or type(label) is not int
            or not 0 <= label < len(LABELS) or row.get("label") != label
            or row.get("diagnosis_source") != "npu" or row.get("model_id") != "sfaug-r1"
            or not state.get("run_id") or row.get("run_id") != state["run_id"]):
        raise ValueError("诊断帧与当前纯 NPU 会话不一致")
    box = row.get("mailbox", {})
    if (box.get("sequence") != seq or box.get("label") != label
            or type(box.get("flags")) is not int or box["flags"] & 3 != 3
            or box.get("error") != 0 or row.get("axi", {}).get("physical") is not True
            or row.get("sample_transport", {}).get("physical") is not True):
        raise ValueError("当前帧没有匹配的物理 PL / NPU / AXI 回执")
    currents = channels(row.get("waveform"), normalized=True)
    network = row.get("network_input") or {}
    metadata = display_metadata(network)
    unit, scale, sim_time = ("A", metadata["scale_a"], metadata["sim_time_s"]) if metadata else ("p.u.", 1, None)
    currents = [[v * scale for v in phase] for phase in currents]
    score, inference = row.get("npu_score"), row.get("inference_us")
    if (type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1
            or type(inference) is not int or inference < 0):
        raise ValueError("Invalid NPU metrics")
    if box.get("inference_us") != inference or box.get("confidence_q") != round(score * 10000):
        raise ValueError("NPU 指标与 AXI 帧不一致")
    return dict(key=(state.get("session"), row["run_id"], seq), sequence=seq,
                currents=currents, unit=unit, sim_time_s=sim_time, window_ms=25.0,
                input_id=network.get("input_id"), label=label, label_name=LABELS[label],
                inference_us=inference, score=score, physical=True,
                sha256=network.get("npu_input_sha256"),
                rms=current_rms(currents))


def from_simulink(state):
    """Local preview exposes actual Simulink currents, without any diagnosis."""
    if state.get("engine") != "simulink":
        raise ValueError("预览数据源需要 MATLAB / Simulink")
    row = state.get("latest")
    if not row:
        return None
    currents = channels(row.get("measured"))
    return dict(key=(state.get("session"), row["window_id"]), sequence=row["window_id"],
                currents=currents, unit="A", sim_time_s=row["sim_time_s"], window_ms=25.0,
                input_id=None, label=None, label_name=None, inference_us=None,
                score=None, physical=False, sha256=row.get("sha256"),
                rms=current_rms(currents))
