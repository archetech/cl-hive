"""Tests for current Nostr transport behavior (legacy alias + external transport)."""

import sys
import types
from unittest.mock import MagicMock

import pytest

# Minimal stub for environments without pyln installed.
if "pyln.client" not in sys.modules:
    pyln_module = sys.modules.setdefault("pyln", types.ModuleType("pyln"))
    client_module = types.ModuleType("pyln.client")

    class RpcError(Exception):
        pass

    client_module.RpcError = RpcError
    sys.modules["pyln.client"] = client_module
    pyln_module.client = client_module

from modules.nostr_transport import ExternalCommsTransport


@pytest.fixture
def mock_plugin():
    plugin = MagicMock()
    plugin.log = MagicMock()
    plugin.rpc = MagicMock()
    return plugin


def test_external_transport_identity_uses_rpc(mock_plugin):
    mock_plugin.rpc.call.return_value = {"pubkey": "a" * 64}
    transport = ExternalCommsTransport(mock_plugin)

    identity = transport.get_identity()

    assert identity["pubkey"] == "a" * 64
    assert identity["privkey"] == ""
    mock_plugin.rpc.call.assert_called_once_with("hive-client-identity", {"action": "get"})


def test_publish_calls_comms_rpc(mock_plugin):
    mock_plugin.rpc.call.return_value = {"id": "evt1"}
    transport = ExternalCommsTransport(mock_plugin)

    result = transport.publish({"kind": 1, "content": "hello"})

    assert result == {"id": "evt1"}
    method, params = mock_plugin.rpc.call.call_args[0]
    assert method == "hive-comms-publish-event"
    assert "event_json" in params


def test_send_dm_empty_recipient_is_dropped(mock_plugin):
    transport = ExternalCommsTransport(mock_plugin)

    result = transport.send_dm("", "hello")

    assert result == {}
    mock_plugin.rpc.call.assert_not_called()


def test_inject_packet_processes_callbacks(mock_plugin):
    transport = ExternalCommsTransport(mock_plugin)
    seen = []
    transport.receive_dm(lambda evt: seen.append(evt))

    assert transport.inject_packet({"kind": 4, "content": "hi"}, transport_pubkey="b" * 64) is True
    processed = transport.process_inbound()

    assert processed == 1
    assert len(seen) == 1
    assert seen[0]["pubkey"] == "b" * 64
    assert seen[0]["payload"]["content"] == "hi"
    assert "\"content\": \"hi\"" in seen[0]["plaintext"]


def test_subscribe_and_unsubscribe_placeholders(mock_plugin):
    transport = ExternalCommsTransport(mock_plugin)

    sub_id = transport.subscribe({"kinds": [38901]}, lambda evt: None)

    assert sub_id == "remote-sub-placeholder"
    assert transport.unsubscribe(sub_id) is True
