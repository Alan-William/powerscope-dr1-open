"""Optional PS-only sensor rules. Decisions receive samples, never labels/indices.

Input is the 3x500 normalized float32 waveform BEFORE NPU padding/quantization.
Zero-output DF is a dataset convention: a sensor stuck at zero is ambiguous.
"""

import hashlib
import json
import math
from pathlib import Path


def features(samples):
    if len(samples) != 1500 or any(
        not math.isfinite(v) or abs(v) > 1.00001 for v in samples
    ):
        raise ValueError("Rules expect 1500 finite, jointly normalized samples")
    means, deviations = [], []
    for p in range(3):
        values = samples[p * 500 : (p + 1) * 500]
        mean = math.fsum(values) / 500
        means.append(mean)
        deviations.append(math.sqrt(math.fsum((v - mean) ** 2 for v in values) / 500))
    return {"mean": means, "std": deviations}


def decide(feature, thresholds):
    mean, std = feature["mean"], feature["std"]
    result = dict(
        matched=False,
        candidate_label=None,
        phase=None,
        kind=None,
        reason="no_low_variation_phase",
        features=feature,
    )
    low = [p for p in range(3) if std[p] <= thresholds["std_max"]]
    if len(low) != 1:
        if len(low) > 1:
            result["reason"] = "multiple_low_variation_phases"
        return result
    p = low[0]
    other = min(std[q] for q in range(3) if q != p)
    result["phase"] = "ABC"[p]
    if other < thresholds["other_std_min"] or std[p] / other > thresholds["ratio_max"]:
        result["reason"] = "insufficient_other_phase_activity"
        return result
    amplitude = abs(mean[p])
    if amplitude <= thresholds["zero_mean_max"]:
        label, kind = 13 + p, "near_zero_df_candidate"
    elif amplitude >= thresholds["stuck_mean_min"]:
        label, kind = 16 + p, "nonzero_constant_sf"
    else:
        result["reason"] = "zero_stuck_ambiguous"
        return result
    result.update(matched=True, candidate_label=label, kind=kind, reason=kind)
    return result


class SensorRules:
    def __init__(self, path):
        raw = Path(path).read_bytes()
        self.config = json.loads(raw)
        self.sha256 = hashlib.sha256(raw).hexdigest()
        c = self.config
        if (
            c.get("schema") != 1
            or c.get("id") != "sensor-flat-v1"
            or c.get("input") != "normalized_f32_3x500"
        ):
            raise ValueError("Unsupported sensor rule configuration")
        self.thresholds = c["thresholds"]
        names = {
            "std_max",
            "ratio_max",
            "other_std_min",
            "zero_mean_max",
            "stuck_mean_min",
        }
        if set(self.thresholds) != names or any(
            type(v) not in (int, float) or not math.isfinite(v) or not 0 < v < 1
            for v in self.thresholds.values()
        ):
            raise ValueError("Invalid rule thresholds")
        if (
            not self.thresholds["zero_mean_max"] < self.thresholds["stuck_mean_min"]
            or not self.thresholds["std_max"] < self.thresholds["other_std_min"]
        ):
            raise ValueError("Rule guard bands overlap")

    def public(self):
        return dict(
            enabled=True,
            id=self.config["id"],
            sha256=self.sha256,
            scope="PS waveform rules; NPU statistical input channels remain disabled",
        )

    def diagnose(self, samples):
        return decide(features(samples), self.thresholds)
