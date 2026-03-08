"""Batched log writer — reduces write_lock contention on plugin stdout.

pyln-client's plugin.log() acquires write_lock per-line (same lock as RPC
responses). With 16 msg threads + 9 background loops, the IO thread gets
starved. This writer queues log messages and flushes them in batches with
a single write_lock acquisition per batch.
"""

import queue
import threading


class BatchedLogWriter:
    """Queue-based log writer that batches plugin.log() calls."""

    _FLUSH_INTERVAL = 0.05   # 50ms between flushes
    _MAX_BATCH = 200          # max messages per flush
    _QUEUE_SIZE = 10_000      # drop on overflow (non-blocking put)

    def __init__(self, plugin_obj):
        self._plugin = plugin_obj
        self._queue: queue.Queue = queue.Queue(maxsize=self._QUEUE_SIZE)
        self._stop = threading.Event()
        self._original_log = plugin_obj.log  # save original
        self._thread = threading.Thread(
            target=self._writer_loop,
            name="hive_log_writer",
            daemon=True,
        )
        self._thread.start()
        # Monkey-patch plugin.log → queued version
        plugin_obj.log = self._enqueue

    def _enqueue(self, message: str, level: str = 'info') -> None:
        """Non-blocking replacement for plugin.log()."""
        try:
            self._queue.put_nowait((level, message))
        except queue.Full:
            pass  # drop — better than blocking the caller

    def _writer_loop(self) -> None:
        """Drain queue and write batches with one write_lock acquisition."""
        while not self._stop.is_set():
            self._stop.wait(self._FLUSH_INTERVAL)
            self._flush_batch()

    def _flush_batch(self) -> int:
        """Write up to _MAX_BATCH messages in one lock acquisition."""
        batch = []
        for _ in range(self._MAX_BATCH):
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return 0

        # Build all JSON-RPC notification bytes, write with one lock hold
        import json as _json
        parts = []
        for level, message in batch:
            for line in message.split('\n'):
                parts.append(
                    bytes(
                        _json.dumps({
                            'jsonrpc': '2.0',
                            'method': 'log',
                            'params': {'level': level, 'message': line},
                        }, ensure_ascii=False) + '\n\n',
                        encoding='utf-8',
                    )
                )
        try:
            with self._plugin.write_lock:
                for part in parts:
                    self._plugin.stdout.buffer.write(part)
                self._plugin.stdout.flush()
        except Exception:
            pass  # stdout closed during shutdown
        return len(batch)

    def stop(self) -> None:
        """Flush remaining messages and stop the writer thread."""
        self._stop.set()
        # Restore the original logger first so new shutdown logs bypass the queue.
        self._plugin.log = self._original_log
        self._thread.join(timeout=2)
        # Drain all queued messages (not just one batch) so shutdown diagnostics
        # are not silently dropped during noisy exits.
        while self._flush_batch():
            pass
