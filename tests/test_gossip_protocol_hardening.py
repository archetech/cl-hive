"""
Hardening tests for the strict v2 gossip/state-sync protocol helpers.
"""

import json
import os
import sys
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import modules.protocol as protocol
import modules.protocol_handlers as protocol_handlers
from modules.gossip import GossipManager


def _make_v2_gossip_payload(sender_id, envelope_version=2):
    return {
        "_envelope_version": envelope_version,
        "sender_id": sender_id,
        "peer_id": sender_id,
        "timestamp": int(time.time()),
        "version": 7,
        "fleet_hash": "f" * 64,
        "capacity_sats": 1000,
        "available_sats": 500,
        "fee_policy": {"base_fee": 1000, "fee_rate": 10},
        "topology": ["03" + "b" * 64],
        "addresses": ["1.2.3.4:9735"],
        "capabilities": ["mcf"],
        "signature": "legacy_signature",
        "signature_v2": "strict_signature_v2",
    }


@pytest.fixture
def gossip_handler_env(monkeypatch):
    plugin = MagicMock()
    plugin.log = MagicMock()
    plugin.rpc.listpeers.return_value = {"peers": []}

    database = MagicMock()
    database.is_banned.return_value = False

    gossip_mgr = MagicMock()
    gossip_mgr.process_gossip.return_value = True

    monkeypatch.setattr(protocol_handlers, "plugin", plugin)
    monkeypatch.setattr(protocol_handlers, "database", database)
    monkeypatch.setattr(protocol_handlers, "gossip_mgr", gossip_mgr)
    monkeypatch.setattr(protocol_handlers, "relay_mgr", None)
    monkeypatch.setattr(protocol_handlers, "our_pubkey", "02" + "f" * 64)

    return plugin, database, gossip_mgr


def _make_v2_state_hash_payload(sender_id, envelope_version=2):
    return {
        "_envelope_version": envelope_version,
        "sender_id": sender_id,
        "fleet_hash": "f" * 64,
        "membership_hash": "m" * 64,
        "timestamp": int(time.time()),
        "peer_count": 3,
        "signature": "legacy_signature",
        "signature_v2": "strict_signature_v2",
    }


def _make_v2_full_sync_payload(sender_id, envelope_version=2, states=None, members=None):
    states = [] if states is None else states
    members = [] if members is None else members
    return {
        "_envelope_version": envelope_version,
        "sender_id": sender_id,
        "fleet_hash": "",
        "timestamp": int(time.time()),
        "states": states,
        "members": members,
        "states_hash_v2": protocol.compute_full_sync_states_hash_v2(states),
        "members_hash_v2": protocol.compute_full_sync_members_hash_v2(members),
        "signature": "legacy_signature",
        "signature_v2": "strict_signature_v2",
    }


@pytest.fixture
def state_sync_handler_env(monkeypatch):
    plugin = MagicMock()
    plugin.log = MagicMock()
    plugin.rpc.listpeers.return_value = {"peers": []}

    database = MagicMock()
    database.is_banned.return_value = False

    gossip_mgr = MagicMock()
    gossip_mgr.process_state_hash.return_value = True
    gossip_mgr.process_full_sync.return_value = 0

    state_manager = MagicMock()

    monkeypatch.setattr(protocol_handlers, "plugin", plugin)
    monkeypatch.setattr(protocol_handlers, "database", database)
    monkeypatch.setattr(protocol_handlers, "gossip_mgr", gossip_mgr)
    monkeypatch.setattr(protocol_handlers, "state_manager", state_manager)
    monkeypatch.setattr(protocol_handlers, "our_pubkey", "02" + "f" * 64)

    return plugin, database, gossip_mgr, state_manager


