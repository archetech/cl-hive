"""
Identity adapter for Phase 6 handover.

Supports two modes:
1. LocalIdentity: Signs via CLN HSM directly (Monolith Mode)
2. RemoteArchonIdentity: Delegates signing to cl-hive-archon via RPC (Coordinated Mode)
"""

from typing import Any, Dict

from modules.bridge import CircuitBreaker


class IdentityInterface:
    """Abstract base class for identity operations."""

    def sign_message(self, message: str) -> str:
        """Sign a message, returning the zbase signature."""
        raise NotImplementedError

    def check_message(self, message: str, signature: str, pubkey: str = "") -> bool:
        """Verify a message signature. Returns True if valid."""
        raise NotImplementedError

    def get_info(self) -> Dict[str, Any]:
        """Return identity info (pubkey, mode, etc.)."""
        raise NotImplementedError


class LocalIdentity(IdentityInterface):
    """Signs via CLN HSM directly (default/monolith mode)."""

    def __init__(self, rpc):
        self._rpc = rpc

    def sign_message(self, message: str) -> str:
        try:
            result = self._rpc.signmessage(message)
            if isinstance(result, dict):
                return str(result.get("zbase", ""))
            return ""
        except Exception:
            return ""

    def check_message(self, message: str, signature: str, pubkey: str = "") -> bool:
        try:
            if pubkey:
                result = self._rpc.checkmessage(message, signature, pubkey)
            else:
                result = self._rpc.checkmessage(message, signature)
            if isinstance(result, dict):
                return bool(result.get("verified", False))
            return False
        except Exception:
            return False

    def get_info(self) -> Dict[str, Any]:
        return {"mode": "local", "backend": "cln-hsm"}


class RemoteArchonIdentity(IdentityInterface):
    """Delegates signing to cl-hive-archon via RPC with CircuitBreaker.

    checkmessage is always done locally (it doesn't require secrets).
    Only signmessage is delegated to archon.
    """

    def __init__(self, plugin):
        self._plugin = plugin
        self._circuit = CircuitBreaker(name="archon-identity", max_failures=3, reset_timeout=60)

    def sign_message(self, message: str) -> str:
        if not self._circuit.is_available():
            self._plugin.log("cl-hive: archon identity circuit open, signing unavailable", level="warn")
            return ""
        try:
            result = self._plugin.rpc.call("hive-archon-sign-message", {"message": message})
            if isinstance(result, dict) and result.get("ok"):
                self._circuit.record_success()
                return str(result.get("signature", ""))
            self._circuit.record_failure()
            return ""
        except Exception as e:
            self._circuit.record_failure()
            self._plugin.log(f"cl-hive: archon sign_message failed: {e}", level="warn")
            return ""

    def check_message(self, message: str, signature: str, pubkey: str = "") -> bool:
        # checkmessage is always local — it doesn't need private keys
        try:
            if pubkey:
                result = self._plugin.rpc.checkmessage(message, signature, pubkey)
            else:
                result = self._plugin.rpc.checkmessage(message, signature)
            if isinstance(result, dict):
                return bool(result.get("verified", False))
            return False
        except Exception:
            return False

    def get_info(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "mode": "remote",
            "backend": "cl-hive-archon",
            "circuit_state": self._circuit.state.value,
        }
        if not self._circuit.is_available():
            return info

        try:
            status = self._plugin.rpc.call("hive-archon-status")
            if isinstance(status, dict):
                self._circuit.record_success()
                info["archon_ok"] = bool(status.get("ok", False))
                identity = status.get("identity")
                if isinstance(identity, dict):
                    info["identity"] = identity
                return info
            self._circuit.record_failure()
        except Exception:
            self._circuit.record_failure()
        return info
