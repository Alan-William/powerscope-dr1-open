"""Native NPU + physical UIO workload; UART correlation is checked separately.

Unlike preview acceptance, every stored inference must carry a real ACK and a
stable PL request snapshot. A completed workload is not a joint UART pass.
"""
import argparse
import json
from pathlib import Path
import subprocess
import time
import urllib.request

from board_acceptance import dump, resources


def analyze_rows(path, count, minimum_seconds, expected_input, expected_model=None):
    n = correct = npu_correct = 0
    first = last = previous = None
    inference = []
    application = []
    first_snapshot = last_snapshot = None
    for line in path.open():
        row = json.loads(line)
        assert row['backend'] == 'Alnpu'
        if expected_model is not None:
            assert row['model_id'] == expected_model
        assert row['axi']['mode'] == 'uio' and row['axi']['physical'] is True
        assert row['mailbox']['flags'] == 7 and row['mailbox']['error'] == 0
        assert row['mailbox']['sequence'] == row['sequence']
        snapshot = row['axi']['snapshot']
        assert snapshot['token'] & 1 == 0
        if previous:
            assert row['sequence'] == ((previous['sequence'] + 1) & 0xFFFFFFFF)
            expected_ack = ((previous['axi']['ack_token'] + 1) & 0xFFFFFFFF) or 1
            assert row['axi']['ack_token'] == expected_ack
        if expected_input == 'pl-q15':
            transport = row['sample_transport']
            assert transport['physical'] and transport['mode'] == 'pl-q15'
            assert transport['bytes_equal'] and transport['sequence'] == row['sequence']
            assert transport['error_count'] == 0
        else:
            assert row.get('sample_transport', {'mode': 'direct-f32'})['mode'] == 'direct-f32'
        first = row['elapsed_ms'] if first is None else first
        first_snapshot = snapshot if first_snapshot is None else first_snapshot
        last, last_snapshot, previous = row['elapsed_ms'], snapshot, row
        inference.append(row['inference_us'])
        application.append(row['ps_application_us'])
        n += 1
        correct += int(row['correct'])
        npu_correct += int(row['npu_correct'])
    assert n == count, (n, count)
    duration = (last - first) / 1000
    assert duration >= minimum_seconds, duration

    def distribution(values):
        values.sort()
        return dict(min=values[0], median=values[len(values) // 2],
                    p99=values[min(len(values) - 1, int(len(values) * .99))], max=values[-1])

    return dict(frames=n, correct=correct, npu_correct=npu_correct, accuracy=correct / n,
                npu_accuracy=npu_correct / n, first_to_last_result_seconds=duration,
                inference_us=distribution(inference), ps_application_us=distribution(application),
                all_rows_physical_uio_and_alnpu=True, all_ack_tokens_sequential=True,
                first_snapshot=first_snapshot, last_snapshot=last_snapshot,
                input_transport=expected_input)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=['baseline', 'endurance'], required=True)
    parser.add_argument('--expected-input', choices=['direct-f32', 'pl-q15'], default='direct-f32')
    parser.add_argument('--expected-model', choices=['kd-s1', 'sfaug-r1'], default='kd-s1')
    parser.add_argument('--rules-policy', choices=['frozen', 'disabled'], default='frozen')
    parser.add_argument('--server-pid', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeat', type=int, default=66)
    parser.add_argument('--period-ms', type=float, default=25)
    args = parser.parse_args()
    if args.repeat < 1 or args.period_ms < 0:
        raise ValueError('Invalid workload')
    args.output.mkdir(parents=True, exist_ok=False)

    def api(path, body=None, token=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-PowerScope-Token'] = token
        request = urllib.request.Request('http://127.0.0.1:8765' + path,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers=headers)
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)

    initial = api('/api/state')
    assert initial['status'] in ['idle', 'completed']
    assert initial['axi']['mode'] == 'uio'
    assert initial['model'] == args.expected_model and initial['statistics'] is False
    if args.rules_policy == 'frozen':
        assert initial['sensor_rules']['sha256'] == '1088f14c3a498f15239ea4f71435281ff72fecb6d4e1df5a592ee80443fe51dc'
    else:
        assert initial['sensor_rules'] == {'enabled': False}
    assert initial.get('sample_transport', {'mode': 'direct-f32'})['mode'] == args.expected_input
    session, token = initial['session'], initial['csrf']
    store = Path(initial['storage'])
    before = resources(args.server_pid, store)
    dump(args.output / 'resources-before.json', before)
    for label, command in [('kernel-before', ['dmesg', '--color=never']),
                           ('interrupts-before', ['cat', '/proc/interrupts'])]:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        (args.output / (label + '.txt')).write_text(result.stdout)
    status = dict(status='running', phase=args.phase, session=session, server_pid=args.server_pid,
                  expected_input=args.expected_input, boot_id=before['boot_id'], runs=[], http_errors=0,
                  uart_correlation='pending separate host log analysis')
    dump(args.output / 'status.json', status)
    workloads = [('validation', 'validation', 1, 0), ('heldout', 'heldout', 1, 0)]
    if args.phase == 'endurance':
        workloads = [('endurance', 'validation', args.repeat, args.period_ms)]
    worker_identity = None
    try:
        for label, dataset, repeat, period in workloads:
            rid = api('/api/start', dict(dataset=dataset, repeat=repeat, period_ms=period), token)['run_id']
            start = time.monotonic()
            last_resource = last_progress = 0
            expected = initial['datasets'][dataset]['count'] * repeat
            status.update(stage=label, run_id=rid, processed=0, expected_frames=expected)
            dump(args.output / 'status.json', status)
            print(label, rid, 'started', flush=True)
            with (args.output / (label + '-resources.jsonl')).open('x') as log:
                while True:
                    now = time.monotonic()
                    try:
                        s = api('/api/state')
                    except Exception:
                        status['http_errors'] += 1
                        raise
                    assert s['session'] == session and s['run_id'] == rid
                    assert s['axi']['mode'] == 'uio'
                    if s['processed']:
                        assert s['axi']['physical']
                    assert s['sensor_rules'] == initial['sensor_rules']
                    if now - last_resource >= 5 or s['status'] not in ['starting', 'running', 'finalizing']:
                        sample = resources(args.server_pid, store)
                        assert sample['boot_id'] == before['boot_id']
                        assert sample['service']['start_ticks'] == before['service']['start_ticks']
                        assert sample['free_bytes'] >= 200 * 1024 ** 2
                        if s['worker_ready']:
                            assert len(sample['workers']) == 1
                            identity = (sample['workers'][0]['pid'], sample['workers'][0]['start_ticks'])
                            if worker_identity is None:
                                worker_identity = identity
                            assert identity == worker_identity, 'Worker restarted'
                        log.write(json.dumps(dict(stage=label, run_id=rid, processed=s['processed'],
                                                   axi=s['axi'], storage_io=s.get('storage_io'),
                                                   resources=sample)) + '\n')
                        log.flush()
                        last_resource = now
                    if now - last_progress >= 15 or s['status'] not in ['starting', 'running', 'finalizing']:
                        status.update(processed=s['processed'], correct=s['correct'],
                                      npu_correct=s['npu_correct'], elapsed_seconds=now - start,
                                      late_frames=s['late_frames'], current_status=s['status'],
                                      worker_identity=worker_identity)
                        dump(args.output / 'status.json', status)
                        print(label, s['processed'], round(now - start, 1), s['status'], flush=True)
                        last_progress = now
                    if s['status'] not in ['starting', 'running', 'finalizing']:
                        assert s['status'] == 'completed', s.get('error') or s['status']
                        assert s['processed'] == expected and s['total'] == expected
                        assert s['mailbox']['flags'] == 4 and s['mailbox']['error'] == 0
                        minimum = 1800 if args.phase == 'endurance' else 0
                        analysis = analyze_rows(store / rid / 'results.jsonl', expected, minimum, args.expected_input, args.expected_model)
                        manifest = json.loads((store / rid / 'manifest.json').read_text())
                        storage = manifest.get('storage_io')
                        if storage is not None:
                            assert storage['finished'] and storage['error'] is None
                            assert storage['submitted'] == storage['written'] == storage['durable'] == expected
                            assert storage['queue_depth'] == 0
                            analysis['storage_io'] = storage
                        dump(args.output / (label + '-manifest.json'), manifest)
                        status['runs'].append(dict(stage=label, run_id=rid, analysis=analysis, manifest=manifest))
                        break
                    assert now - start < (4200 if args.phase == 'endurance' else 600), 'Workload timeout'
                    time.sleep(.3)
        status.update(status='completed_rows_verified', finished_monotonic=time.monotonic())
    except Exception as error:
        status.update(status='error', error=str(error), finished_monotonic=time.monotonic())
        try:
            api('/api/stop', {}, token)
        except Exception:
            pass
        raise
    finally:
        dump(args.output / 'status.json', status)
        dump(args.output / 'resources-after.json', resources(args.server_pid, store))
        for label, command in [('kernel-after', ['dmesg', '--color=never']),
                               ('interrupts-after', ['cat', '/proc/interrupts'])]:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            (args.output / (label + '.txt')).write_text(result.stdout)
    print('Native NPU/UIO rows verified; UART correlation remains separate.', flush=True)


if __name__ == '__main__':
    main()
