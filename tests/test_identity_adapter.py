"""Tests for modules/identity_adapter.py — Phase 6 identity delegation."""

import sys
import os
import time
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock pyln.client before importing modules that depend on it
_mock_pyln = MagicMock()
_mock_pyln.Plugin = MagicMock
_mock_pyln.RpcError = type("RpcError", (Exception,), {})
sys.modules.setdefault("pyln", _mock_pyln)
sys.modules.setdefault("pyln.client", _mock_pyln)

import pytest

from modules.identity_adapter import (
    IdentityInterface,
    LocalIdentity,
    RemoteArchonIdentity,
)
from modules.bridge import CircuitState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeRpc:
    """Minimal RPC mock for LocalIdentity."""

    def __init__(self, sign_result=None, check_result=None, raise_on_check=False):
        self._sign_result = sign_result or {"zbase": "mock_zbase_sig"}
        self._check_result = check_result or {"verified": True}
        self._raise_on_check = raise_on_check

    def signmessage(self, message):
        return self._sign_result

    def checkmessage(self, message, signature, pubkey=None):
        if self._raise_on_check:
            raise RuntimeError("rpc error")
        return self._check_result


class _FakePlugin:
    """Minimal plugin mock for RemoteArchonIdentity."""

    def __init__(self, call_result=None, raise_on_call=False):
        self._call_result = call_result or {"ok": True, "signature": "remote_zbase"}
        self._raise_on_call = raise_on_call
        self.logs = []
        self.rpc = self._Rpc(self)

    def log(self, msg, level="info"):
        self.logs.append((msg, level))

    class _Rpc:
        def __init__(self, plugin):
            self._plugin = plugin

        def call(self, method, params=None):
            if self._plugin._raise_on_call:
                raise RuntimeError("rpc call failed")
            if (
                isinstance(self._plugin._call_result, dict)
                and method in self._plugin._call_result
                and isinstance(self._plugin._call_result[method], dict)
            ):
                return self._plugin._call_result[method]
            return self._plugin._call_result

        def checkmessage(self, message, signature, pubkey=None):
            return {"verified": True}


# ---------------------------------------------------------------------------
# IdentityInterface ABC
# ---------------------------------------------------------------------------

class TestIdentityInterface:
    def test_sign_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            IdentityInterface().sign_message("hello")

    def test_check_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            IdentityInterface().check_message("hello", "sig")

    def test_get_info_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            IdentityInterface().get_info()


# ---------------------------------------------------------------------------
# LocalIdentity
# ---------------------------------------------------------------------------

class TestLocalIdentity:
    def test_sign_message_returns_zbase(self):
        rpc = _FakeRpc(sign_result={"zbase": "abc123"})
        li = LocalIdentity(rpc)
        assert li.sign_message("test") == "abc123"

    def test_sign_message_empty_on_missing_key(self):
        rpc = _FakeRpc(sign_result={"other": "value"})
        li = LocalIdentity(rpc)
        assert li.sign_message("test") == ""

    def test_sign_message_handles_non_dict(self):
        rpc = _FakeRpc(sign_result="not_a_dict")
        li = LocalIdentity(rpc)
        assert li.sign_message("test") == ""

    def test_check_message_returns_true(self):
        rpc = _FakeRpc(check_result={"verified": True})
        li = LocalIdentity(rpc)
        assert li.check_message("msg", "sig") is True

    def test_check_message_returns_false(self):
        rpc = _FakeRpc(check_result={"verified": False})
        li = LocalIdentity(rpc)
        assert li.check_message("msg", "sig") is False

    def test_check_message_with_pubkey(self):
        rpc = _FakeRpc(check_result={"verified": True})
        li = LocalIdentity(rpc)
        assert li.check_message("msg", "sig", pubkey="02aabb") is True

    def test_check_message_exception_returns_false(self):
        rpc = _FakeRpc(raise_on_check=True)
        li = LocalIdentity(rpc)
        assert li.check_message("msg", "sig") is False

    def test_get_info(self):
        rpc = _FakeRpc()
        li = LocalIdentity(rpc)
        info = li.get_info()
        assert info["mode"] == "local"
        assert info["backend"] == "cln-hsm"


