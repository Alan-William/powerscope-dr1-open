"""Read the PS state over a confirmed SSH tunnel during joint hardware acceptance."""
import argparse
import json
from pathlib import Path
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--duration', type=float, default=4200)
    parser.add_argument('--period', type=float, default=5)
    parser.add_argument('--stop-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.duration <= 0 or args.period < 1 or args.stop_file.exists():
        raise ValueError('Positive duration, period >= 1 second, and a fresh stop file required')
    totals = dict(requests=0, failures=0)
    start = time.monotonic()
    with args.output.open('x', encoding='utf-8') as log:
        log.write(json.dumps(dict(event='start', url=args.url, session=args.session,
                                  monotonic_ns=time.monotonic_ns())) + '\n')
        while time.monotonic() - start < args.duration and not args.stop_file.exists():
            before = time.monotonic_ns()
            result = dict(event='state', start_monotonic_ns=before)
            totals['requests'] += 1
            try:
                with urllib.request.urlopen(args.url, timeout=3) as response:
                    state = json.load(response)
                if state['session'] != args.session:
                    raise RuntimeError('PS service session changed')
                result.update(ok=True, session=state['session'], run_id=state['run_id'],
                              status=state['status'], processed=state['processed'],
                              late_frames=state['late_frames'], error=state.get('error'),
                              axi=state['axi'], sample_transport=state['sample_transport'],
                              storage_io=state.get('storage_io'))
            except Exception as error:
                totals['failures'] += 1
                result.update(ok=False, error=str(error))
            after = time.monotonic_ns()
            result.update(end_monotonic_ns=after, response_us=(after - before) / 1000)
            log.write(json.dumps(result) + '\n')
            log.flush()
            time.sleep(max(0, args.period - (after - before) / 1e9))
        log.write(json.dumps(dict(event='complete', monotonic_ns=time.monotonic_ns(),
                                  stop_reason='stop_file' if args.stop_file.exists() else 'duration',
                                  **totals)) + '\n')
    print(json.dumps(totals))


if __name__ == '__main__':
    main()
