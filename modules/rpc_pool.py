"""
Subprocess-isolated, timeout-safe RPC execution pool for cl-hive.

Extracted from cl-hive.py monolith. Contains:
- RpcLockTimeoutError: Deprecated exception (kept for backwards compatibility)
- RpcPool: Bounded execution via subprocess isolation with hard timeout guarantees
- RpcPoolProxy: Transparent proxy that routes through RpcPool
"""

import multiprocessing
import queue
import threading
import time
import uuid
from typing import Dict, Optional, Any

from pyln.client import RpcError

from modules.identity_adapter import RemoteArchonIdentity

# Module-level reference set by cl-hive.py when identity adapter is initialized.
# RpcPoolProxy._maybe_sign_via_identity reads this global.
identity_adapter = None


# =============================================================================
# RPC THREAD SAFETY NOTE
# =============================================================================
# pyln-client's UnixDomainSocketRpc.call() opens a NEW socket per call,
# making calls inherently isolated and thread-safe. No global locking is needed.
# This was confirmed during the nexus-01 hang investigation (57 failures in 16 days)
# which traced to the unnecessary global RPC_LOCK causing serialization bottlenecks.


class RpcLockTimeoutError(TimeoutError):
    """
    DEPRECATED: This exception is no longer raised by cl-hive.

    Previously raised when RPC lock could not be acquired. Kept for backwards
    compatibility with code that may catch this exception type.

    pyln-client is inherently thread-safe (opens new socket per call),
    so global RPC locking was removed.
    """
    pass


# =============================================================================
# RPC POOL (Phase 3 — bounded execution via subprocess isolation)
# =============================================================================
# While pyln-client is thread-safe, it can hang indefinitely on certain
# transport / plugin interactions. The pool provides hard timeout guarantees
# by isolating RPC calls in worker subprocesses.

