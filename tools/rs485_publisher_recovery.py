"""Physical watchdog check after an owned diagnostic publisher exits abruptly."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from protocol.modbus import read_request, reply  # noqa: E402
from tools.rs485_capture import read_response  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['port', 'ssh-config', 'host', 'release', 'axi-config']:
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    import serial
    command = shlex.join(['timeout', '20s', 'python3', '-u', args.release + '/tools/pl_mailbox_probe.py',
                          '--config', args.axi_config, '--mmio-lib', args.release + '/bin/libpowerscope_mmio.so',
                          '--allow-test-exit'])
    ssh = ['ssh', '-F', args.ssh_config, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
           args.host, command]
    processes = []
    session = uuid.uuid4().hex
    with (args.output / 'raw.jsonl').open('x', encoding='utf-8') as log, \
            (args.output / 'stderr.log').open('xb') as errors:
        def record(data):
            log.write(json.dumps(dict(host_monotonic_ns=time.monotonic_ns(), session=session, **data)) + '\n')
            log.flush()

        def bridge(process, data=None):
            if data is not None:
                process.stdin.write(json.dumps(data) + '\n')
                process.stdin.flush()
            line = process.stdout.readline()
            if not line:
                raise RuntimeError('Diagnostic publisher ended unexpectedly; inspect stderr')
            response = json.loads(line)
            record(dict(event='board', response=response))
            return response

        def launch():
            process = subprocess.Popen(ssh, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=errors, text=True, bufsize=1)
            processes.append(process)
            ready = bridge(process)
            if (ready.get('event') != 'ready' or not ready['physical']
                    or ready['npu_executed'] or not ready['test_exit_enabled']):
                raise RuntimeError('Expected explicitly enabled diagnostic publisher')
            return process, ready

        try:
            process, ready = launch()
            before = ready['snapshot']
            with serial.Serial(args.port, 115200, bytesize=8, parity='E', stopbits=1,
                               timeout=.2, write_timeout=1) as port:
                def query(name, expected):
                    stale = port.read(port.in_waiting) if port.in_waiting else b''
                    if stale:
                        raise RuntimeError('Stale serial bytes: ' + stale.hex())
                    request = read_request()
                    start = time.monotonic_ns()
                    if port.write(request) != len(request):
                        raise RuntimeError('Partial request write')
                    port.flush()
                    received = read_response(port, .2)
                    record(dict(event='query', name=name, start_monotonic_ns=start,
                                request_hex=request.hex(), response_hex=received.hex(),
                                expected_hex=expected.hex(), matched=received == expected))
                    if received != expected:
                        raise RuntimeError('Unexpected response: ' + name)
                    time.sleep(.025)

                values = dict(sequence=0xB0000000, label=4, confidence=.625, inference_us=12345)
                first = bridge(process, dict(op='publish', values=values))
                query('before_publisher_exit', bytes.fromhex(first['packet']['rtu_expected_response_hex']))
                process.stdin.write(json.dumps(dict(op='test_exit')) + '\n')
                process.stdin.flush()
                code = process.wait(timeout=10)
                record(dict(event='abrupt_exit', returncode=code, board_pid=ready['pid']))
                if code != 17:
                    raise RuntimeError('Expected deliberate abrupt exit code 17')
                time.sleep(.65)
                expired = [1, 4, 65535, 0, 0xB000, 0, 0, 0, 0, 5]
                query('expired_without_cleanup', reply(read_request(), expired))
                recovered, new_ready = launch()
                if new_ready['boot_id'] != ready['boot_id']:
                    raise RuntimeError('Board rebooted during process recovery')
                second = bridge(recovered, dict(op='publish', values=dict(
                    sequence=0xB0000001, label=3, confidence=.75, inference_us=12000)))
                if second['ack_token'] != (((first['ack_token'] + 1) & 0xFFFFFFFF) or 1):
                    raise RuntimeError('New process did not resume the hardware ACK sequence')
                query('new_process_recovers', bytes.fromhex(second['packet']['rtu_expected_response_hex']))
                invalid = bridge(recovered, dict(op='publish', values=dict(sequence=0xB0000002)))
                query('clean_final_invalidation', bytes.fromhex(invalid['packet']['rtu_expected_response_hex']))
                after = bridge(recovered, dict(op='snapshot'))['snapshot']
                if ((after['rx_valid'] - before['rx_valid']) & 0xFFFFFFFF) != 4:
                    raise RuntimeError('Request count mismatch')
                if after['rx_crc_errors'] != before['rx_crc_errors']:
                    raise RuntimeError('Unexpected CRC error')
                summary = dict(status='passed', physical=True, npu_executed=False, session=session,
                               boot_id=ready['boot_id'], abrupt_publisher_exit_code=17,
                               expiry_observed_after_exit_seconds=.65, recovered_without_board_reset=True,
                               hardware_ack_resumed=True, queries=4, before=before, after=after)
                (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
                print(json.dumps(summary))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.stdin.close()
                process.wait(timeout=25)


if __name__ == '__main__':
    main()
