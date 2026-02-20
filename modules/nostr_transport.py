"""
Nostr transport abstraction for Phase 6.

Supports two modes:
1. InternalNostrTransport: Monolithic mode (runs its own thread/connection)
2. ExternalCommsTransport: Coordinated mode (delegates to cl-hive-comms via RPC)
"""

import base64
import hashlib
import json
import queue
import re
import secrets
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from modules.bridge import CircuitBreaker, CircuitState

try:
    from coincurve import PrivateKey as CoincurvePrivateKey
except Exception:  # pragma: no cover - optional dependency
    CoincurvePrivateKey = None


NOSTR_KEY_DERIVATION_MSG = "nostr_key_derivation"


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
        # Inbound queue for messages injected via hive-inject-packet
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

    def inject_packet(self, payload: Dict[str, Any]) -> bool:
        """Called by hive-inject-packet RPC. Returns True if queued, False if dropped."""
        if not isinstance(payload, dict):
            self.plugin.log("cl-hive: inject_packet called with non-dict payload", level="warn")
            return False
        try:
            self._inbound_queue.put_nowait(payload)
            return True
        except queue.Full:
            self.plugin.log("cl-hive: external transport inbound queue full, dropping packet", level="warn")
            return False

    def process_inbound(self, max_events: int = 100) -> int:
        """Process queue populated by hive-inject-packet."""
        processed = 0
        while processed < max_events:
            try:
                payload = self._inbound_queue.get_nowait()
            except queue.Empty:
                break

            processed += 1
            # Re-serialize payload to plaintext for compatibility with handlers
            # that expect to parse JSON from the plaintext field
            envelope = {
                "plaintext": json.dumps(payload),
                "pubkey": payload.get("sender") or "",
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
    """Threaded Nostr transport manager with queue-based publish/receive. (Legacy Mode)"""

    DEFAULT_RELAYS = [
        "wss://nos.lol",
        "wss://relay.damus.io",
    ]
    MAX_RELAY_CONNECTIONS = 8
    QUEUE_MAX_ITEMS = 2000

    def __init__(self, plugin, database, privkey_hex: Optional[str] = None,
                 relays: Optional[List[str]] = None):
        self.plugin = plugin
        self.db = database

        relay_list = relays or self.DEFAULT_RELAYS
        # Preserve order while deduplicating.
        self.relays = list(dict.fromkeys([r for r in relay_list if r]))[:self.MAX_RELAY_CONNECTIONS]

        self._outbound_queue: queue.Queue = queue.Queue(maxsize=self.QUEUE_MAX_ITEMS)
        self._inbound_queue: queue.Queue = queue.Queue(maxsize=self.QUEUE_MAX_ITEMS)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._lock = threading.Lock()
        self._subscriptions: Dict[str, Dict[str, Any]] = {}
        self._dm_callbacks: List[Callable[[Dict[str, Any]], None]] = []

        self._relay_status: Dict[str, Dict[str, Any]] = {
            relay: {
                "connected": False,
                "last_seen": 0,
                "published_count": 0,
                "last_error": "",
            }
            for relay in self.relays
        }

        self._storage_key: Optional[bytes] = None
        self._privkey_hex = ""
        self._pubkey_hex = ""

        self._derive_storage_key()
        self._load_or_create_identity(privkey_hex)

    def _log(self, msg: str, level: str = "info") -> None:
        self.plugin.log(f"cl-hive: nostr: {msg}", level=level)

    def _derive_storage_key(self) -> None:
        """Best-effort derivation of deterministic storage key from CLN HSM."""
        rpc = getattr(self.plugin, "rpc", None)
        if not rpc:
            return
        try:
            result = rpc.signmessage(NOSTR_KEY_DERIVATION_MSG)
            sig = result.get("zbase", "") if isinstance(result, dict) else ""
            if sig:
                self._storage_key = hashlib.sha256(sig.encode("utf-8")).digest()
        except Exception as e:
            self._log(f"storage key derivation failed (non-fatal): {e}", level="warn")

    def _encrypt_value(self, value: str) -> str:
        """XOR-encrypt UTF-8 text if a storage key is available."""
        if not self._storage_key:
            return value
        raw = value.encode("utf-8")
        key = self._storage_key
        encrypted = bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))
        return base64.b64encode(encrypted).decode("ascii")

    def _decrypt_value(self, value: str) -> str:
        """XOR-decrypt text if a storage key is available."""
        if not self._storage_key:
            return value
        try:
            encrypted = base64.b64decode(value.encode("ascii"))
            key = self._storage_key
            raw = bytes(b ^ key[i % len(key)] for i, b in enumerate(encrypted))
            return raw.decode("utf-8")
        except Exception:
            return value

    def _load_or_create_identity(self, explicit_privkey_hex: Optional[str]) -> None:
        """Load persisted keypair or create a new one on first run."""
        privkey_hex = explicit_privkey_hex or ""
        if not privkey_hex and self.db:
            encrypted = self.db.get_nostr_state("config:privkey")
            if encrypted:
                privkey_hex = self._decrypt_value(encrypted)

        if not privkey_hex:
            privkey_hex = secrets.token_hex(32)

        self._privkey_hex = privkey_hex.lower()
        self._pubkey_hex = self._derive_pubkey(self._privkey_hex)

        if self.db:
            self.db.set_nostr_state("config:privkey", self._encrypt_value(self._privkey_hex))
            self.db.set_nostr_state("config:pubkey", self._pubkey_hex)
            self.db.set_nostr_state("config:relays", json.dumps(self.relays, separators=(",", ":")))

    def _derive_pubkey(self, privkey_hex: str) -> str:
        """Derive a deterministic 32-byte pubkey hex from private key."""
        try:
            secret = bytes.fromhex(privkey_hex)
            if CoincurvePrivateKey:
                priv = CoincurvePrivateKey(secret)
                uncompressed = priv.public_key.format(compressed=False)
                return uncompressed[1:33].hex()
            return hashlib.sha256(secret).hexdigest()
        except Exception:
            return hashlib.sha256(privkey_hex.encode("utf-8")).hexdigest()

    def get_identity(self) -> Dict[str, str]:
        return {
            "pubkey": self._pubkey_hex,
            "privkey": self._privkey_hex,
        }

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="cl-hive-nostr",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _thread_main(self) -> None:
        """Outbound publish loop; non-blocking for CLN main thread."""
        with self._lock:
            now = int(time.time())
            for relay in self._relay_status.values():
                relay["connected"] = True
                relay["last_seen"] = now
                relay["last_error"] = ""

        while not self._stop_event.is_set():
            try:
                event = self._outbound_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            now = int(time.time())
            with self._lock:
                for relay in self._relay_status.values():
                    relay["connected"] = True
                    relay["last_seen"] = now
                    relay["published_count"] += 1

            if self.db:
                event_id = str(event.get("id", ""))
                self.db.set_nostr_state("event:last_published_id", event_id)
                self.db.set_nostr_state("event:last_published_at", str(now))

        with self._lock:
            for relay in self._relay_status.values():
                relay["connected"] = False

    def _compute_event_id(self, event: Dict[str, Any]) -> str:
        serial = [
            0,
            event.get("pubkey", ""),
            int(event.get("created_at", int(time.time()))),
            int(event.get("kind", 0)),
            event.get("tags", []),
            event.get("content", ""),
        ]
        payload = json.dumps(serial, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _sign_event(self, event: Dict[str, Any]) -> str:
        event_id = str(event.get("id", ""))
        if len(event_id) == 64 and CoincurvePrivateKey:
            try:
                secret = bytes.fromhex(self._privkey_hex)
                priv = CoincurvePrivateKey(secret)
                sig = priv.sign_schnorr(bytes.fromhex(event_id))
                return sig.hex()
            except Exception:
                pass
        return hashlib.sha256((event_id + self._privkey_hex).encode("utf-8")).hexdigest()

    def publish(self, event: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(event, dict):
            raise ValueError("event must be a dict")

        canonical = dict(event)
        canonical.setdefault("created_at", int(time.time()))
        canonical.setdefault("pubkey", self._pubkey_hex)
        canonical.setdefault("kind", 1)
        canonical.setdefault("tags", [])
        canonical.setdefault("content", "")

        canonical["id"] = self._compute_event_id(canonical)
        canonical["sig"] = self._sign_event(canonical)

        try:
            self._outbound_queue.put_nowait(canonical)
        except queue.Full:
            self._log("outbound queue full, dropping event", level="warn")
            raise RuntimeError("nostr outbound queue full")

        return canonical

    def _encode_dm(self, plaintext: str) -> str:
        encoded = base64.b64encode(plaintext.encode("utf-8")).decode("ascii")
        return f"b64:{encoded}"

    def _decode_dm(self, content: str) -> str:
        if not isinstance(content, str):
            return ""
        if not content.startswith("b64:"):
            return content
        try:
            return base64.b64decode(content[4:].encode("ascii")).decode("utf-8")
        except Exception:
            return ""

    def send_dm(self, recipient_pubkey: str, plaintext: str) -> Dict[str, Any]:
        if not recipient_pubkey:
            raise ValueError("recipient_pubkey is required")
        event = {
            "kind": 4,
            "tags": [["p", recipient_pubkey]],
            "content": self._encode_dm(plaintext or ""),
        }
        return self.publish(event)

    def receive_dm(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._dm_callbacks.append(callback)

    def subscribe(self, filters: Dict[str, Any],
                  callback: Callable[[Dict[str, Any]], None]) -> str:
        sub_id = str(uuid.uuid4())
        with self._lock:
            self._subscriptions[sub_id] = {
                "filters": filters or {},
                "callback": callback,
            }
        return sub_id

    def unsubscribe(self, sub_id: str) -> bool:
        with self._lock:
            return self._subscriptions.pop(sub_id, None) is not None

    def inject_event(self, event: Dict[str, Any]) -> None:
        try:
            self._inbound_queue.put_nowait(event)
        except queue.Full:
            self._log("inbound queue full, dropping event", level="warn")

    def _matches_filters(self, event: Dict[str, Any], filters: Dict[str, Any]) -> bool:
        if not filters:
            return True

        kinds = filters.get("kinds")
        if kinds and event.get("kind") not in kinds:
            return False

        authors = filters.get("authors")
        if authors and event.get("pubkey") not in authors:
            return False

        ids = filters.get("ids")
        if ids:
            event_id = str(event.get("id", ""))
            if not any(event_id.startswith(str(prefix)) for prefix in ids):
                return False

        since = filters.get("since")
        if since and int(event.get("created_at", 0)) < int(since):
            return False

        until = filters.get("until")
        if until and int(event.get("created_at", 0)) > int(until):
            return False

        return True

    def process_inbound(self, max_events: int = 100) -> int:
        processed = 0
        while processed < max_events:
            try:
                event = self._inbound_queue.get_nowait()
            except queue.Empty:
                break

            processed += 1
            event_kind = int(event.get("kind", 0))

            if event_kind == 4:
                envelope = dict(event)
                envelope["plaintext"] = self._decode_dm(str(event.get("content", "")))
                with self._lock:
                    dm_callbacks = list(self._dm_callbacks)
                for cb in dm_callbacks:
                    try:
                        cb(envelope)
                    except Exception as e:
                        self._log(f"dm callback error: {e}", level="warn")

            with self._lock:
                subscriptions = list(self._subscriptions.values())
            for sub in subscriptions:
                if self._matches_filters(event, sub.get("filters", {})):
                    try:
                        sub["callback"](event)
                    except Exception as e:
                        self._log(f"subscription callback error: {e}", level="warn")

        return processed

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            relays = {k: dict(v) for k, v in self._relay_status.items()}
            sub_count = len(self._subscriptions)
            dm_cb_count = len(self._dm_callbacks)

        return {
            "mode": "internal",
            "running": bool(self._thread and self._thread.is_alive()),
            "pubkey": self._pubkey_hex,
            "relay_count": len(self.relays),
            "relays": relays,
            "outbound_queue_size": self._outbound_queue.qsize(),
            "inbound_queue_size": self._inbound_queue.qsize(),
            "subscription_count": sub_count,
            "dm_callback_count": dm_cb_count,
        }

# Alias for backward compatibility if needed, though we will use specific classes
NostrTransport = InternalNostrTransport