class RpcPool:
    """
    A pool of RPC worker processes with hard timeout guarantees.

    Design:
    - N worker processes share one request queue and one response queue
    - A dispatcher thread routes responses to per-request Event slots
    - Callers block only on their own Event — not on each other
    - Dead workers are auto-respawned by the dispatcher's health check
    """

    def __init__(self, socket_path: str, log_fn, pool_size: int = 3):
        self.socket_path = socket_path
        self._log = log_fn
        self._pool_size = max(1, min(pool_size, 8))

        self._ctx = multiprocessing.get_context("spawn")

        self._workers: list = []
        self._req_q: Any = None
        self._resp_q: Any = None

        self._pending: Dict[str, dict] = {}
        self._pending_lock = threading.Lock()

        self._dispatcher: Optional[threading.Thread] = None
        self._dispatcher_stop = threading.Event()

        self._lifecycle_lock = threading.Lock()
        self._last_restart_time = 0.0
        self._last_resp_time = time.time()
        self._restart_scheduled = False
        self._restart_scheduled_lock = threading.Lock()

        self.start()

    @staticmethod
    def _worker_main(socket_path: str, req_q, resp_q):
        """Runs in a separate process — each worker has its own LightningRpc."""
        from pyln.client import LightningRpc, RpcError as _RpcError
        import traceback as _tb

        rpc = LightningRpc(socket_path)

        while True:
            req = req_q.get()
            if not req:
                continue
            if req.get("op") == "stop":
                break

            req_id = req.get("id")
            method = req.get("method")
            payload = req.get("payload")
            args = req.get("args") or []
            kwargs = req.get("kwargs") or {}

            try:
                if payload is not None:
                    # Explicit rpc.call(method, payload) — pass through
                    result = rpc.call(method, payload)
                else:
                    # Attribute-style: rpc.method(*args, **kwargs)
                    # Use getattr to match pyln-client's natural calling
                    # convention (handles positional args, __getattr__).
                    # Fall back to rpc.call() on TypeError for methods where
                    # pyln-client has explicit signatures with different param
                    # names (e.g. listnodes(node_id=) vs caller passing id=).
                    try:
                        result = getattr(rpc, method)(*args, **kwargs)
                    except TypeError:
                        if kwargs:
                            result = rpc.call(method, kwargs)
                        elif args:
                            result = rpc.call(method, args[0] if len(args) == 1 else args)
                        else:
                            result = rpc.call(method, {})
                resp_q.put({"id": req_id, "ok": True, "result": result})
            except _RpcError as e:
                resp_q.put({
                    "id": req_id, "ok": False,
                    "error_type": "RpcError",
                    "error": getattr(e, "error", None),
                    "message": str(e),
                })
            except Exception as e:
                resp_q.put({
                    "id": req_id, "ok": False,
                    "error_type": "Exception",
                    "message": str(e),
                    "traceback": _tb.format_exc(),
                })

    def _dispatch_loop(self):
        """Read resp_q, route to per-request Event slots."""
        health_check_interval = 10.0
        last_health_check = time.time()

        while not self._dispatcher_stop.is_set():
            try:
                try:
                    resp = self._resp_q.get(timeout=1.0)
                except (queue.Empty, OSError, AttributeError, TypeError, EOFError, BrokenPipeError):
                    resp = None

                if resp is not None:
                    self._last_resp_time = time.time()
                    req_id = resp.get("id")
                    if req_id:
                        with self._pending_lock:
                            slot = self._pending.get(req_id)
                        if slot is not None:
                            slot["resp"] = resp
                            slot["event"].set()

                now = time.time()
                if now - last_health_check >= health_check_interval:
                    last_health_check = now
                    self._check_worker_health()
            except Exception as e:
                # Never let the dispatcher die silently; losing this thread makes
                # the plugin appear hung because responses stop reaching callers.
                self._log(f"RPC pool dispatcher error: {e}", "error")
                if not self._dispatcher_stop.is_set():
                    self._schedule_restart("dispatcher exception")
                # Avoid a tight error loop if queue/IPC is broken.
                time.sleep(0.2)

    def _schedule_restart(self, reason: str):
        """Restart pool from a helper thread (safe when called by dispatcher)."""
        with self._restart_scheduled_lock:
            if self._restart_scheduled:
                return
            self._restart_scheduled = True

        def _run():
            try:
                self.restart(reason)
            finally:
                with self._restart_scheduled_lock:
                    self._restart_scheduled = False

        threading.Thread(
            target=_run,
            daemon=True,
            name="hive_rpc_pool_restart",
        ).start()

    def _check_worker_health(self):
        # Non-blocking acquire: avoids deadlock when stop() holds this lock
        # while joining the dispatcher thread (which calls this method).
        if not self._lifecycle_lock.acquire(blocking=False):
            return
        try:
            if not self._req_q or self._dispatcher_stop.is_set():
                return
            for i, w in enumerate(self._workers):
                if not w.is_alive():
                    try:
                        w.join(timeout=0.1)
                    except Exception:
                        pass
                    new_w = self._ctx.Process(
                        target=RpcPool._worker_main,
                        args=(self.socket_path, self._req_q, self._resp_q),
                        daemon=True, name=f"hive_rpc_pool_{i}",
                    )
                    new_w.start()
                    self._workers[i] = new_w
                    self._log(f"RPC pool: respawned dead worker {i}", "warn")
            # Detect wedged workers/process pipeline: requests pending for too long
            # with no responses seen recently usually means workers are alive but stuck.
            now = time.time()
            stale_pending = None
            with self._pending_lock:
                if self._pending:
                    oldest = min(float(slot.get("started_at", now)) for slot in self._pending.values())
                    stale_pending = max(0.0, now - oldest)
            if stale_pending is not None and stale_pending > 45.0 and (now - self._last_resp_time) > 20.0:
                self._log(
                    f"RPC pool appears wedged (oldest pending {stale_pending:.1f}s, no responses for {now - self._last_resp_time:.1f}s)",
                    "warn",
                )
                self._schedule_restart("wedged workers / no responses")
        finally:
            self._lifecycle_lock.release()

    def start(self):
        with self._lifecycle_lock:
            self._req_q = self._ctx.Queue()
            self._resp_q = self._ctx.Queue()
            self._workers = []
            for i in range(self._pool_size):
                w = self._ctx.Process(
                    target=RpcPool._worker_main,
                    args=(self.socket_path, self._req_q, self._resp_q),
                    daemon=True, name=f"hive_rpc_pool_{i}",
                )
                w.start()
                self._workers.append(w)
            self._dispatcher_stop.clear()
            self._dispatcher = threading.Thread(
                target=self._dispatch_loop, daemon=True, name="hive_rpc_dispatcher",
            )
            self._dispatcher.start()

    def stop(self):
        with self._lifecycle_lock:
            self._dispatcher_stop.set()
            for _ in self._workers:
                try:
                    if self._req_q:
                        self._req_q.put_nowait({"op": "stop"})
                except Exception:
                    pass
            for w in self._workers:
                try:
                    if w.is_alive():
                        w.terminate()
                        w.join(timeout=1.0)
                except Exception:
                    pass
            self._workers = []
            if self._dispatcher and self._dispatcher.is_alive():
                self._dispatcher.join(timeout=2.0)
            self._dispatcher = None
            self._req_q = None
            self._resp_q = None
            with self._pending_lock:
                for slot in self._pending.values():
                    slot["event"].set()
                self._pending.clear()

    def restart(self, reason: str):
        # Thundering herd prevention: skip if restarted within last 5 seconds
        now = time.time()
        if now - self._last_restart_time < 5.0:
            self._log(f"RPC pool restart skipped (cooldown): {reason}", "info")
            return
        self._last_restart_time = now
        self._log(f"RPC pool restart ({self._pool_size} workers): {reason}", "warn")
        self.stop()
        self.start()

    def status(self) -> Dict[str, Any]:
        """Lightweight pool health/status for debugging stalls."""
        now = time.time()
        with self._pending_lock:
            pending_items = [
                {
                    "method": str(slot.get("method") or ""),
                    "age_seconds": round(max(0.0, now - float(slot.get("started_at", now))), 3),
                }
                for slot in self._pending.values()
                if isinstance(slot, dict)
            ]
        pending_items.sort(key=lambda x: x["age_seconds"], reverse=True)
        workers = []
        for i, w in enumerate(self._workers):
            try:
                alive = bool(w.is_alive())
                pid = int(w.pid) if w.pid else None
                exitcode = w.exitcode
            except Exception:
                alive = False
                pid = None
                exitcode = None
            workers.append({
                "index": i,
                "pid": pid,
                "alive": alive,
                "exitcode": exitcode,
            })
        return {
            "running": bool(self._req_q is not None and self._resp_q is not None),
            "pool_size": self._pool_size,
            "workers": workers,
            "dispatcher_alive": bool(self._dispatcher and self._dispatcher.is_alive()),
            "dispatcher_stop_set": self._dispatcher_stop.is_set(),
            "pending_count": len(pending_items),
            "pending_top": pending_items[:10],
            "last_response_age_seconds": round(max(0.0, now - float(self._last_resp_time)), 3),
            "last_restart_age_seconds": round(max(0.0, now - float(self._last_restart_time)), 3)
            if self._last_restart_time else None,
            "restart_scheduled": bool(self._restart_scheduled),
            "socket_path": self.socket_path,
        }

    def request(self, *, method: str,
                payload: Any = None, args: list = None,
                kwargs: dict = None, timeout: int = 30):
        """Send an RPC request through the pool. Blocks only this caller."""
        req_id = uuid.uuid4().hex
        slot = {"event": threading.Event(), "resp": None, "started_at": time.time(), "method": method}

        with self._pending_lock:
            self._pending[req_id] = slot

        req = {
            "id": req_id, "method": method,
            "payload": payload, "args": args or [],
            "kwargs": kwargs or {},
        }

        try:
            try:
                if self._req_q is None:
                    self.restart("pool not running")
                self._req_q.put(req, timeout=1.0)
            except (queue.Full, OSError, ValueError, AttributeError, EOFError, BrokenPipeError, TypeError):
                self.restart(f"queue error on {method}")
                raise TimeoutError(f"RPC pool queue error on {method}")

            if not slot["event"].wait(timeout=timeout):
                self.restart(f"timeout ({timeout}s) on {method}")
                raise TimeoutError(f"RPC pool timeout on {method}")

            resp = slot["resp"]
            if resp is None:
                raise TimeoutError(f"RPC pool shutdown during {method}")
        finally:
            with self._pending_lock:
                self._pending.pop(req_id, None)

        if resp.get("ok"):
            return resp.get("result")

        if resp.get("traceback"):
            self._log(
                f"RPC pool exception in {method}: {resp.get('message')}\n{resp.get('traceback')}",
                "error"
            )

        err = resp.get("error")
        msg = resp.get("message") or "RPC error"
        raise RpcError(method, {} if payload is None else payload,
                       err if err is not None else msg)


