"""22-class physical AXI/RS485 checks, negative requests and watchdog recovery.

The diagnostic publisher never claims NPU execution. Real inference and joint
endurance are separate acceptance stages. Raw UART and board replies are saved.
"""
import argparse
import json
from pathlib import Path
import shlex
import struct
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from protocol.modbus import frame, read_request, reply  # noqa: E402
from tools.rs485_capture import read_response  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['port', 'ssh-config', 'host', 'release', 'axi-config']:
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    import serial
    command = shlex.join(['timeout', '45s', 'python3', '-u', args.release + '/tools/pl_mailbox_probe.py',
                          '--config', args.axi_config, '--mmio-lib', args.release + '/bin/libpowerscope_mmio.so'])
    ssh = ['ssh', '-F', args.ssh_config, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
           '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2', args.host, command]
    session = uuid.uuid4().hex
    expected_valid = expected_crc = queries = 0
    with (args.output / 'raw.jsonl').open('x', encoding='utf-8') as log, \
            (args.output / 'bridge-stderr.log').open('xb') as err:
        process = subprocess.Popen(ssh, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=err, text=True, bufsize=1)

        def record(value):
            log.write(json.dumps(dict(session=session, host_monotonic_ns=time.monotonic_ns(), **value)) + '\n')
            log.flush()

        def bridge(value=None):
            if value is not None:
                process.stdin.write(json.dumps(value) + '\n')
                process.stdin.flush()
            line = process.stdout.readline()
            if not line:
                raise RuntimeError('Board probe ended; inspect stderr')
            response = json.loads(line)
            record(dict(event='board', response=response))
            return response

        ready = bridge()
        if ready.get('event') != 'ready' or not ready.get('physical'):
            raise RuntimeError('Expected verified physical UIO probe')
        before = ready['snapshot']
        try:
            with serial.Serial(args.port, 115200, bytesize=8, parity='E', stopbits=1,
                               timeout=.2, write_timeout=.2) as port:
                def query(name, request, expected, valid=1, crc=0):
                    nonlocal queries, expected_valid, expected_crc
                    stale = port.read(port.in_waiting) if port.in_waiting else b''
                    if stale:
                        raise RuntimeError('Unexpected bytes before request: ' + stale.hex())
                    start = time.monotonic_ns()
                    if port.write(request) != len(request):
                        raise RuntimeError('Partial UART write')
                    port.flush()
                    received = read_response(port, .2)
                    queries += 1
                    record(dict(event='query', name=name, start_monotonic_ns=start,
                                end_monotonic_ns=time.monotonic_ns(), request_hex=request.hex(),
                                response_hex=received.hex(), expected_hex=(expected or b'').hex(),
                                matched=received == (expected or b'')))
                    if received != (expected or b''):
                        raise AssertionError('Response mismatch: ' + name)
                    expected_valid += valid
                    expected_crc += crc
                    time.sleep(.025)

                for label in range(22):
                    start = time.monotonic()
                    values = dict(sequence=0xA0000000 + label, label=label,
                                  confidence=(label + 1) / 22, inference_us=12345 + label)
                    p = bridge(dict(op='publish', values=values))['packet']
                    for repeat in range(2):
                        query('class_%d_%d' % (label, repeat), read_request(),
                              bytes.fromhex(p['rtu_expected_response_hex']))
                        time.sleep(.06)
                    time.sleep(max(0, .25 - (time.monotonic() - start)))
                p = bridge(dict(op='publish', values=dict(sequence=0xA1000000, error=4)))['packet']
                query('invalid_application_result', read_request(), bytes.fromhex(p['rtu_expected_response_hex']))
                for name, request, valid, crc in [
                    ('bad_crc', bytes.fromhex('01030000000ac5cc'), 0, 1),
                    ('other_address', read_request(address=2), 0, 0),
                    ('broadcast', frame(struct.pack('>BBHH', 0, 3, 0, 10)), 0, 0),
                    ('zero_quantity', frame(struct.pack('>BBHH', 1, 3, 0, 0)), 1, 0),
                    ('range_error', read_request(start=9, count=2), 1, 0),
                    ('unsupported_function', frame(struct.pack('>BBHH', 1, 4, 0, 10)), 1, 0),
                ]:
                    query(name, request, reply(request, p['registers']), valid, crc)
                p = bridge(dict(op='publish', values=dict(sequence=0xA2000000, label=7,
                                                          confidence=.875, inference_us=12345)))['packet']
                query('before_watchdog', read_request(), bytes.fromhex(p['rtu_expected_response_hex']))
                time.sleep(.65)
                regs = [1, 4, 65535, 0, 0xA200, 0, 0, 0, 0, 5]
                query('watchdog_expired', read_request(), reply(read_request(), regs))
                p = bridge(dict(op='publish', values=dict(sequence=0xA2000001, label=2,
                                                          confidence=.75, inference_us=123)))['packet']
                query('watchdog_recovered', read_request(), bytes.fromhex(p['rtu_expected_response_hex']))
                after = bridge(dict(op='snapshot'))['snapshot']
                if ((after['rx_valid'] - before['rx_valid']) & 0xFFFFFFFF) != expected_valid:
                    raise AssertionError('Legal request counter mismatch')
                if ((after['rx_crc_errors'] - before['rx_crc_errors']) & 0xFFFFFFFF) != expected_crc:
                    raise AssertionError('CRC error counter mismatch')
                if after['last_request'] != 10 or after['token'] & 1:
                    raise AssertionError('Unstable or wrong request snapshot')
                summary = dict(status='passed', physical=True, npu_executed=False, session=session,
                               port=args.port, boot_id=ready['boot_id'], queries=queries, classes=22,
                               valid_request_delta=expected_valid, injected_crc_error_delta=expected_crc,
                               watchdog_expiry_and_recovery_passed=True, before=before, after=after,
                               de_electrical_waveform_measured=False)
                (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
                print(json.dumps(summary))
        finally:
            if process.poll() is None:
                process.stdin.close()
            process.wait(timeout=50)


if __name__ == '__main__':
    main()