# ---------------------------------------------------------------------------
# RemoteArchonIdentity
# ---------------------------------------------------------------------------

class TestRemoteArchonIdentity:
    def test_sign_message_delegates_to_archon(self):
        plugin = _FakePlugin(call_result={"ok": True, "signature": "remote_sig"})
        ra = RemoteArchonIdentity(plugin)
        assert ra.sign_message("test") == "remote_sig"

    def test_sign_message_records_success(self):
        plugin = _FakePlugin(call_result={"ok": True, "signature": "s"})
        ra = RemoteArchonIdentity(plugin)
        ra.sign_message("test")
        assert ra._circuit._state == CircuitState.CLOSED
        assert ra._circuit._failure_count == 0

    def test_sign_message_records_failure_on_error_response(self):
        plugin = _FakePlugin(call_result={"error": "bad"})
        ra = RemoteArchonIdentity(plugin)
        result = ra.sign_message("test")
        assert result == ""
        assert ra._circuit._failure_count == 1

    def test_sign_message_records_failure_on_exception(self):
        plugin = _FakePlugin(raise_on_call=True)
        ra = RemoteArchonIdentity(plugin)
        result = ra.sign_message("test")
        assert result == ""
        assert ra._circuit._failure_count == 1

    def test_circuit_opens_after_max_failures(self):
        plugin = _FakePlugin(raise_on_call=True)
        ra = RemoteArchonIdentity(plugin)
        for _ in range(3):
            ra.sign_message("test")
        assert ra._circuit._state == CircuitState.OPEN

    def test_sign_returns_empty_when_circuit_open(self):
        plugin = _FakePlugin(call_result={"ok": True, "signature": "s"})
        ra = RemoteArchonIdentity(plugin)
        # Force circuit open with recent failure so it doesn't auto-transition to HALF_OPEN
        ra._circuit._state = CircuitState.OPEN
        ra._circuit._last_failure_time = int(time.time())
        result = ra.sign_message("test")
        assert result == ""
        # Verify it logged a warning
        assert any("circuit open" in msg for msg, _ in plugin.logs)

    def test_check_message_always_local(self):
        plugin = _FakePlugin(raise_on_call=True)
        ra = RemoteArchonIdentity(plugin)
        # Even with RPC errors, checkmessage should work (it's local)
        assert ra.check_message("msg", "sig") is True

    def test_check_message_with_pubkey(self):
        plugin = _FakePlugin()
        ra = RemoteArchonIdentity(plugin)
        assert ra.check_message("msg", "sig", pubkey="02aabb") is True

    def test_get_info_shows_remote_mode(self):
        plugin = _FakePlugin(call_result={
            "hive-archon-status": {
                "ok": True,
                "identity": {"did": "did:cid:test", "status": "active"},
            }
        })
        ra = RemoteArchonIdentity(plugin)
        info = ra.get_info()
        assert info["mode"] == "remote"
        assert info["backend"] == "cl-hive-archon"
        assert info["circuit_state"] == "closed"
        assert info["archon_ok"] is True
        assert info["identity"]["did"] == "did:cid:test"

    def test_get_info_shows_open_circuit(self):
        plugin = _FakePlugin()
        ra = RemoteArchonIdentity(plugin)
        ra._circuit._state = CircuitState.OPEN
        ra._circuit._last_failure_time = int(time.time())
        info = ra.get_info()
        assert info["circuit_state"] == "open"

    def test_get_info_records_failure_when_status_call_errors(self):
        plugin = _FakePlugin(raise_on_call=True)
        ra = RemoteArchonIdentity(plugin)
        info = ra.get_info()
        assert info["mode"] == "remote"
        assert ra._circuit._failure_count == 1
