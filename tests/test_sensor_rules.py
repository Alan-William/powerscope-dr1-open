"""Guard cases and provenance for PS rules, independent of measured accuracy."""

import math
from pathlib import Path
import unittest
from ps.sensor_rules import SensorRules

CONFIG = Path(__file__).resolve().parents[1] / "configs/sensor_rules_v1.json"


def wave(flat_phase=None, value=0.2):
    return [
        (
            value
            if p == flat_phase
            else 0.7 * math.sin(2 * math.pi * i / 400 + p * 2 * math.pi / 3)
        )
        for p in range(3)
        for i in range(500)
    ]


class SensorRuleGuards(unittest.TestCase):
    def setUp(self):
        self.rules = SensorRules(CONFIG)

    def test_all_phases_and_signs(self):
        for p in range(3):
            for value in [-0.4, 0.4]:
                r = self.rules.diagnose(wave(p, value))
                self.assertEqual(r["candidate_label"], 16 + p)
            self.assertEqual(self.rules.diagnose(wave(p, 0))["candidate_label"], 13 + p)

    def test_abstain_when_not_identifiable(self):
        self.assertFalse(self.rules.diagnose(wave())["matched"])
        self.assertFalse(self.rules.diagnose([0.0] * 1500)["matched"])
        x = wave(0, 0.3)
        x[500:1000] = [0.3] * 500
        self.assertFalse(self.rules.diagnose(x)["matched"])
        middle = (
            self.rules.thresholds["zero_mean_max"]
            + self.rules.thresholds["stuck_mean_min"]
        ) / 2
        self.assertEqual(
            self.rules.diagnose(wave(1, middle))["reason"], "zero_stuck_ambiguous"
        )
        x = [v * 0.01 for v in wave(2, 0.3)]
        self.assertFalse(self.rules.diagnose(x)["matched"])

    def test_noise_and_step_do_not_become_exact_zero_test(self):
        x = wave(1, 0.2)
        for i in range(500):
            x[500 + i] += 0.003 * math.sin(i * 1.7)
        self.assertEqual(self.rules.diagnose(x)["candidate_label"], 17)
        x[500:750] = [-0.2] * 250
        self.assertFalse(self.rules.diagnose(x)["matched"])

    def test_invalid_input_rejected(self):
        for bad in [
            wave()[:-1],
            [float("nan")] * 1500,
            [float("inf")] * 1500,
            [2.0] * 1500,
        ]:
            with self.assertRaises(ValueError):
                self.rules.diagnose(bad)


if __name__ == "__main__":
    unittest.main()