@pytest.fixture
def outbound_handler_env(monkeypatch):
    plugin = MagicMock()
    plugin.log = MagicMock()
    plugin.rpc.signmessage.return_value = {"zbase": "strict_signature_v2"}

    database = MagicMock()
    database.is_banned.return_value = False
    database.get_all_members.return_value = []

    gossip_mgr = MagicMock()

    monkeypatch.setattr(protocol_handlers, "plugin", plugin)
    monkeypatch.setattr(protocol_handlers, "database", database)
    monkeypatch.setattr(protocol_handlers, "gossip_mgr", gossip_mgr)
    monkeypatch.setattr(protocol_handlers, "our_pubkey", "02" + "f" * 64)

    return plugin, database, gossip_mgr


def test_handle_gossip_rejects_legacy_payload_without_signature_v2(gossip_handler_env):
    plugin, database, gossip_mgr = gossip_handler_env
    sender_id = "02" + "a" * 64
    payload = _make_v2_gossip_payload(sender_id)
    payload.pop("signature_v2")
    payload.pop("_envelope_version")

    database.get_member.return_value = {"peer_id": sender_id, "tier": "member"}

    result = protocol_handlers.handle_gossip(sender_id, payload, plugin)

    assert result == {"result": "continue"}
    plugin.rpc.checkmessage.assert_not_called()
    gossip_mgr.process_gossip.assert_not_called()
    database.update_member.assert_not_called()
    plugin.rpc.connect.assert_not_called()


def test_handle_gossip_rejects_envelope_v1(gossip_handler_env):
    plugin, database, gossip_mgr = gossip_handler_env
    sender_id = "02" + "a" * 64
    payload = _make_v2_gossip_payload(sender_id, envelope_version=1)

    database.get_member.return_value = {"peer_id": sender_id, "tier": "member"}

    result = protocol_handlers.handle_gossip(sender_id, payload, plugin)

    assert result == {"result": "continue"}
    plugin.rpc.checkmessage.assert_not_called()
    gossip_mgr.process_gossip.assert_not_called()
    database.update_member.assert_not_called()
    plugin.rpc.connect.assert_not_called()


def test_handle_gossip_accepts_v2_payload_and_persists_addresses(gossip_handler_env):
    plugin, database, gossip_mgr = gossip_handler_env
    sender_id = "02" + "a" * 64
    payload = _make_v2_gossip_payload(sender_id)

    database.get_member.return_value = {"peer_id": sender_id, "tier": "member"}
    plugin.rpc.checkmessage.return_value = {"verified": True, "pubkey": sender_id}

    result = protocol_handlers.handle_gossip(sender_id, payload, plugin)

    assert result == {"result": "continue"}
    assert gossip_mgr.process_gossip.call_count == 1
    assert gossip_mgr.process_gossip.call_args.args == (sender_id, payload)
    plugin.rpc.checkmessage.assert_called_once_with(
        protocol.get_gossip_signing_payload_v2(payload),
        payload["signature_v2"],
        sender_id,
    )
    database.update_member.assert_called_once_with(
        sender_id,
        addresses=json.dumps(["1.2.3.4:9735"]),
    )


def test_handle_gossip_accepts_v2_payload_and_autoconnects(gossip_handler_env):
    plugin, database, gossip_mgr = gossip_handler_env
    sender_id = "02" + "a" * 64
    payload = _make_v2_gossip_payload(sender_id)

    database.get_member.return_value = {"peer_id": sender_id, "tier": "member"}
    plugin.rpc.checkmessage.return_value = {"verified": True, "pubkey": sender_id}

    result = protocol_handlers.handle_gossip(sender_id, payload, plugin)

    assert result == {"result": "continue"}
    assert gossip_mgr.process_gossip.call_count == 1
    plugin.rpc.connect.assert_called_once_with(f"{sender_id}@1.2.3.4:9735")


def test_handle_state_hash_rejects_legacy_payload_without_signature_v2(state_sync_handler_env):
    plugin, database, gossip_mgr, _state_manager = state_sync_handler_env
    sender_id = "02" + "a" * 64
    payload = _make_v2_state_hash_payload(sender_id)
    payload.pop("signature_v2")
    payload.pop("_envelope_version")

    database.get_member.return_value = {"peer_id": sender_id, "tier": "member"}

    result = protocol_handlers.handle_state_hash(sender_id, payload, plugin)

    assert result == {"result": "continue"}
    plugin.rpc.checkmessage.assert_not_called()
    gossip_mgr.process_state_hash.assert_not_called()


