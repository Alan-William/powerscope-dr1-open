import unittest
from ps.scenario import FaultScenario


class OperatingScenarioTests(unittest.TestCase):
    def test_new_windows_and_wrap_without_changing_input_records(self):
        scenario = FaultScenario(list(range(22)) * 2, 10)
        self.assertEqual([scenario.select(10+i)[0] for i in range(3)], [0, 22, 0])
        scenario.request(8, 14)
        self.assertEqual(scenario.select(14)[0], 8)
        self.assertEqual(scenario.select(15)[0], 30)

    def test_wrong_prediction_is_preserved_and_not_counted_as_correct_response(self):
        s = FaultScenario(list(range(22)), 10)
        _, initial = s.select(10)
        s.observe(0, 1, 10.1, initial)
        s.request(16, 11)
        _, context = s.select(11.02)
        event = s.observe(13, 2, 11.08, context)
        self.assertEqual((event['kind'], event['label']), ('fault_detected', 13))
        self.assertIsNone(s.requests[-1]['first_match_ms'])
        event = s.observe(16, 3, 11.2, context)
        self.assertEqual(event['kind'], 'fault_changed')
        self.assertEqual(s.requests[-1]['command_to_result_ms'], 200)
        self.assertEqual(s.requests[-1]['input_to_result_ms'], 180)

    def test_inflight_frame_cannot_acknowledge_new_command(self):
        s = FaultScenario(list(range(22)), 0)
        _, old = s.select(0)
        s.request(1, .01)
        s.observe(1, 1, .06, old)
        self.assertIsNone(s.requests[-1]['first_match_sequence'])
        _, new = s.select(.075)
        s.observe(1, 2, .13, new)
        self.assertEqual(s.requests[-1]['first_match_sequence'], 2)
        self.assertEqual(s.requests[-1]['input_to_result_ms'], 55)

    def test_sustained_fault_and_recovery_events_are_not_repeated_each_frame(self):
        s = FaultScenario(list(range(22)), 0)
        _, c = s.select(0)
        kinds = [s.observe(y, i, i, c) for i,y in enumerate([0,0,4,4,4,0,0])]
        self.assertEqual([e['kind'] for e in kinds if e], ['initial','fault_detected','recovered'])
        self.assertEqual(s.event_count, 3)

    def test_reject_unavailable_classes_and_invalid_labels(self):
        with self.assertRaises(ValueError): FaultScenario([0,1], 0)
        s = FaultScenario(list(range(22)), 0)
        for label in [True, -1, 22, '1', None]:
            with self.assertRaises(ValueError): s.request(label, 1)
        self.assertEqual(s.request(0, 2)['id'], 1)

    def test_public_state_does_not_allow_mutating_tracker(self):
        s = FaultScenario(list(range(22)), 0)
        p = s.public()
        p['requests'][0]['label'] = 21
        self.assertEqual(s.requests[0]['label'], 0)


if __name__ == '__main__':
    unittest.main()
