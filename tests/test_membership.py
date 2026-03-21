"""
Tests for membership module: single-role member model.
"""

import time
from unittest.mock import MagicMock

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.membership import MembershipManager, MEMBER_TIER
from modules.contribution import ContributionManager, LEECH_WINDOW_DAYS


class DummyState:
    def __init__(self, topology):
        self.topology = topology


class DummyConfig:
    pass


def test_member_tier_constant():
    """MEMBER_TIER should be 'member'."""
    assert MEMBER_TIER == "member"


def test_uptime_thresholds():
    db = MagicMock()
    contribution_mgr = MagicMock()
    state_manager = MagicMock()
    config = DummyConfig()
    mgr = MembershipManager(db, state_manager, contribution_mgr, None, config)

    now = int(time.time())
    window_seconds = 30 * 86400
    db.get_presence.return_value = {
        "last_change_ts": now - 100,
        "is_online": 0,
        "online_seconds_rolling": int(window_seconds * 0.996),
        "window_start_ts": now - window_seconds
    }
    assert mgr.calculate_uptime("peer") >= 99.5

    db.get_presence.return_value = {
        "last_change_ts": now - 100,
        "is_online": 0,
        "online_seconds_rolling": int(window_seconds * 0.994),
        "window_start_ts": now - window_seconds
    }
    assert mgr.calculate_uptime("peer") < 99.5


def test_ratio_thresholds():
    db = MagicMock()
    state_manager = MagicMock()
    config = DummyConfig()
    contribution_mgr = MagicMock()
    contribution_mgr.get_contribution_stats.return_value = {
        "forwarded": 100000,
        "received": 90000,
        "ratio": 1.11
    }
    mgr = MembershipManager(db, state_manager, contribution_mgr, None, config)
    assert mgr.calculate_contribution_ratio("peer") >= 1.0

    contribution_mgr.get_contribution_stats.return_value = {
        "forwarded": 80000,
        "received": 100000,
        "ratio": 0.8
    }
    assert mgr.calculate_contribution_ratio("peer") < 1.0


def test_uniqueness_check():
    db = MagicMock()
    contribution_mgr = MagicMock()
    config = DummyConfig()
    state_manager = MagicMock()

    state_manager.get_peer_state.side_effect = [
        DummyState(["peer_a", "peer_b", "peer_unique"]),
        DummyState(["peer_a", "peer_b"])
    ]
    db.get_all_members.return_value = [
        {"peer_id": "member_1", "tier": "member"}
    ]

    mgr = MembershipManager(db, state_manager, contribution_mgr, None, config)
    unique = mgr.get_unique_peers("candidate")
    assert "peer_unique" in unique


def test_leech_trigger():
    db = MagicMock()
    config = DummyConfig()
    rpc = MagicMock()
    plugin = MagicMock()
    mgr = ContributionManager(rpc, db, plugin, config)

    db.get_contribution_stats.return_value = {"forwarded": 40, "received": 100}
    low_since = int(time.time()) - (LEECH_WINDOW_DAYS * 86400)
    db.get_leech_flag.return_value = {"low_since_ts": low_since, "ban_triggered": 0}

    result = mgr.check_leech_status("peer")
    assert result["is_leech"] is True
    assert result["ratio"] < 0.5
    db.set_leech_flag.assert_called()


def test_is_member():
    db = MagicMock()
    contribution_mgr = MagicMock()
    state_manager = MagicMock()
    config = DummyConfig()
    mgr = MembershipManager(db, state_manager, contribution_mgr, None, config)

    db.get_member.return_value = {"peer_id": "abc", "tier": "member"}
    assert mgr.is_member("abc") is True

    db.get_member.return_value = None
    assert mgr.is_member("xyz") is False


def test_add_member_always_uses_member_tier():
    """add_member should always store MEMBER_TIER regardless of input."""
    db = MagicMock()
    db.add_member.return_value = True
    config = DummyConfig()
    mgr = MembershipManager(db, MagicMock(), MagicMock(), None, config)

    mgr.add_member("peer123")
    call_args = db.add_member.call_args
    assert call_args[1]["tier"] == MEMBER_TIER


def test_get_active_members():
    db = MagicMock()
    contribution_mgr = MagicMock()
    state_manager = MagicMock()
    config = DummyConfig()
    mgr = MembershipManager(db, state_manager, contribution_mgr, None, config)

    now = int(time.time())
    db.get_all_members.return_value = [
        {"peer_id": "a", "tier": "member", "last_seen": now - 100},
        {"peer_id": "b", "tier": "member", "last_seen": now - 100},
        {"peer_id": "c", "tier": "member", "last_seen": now - 100000},  # stale
    ]
    db.is_banned.return_value = False

    active = mgr.get_active_members()
    assert "a" in active
    assert "b" in active
    assert "c" not in active


def test_sync_bridge_policies():
    db = MagicMock()
    contribution_mgr = MagicMock()
    state_manager = MagicMock()
    config = DummyConfig()
    bridge = MagicMock()
    bridge.status = MagicMock()
    bridge.status.value = "enabled"
    bridge.set_hive_policy.return_value = True

    mgr = MembershipManager(db, state_manager, contribution_mgr, bridge, config)

    db.get_all_members.return_value = [
        {"peer_id": "a", "tier": "member"},
        {"peer_id": "b", "tier": "member"},
    ]

    synced = mgr.sync_bridge_policies()
    assert synced == 2
    assert bridge.set_hive_policy.call_count == 2


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