def test_handle_state_hash_accepts_v2_payload_with_membership_hash(state_sync_handler_env):
    plugin, database, gossip_mgr, _state_manager = state_sync_handler_env
    sender_id = "02" + "a" * 64
    payload = _make_v2_state_hash_payload(sender_id)

    database.get_member.return_value = {"peer_id": sender_id, "tier": "member"}
    plugin.rpc.checkmessage.return_value = {"verified": True, "pubkey": sender_id}

    result = protocol_handlers.handle_state_hash(sender_id, payload, plugin)

    assert result == {"result": "continue"}
    plugin.rpc.checkmessage.assert_called_once_with(
        protocol.get_state_hash_signing_payload_v2(payload),
        payload["signature_v2"],
        sender_id,
    )
    gossip_mgr.process_state_hash.assert_called_once_with(sender_id, payload)
    plugin.rpc.call.assert_not_called()


def test_handle_full_sync_rejects_legacy_payload_without_signature_v2(state_sync_handler_env):
    plugin, database, gossip_mgr, _state_manager = state_sync_handler_env
    sender_id = "02" + "a" * 64
    payload = _make_v2_full_sync_payload(sender_id)
    payload.pop("signature_v2")
    payload.pop("_envelope_version")

    database.get_member.return_value = {"peer_id": sender_id, "tier": "member"}

    result = protocol_handlers.handle_full_sync(sender_id, payload, plugin)

    assert result == {"result": "continue"}
    plugin.rpc.checkmessage.assert_not_called()
    gossip_mgr.process_full_sync.assert_not_called()


def test_handle_full_sync_rejects_envelope_v1(state_sync_handler_env):
    plugin, database, gossip_mgr, _state_manager = state_sync_handler_env
    sender_id = "02" + "a" * 64
    payload = _make_v2_full_sync_payload(sender_id, envelope_version=1)

    database.get_member.return_value = {"peer_id": sender_id, "tier": "member"}

    result = protocol_handlers.handle_full_sync(sender_id, payload, plugin)

    assert result == {"result": "continue"}
    plugin.rpc.checkmessage.assert_not_called()
    gossip_mgr.process_full_sync.assert_not_called()


def test_handle_full_sync_v2_applies_foreign_rows_when_hashes_verify(state_sync_handler_env):
    plugin, database, gossip_mgr, _state_manager = state_sync_handler_env
    sender_id = "02" + "a" * 64
    states = [
        {
            "peer_id": "02" + "b" * 64,
            "version": 2,
            "timestamp": 1711200100,
            "capacity_sats": 1000,
            "available_sats": 500,
            "fee_policy": {"base_fee": 1000, "fee_rate": 10},
            "topology": ["03" + "c" * 64],
            "addresses": ["10.0.0.1:9735"],
            "capabilities": ["mcf"],
        }
    ]
    members = [
        {
            "peer_id": "02" + "d" * 64,
            "tier": "member",
            "joined_at": 1711200200,
            "addresses": ["10.0.0.2:9735"],
            "capabilities": ["mcf"],
        }
    ]
    payload = _make_v2_full_sync_payload(sender_id, states=states, members=members)

    def get_member(peer):
        if peer == sender_id:
            return {"peer_id": sender_id, "tier": "member"}
        return None

    database.get_member.side_effect = get_member
    plugin.rpc.checkmessage.return_value = {"verified": True, "pubkey": sender_id}
    gossip_mgr.process_full_sync.return_value = 1

    result = protocol_handlers.handle_full_sync(sender_id, payload, plugin)

    assert result == {"result": "continue"}
    plugin.rpc.checkmessage.assert_called_once_with(
        protocol.get_full_sync_signing_payload_v2(payload),
        payload["signature_v2"],
        sender_id,
    )
    gossip_mgr.process_full_sync.assert_called_once_with(sender_id, payload)
    database.add_member.assert_called_once_with(
        peer_id="02" + "d" * 64,
        tier="member",
        joined_at=1711200200,
    )
    database.update_member.assert_called_once_with(
        "02" + "d" * 64,
        addresses=json.dumps(["10.0.0.2:9735"]),
    )


