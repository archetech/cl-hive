"""Regression tests for membership protocol handlers."""

import os
import sys
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.database import HiveDatabase
import modules.protocol as protocol
import modules.protocol_handlers as ph


PEER_A = "02" + "a" * 64
PEER_B = "02" + "b" * 64


@pytest.fixture
def mock_plugin():
    plugin = MagicMock()
    plugin.log = MagicMock()
    plugin.rpc = MagicMock()
    plugin.rpc.call = MagicMock()
    plugin.rpc.signmessage.return_value = {"zbase": "signed"}
    plugin.rpc.checkmessage.return_value = {"verified": True, "pubkey": PEER_B}
    plugin.rpc.listpeers.return_value = {"peers": [{"netaddr": ["1.2.3.4:9735"]}]}
    plugin.rpc.listpeerchannels.return_value = {"channels": []}
    plugin.rpc.listnodes.return_value = {"nodes": [{"alias": "peer"}]}
    plugin.rpc.getinfo.return_value = {"id": PEER_A}
    return plugin


@pytest.fixture
def db(mock_plugin, tmp_path):
    database = HiveDatabase(str(tmp_path / "membership_protocol.db"), mock_plugin)
    database.initialize()
    return database


@pytest.fixture(autouse=True)
def restore_protocol_handler_globals():
    saved = {
        "database": ph.database,
        "plugin": ph.plugin,
        "our_pubkey": ph.our_pubkey,
        "state_manager": ph.state_manager,
        "handshake_mgr": ph.handshake_mgr,
        "config": ph.config,
        "relay_mgr": ph.relay_mgr,
        "gossip_mgr": ph.gossip_mgr,
        "intent_mgr": ph.intent_mgr,
    }
    yield
    for name, value in saved.items():
        setattr(ph, name, value)


def attest_payload_for(pubkey: str) -> dict:
    return {
        "manifest": {
            "pubkey": pubkey,
            "version": "cl-hive v2.2.6",
            "features": ["proto-v2"],
            "timestamp": int(time.time()),
            "nonce": "n" * 64,
        },
        "pubkey": pubkey,
        "version": "cl-hive v2.2.6",
        "features": ["proto-v2"],
        "nonce_signature": "nonce_sig",
        "manifest_signature": "manifest_sig",
    }


def test_handle_challenge_requires_pending_outbound_hello(mock_plugin):
    ph.handshake_mgr = MagicMock()
    ph.handshake_mgr.has_pending_outbound_hello.return_value = False
    ph.handshake_mgr.create_manifest = MagicMock()

    result = ph.handle_challenge(PEER_B, {"nonce": "n" * 64, "hive_id": "h"}, mock_plugin)

    assert result == {"result": "continue"}
    ph.handshake_mgr.create_manifest.assert_not_called()
    mock_plugin.rpc.call.assert_not_called()


def test_handle_attest_does_not_activate_member_when_welcome_send_fails(db, mock_plugin):
    mock_plugin.rpc.call.side_effect = Exception("send failed")
    ph.database = db
    ph.plugin = mock_plugin
    ph.our_pubkey = PEER_A
    ph.config = MagicMock()
    ph.state_manager = MagicMock()
    ph.state_manager.calculate_fleet_hash.return_value = "0" * 64
    ph.handshake_mgr = MagicMock()
    ph.handshake_mgr.get_pending_challenge.return_value = {
        "nonce": "n" * 64,
        "issued_at": int(time.time()),
        "requirements": 0,
        "initial_tier": "member",
    }
    ph.handshake_mgr.verify_manifest.return_value = (True, "")
    ph.handshake_mgr.check_requirements.return_value = (True, [])

    result = ph.handle_attest(PEER_B, attest_payload_for(PEER_B), mock_plugin)

    assert result == {"result": "continue"}
    assert db.get_member(PEER_B) is None
    ph.handshake_mgr.clear_challenge.assert_not_called()


def test_handle_ban_rejects_non_member_sender(db, mock_plugin):
    db.add_member(PEER_B, tier="member", joined_at=1)
    ph.database = db

    result = ph.handle_ban(PEER_A, {"peer_id": PEER_B, "reason": "spoofed"}, mock_plugin)

    assert result["status"] == "ignored"
    assert db.is_banned(PEER_B) is False
    assert db.get_member(PEER_B) is not None


def test_handle_member_left_rejects_stale_timestamp(db, mock_plugin):
    db.add_member(PEER_B, tier="member", joined_at=int(time.time()))
    ph.database = db
    ph.config = MagicMock()
    mock_plugin.rpc.checkmessage.return_value = {"verified": True, "pubkey": PEER_B}

    result = ph.handle_member_left(
        PEER_B,
        {
            "peer_id": PEER_B,
            "timestamp": 1,
            "reason": "old-leave",
            "signature": "sig",
        },
        mock_plugin,
    )

    assert result == {"result": "continue"}
    assert db.get_member(PEER_B) is not None


