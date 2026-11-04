"""Correlate one native NPU/UIO run with physical UART and Ethernet evidence.

Sequence numbers are scoped by the PS session and boot ID. Repeated or skipped
UART samples are allowed; torn fields, stale bytes and invalid active results
fail the check. Host and board monotonic clocks are never subtracted.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from protocol.modbus import decode_response, read_request  # noqa: E402


def require(condition, message):
    if not condition:
        raise ValueError(message)


def distribution(values):
    ordered = sorted(values)
    require(bool(ordered), 'No measurements')
    return dict(min=ordered[0], median=ordered[len(ordered) // 2],
                p99=ordered[min(len(ordered) - 1, int(len(ordered) * .99))], max=ordered[-1])


def correlate(rows, serial, run_id, minimum_seconds=1800, allow_reset_prefix=False):
    expected = {}
    first = last = previous = None
    inference, application, transfers, intervals = [], [], [], []
    templates = {}
    output_changes = label_changes = 0
    correct = npu_correct = 0
    for row in rows:
        require(row['run_id'] == run_id, 'Mixed PS run IDs')
        require(row['backend'] == 'Alnpu', 'Non-native inference')
        require(row['axi']['mode'] == 'uio' and row['axi']['physical'], 'Unverified result transport')
        link = row['sample_transport']
        require(link['mode'] == 'pl-q15' and link['physical'] and link['bytes_equal']
                and link['sequence'] == row['sequence'] and link['error_count'] == 0,
                'Invalid physical sample return')
        packet = row['mailbox']
        require(packet['flags'] == 7 and packet['error'] == 0 and packet['sequence'] == row['sequence'],
                'Invalid native result packet')
        require(row['sequence'] not in expected, 'Duplicate sequence in this PS run')
        if previous is not None:
            require(row['sequence'] == ((previous['sequence'] + 1) & 0xFFFFFFFF), 'PS sequence gap')
            for section in ['axi', 'sample_transport']:
                token = ((previous[section]['ack_token'] + 1) & 0xFFFFFFFF) or 1
                require(row[section]['ack_token'] == token, section + ' ACK discontinuity')
            require(link['accepted_count'] == ((previous['sample_transport']['accepted_count'] + 1)
                                               & 0xFFFFFFFF), 'Sample acceptance count gap')
            intervals.append((row['elapsed_ms'] - previous['elapsed_ms']) * 1000)
        key = (row['dataset'], row['dataset_index'])
        template = templates.setdefault(key, (row['logits'], row['npu_label'], row['label']))
        output_changes += int(template[0] != row['logits'])
        label_changes += int(template[1:] != (row['npu_label'], row['label']))
        expected[row['sequence']] = packet['registers']
        first = row['elapsed_ms'] if first is None else first
        last, previous = row['elapsed_ms'], row
        inference.append(row['inference_us'])
        application.append(row['ps_application_us'])
        transfers.append(link['transfer_us'])
        correct += int(row['correct'])
        npu_correct += int(row['npu_correct'])
    require(bool(expected), 'Empty native run')
    board_seconds = (last - first) / 1000
    require(board_seconds >= minimum_seconds, 'Board run shorter than required duration')
    require(output_changes == 0 and label_changes == 0, 'Repeated Q15 inputs changed NPU outputs')

    session = None
    complete = None
    count = matched = idle = 0
    reset_count = 0
    reset_prefix_open = allow_reset_prefix
    first_match = last_match = None
    pending_idle = 0
    observed = set()
    ordinal = {sequence: index for index, sequence in enumerate(expected)}
    last_ordinal = -1
    rtt = []
    writes = []
    labels = set()
    request = read_request().hex()
    for item in serial:
        if item['event'] == 'start':
            require(session is None, 'Multiple serial sessions')
            require(item['baud'] == 115200 and item['format'] == '8E1' and item['slave'] == 1,
                    'Wrong UART configuration')
            session = item['session']
        elif item['event'] == 'query':
            require(session is not None and item['session'] == session, 'Mixed serial sessions')
            require(complete is None, 'Serial query after completion')
            require(item['request_hex'] == request, 'Unexpected UART request')
            require(not item['stale_hex'] and not item['timeout'] and item['valid_response']
                    and item['crc_ok'], 'UART timeout, stale bytes or invalid response')
            registers = list(decode_response(bytes.fromhex(item['response_hex'])))
            require(registers == item['registers'], 'Decoded evidence differs from raw UART bytes')
            count += 1
            rtt.append(item['roundtrip_us'])
            if 'write_us' in item:
                writes.append(item['write_us'])
            sequence = registers[4] * 65536 + registers[5]
            if registers[1] == 0:
                require(reset_prefix_open and first_match is None and
                        registers == [1, 0, 0xFFFF, 0, 0, 0, 0, 0, 0, 0],
                        'Unexpected reset response outside the verified boot prefix')
                reset_count += 1
            elif registers[1] == 7:
                reset_prefix_open = False
                require(sequence in expected, 'UART sequence absent from this PS run')
                require(registers == expected[sequence], 'UART fields differ from the committed PS packet')
                require(ordinal[sequence] >= last_ordinal, 'UART result sequence went backward')
                require(pending_idle == 0, 'Invalid result occurred between active NPU responses')
                last_ordinal = ordinal[sequence]
                first_match = item['end_monotonic_ns'] if first_match is None else first_match
                last_match = item['end_monotonic_ns']
                observed.add(sequence)
                labels.add(registers[2])
                matched += 1
            else:
                reset_prefix_open = False
                require(registers[1] == 4 and registers[2] == 0xFFFF and registers[3] == 0
                        and registers[6:8] == [0, 0] and registers[8] == 0 and registers[9] in [0, 5],
                        'Unexpected inactive/error response')
                idle += 1
                if first_match is not None:
                    pending_idle += 1
        elif item['event'] == 'complete':
            require(complete is None, 'Duplicate serial completion')
            complete = item
        else:
            raise ValueError('Failed or unknown serial event: ' + item['event'])
    require(complete is not None and session is not None, 'Incomplete serial capture')
    require(complete['queries'] == count == complete['valid_responses']
            and complete['timeouts'] == complete['invalid_responses'] == 0, 'Serial totals disagree')
    require(matched > 0, 'No native results seen on UART')
    host_seconds = (last_match - first_match) / 1e9
    require(host_seconds >= minimum_seconds, 'Host UART observation shorter than required duration')
    return dict(status='correlated', run_id=run_id, serial_session=session,
                frames=len(expected), correct=correct, npu_correct=npu_correct,
                board_result_span_seconds=board_seconds, uart_native_span_seconds=host_seconds,
                queries=count, matched_native_responses=matched, inactive_responses=idle,
                reset_prefix_responses=reset_count,
                unique_sequences_observed=len(observed), unsampled_sequences=len(expected) - len(observed),
                labels_observed=sorted(labels), field_mismatches=0, unexpected_crc_errors=0,
                timeouts=0, stale_bytes=0, active_invalid_responses=0,
                repeated_input_logit_changes=output_changes, repeated_input_label_changes=label_changes,
                inference_us=distribution(inference), application_us=distribution(application),
                sample_transfer_us=distribution(transfers), result_interval_us=distribution(intervals),
                uart_roundtrip_us=distribution(rtt),
                usb_write_us=distribution(writes) if len(writes) == count else None)


def network_summary(records, session, run_id, minimum_seconds):
    states = []
    complete = None
    for item in records:
        if item['event'] == 'state':
            require(item['ok'] and item['session'] == session and item['run_id'] == run_id,
                    'Network failure or changed service/run identity')
            require(item['status'] in ['running', 'finalizing', 'completed'] and item['error'] is None,
                    'Network observed a failed run')
            states.append(item)
        elif item['event'] == 'complete':
            complete = item
    require(complete is not None and complete['failures'] == 0
            and complete['requests'] == len(states) and len(states) > 1, 'Incomplete network monitoring')
    span = (states[-1]['end_monotonic_ns'] - states[0]['end_monotonic_ns']) / 1e9
    require(span >= minimum_seconds, 'Network observation shorter than required duration')
    require(all(b['processed'] >= a['processed'] for a, b in zip(states, states[1:])),
            'Network saw progress go backward')
    return dict(requests=len(states), failures=0, observation_seconds=span,
                response_us=distribution([s['response_us'] for s in states]),
                transport='HTTP through SSH over board Ethernet; not a throughput benchmark')


def jsonlines(path):
    with path.open(encoding='utf-8') as source:
        for line in source:
            yield json.loads(line)


def interrupt_count(path, label):
    matches = [line.split() for line in path.read_text().splitlines() if line.endswith(label)]
    require(len(matches) == 1, 'Expected one IRQ row for ' + label)
    counts = []
    for field in matches[0][1:]:
        if not field.isdigit():
            break
        counts.append(int(field))
    require(bool(counts), 'No CPU interrupt counts for ' + label)
    return sum(counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['rows', 'run-status', 'serial', 'network', 'before', 'after', 'output',
                 'interrupts-before', 'interrupts-after']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    status = json.loads(args.run_status.read_text())
    before = json.loads(args.before.read_text())
    after = json.loads(args.after.read_text())
    require(status['status'] == 'completed_rows_verified' and status['phase'] == 'endurance',
            'Native endurance did not complete')
    require(status['boot_id'] == before['boot_id'] == after['boot_id'], 'Board restarted')
    require(status['session'] == before['state']['session'], 'PS session changed')
    require(len(status['runs']) == 1, 'Expected one run')
    run_id = status['runs'][0]['run_id']
    counters_before = before.get('snapshot') or before['state']['axi']['snapshot']
    allow_reset_prefix = (before.get('ack_token') == 0 and before['state']['status'] == 'idle'
                          and counters_before == dict(token=0, rx_valid=0, rx_crc_errors=0, last_request=0))
    summary = correlate(jsonlines(args.rows), jsonlines(args.serial), run_id,
                        allow_reset_prefix=allow_reset_prefix)
    require(summary['frames'] == status['runs'][0]['analysis']['frames'], 'Native row count mismatch')
    require(summary['labels_observed'] == list(range(22)), 'UART did not observe all 22 classes')
    counters_after = after['snapshot']
    delta = {k: (counters_after[k] - counters_before[k]) & 0xFFFFFFFF
             for k in ['rx_valid', 'rx_crc_errors']}
    require(delta['rx_valid'] == summary['queries'] and delta['rx_crc_errors'] == 0,
            'PL request counters do not reconcile with UART capture')
    require(counters_after['token'] & 1 == 0 and counters_after['last_request'] == 10,
            'Invalid final PL snapshot')
    interrupts = {}
    for label in ['HARD_NPU_IRQ', 'SOFT_NPU_IRQ']:
        start = interrupt_count(args.interrupts_before, label)
        finish = interrupt_count(args.interrupts_after, label)
        interrupts[label] = dict(before=start, after=finish, delta=finish - start)
    manifest = status['runs'][0]['manifest']
    frozen = {
        'kd-s1': ('62078166df42359fc4ebc345ba64d03aeda843147523ba45242885774ab8bc0a',
                  'b5edd4aa73b92b3dc008387cc27d766f4752b7864d6eea7858fef679f538e267'),
        'sfaug-r1': ('2fe64b5ee90792e7bfa16d9cc77e2def9d8f156465df07da1360516e9906c698',
                     '9fce4ffba4a3d1cdd8d980fdd7c9c83ac860c70baa5ba3b4a3df41050a212e24'),
    }
    require(manifest['model_id'] in frozen and
            (manifest['trunk_sha256'], manifest['head_sha256']) == frozen[manifest['model_id']],
            'IRQ gate requires an identified frozen model with four measured hardware layers')
    require(interrupts['HARD_NPU_IRQ']['delta'] == summary['frames'] * 4,
            'Hard NPU IRQ delta differs from four hardware compute layers per frozen-model frame')
    summary.update(status='passed', ps_session=status['session'], boot_id=status['boot_id'],
                   counter_delta=delta, counters_before=counters_before, counters_after=counters_after,
                   kernel_interrupts=interrupts,
                   network=network_summary(jsonlines(args.network), status['session'], run_id, 1800),
                   source_sha256={name: hashlib.sha256(getattr(args, name).read_bytes()).hexdigest()
                                  for name in ['rows', 'run_status', 'serial', 'network', 'before', 'after',
                                               'interrupts_before', 'interrupts_after']},
                   limits=['Recorded bench conditions only; no voltage/temperature sweep',
                           'No oscilloscope measurement of DE edges',
                           'Frozen simulated samples; no physical ADC acquisition'])
    args.output.write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
