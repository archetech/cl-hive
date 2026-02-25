"""
Nostr transport abstraction for Phase 6.

Supported mode:
1. ExternalCommsTransport: delegates transport to cl-hive-comms via RPC

Legacy note:
- InternalNostrTransport has been removed from cl-hive runtime. A fail-fast
  compatibility stub remains so callers get a clear migration error.
"""

import json
import queue
import re
import threading
from typing import Any, Callable, Dict, List, Optional

from modules.bridge import CircuitBreaker


class TransportInterface:
    """Abstract base class for Nostr transport."""
    
    def get_identity(self) -> Dict[str, str]:
        raise NotImplementedError

    def start(self) -> bool:
        raise NotImplementedError

    def stop(self, timeout: float = 5.0) -> None:
        raise NotImplementedError

    def publish(self, event: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def send_dm(self, recipient_pubkey: str, plaintext: str) -> Dict[str, Any]:
        raise NotImplementedError

    def receive_dm(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        raise NotImplementedError

    def subscribe(self, filters: Dict[str, Any], callback: Callable[[Dict[str, Any]], None]) -> str:
        raise NotImplementedError

    def unsubscribe(self, sub_id: str) -> bool:
        raise NotImplementedError

    def process_inbound(self, max_events: int = 100) -> int:
        raise NotImplementedError

    def get_status(self) -> Dict[str, Any]:
        raise NotImplementedError


class ExternalCommsTransport(TransportInterface):
    """Delegates transport to cl-hive-comms plugin via RPC with CircuitBreaker."""

    def __init__(self, plugin):
        self.plugin = plugin
        self._identity_cache = {}
        self._dm_callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._lock = threading.Lock()
        # Inbound queue for messages injected via hive-inject-packet.
        # Queue items are dicts containing the raw payload plus authenticated
        # transport metadata (e.g. sender pubkey) supplied by the caller.
        self._inbound_queue: queue.Queue = queue.Queue(maxsize=2000)
        # Circuit breaker for comms RPC calls
        self._circuit = CircuitBreaker(name="external-comms", max_failures=3, reset_timeout=60)

    def get_identity(self) -> Dict[str, str]:
        if not self._identity_cache:
            if not self._circuit.is_available():
                self.plugin.log("cl-hive: comms circuit open, using cached/empty identity", level="warn")
                return {"pubkey": "", "privkey": ""}
            try:
                res = self.plugin.rpc.call("hive-client-identity", {"action": "get"})
                if not isinstance(res, dict):
                    self._circuit.record_failure()
                    self.plugin.log("cl-hive: comms identity returned non-dict", level="warn")
                    return {"pubkey": "", "privkey": ""}
                pubkey = str(res.get("pubkey") or "")
                if pubkey and not re.fullmatch(r"[0-9a-f]{64}", pubkey):
                    self._circuit.record_failure()
                    self.plugin.log(f"cl-hive: comms returned invalid pubkey format", level="warn")
                    return {"pubkey": "", "privkey": ""}
                self._circuit.record_success()
                self._identity_cache = {
                    "pubkey": pubkey,
                    "privkey": "",  # Remote mode doesn't expose privkey
                }
            except Exception as e:
                self._circuit.record_failure()
                self.plugin.log(f"cl-hive: failed to get identity from comms: {e}", level="warn")
                return {"pubkey": "", "privkey": ""}
        return self._identity_cache

    def start(self) -> bool:
        return True  # Remote is already running

    def stop(self, timeout: float = 5.0) -> None:
        pass

    def publish(self, event: Dict[str, Any]) -> Dict[str, Any]:
        if not self._circuit.is_available():
            self.plugin.log("cl-hive: comms circuit open, dropping publish", level="warn")
            return {}
        try:
            result = self.plugin.rpc.call("hive-comms-publish-event", {"event_json": json.dumps(event)})
            self._circuit.record_success()
            return result
        except Exception as e:
            self._circuit.record_failure()
            self.plugin.log(f"cl-hive: remote publish failed: {e}", level="error")
            return {}

    def send_dm(self, recipient_pubkey: str, plaintext: str) -> Dict[str, Any]:
        if not recipient_pubkey:
            self.plugin.log("cl-hive: send_dm called with empty recipient_pubkey", level="warn")
            return {}
        if not self._circuit.is_available():
            self.plugin.log("cl-hive: comms circuit open, dropping send_dm", level="warn")
            return {}
        try:
            result = self.plugin.rpc.call("hive-comms-send-dm", {
                "recipient": recipient_pubkey,
                "message": plaintext
            })
            self._circuit.record_success()
            return result
        except Exception as e:
            self._circuit.record_failure()
            self.plugin.log(f"cl-hive: remote send_dm failed: {e}", level="error")
            return {}

    def receive_dm(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._dm_callbacks.append(callback)

    def subscribe(self, filters: Dict[str, Any], callback: Callable[[Dict[str, Any]], None]) -> str:
        return "remote-sub-placeholder"

    def unsubscribe(self, sub_id: str) -> bool:
        return True

    def inject_packet(self, payload: Dict[str, Any], transport_pubkey: str = "") -> bool:
        """Called by hive-inject-packet RPC. Returns True if queued, False if dropped."""
        if not isinstance(payload, dict):
            self.plugin.log("cl-hive: inject_packet called with non-dict payload", level="warn")
            return False
        item = {
            "payload": payload,
            "transport_pubkey": str(transport_pubkey or ""),
        }
        try:
            self._inbound_queue.put_nowait(item)
            return True
        except queue.Full:
            self.plugin.log("cl-hive: external transport inbound queue full, dropping packet", level="warn")
            return False

    def process_inbound(self, max_events: int = 100) -> int:
        """Process queue populated by hive-inject-packet."""
        processed = 0
        while processed < max_events:
            try:
                item = self._inbound_queue.get_nowait()
            except queue.Empty:
                break

            payload = item
            transport_pubkey = ""
            if isinstance(item, dict) and "payload" in item:
                payload = item.get("payload")
                transport_pubkey = str(item.get("transport_pubkey") or "")

            if not isinstance(payload, dict):
                self.plugin.log("cl-hive: invalid injected packet entry (payload not dict)", level="warn")
                continue

            processed += 1
            # Re-serialize payload to plaintext for compatibility with handlers
            # that expect to parse JSON from the plaintext field
            envelope = {
                "plaintext": json.dumps(payload),
                "pubkey": transport_pubkey,
                "payload": payload,
            }

            with self._lock:
                dm_callbacks = list(self._dm_callbacks)
            for cb in dm_callbacks:
                try:
                    cb(envelope)
                except Exception as exc:
                    self.plugin.log(f"cl-hive: DM callback error: {exc}", level="warn")
        return processed

    def get_status(self) -> Dict[str, Any]:
        return {
            "mode": "external",
            "plugin": "cl-hive-comms",
            "circuit_state": self._circuit.state.value,
        }


class InternalNostrTransport(TransportInterface):
    """Legacy stub kept for import compatibility after internal transport removal."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "InternalNostrTransport has been removed from cl-hive. "
            "Use cl-hive-comms with ExternalCommsTransport."
        )


# Alias kept for backward compatibility with older imports. Instantiation fails
# fast with migration guidance via the legacy stub above.
NostrTransport = InternalNostrTransport