def test_handle_full_sync_v2_rejects_state_hash_mismatch(state_sync_handler_env):
    plugin, database, gossip_mgr, _state_manager = state_sync_handler_env
    sender_id = "02" + "a" * 64
    states = [
        {
            "peer_id": "02" + "b" * 64,
            "version": 2,
            "timestamp": 1711200100,
            "capacity_sats": 1000,
            "available_sats": 500,
            "fee_policy": {"base_fee": 1000, "fee_rate": 10},
            "topology": ["03" + "c" * 64],
            "addresses": ["10.0.0.1:9735"],
            "capabilities": ["mcf"],
        }
    ]
    payload = _make_v2_full_sync_payload(sender_id, states=states)
    payload["states_hash_v2"] = "x" * 64

    database.get_member.return_value = {"peer_id": sender_id, "tier": "member"}

    result = protocol_handlers.handle_full_sync(sender_id, payload, plugin)

    assert result == {"result": "continue"}
    plugin.rpc.checkmessage.assert_not_called()
    gossip_mgr.process_full_sync.assert_not_called()


def test_handle_full_sync_v2_rejects_invalid_member_pubkey(state_sync_handler_env):
    plugin, database, gossip_mgr, _state_manager = state_sync_handler_env
    sender_id = "02" + "a" * 64
    payload = _make_v2_full_sync_payload(
        sender_id,
        members=[
            {
                "peer_id": "02" + "d" * 64,
                "tier": "member",
                "joined_at": 1711200200,
                "addresses": ["10.0.0.2:9735"],
                "capabilities": ["mcf"],
            }
        ],
    )
    payload["members"][0]["peer_id"] = "not-a-pubkey"
    payload["members_hash_v2"] = "x" * 64

    database.get_member.return_value = {"peer_id": sender_id, "tier": "member"}

    result = protocol_handlers.handle_full_sync(sender_id, payload, plugin)

    assert result == {"result": "continue"}
    plugin.rpc.checkmessage.assert_not_called()
    gossip_mgr.process_full_sync.assert_not_called()


def test_handle_full_sync_v2_rejects_invalid_address_shape(state_sync_handler_env):
    plugin, database, gossip_mgr, _state_manager = state_sync_handler_env
    sender_id = "02" + "a" * 64
    payload = _make_v2_full_sync_payload(
        sender_id,
        members=[
            {
                "peer_id": "02" + "d" * 64,
                "tier": "member",
                "joined_at": 1711200200,
                "addresses": ["10.0.0.2:9735"],
                "capabilities": ["mcf"],
            }
        ],
    )
    payload["members"][0]["addresses"] = ["invalid-address"]
    payload["members_hash_v2"] = "d16dca6cd60cda0c08ca334a5cda9fa3517e35998bb0632cebfff0e293aff143"

    database.get_member.return_value = {"peer_id": sender_id, "tier": "member"}

    result = protocol_handlers.handle_full_sync(sender_id, payload, plugin)

    assert result == {"result": "continue"}
    plugin.rpc.checkmessage.assert_not_called()
    gossip_mgr.process_full_sync.assert_not_called()


def test_create_signed_gossip_msg_emits_envelope_v2_and_signature_v2(outbound_handler_env):
    plugin, _database, gossip_mgr = outbound_handler_env
    gossip_mgr.create_gossip_payload.return_value = {
        "peer_id": "02" + "f" * 64,
        "capacity_sats": 1000,
        "available_sats": 500,
        "fee_policy": {"base_fee": 1000, "fee_rate": 10},
        "topology": ["03" + "b" * 64],
        "version": 7,
        "timestamp": 1711200100,
        "fleet_hash": "f" * 64,
        "addresses": ["1.2.3.4:9735"],
        "capabilities": ["mcf"],
    }

    message = protocol_handlers._create_signed_gossip_msg(
        capacity_sats=1000,
        available_sats=500,
        fee_policy={"base_fee": 1000, "fee_rate": 10},
        topology=["03" + "b" * 64],
        addresses=["1.2.3.4:9735"],
    )

    msg_type, payload = protocol.deserialize(message)

    assert msg_type == protocol.HiveMessageType.GOSSIP
    assert payload["_envelope_version"] == protocol.STRICT_STATE_SYNC_VERSION
    assert payload["signature_v2"] == "strict_signature_v2"
    plugin.rpc.signmessage.assert_called_once_with(
        protocol.get_gossip_signing_payload_v2(payload)
    )


