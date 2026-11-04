"""Explicit hardware probe over JSON stdin; only a verified UIO map is allowed."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from protocol.mailbox import MailboxClient, packet  # noqa: E402
from ps.axi import UioRegisters  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--mmio-lib', type=Path, required=True)
    parser.add_argument('--allow-test-exit', action='store_true',
                        help='Permit deliberate abrupt exit of this diagnostic publisher')
    args = parser.parse_args()
    io = UioRegisters(json.loads(args.config.read_text()), args.mmio_lib)
    try:
        if io.read(0) != 1:
            raise RuntimeError('Mailbox ABI mismatch')
        client = MailboxClient(io)
        print(json.dumps(dict(event='ready', physical=True, npu_executed=False,
                              pid=os.getpid(), test_exit_enabled=args.allow_test_exit,
                              boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                              snapshot=client.snapshot())), flush=True)
        for line in sys.stdin:
            command = json.loads(line)
            if command['op'] == 'publish':
                # These are diagnostic payloads, never fresh NPU inference.
                p = packet(npu=False, **command['values'])
                result = dict(event='published', packet=p, **client.commit(p))
            elif command['op'] == 'snapshot':
                result = dict(event='snapshot', snapshot=client.snapshot(), ack=io.read(0x20))
            elif command['op'] == 'exit':
                break
            elif command['op'] == 'test_exit' and args.allow_test_exit:
                # Intentionally skip finally: hardware must invalidate the old
                # result even when this publisher does not clean up its state.
                os._exit(17)
            else:
                raise ValueError('Unsupported probe command')
            print(json.dumps(dict(monotonic_ns=time.monotonic_ns(), **result)), flush=True)
    finally:
        io.close()


if __name__ == '__main__':
    main()
