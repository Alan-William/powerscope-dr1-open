"""Bounded result persistence; slow fsync must not refresh or stall PL results."""
import csv
import json
import os
import queue
import shutil
import threading
import time


class ResultStoreError(OSError):
    pass


class ResultWriter:
    def __init__(self, folder, header, capacity=64, sync_every=20, sync=None):
        if capacity < 1 or sync_every < 1:
            raise ValueError('Positive queue capacity and sync interval required')
        self.folder, self.capacity, self.sync_every = folder, capacity, sync_every
        self.sync = os.fsync if sync is None else sync
        self.queue = queue.Queue(maxsize=capacity)
        self.guard = threading.Lock()
        self.closing, self.done = threading.Event(), threading.Event()
        self.error = None
        self.stats = dict(submitted=0, written=0, durable=0, max_queue_depth=0,
                          max_write_us=0, max_sync_us=0, sync_batches=0)
        self.rows = (folder / 'results.jsonl').open('x', encoding='utf-8')
        try:
            self.csv_file = (folder / 'results.csv').open('x', newline='', encoding='utf-8')
            self.csv = csv.writer(self.csv_file)
            self.csv.writerow(header)
        except Exception:
            self.rows.close()
            if hasattr(self, 'csv_file'):
                self.csv_file.close()
            raise
        self.thread = threading.Thread(target=self._run, name='result-writer', daemon=True)
        self.thread.start()

    def check_capacity(self):
        if self.error:
            raise ResultStoreError(self.error)
        if self.closing.is_set():
            raise ResultStoreError('Result writer is closing')
        if self.queue.full():
            raise ResultStoreError('Result storage queue full; inference stopped without dropping queued rows')

    def submit(self, row, csv_values):
        self.check_capacity()
        with self.guard:
            try:
                self.queue.put_nowait((row, csv_values))
            except queue.Full as error:
                raise ResultStoreError('Result storage queue full') from error
            self.stats['submitted'] += 1
            self.stats['max_queue_depth'] = max(self.stats['max_queue_depth'], self.queue.qsize())

    def _sync(self, written):
        start = time.monotonic_ns()
        self.rows.flush()
        self.csv_file.flush()
        self.sync(self.rows.fileno())
        self.sync(self.csv_file.fileno())
        with self.guard:
            self.stats['durable'] = written
            self.stats['sync_batches'] += 1
            self.stats['max_sync_us'] = max(self.stats['max_sync_us'], (time.monotonic_ns() - start) // 1000)
        if shutil.disk_usage(self.folder).free < 200 * 1024 ** 2:
            raise ResultStoreError('Storage reserve reached')

    def _run(self):
        written = 0
        try:
            while True:
                try:
                    row, csv_values = self.queue.get(timeout=.05)
                except queue.Empty:
                    if self.closing.is_set():
                        break
                    continue
                start = time.monotonic_ns()
                self.rows.write(json.dumps(row, separators=(',', ':')) + '\n')
                self.csv.writerow(csv_values)
                self.rows.flush()
                self.csv_file.flush()
                written += 1
                with self.guard:
                    self.stats['written'] = written
                    self.stats['max_write_us'] = max(self.stats['max_write_us'],
                                                     (time.monotonic_ns() - start) // 1000)
                if written % self.sync_every == 0:
                    self._sync(written)
            self._sync(written)
        except Exception as error:
            self.error = str(error) or type(error).__name__
        finally:
            for stream in [self.rows, self.csv_file]:
                try:
                    stream.close()
                except Exception as error:
                    self.error = self.error or str(error) or type(error).__name__
            self.done.set()

    def snapshot(self):
        with self.guard:
            return dict(self.stats, queue_capacity=self.capacity, queue_depth=self.queue.qsize(),
                        sync_every=self.sync_every, finished=self.done.is_set(), error=self.error,
                        policy='bounded async queue; flush each row; fsync each 20 rows and on finish')

    def finish(self, timeout=10):
        self.closing.set()
        self.thread.join(timeout)
        if not self.done.is_set():
            raise ResultStoreError('Result writer did not finish; new runs must wait for it')
        if self.error:
            raise ResultStoreError(self.error)
        result = self.snapshot()
        if not result['submitted'] == result['written'] == result['durable']:
            raise ResultStoreError('Result persistence count mismatch')
        return result
