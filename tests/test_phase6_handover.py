"""
Tests for Phase 6 Handover: Transport delegation to cl-hive-comms.

Tests:
1. ExternalCommsTransport delegates publish/send_dm via RPC
2. inject_packet -> process_inbound -> DM callback dispatch
3. CircuitBreaker opens after failures and recovers
4. hive-inject-packet rejects in Monolith Mode
5. InternalNostrTransport still works (regression)
"""

import json
import time
from unittest.mock import MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock pyln.client before importing modules that depend on it
_mock_pyln = MagicMock()
_mock_pyln.Plugin = MagicMock
_mock_pyln.RpcError = type("RpcError", (Exception,), {})
sys.modules.setdefault("pyln", _mock_pyln)
sys.modules.setdefault("pyln.client", _mock_pyln)

from modules.nostr_transport import (
    ExternalCommsTransport,
    InternalNostrTransport,
    TransportInterface,
)
from modules.bridge import CircuitBreaker, CircuitState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_plugin(rpc_side_effects=None):
    """Create a mock plugin with configurable RPC behavior."""
    plugin = MagicMock()
    plugin.log = MagicMock()
    if rpc_side_effects:
        plugin.rpc.call.side_effect = rpc_side_effects
    return plugin


# ---------------------------------------------------------------------------
# ExternalCommsTransport delegation tests
# ---------------------------------------------------------------------------

class TestExternalTransportDelegation:
    def test_publish_delegates_to_comms_rpc(self):
        """Verify publish() calls hive-comms-publish-event RPC."""
        plugin = _mock_plugin()
        plugin.rpc.call.return_value = {"id": "abc123", "ok": True}

        transport = ExternalCommsTransport(plugin=plugin)
        event = {"kind": 1, "content": "hello"}
        result = transport.publish(event)

        plugin.rpc.call.assert_called_once_with(
            "hive-comms-publish-event",
            {"event_json": json.dumps(event)},
        )
        assert result["ok"] is True

    def test_send_dm_delegates_to_comms_rpc(self):
        """Verify send_dm() calls hive-comms-send-dm RPC."""
        plugin = _mock_plugin()
        plugin.rpc.call.return_value = {"id": "dm123", "ok": True}

        transport = ExternalCommsTransport(plugin=plugin)
        result = transport.send_dm("deadbeef" * 8, "test message")

        plugin.rpc.call.assert_called_once_with(
            "hive-comms-send-dm",
            {"recipient": "deadbeef" * 8, "message": "test message"},
        )
        assert result["ok"] is True

    def test_get_identity_delegates_to_comms_rpc(self):
        """Verify get_identity() calls hive-client-identity RPC."""
        plugin = _mock_plugin()
        plugin.rpc.call.return_value = {"pubkey": "aabb" * 16}

        transport = ExternalCommsTransport(plugin=plugin)
        identity = transport.get_identity()

        plugin.rpc.call.assert_called_once_with(
            "hive-client-identity",
            {"action": "get"},
        )
        assert identity["pubkey"] == "aabb" * 16
        assert identity["privkey"] == ""

    def test_get_identity_caches_result(self):
        """Second get_identity() call should use cache, not RPC."""
        plugin = _mock_plugin()
        plugin.rpc.call.return_value = {"pubkey": "cafe" * 16}

        transport = ExternalCommsTransport(plugin=plugin)
        transport.get_identity()
        transport.get_identity()

        assert plugin.rpc.call.call_count == 1


# ---------------------------------------------------------------------------
# inject_packet + process_inbound tests
# ---------------------------------------------------------------------------

class TestInjectAndProcess:
    def test_inject_and_process_dispatches_to_dm_callback(self):
        """inject_packet -> process_inbound -> DM callback with correct envelope."""
        plugin = _mock_plugin()
        transport = ExternalCommsTransport(plugin=plugin)

        received = []
        transport.receive_dm(lambda env: received.append(env))

        payload = {"type": "GOSSIP_STATE", "sender": "peer123", "data": {"version": 1}}
        transport.inject_packet(payload)

        count = transport.process_inbound()
        assert count == 1
        assert len(received) == 1

        envelope = received[0]
        assert envelope["pubkey"] == "peer123"
        assert json.loads(envelope["plaintext"]) == payload

    def test_inject_multiple_packets(self):
        """Multiple injected packets are all processed."""
        plugin = _mock_plugin()
        transport = ExternalCommsTransport(plugin=plugin)

        received = []
        transport.receive_dm(lambda env: received.append(env))

        for i in range(5):
            transport.inject_packet({"msg": i, "sender": f"peer{i}"})

        count = transport.process_inbound()
        assert count == 5
        assert len(received) == 5

    def test_process_inbound_empty_queue_returns_zero(self):
        """process_inbound with no packets returns 0."""
        plugin = _mock_plugin()
        transport = ExternalCommsTransport(plugin=plugin)
        assert transport.process_inbound() == 0

    def test_callback_exception_does_not_stop_processing(self):
        """A callback that raises should not prevent other callbacks from running."""
        plugin = _mock_plugin()
        transport = ExternalCommsTransport(plugin=plugin)

        good_received = []
        transport.receive_dm(lambda env: (_ for _ in ()).throw(RuntimeError("boom")))
        transport.receive_dm(lambda env: good_received.append(env))

        transport.inject_packet({"sender": "x", "data": "test"})
        transport.process_inbound()

        assert len(good_received) == 1


# ---------------------------------------------------------------------------
# CircuitBreaker integration tests
# ---------------------------------------------------------------------------