def test_create_signed_state_hash_msg_emits_envelope_v2_and_signature_v2(outbound_handler_env, monkeypatch):
    plugin, _database, gossip_mgr = outbound_handler_env
    gossip_mgr.create_state_hash_payload.return_value = {
        "fleet_hash": "f" * 64,
        "membership_hash": "m" * 64,
        "peer_count": 3,
        "timestamp": 1711200200,
    }
    monkeypatch.setattr(protocol_handlers.time, "time", lambda: 1711200300)

    message = protocol_handlers._create_signed_state_hash_msg()

    msg_type, payload = protocol.deserialize(message)

    assert msg_type == protocol.HiveMessageType.STATE_HASH
    assert payload["_envelope_version"] == protocol.STRICT_STATE_SYNC_VERSION
    assert payload["signature_v2"] == "strict_signature_v2"
    assert payload["membership_hash"] == "m" * 64
    plugin.rpc.signmessage.assert_called_once_with(
        protocol.get_state_hash_signing_payload_v2(payload)
    )


def test_create_signed_full_sync_msg_emits_envelope_v2_hashes_and_signature_v2(outbound_handler_env, monkeypatch):
    plugin, database, gossip_mgr = outbound_handler_env
    states = [
        {
            "peer_id": "02" + "a" * 64,
            "version": 2,
            "timestamp": 1711200100,
            "capacity_sats": 1000,
            "available_sats": 500,
            "fee_policy": {"base_fee": 1000, "fee_rate": 10},
            "topology": ["03" + "b" * 64],
            "addresses": ["10.0.0.1:9735"],
            "capabilities": ["mcf"],
        }
    ]
    database.get_all_members.return_value = [
        {
            "peer_id": "02" + "d" * 64,
            "tier": "member",
            "joined_at": 1711200200,
            "addresses": json.dumps(["10.0.0.2:9735"]),
        }
    ]
    gossip_mgr.create_full_sync_payload.return_value = {
        "states": states,
        "fleet_hash": "f" * 64,
        "timestamp": 1711200200,
    }
    monkeypatch.setattr(protocol_handlers.time, "time", lambda: 1711200300)

    message = protocol_handlers._create_signed_full_sync_msg()

    msg_type, payload = protocol.deserialize(message)

    assert msg_type == protocol.HiveMessageType.FULL_SYNC
    assert payload["_envelope_version"] == protocol.STRICT_STATE_SYNC_VERSION
    assert payload["signature_v2"] == "strict_signature_v2"
    assert payload["states_hash_v2"] == protocol.compute_full_sync_states_hash_v2(payload["states"])
    assert payload["members_hash_v2"] == protocol.compute_full_sync_members_hash_v2(payload["members"])
    plugin.rpc.signmessage.assert_called_once_with(
        protocol.get_full_sync_signing_payload_v2(payload)
    )