class RpcPoolProxy:
    """
    Transparent proxy that behaves like plugin.rpc but routes through RpcPool.

    Supports both styles:
    - proxy.getinfo()              -> attribute-style (kind="attr")
    - proxy.call("method", {})     -> explicit call-style (kind="call")
    """

    def __init__(self, pool: RpcPool, timeout: int = 30):
        self._pool = pool
        self._timeout = timeout

    @property
    def socket_path(self) -> str:
        return self._pool.socket_path

    def get_socket_path(self) -> str:
        return self._pool.socket_path

    def _maybe_sign_via_identity(self, message: Any) -> Optional[Dict[str, Any]]:
        """
        Route signmessage through RemoteArchonIdentity when coordinated identity is active.
        """
        global identity_adapter
        if not isinstance(identity_adapter, RemoteArchonIdentity):
            return None
        if not isinstance(message, str):
            return None
        sig = identity_adapter.sign_message(message)
        return {"zbase": sig, "signature": sig}

    def call(self, method: str, payload: Any = None) -> Any:
        if method == "signmessage":
            msg = payload.get("message") if isinstance(payload, dict) else payload
            delegated = self._maybe_sign_via_identity(msg)
            if delegated is not None:
                return delegated
        return self._pool.request(method=method, payload=payload,
                                  timeout=self._timeout)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        if name == "signmessage":
            def _sign_proxy(*args, **kwargs):
                message = args[0] if args else kwargs.get("message")
                delegated = self._maybe_sign_via_identity(message)
                if delegated is not None:
                    return delegated
                return self._pool.request(
                    method=name,
                    args=list(args) if args else None,
                    kwargs=kwargs if kwargs else None,
                    timeout=self._timeout,
                )
            return _sign_proxy

        def _method_proxy(*args, **kwargs):
            return self._pool.request(
                method=name,
                args=list(args) if args else None,
                kwargs=kwargs if kwargs else None,
                timeout=self._timeout,
            )

        return _method_proxy