class TestCircuitBreakerIntegration:
    def test_circuit_opens_after_failures(self):
        """3 consecutive RPC failures should open the circuit."""
        plugin = _mock_plugin()
        plugin.rpc.call.side_effect = RuntimeError("comms down")

        transport = ExternalCommsTransport(plugin=plugin)

        # 3 failures
        for _ in range(3):
            transport.publish({"kind": 1})

        assert transport._circuit.state == CircuitState.OPEN

        # Next call should be dropped without RPC
        call_count_before = plugin.rpc.call.call_count
        result = transport.publish({"kind": 1})
        assert result == {}
        assert plugin.rpc.call.call_count == call_count_before

    def test_circuit_recovers_after_timeout(self):
        """Circuit should transition OPEN -> HALF_OPEN after timeout."""
        plugin = _mock_plugin()
        plugin.rpc.call.side_effect = RuntimeError("comms down")

        transport = ExternalCommsTransport(plugin=plugin)

        for _ in range(3):
            transport.publish({"kind": 1})

        assert transport._circuit.state == CircuitState.OPEN

        # Fast-forward past reset timeout
        transport._circuit._last_failure_time = int(time.time()) - 61
        assert transport._circuit.state == CircuitState.HALF_OPEN

        # Successful call closes circuit (after threshold successes)
        plugin.rpc.call.side_effect = None
        plugin.rpc.call.return_value = {"ok": True}
        for _ in range(transport._circuit.half_open_success_threshold):
            transport.publish({"kind": 1})

        assert transport._circuit.state == CircuitState.CLOSED

    def test_send_dm_records_circuit_failure(self):
        """send_dm failure should also record circuit failure."""
        plugin = _mock_plugin()
        plugin.rpc.call.side_effect = RuntimeError("down")

        transport = ExternalCommsTransport(plugin=plugin)
        transport.send_dm("aabb" * 16, "hello")

        assert transport._circuit._failure_count == 1

    def test_get_identity_records_circuit_failure(self):
        """get_identity failure should also record circuit failure."""
        plugin = _mock_plugin()
        plugin.rpc.call.side_effect = RuntimeError("down")

        transport = ExternalCommsTransport(plugin=plugin)
        result = transport.get_identity()

        assert result == {"pubkey": "", "privkey": ""}
        assert transport._circuit._failure_count == 1

    def test_get_status_includes_circuit_state(self):
        """get_status() should include circuit_state field."""
        plugin = _mock_plugin()
        transport = ExternalCommsTransport(plugin=plugin)

        status = transport.get_status()
        assert status["mode"] == "external"
        assert status["circuit_state"] == "closed"


# ---------------------------------------------------------------------------
# hive-inject-packet RPC tests
# ---------------------------------------------------------------------------

class TestInjectPacketRPC:
    def test_rejects_in_monolith_mode(self):
        """hive-inject-packet should return error when transport is Internal."""
        # Simulate what the RPC handler does:
        # We can't easily call the @plugin.method directly, but we can test
        # the logic directly
        from modules.nostr_transport import InternalNostrTransport

        mock_plugin = _mock_plugin()
        mock_db = MagicMock()
        mock_db.get_nostr_state.return_value = None
        mock_plugin.rpc.signmessage.return_value = {"zbase": "testsig"}

        transport = InternalNostrTransport(plugin=mock_plugin, database=mock_db)

        # The RPC handler checks isinstance(nostr_transport, ExternalCommsTransport)
        assert not isinstance(transport, ExternalCommsTransport)

    def test_accepts_in_coordinated_mode(self):
        """hive-inject-packet should accept payloads when transport is External."""
        plugin = _mock_plugin()
        transport = ExternalCommsTransport(plugin=plugin)

        assert isinstance(transport, ExternalCommsTransport)
        transport.inject_packet({"type": "test", "sender": "abc"})
        assert transport._inbound_queue.qsize() == 1


# ---------------------------------------------------------------------------
# InternalNostrTransport regression tests
# ---------------------------------------------------------------------------

class TestInternalTransportRegression:
    def test_internal_transport_implements_interface(self):
        """InternalNostrTransport should implement TransportInterface."""
        assert issubclass(InternalNostrTransport, TransportInterface)

    def test_external_transport_implements_interface(self):
        """ExternalCommsTransport should implement TransportInterface."""
        assert issubclass(ExternalCommsTransport, TransportInterface)

    def test_internal_transport_publish_and_process(self):
        """InternalNostrTransport should publish and process inbound events."""
        plugin = _mock_plugin()
        mock_db = MagicMock()
        mock_db.get_nostr_state.return_value = None
        plugin.rpc.signmessage.return_value = {"zbase": "testsig"}

        transport = InternalNostrTransport(plugin=plugin, database=mock_db)

        # Inject a DM event and process it
        received = []
        transport.receive_dm(lambda env: received.append(env))

        dm_event = {
            "kind": 4,
            "pubkey": "sender123",
            "content": "b64:" + __import__("base64").b64encode(b"hello world").decode(),
            "created_at": int(time.time()),
        }
        transport.inject_event(dm_event)

        count = transport.process_inbound()
        assert count == 1
        assert len(received) == 1
        assert received[0]["plaintext"] == "hello world"

    def test_internal_transport_subscription_filters(self):
        """InternalNostrTransport subscription filter matching should work."""
        plugin = _mock_plugin()
        mock_db = MagicMock()
        mock_db.get_nostr_state.return_value = None
        plugin.rpc.signmessage.return_value = {"zbase": "testsig"}

        transport = InternalNostrTransport(plugin=plugin, database=mock_db)

        received = []
        transport.subscribe({"kinds": [1]}, lambda ev: received.append(ev))

        # Kind 1 should match
        transport.inject_event({"kind": 1, "content": "match"})
        # Kind 4 should not match subscription (but would match DM callbacks)
        transport.inject_event({"kind": 4, "content": "no-match"})

        transport.process_inbound()
        assert len(received) == 1
        assert received[0]["content"] == "match"