def test_broadcast_member_message_preserves_v2_envelope_for_prebuilt_full_sync_bytes(
    outbound_handler_env,
    monkeypatch,
):
    plugin, database, gossip_mgr = outbound_handler_env
    target_id = "02" + "a" * 64
    database.get_all_members.return_value = [
        {
            "peer_id": target_id,
            "tier": "member",
            "joined_at": 1711200200,
        }
    ]
    gossip_mgr.create_full_sync_payload.return_value = {
        "states": [
            {
                "peer_id": "02" + "b" * 64,
                "version": 2,
                "timestamp": 1711200100,
                "capacity_sats": 1000,
                "available_sats": 500,
                "fee_policy": {"base_fee": 1000, "fee_rate": 10},
                "topology": ["03" + "c" * 64],
                "addresses": ["10.0.0.1:9735"],
                "capabilities": ["mcf"],
            }
        ],
        "fleet_hash": "f" * 64,
        "timestamp": 1711200200,
    }
    shutdown_event = MagicMock()
    shutdown_event.wait = MagicMock()
    monkeypatch.setattr(protocol_handlers, "shutdown_event", shutdown_event)
    monkeypatch.setattr(protocol_handlers.time, "time", lambda: 1711200300)

    full_sync_msg = protocol_handlers._create_signed_full_sync_msg()

    result = protocol_handlers._broadcast_member_message(
        message_bytes=full_sync_msg,
        reliability="direct",
        targets=[target_id],
    )

    sent_hex = plugin.rpc.call.call_args.args[1]["msg"]
    msg_type, payload = protocol.deserialize(bytes.fromhex(sent_hex))

    assert result["ok"] is True
    assert msg_type == protocol.HiveMessageType.FULL_SYNC
    assert payload["_envelope_version"] == protocol.STRICT_STATE_SYNC_VERSION


def test_full_sync_processing_uses_protocol_limit():
    plugin = MagicMock()
    plugin.log = MagicMock()
    state_manager = MagicMock()
    gossip_manager = GossipManager(state_manager, plugin)
    sender_id = "02" + "a" * 64
    oversized_states = [
        {
            "peer_id": f"peer_{i:04d}",
            "version": 1,
            "timestamp": 1711200100,
        }
        for i in range(protocol.MAX_FULL_SYNC_STATES + 1)
    ]

    result = gossip_manager.process_full_sync(
        sender_id,
        {"states": oversized_states},
    )

    assert result == 0
    state_manager.apply_full_sync.assert_not_called()


def test_full_sync_states_hash_v2_changes_when_state_contents_change():
    base_states = [
        {
            "peer_id": "02" + "a" * 64,
            "version": 1,
            "timestamp": 1711200100,
            "topology": ["03" + "b" * 64, "02" + "c" * 64],
            "addresses": ["10.0.0.1:9735"],
            "capabilities": ["mcf"],
        }
    ]
    changed_states = [
        {
            "peer_id": "02" + "a" * 64,
            "version": 1,
            "timestamp": 1711200100,
            "topology": ["03" + "b" * 64, "02" + "c" * 64],
            "addresses": ["10.0.0.2:9735"],
            "capabilities": ["mcf"],
        }
    ]

    base = protocol.compute_full_sync_states_hash_v2(base_states)
    changed = protocol.compute_full_sync_states_hash_v2(changed_states)

    assert base != changed


def test_full_sync_states_hash_v2_fails_closed_on_malformed_state():
    base_states = [
        {
            "peer_id": "02" + "a" * 64,
            "version": 1,
            "timestamp": 1711200100,
            "topology": ["03" + "b" * 64],
            "addresses": ["10.0.0.1:9735"],
            "capabilities": ["mcf"],
        }
    ]
    malformed_states = base_states + [123]

    base = protocol.compute_full_sync_states_hash_v2(base_states)

    assert base
    with pytest.raises(ValueError):
        protocol.compute_full_sync_states_hash_v2(malformed_states)


def test_full_sync_members_hash_v2_fails_closed_on_malformed_member():
    base_members = [
        {
            "peer_id": "02" + "a" * 64,
            "tier": "member",
            "joined_at": 1711200200,
            "addresses": ["10.0.0.1:9735"],
            "capabilities": ["mcf"],
        }
    ]
    malformed_members = base_members + [123]

    base = protocol.compute_full_sync_members_hash_v2(base_members)

    assert base
    with pytest.raises(ValueError):
        protocol.compute_full_sync_members_hash_v2(malformed_members)


