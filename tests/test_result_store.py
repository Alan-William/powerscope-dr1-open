import csv
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from ps.result_store import ResultStoreError, ResultWriter


class ResultWriterTests(unittest.TestCase):
    def test_complete_files_keep_order_and_final_durability_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            writer = ResultWriter(folder, ['sequence'])
            for i in range(43):
                writer.submit(dict(sequence=i), [i])
            stats = writer.finish()
            self.assertEqual(stats['submitted'], 43)
            self.assertEqual(stats['written'], stats['durable'])
            self.assertEqual(stats['durable'], 43)
            self.assertEqual(stats['sync_batches'], 3)
            self.assertEqual([json.loads(s)['sequence'] for s in (folder / 'results.jsonl').read_text().splitlines()], list(range(43)))
            with (folder / 'results.csv').open(newline='') as stream:
                self.assertEqual(list(csv.reader(stream)), [['sequence']] + [[str(i)] for i in range(43)])

    def test_slow_sync_does_not_block_submission_and_queue_never_drops(self):
        entered, release = threading.Event(), threading.Event()

        def slow_sync(fd):
            entered.set()
            if not release.wait(3):
                raise OSError('Test release timed out')

        with tempfile.TemporaryDirectory() as temp:
            writer = ResultWriter(Path(temp), ['sequence'], capacity=4, sync_every=1, sync=slow_sync)
            try:
                writer.submit(dict(sequence=0), [0])
                self.assertTrue(entered.wait(1))
                start = time.monotonic()
                for i in range(1, 5):
                    writer.submit(dict(sequence=i), [i])
                self.assertLess(time.monotonic() - start, .2)
                with self.assertRaisesRegex(ResultStoreError, 'queue full'):
                    writer.check_capacity()
                with self.assertRaisesRegex(ResultStoreError, 'queue full'):
                    writer.submit(dict(sequence=5), [5])
                self.assertEqual(writer.snapshot()['submitted'], 5)
            finally:
                release.set()
                stats = writer.finish()
            self.assertEqual(stats['durable'], 5)
            self.assertEqual(len((Path(temp) / 'results.jsonl').read_text().splitlines()), 5)

    def test_io_failure_is_reported_to_producer_and_completion(self):
        def fail(fd):
            raise OSError('Injected disk sync failure')

        with tempfile.TemporaryDirectory() as temp:
            writer = ResultWriter(Path(temp), ['sequence'], sync_every=1, sync=fail)
            writer.submit(dict(sequence=1), [1])
            self.assertTrue(writer.done.wait(1))
            with self.assertRaisesRegex(ResultStoreError, 'disk sync failure'):
                writer.check_capacity()
            with self.assertRaisesRegex(ResultStoreError, 'disk sync failure'):
                writer.finish()
            self.assertEqual(writer.snapshot()['durable'], 0)

    def test_finish_timeout_keeps_writer_unresolved_until_actual_drain(self):
        entered, release = threading.Event(), threading.Event()

        def blocked_sync(fd):
            entered.set()
            release.wait(3)

        with tempfile.TemporaryDirectory() as temp:
            writer = ResultWriter(Path(temp), ['sequence'], sync_every=1, sync=blocked_sync)
            writer.submit(dict(sequence=1), [1])
            self.assertTrue(entered.wait(1))
            try:
                with self.assertRaisesRegex(ResultStoreError, 'did not finish'):
                    writer.finish(timeout=.01)
                self.assertFalse(writer.done.is_set())
            finally:
                release.set()
                self.assertEqual(writer.finish()['durable'], 1)


if __name__ == '__main__':
    unittest.main()
