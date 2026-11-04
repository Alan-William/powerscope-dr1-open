"""Evidence validator tests, not hardware acceptance or generated waveform data."""
import copy
import unittest

from protocol.mailbox import packet
from protocol.modbus import reply
from tools.correlate_pl_acceptance import correlate, network_summary


def row(sequence):
    p = packet(sequence, label=2, confidence=.8, inference_us=600, npu=True)
    return dict(run_id='run-a', backend='Alnpu', sequence=sequence, mailbox=p,
                axi=dict(mode='uio', physical=True, ack_token=sequence),
                sample_transport=dict(mode='pl-q15', physical=True, bytes_equal=True,
                                      sequence=sequence, ack_token=sequence,
                                      accepted_count=sequence, error_count=0, transfer_us=45000),
                dataset='validation', dataset_index=0, logits=[0, 0, 1], npu_label=2, label=2,
                elapsed_ms=sequence * 100, inference_us=600, ps_application_us=56000,
                correct=True, npu_correct=True)


def query(p, milliseconds):
    return dict(event='query', session='uart-a', request_hex=p['rtu_request_hex'],
                stale_hex='', timeout=False, valid_response=True, crc_ok=True,
                response_hex=p['rtu_expected_response_hex'], registers=p['registers'],
                end_monotonic_ns=milliseconds * 1000000, roundtrip_us=7000)


def capture(packets):
    return [dict(event='start', session='uart-a', baud=115200, format='8E1', slave=1),
            *(query(p, 100 * i) for i, p in enumerate(packets)),
            dict(event='complete', queries=len(packets), valid_responses=len(packets),
                 timeouts=0, invalid_responses=0)]


class CorrelationTests(unittest.TestCase):
    def setUp(self):
        self.rows = [row(i) for i in [1, 2, 3]]

    def test_sampling_can_repeat_and_skip_with_idle_at_edges(self):
        data = capture([packet(0), self.rows[0]['mailbox'], self.rows[0]['mailbox'],
                        self.rows[2]['mailbox'], packet(3)])
        result = correlate(self.rows, data, 'run-a', .1)
        self.assertEqual(result['unique_sequences_observed'], 2)
        self.assertEqual(result['unsampled_sequences'], 1)
        self.assertEqual(result['inactive_responses'], 2)

    def reset_packet(self):
        p = packet(0)
        p['registers'][1] = 0
        p['rtu_expected_response_hex'] = reply(bytes.fromhex(p['rtu_request_hex']), p['registers']).hex()
        return p

    def test_exact_boot_reset_requires_explicit_prefix_authorization(self):
        data = capture([self.reset_packet(), packet(0), *[r['mailbox'] for r in self.rows]])
        with self.assertRaisesRegex(ValueError, 'reset response'):
            correlate(self.rows, data, 'run-a', .1)
        result = correlate(self.rows, data, 'run-a', .1, allow_reset_prefix=True)
        self.assertEqual(result['reset_prefix_responses'], 1)
        self.assertEqual(result['inactive_responses'], 1)
        self.assertEqual(result['matched_native_responses'], 3)

    def test_reset_after_idle_or_active_and_inexact_reset_are_rejected(self):
        reset = self.reset_packet()
        for packets in [[packet(0), reset, *[r['mailbox'] for r in self.rows]],
                        [self.rows[0]['mailbox'], reset, self.rows[2]['mailbox']],
                        [*[r['mailbox'] for r in self.rows], reset]]:
            with self.assertRaisesRegex(ValueError, 'reset response'):
                correlate(self.rows, capture(packets), 'run-a', .1, allow_reset_prefix=True)
        reset['registers'][5] = 1
        reset['rtu_expected_response_hex'] = reply(bytes.fromhex(reset['rtu_request_hex']), reset['registers']).hex()
        with self.assertRaisesRegex(ValueError, 'reset response'):
            correlate(self.rows, capture([reset, *[r['mailbox'] for r in self.rows]]),
                      'run-a', .1, allow_reset_prefix=True)

    def test_valid_crc_does_not_hide_torn_fields(self):
        torn = packet(2, label=3, confidence=.8, inference_us=600, npu=True)
        data = capture([self.rows[0]['mailbox'], torn, self.rows[2]['mailbox']])
        with self.assertRaisesRegex(ValueError, 'fields differ'):
            correlate(self.rows, data, 'run-a', .1)

    def test_inactive_response_inside_run_is_rejected(self):
        data = capture([self.rows[0]['mailbox'], packet(1, error=5), self.rows[2]['mailbox']])
        with self.assertRaisesRegex(ValueError, 'between active'):
            correlate(self.rows, data, 'run-a', .1)

    def test_raw_crc_and_stale_bytes_are_checked_independently(self):
        for change in [dict(stale_hex='ff'), dict(response_hex='010300000000')]:
            with self.subTest(change=change):
                data = capture([r['mailbox'] for r in self.rows])
                data[2].update(change)
                with self.assertRaises(ValueError):
                    correlate(self.rows, data, 'run-a', .1)

    def test_wrong_run_ack_gap_and_nondeterministic_npu_are_rejected(self):
        for kind in ['run_id', 'ack', 'logits']:
            with self.subTest(kind=kind):
                rows = copy.deepcopy(self.rows)
                if kind == 'run_id':
                    rows[1]['run_id'] = 'another-run'
                elif kind == 'ack':
                    rows[1]['sample_transport']['ack_token'] = 99
                else:
                    rows[1]['logits'][2] = 2
                with self.assertRaises(ValueError):
                    correlate(rows, capture([r['mailbox'] for r in rows]), 'run-a', .1)

    def test_incomplete_capture_short_duration_and_reverse_order_fail(self):
        data = capture([r['mailbox'] for r in self.rows])
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            correlate(self.rows, data[:-1], 'run-a', .1)
        with self.assertRaisesRegex(ValueError, 'shorter'):
            correlate(self.rows, data, 'run-a', 1800)
        with self.assertRaisesRegex(ValueError, 'backward'):
            correlate(self.rows, capture([r['mailbox'] for r in reversed(self.rows)]), 'run-a', .1)

    def test_network_requires_same_session_and_complete_counts(self):
        states = [dict(event='state', ok=True, session='ps-a', run_id='run-a',
                       status='running', error=None, processed=i,
                       end_monotonic_ns=i * 1000000000, response_us=20000)
                  for i in range(3)]
        data = states + [dict(event='complete', requests=3, failures=0)]
        self.assertEqual(network_summary(data, 'ps-a', 'run-a', 1)['requests'], 3)
        states[1]['session'] = 'ps-b'
        with self.assertRaisesRegex(ValueError, 'identity'):
            network_summary(data, 'ps-a', 'run-a', 1)


if __name__ == '__main__':
    unittest.main()
