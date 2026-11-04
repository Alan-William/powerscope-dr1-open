"""Synthetic contract fixtures, never board evidence or displayed NPU predictions."""
import copy
import unittest
from board_display.state import from_board, from_simulink, current_rms
from protocol.labels import LABELS
from protocol.mailbox import packet
from pc_monitor.monitor import LABELS as MONITOR_LABELS


def state():
    p = packet(42, 16, .9, 800, npu=True)
    return dict(model="sfaug-r1", session="unit-only", sensor_rules={"enabled": False},
        run_id="unit-run", status="running", latest=dict(sequence=42, run_id="unit-run",
        label=16, npu_label=16, npu_score=.9, diagnosis_source="npu", model_id="sfaug-r1",
        inference_us=800, mailbox=p, axi={"physical": True}, sample_transport={"physical": True},
        waveform=[[.25]*500, [-.5]*500, [.125]*500], network_input=dict(input_id=7,
        display=dict(scale_a=8.0, sample_rate_hz=20000, sim_time_s=2.025))))


class DisplayContractTests(unittest.TestCase):
    def test_scale_time_and_result_share_one_frame(self):
        value = from_board(state())
        self.assertEqual(LABELS, MONITOR_LABELS)
        self.assertEqual(value["key"], ("unit-only", "unit-run", 42))
        self.assertEqual(value["currents"], [[2.0]*500, [-4.0]*500, [1.0]*500])
        self.assertEqual(value["rms"], [2., 4., 1.])
        self.assertEqual(value["unit"], "A")
        self.assertEqual(value["label_name"], "A 相传感器卡死")
        self.assertEqual(value["sim_time_s"], 2.025)

    def test_bad_or_nonphysical_result_is_not_displayed(self):
        original = state()
        for field, value in [("label", 0), ("run_id", "other"), ("diagnosis_source", "ps_rule"),
                             ("axi", {"physical": False}), ("waveform", [[float('nan')]*500]*3),
                             ("mailbox", packet(43, 16, .9, 800, npu=True)), ("npu_score", .7)]:
            item = copy.deepcopy(original)
            item["latest"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                from_board(item)
        original["sensor_rules"]["enabled"] = True
        with self.assertRaises(ValueError): from_board(original)

    def test_old_board_units_are_not_invented_and_idle_has_no_prediction(self):
        item = state()
        item["latest"]["network_input"].pop("display")
        value = from_board(item)
        self.assertEqual(value["unit"], "p.u.")
        self.assertEqual(value["rms"], [.25, .5, .125])
        self.assertIsNone(value["sim_time_s"])
        item["latest"] = None
        self.assertIsNone(from_board(item))

    def test_simulink_preview_never_promotes_input_labels_to_diagnosis(self):
        value = from_simulink(dict(engine="simulink", session="local", latest=dict(
            window_id=1, sim_time_s=.025, label=21, measured=[[1.]*125]*3)))
        self.assertIsNone(value["label"])
        self.assertIsNone(value["score"])
        self.assertIsNone(value["inference_us"])
        self.assertFalse(value["physical"])

    def test_rms_matches_the_console_twenty_millisecond_interval(self):
        self.assertEqual(current_rms([[10.]*100+[2.]*400]*3), [2.]*3)


if __name__ == "__main__":
    unittest.main()
