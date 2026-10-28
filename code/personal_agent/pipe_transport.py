"""Bounded blocking-pipe workers for Windows, where select() accepts sockets only.

One transport belongs to one child. A timeout closes that child, never retries a
write, and discards its queues before a new process can be started.
"""

import os
import queue
import threading
import time


class ThreadedPipes:
    def __init__(self, process):
        self.process = process
        self.stopped = threading.Event()
        self.reads = queue.Queue(maxsize=2)
        self.writes = queue.Queue(maxsize=1)
        self.workers = [
            threading.Thread(target=self._reader, daemon=True),
            threading.Thread(target=self._writer, daemon=True),
        ]
        for worker in self.workers:
            worker.start()

    def _offer(self, target, value):
        while not self.stopped.is_set():
            try:
                target.put(value, timeout=.05)
                return
            except queue.Full:
                continue

    def _reader(self):
        try:
            while not self.stopped.is_set():
                data = os.read(self.process.stdout.fileno(), 65536)
                self._offer(self.reads, data)
                if not data:
                    return
        except OSError as exc:
            self._offer(self.reads, exc)

    def _writer(self):
        while not self.stopped.is_set():
            try:
                data, done, outcome = self.writes.get(timeout=.05)
            except queue.Empty:
                continue
            try:
                view = memoryview(data)
                while view and not self.stopped.is_set():
                    count = os.write(self.process.stdin.fileno(), view)
                    if not count:
                        raise BrokenPipeError("MCP request pipe closed")
                    view = view[count:]
            except OSError as exc:
                outcome.append(exc)
            finally:
                done.set()

    def read(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("MCP response timeout")
        try:
            data = self.reads.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError("MCP response timeout") from None
        if isinstance(data, OSError):
            raise data
        return data

    def write(self, data, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or self.stopped.is_set():
            raise TimeoutError("MCP request timeout")
        done, outcome = threading.Event(), []
        self.writes.put_nowait((data, done, outcome))
        if not done.wait(max(0, deadline - time.monotonic())):
            raise TimeoutError("MCP request timeout")
        if outcome:
            raise outcome[0]

    def stop(self):
        # The owner terminates/waits for the child before joining these workers.
        self.stopped.set()

    def join(self):
        for worker in self.workers:
            worker.join(timeout=.5)