def test_handle_member_left_rejects_pre_rejoin_event(db, mock_plugin):
    joined_at = int(time.time())
    replay_timestamp = joined_at - 1
    db.add_member(PEER_B, tier="member", joined_at=joined_at)
    ph.database = db
    ph.config = MagicMock()
    mock_plugin.rpc.checkmessage.return_value = {"verified": True, "pubkey": PEER_B}

    result = ph.handle_member_left(
        PEER_B,
        {
            "peer_id": PEER_B,
            "timestamp": replay_timestamp,
            "reason": "old-leave",
            "signature": "sig",
        },
        mock_plugin,
    )

    assert result == {"result": "continue"}
    assert db.get_member(PEER_B) is not None


def member_removed_payload(actor: str, target: str, timestamp: int, *, joined_at_cutoff: int, reason: str = "maintenance") -> dict:
    return {
        "peer_id": target,
        "actor_peer_id": actor,
        "reason": reason,
        "timestamp": timestamp,
        "event_id": f"evt-{timestamp}",
        "joined_at_cutoff": joined_at_cutoff,
        "signature": "sig",
    }


def test_handle_member_removed_rejects_non_member_sender(db, mock_plugin):
    db.add_member(PEER_B, tier="member", joined_at=100)
    ph.database = db
    ph.state_manager = MagicMock()
    mock_plugin.rpc.checkmessage.return_value = {"verified": True, "pubkey": PEER_A}

    result = ph.handle_member_removed(
        PEER_A,
        member_removed_payload(PEER_A, PEER_B, int(time.time()), joined_at_cutoff=100),
        mock_plugin,
    )

    assert result == {"result": "continue"}
    assert db.get_member(PEER_B) is not None


def test_handle_member_removed_clears_state_and_records_tombstone(db, mock_plugin):
    now = int(time.time())
    db.add_member(PEER_A, tier="member", joined_at=90)
    db.add_member(PEER_B, tier="member", joined_at=100)
    ph.database = db
    ph.state_manager = MagicMock()
    mock_plugin.rpc.checkmessage.return_value = {"verified": True, "pubkey": PEER_A}

    result = ph.handle_member_removed(
        PEER_A,
        member_removed_payload(PEER_A, PEER_B, now, joined_at_cutoff=100),
        mock_plugin,
    )

    assert result == {"result": "continue"}
    assert db.get_member(PEER_B) is None
    ph.state_manager.remove_peer_state.assert_called_once_with(PEER_B)
    assert db.get_membership_tombstones(limit=10)[0]["peer_id"] == PEER_B


def test_apply_membership_sync_applies_membership_events_before_add_only_merge(db, mock_plugin):
    db.add_member(PEER_A, tier="member", joined_at=100)
    db.add_member(PEER_B, tier="member", joined_at=200)
    ph.database = db
    ph.state_manager = MagicMock()

    events = [{
        "event_id": "evt-1",
        "peer_id": PEER_B,
        "event": "removed",
        "actor_peer_id": PEER_A,
        "reason": "maintenance",
        "timestamp": 250,
        "joined_at_cutoff": 200,
    }]
    members = [{"peer_id": PEER_A, "tier": "member", "joined_at": 100}]

    changed = ph._apply_membership_sync(members, PEER_A, mock_plugin, membership_events=events)

    assert changed == 1
    assert db.get_member(PEER_B) is None


def test_handle_full_sync_applies_membership_events_before_member_merge(db, mock_plugin):
    now = int(time.time())
    db.add_member(PEER_A, tier="member", joined_at=100)
    db.add_member(PEER_B, tier="member", joined_at=200)
    ph.database = db
    ph.gossip_mgr = MagicMock()
    ph.gossip_mgr.process_full_sync.return_value = 0
    ph.state_manager = MagicMock()
    mock_plugin.rpc.checkmessage.return_value = {"verified": True, "pubkey": PEER_A}

    payload = {
        "_envelope_version": protocol.STRICT_STATE_SYNC_VERSION,
        "sender_id": PEER_A,
        "timestamp": now,
        "signature": "signedpayload",
        "signature_v2": "signedpayload",
        "fleet_hash": "",
        "states": [],
        "members": [{"peer_id": PEER_A, "tier": "member", "joined_at": 100}],
        "membership_events": [{
            "event_id": "evt-1",
            "peer_id": PEER_B,
            "event": "removed",
            "actor_peer_id": PEER_A,
            "reason": "maintenance",
            "timestamp": now,
            "joined_at_cutoff": 200,
        }],
    }
    payload["states_hash_v2"] = protocol.compute_full_sync_states_hash_v2(payload["states"])
    payload["members_hash_v2"] = protocol.compute_full_sync_members_hash_v2(payload["members"])
    payload["membership_events_hash_v2"] = protocol.compute_full_sync_membership_events_hash_v2(
        payload["membership_events"]
    )

    result = ph.handle_full_sync(PEER_A, payload, mock_plugin)

    assert result == {"result": "continue"}
    assert db.get_member(PEER_B) is None
