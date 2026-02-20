"""Tests for BatchedLogWriter — queue-based log batching to reduce write_lock contention."""

import io
import json
import queue
import threading
import time
from unittest.mock import MagicMock

import pytest

# We cannot import cl-hive.py directly (pyln.client dependency), so we
# replicate the class here for unit testing.  The class under test is
# intentionally self-contained (only uses stdlib queue/threading) which
# makes this approach safe.  Any drift will be caught by integration tests.

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

    def _flush_batch(self) -> None:
        """Write up to _MAX_BATCH messages in one lock acquisition."""
        batch = []
        for _ in range(self._MAX_BATCH):
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return

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

    def stop(self) -> None:
        """Flush remaining messages and stop the writer thread."""
        self._stop.set()
        self._flush_batch()
        self._thread.join(timeout=2)
        self._plugin.log = self._original_log


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_plugin():
    """Create a mock plugin object with the attributes BatchedLogWriter needs."""
    plugin = MagicMock()
    plugin.log = MagicMock()
    plugin.write_lock = threading.Lock()
    buf = io.BytesIO()
    stdout = MagicMock()
    stdout.buffer = buf
    stdout.flush = MagicMock()
    plugin.stdout = stdout
    return plugin


def _stop_writer_thread(writer):
    """Stop the background writer thread so tests can control flushing."""
    writer._stop.set()
    writer._thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEnqueue:
    def test_enqueue_does_not_block(self):
        """_enqueue() should return immediately — no lock contention."""
        plugin = _make_mock_plugin()
        writer = BatchedLogWriter(plugin)
        try:
            start = time.monotonic()
            for i in range(1000):
                writer._enqueue(f"message {i}")
            elapsed = time.monotonic() - start
            assert elapsed < 1.0, f"_enqueue took {elapsed:.3f}s for 1000 calls"
        finally:
            writer.stop()

    def test_overflow_drops_silently(self):
        """When queue is full, _enqueue should not raise."""
        plugin = _make_mock_plugin()
        writer = BatchedLogWriter(plugin)
        try:
            _stop_writer_thread(writer)
            # Fill the queue to capacity
            for i in range(writer._QUEUE_SIZE):
                writer._queue.put_nowait(('info', f'msg {i}'))
            # These should not raise
            writer._enqueue("overflow message")
            writer._enqueue("another overflow")
        finally:
            writer._plugin.log = writer._original_log


class TestFlushBatch:
    def test_flush_batch_writes_to_stdout(self):
        """_flush_batch() should write correct JSON-RPC notifications to stdout."""
        plugin = _make_mock_plugin()
        writer = BatchedLogWriter(plugin)
        _stop_writer_thread(writer)

        writer._queue.put_nowait(('info', 'hello world'))
        writer._queue.put_nowait(('warn', 'danger'))

        plugin.stdout.buffer = io.BytesIO()
        writer._flush_batch()

        output = plugin.stdout.buffer.getvalue().decode('utf-8')
        notifications = [
            json.loads(line) for line in output.strip().split('\n') if line.strip()
        ]
        assert len(notifications) == 2

        assert notifications[0]['jsonrpc'] == '2.0'
        assert notifications[0]['method'] == 'log'
        assert notifications[0]['params']['level'] == 'info'
        assert notifications[0]['params']['message'] == 'hello world'

        assert notifications[1]['params']['level'] == 'warn'
        assert notifications[1]['params']['message'] == 'danger'

        writer._plugin.log = writer._original_log

    def test_batch_uses_single_lock_acquisition(self):
        """50 messages should result in exactly one write_lock acquisition."""
        plugin = _make_mock_plugin()
        lock = MagicMock()
        lock.__enter__ = MagicMock(return_value=None)
        lock.__exit__ = MagicMock(return_value=False)
        plugin.write_lock = lock

        writer = BatchedLogWriter(plugin)
        _stop_writer_thread(writer)

        for i in range(50):
            writer._queue.put_nowait(('info', f'msg {i}'))

        writer._flush_batch()

        assert lock.__enter__.call_count == 1
        assert lock.__exit__.call_count == 1

        writer._plugin.log = writer._original_log

    def test_empty_queue_no_write(self):
        """_flush_batch() on empty queue should not acquire write_lock."""
        plugin = _make_mock_plugin()
        lock = MagicMock()
        lock.__enter__ = MagicMock(return_value=None)
        lock.__exit__ = MagicMock(return_value=False)
        plugin.write_lock = lock

        writer = BatchedLogWriter(plugin)
        _stop_writer_thread(writer)

        writer._flush_batch()

        lock.__enter__.assert_not_called()

        writer._plugin.log = writer._original_log


class TestMultiline:
    def test_multiline_message_split(self):
        """A message with \\n should produce separate JSON-RPC notifications per line."""
        plugin = _make_mock_plugin()
        writer = BatchedLogWriter(plugin)
        _stop_writer_thread(writer)

        writer._queue.put_nowait(('info', 'line1\nline2\nline3'))

        plugin.stdout.buffer = io.BytesIO()
        writer._flush_batch()

        output = plugin.stdout.buffer.getvalue().decode('utf-8')
        notifications = [
            json.loads(line) for line in output.strip().split('\n') if line.strip()
        ]
        assert len(notifications) == 3
        assert notifications[0]['params']['message'] == 'line1'
        assert notifications[1]['params']['message'] == 'line2'
        assert notifications[2]['params']['message'] == 'line3'

        writer._plugin.log = writer._original_log


class TestStopRestore:
    def test_stop_restores_original_log(self):
        """After stop(), plugin.log should be the original function."""
        plugin = _make_mock_plugin()
        original = plugin.log
        writer = BatchedLogWriter(plugin)

        assert plugin.log is not original
        assert plugin.log == writer._enqueue

        writer.stop()

        assert plugin.log is original

    def test_stop_flushes_remaining(self):
        """stop() should flush any remaining queued messages."""
        plugin = _make_mock_plugin()
        writer = BatchedLogWriter(plugin)
        _stop_writer_thread(writer)

        writer._queue.put_nowait(('info', 'final message'))
        writer._stop.clear()

        plugin.stdout.buffer = io.BytesIO()
        writer.stop()

        output = plugin.stdout.buffer.getvalue().decode('utf-8')
        assert 'final message' in output