def test_full_sync_signing_payload_v2_fails_closed_on_malformed_content():
    payload = {
        "sender_id": "02" + "f" * 64,
        "fleet_hash": "9" * 64,
        "timestamp": 1711200300,
        "states": [
            {
                "peer_id": "02" + "a" * 64,
                "version": 1,
                "timestamp": 1711200100,
                "topology": ["03" + "b" * 64],
                "addresses": ["10.0.0.1:9735"],
                "capabilities": ["mcf"],
            }
        ],
        "members": [
            {
                "peer_id": "02" + "c" * 64,
                "tier": "member",
                "joined_at": 1711200200,
                "addresses": ["10.0.0.2:9735"],
                "capabilities": ["mcf"],
            }
        ],
    }
    malformed_payload = dict(
        payload,
        states=payload["states"] + [123],
        members=payload["members"] + [123],
    )

    base = protocol.get_full_sync_signing_payload_v2(payload)

    assert base
    with pytest.raises(ValueError):
        protocol.get_full_sync_signing_payload_v2(malformed_payload)


@pytest.mark.parametrize(
    "field_name, field_value, expected_error",
    [
        ("states", None, "states must be a list"),
        ("states", "", "states must be a list"),
        ("members", None, "members must be a list"),
        ("members", "", "members must be a list"),
    ],
)
def test_full_sync_signing_payload_v2_fails_closed_on_null_or_non_list_collections(
    field_name,
    field_value,
    expected_error,
):
    payload = {
        "sender_id": "02" + "f" * 64,
        "fleet_hash": "9" * 64,
        "timestamp": 1711200300,
        "states": [],
        "members": [],
    }
    payload[field_name] = field_value

    with pytest.raises(ValueError, match=expected_error):
        protocol.get_full_sync_signing_payload_v2(payload)


def test_full_sync_v2_helpers_are_order_insensitive_for_normalized_rows():
    payload = {
        "sender_id": "02" + "f" * 64,
        "fleet_hash": "9" * 64,
        "timestamp": 1711200300,
        "states": [
            {
                "peer_id": "02" + "a" * 64,
                "version": 1,
                "timestamp": 1711200100,
                "topology": ["03" + "b" * 64, "02" + "c" * 64],
                "addresses": ["10.0.0.2:9735", "10.0.0.1:9735"],
                "capabilities": ["beta", "alpha"],
            },
            {
                "peer_id": "02" + "d" * 64,
                "version": 2,
                "timestamp": 1711200200,
                "topology": ["03" + "e" * 64],
                "addresses": ["10.0.0.4:9735", "10.0.0.3:9735"],
                "capabilities": ["mcf"],
            },
        ],
        "members": [
            {
                "peer_id": "02" + "c" * 64,
                "tier": "member",
                "joined_at": 1711200200,
                "addresses": ["10.0.0.2:9735", "10.0.0.1:9735"],
                "capabilities": ["mcf", "alpha"],
            },
            {
                "peer_id": "02" + "b" * 64,
                "tier": "member",
                "joined_at": 1711200100,
                "addresses": ["10.0.0.4:9735", "10.0.0.3:9735"],
                "capabilities": ["beta"],
            },
        ],
    }
    reordered_payload = dict(
        payload,
        states=list(reversed(payload["states"])),
        members=list(reversed(payload["members"])),
    )

    assert protocol.compute_full_sync_states_hash_v2(payload["states"]) == protocol.compute_full_sync_states_hash_v2(reordered_payload["states"])
    assert protocol.compute_full_sync_members_hash_v2(payload["members"]) == protocol.compute_full_sync_members_hash_v2(reordered_payload["members"])
    assert protocol.get_full_sync_signing_payload_v2(payload) == protocol.get_full_sync_signing_payload_v2(reordered_payload)


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"_envelope_version": 2}, True),
        ({"_envelope_version": 1}, False),
        ({}, False),
    ],
)
def test_state_sync_messages_require_envelope_version_2(payload, expected):
    assert protocol.is_strict_state_sync_payload(payload) is expected


def test_state_sync_messages_reject_non_dict_payloads():
    assert protocol.is_strict_state_sync_payload(None) is False
    assert protocol.is_strict_state_sync_payload("not a payload") is False
