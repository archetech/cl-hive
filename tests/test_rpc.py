"""
Tests for Phase 8 RPC Commands

Tests the RPC command interface as specified in IMPLEMENTATION_PLAN.md Section 8.6:
- Genesis Test: Call hive-genesis -> verify DB initialized, returns hive_id
- Approval Flow Test: Pending request -> approve -> verify membership
- Status Test: Verify all fields returned with correct types
- Permission Test: (Pending - requires Issue #25)
- Approve Flow: Create pending action, approve -> verify status change

Author: Lightning Goats Team
"""

import pytest
import time

import json
import importlib.util
import types
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.database import HiveDatabase
from modules.config import HiveConfig
from modules.handshake import HandshakeManager
from modules.handshake import (
    MAX_OUTBOUND_HELLOS,
    MAX_PENDING_CHALLENGES,
    MAX_PENDING_REQUESTS,
)
from modules.membership import MembershipManager, MEMBER_TIER
from modules.contribution import ContributionManager
from modules.governance import RecommendationLogger


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def mock_plugin():
    """Create a mock plugin for testing."""
    plugin = MagicMock()
    plugin.log = MagicMock()
    return plugin


@pytest.fixture
def mock_rpc():
    """Create a mock RPC interface."""
    rpc = MagicMock()
    rpc.getinfo.return_value = {'id': '02' + 'a' * 64}
    rpc.signmessage.return_value = {'zbase': 'test_signature_zbase'}
    rpc.checkmessage.return_value = {'verified': True, 'pubkey': '02' + 'a' * 64}
    return rpc


@pytest.fixture
def database(mock_plugin, tmp_path):
    """Create a test database."""
    db_path = str(tmp_path / "test_rpc.db")
    db = HiveDatabase(db_path, mock_plugin)
    db.initialize()
    return db


@pytest.fixture
def config():
    """Create a test config."""
    return HiveConfig(
        db_path=':memory:',
        membership_enabled=True,
    )


@pytest.fixture
def handshake_mgr(mock_rpc, database, mock_plugin):
    """Create a handshake manager for testing."""
    return HandshakeManager(mock_rpc, database, mock_plugin)


def _peer_id_for_index(index: int) -> str:
    """Return a syntactically valid pubkey for deterministic cap tests."""
    return f"02{index:064x}"


def _load_cl_hive_main():
    """Import the main plugin module without executing plugin.run()."""
    module_path = Path(__file__).resolve().parents[1] / "cl-hive.py"
    spec = importlib.util.spec_from_file_location("cl_hive_main_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None

    class _TestPlugin:
        def __init__(self):
            self.methods = {}

        def method(self, name, *_args, **_kwargs):
            def decorator(fn):
                self.methods[name] = fn.__name__
                return fn

            return decorator

        def init(self):
            return lambda fn: fn

        def hook(self, *_args, **_kwargs):
            return lambda fn: fn

        def subscribe(self, *_args, **_kwargs):
            return lambda fn: fn

        def add_option(self, *_args, **_kwargs):
            return None

        def run(self):
            raise AssertionError("plugin.run() should not execute during tests")

    fake_pyln_client = types.ModuleType("pyln.client")
    fake_pyln_client.Plugin = _TestPlugin
    fake_pyln_client.RpcError = Exception
    fake_pyln = types.ModuleType("pyln")
    fake_pyln.client = fake_pyln_client

    with patch.dict(sys.modules, {"pyln": fake_pyln, "pyln.client": fake_pyln_client}):
        spec.loader.exec_module(module)
    return module


def test_stubbed_rebalance_rpc_not_registered():
    mod = _load_cl_hive_main()

    assert "hive-rebalance-recommendations" not in mod.plugin.methods


# =============================================================================
# GENESIS TESTS
# =============================================================================

class TestGenesisRPC:
    """Test hive-genesis RPC command functionality."""

    def test_genesis_creates_member(self, handshake_mgr, database):
        """Genesis should create a member and return hive_id."""
        result = handshake_mgr.genesis(hive_id="test-hive")

        assert result['status'] == 'genesis_complete'
        assert result['hive_id'] == 'test-hive'
        assert 'member_pubkey' in result

        # Verify founding member was created in DB
        member = database.get_member(result['member_pubkey'])
        assert member is not None
        assert member['tier'] == 'member'

    def test_genesis_auto_generates_hive_id(self, handshake_mgr, database):
        """Genesis without hive_id should auto-generate one."""
        result = handshake_mgr.genesis()

        assert result['status'] == 'genesis_complete'
        assert 'hive_id' in result
        assert len(result['hive_id']) > 0

    def test_genesis_fails_if_already_initialized(self, handshake_mgr, database):
        """Genesis should fail if Hive already has members."""
        # First genesis
        handshake_mgr.genesis(hive_id="test-hive")

        # Second genesis should fail
        with pytest.raises(ValueError, match="Already member"):
            handshake_mgr.genesis(hive_id="another-hive")


# =============================================================================
# APPROVAL FLOW TESTS
# =============================================================================

class TestApprovalFlow:
    """Test the pending request and approval flow."""

    def test_hello_stores_pending_request(self, handshake_mgr):
        """store_pending_request should create a retrievable pending entry."""
        peer_id = '02' + 'b' * 64
        handshake_mgr.store_pending_request(peer_id)

        pending = handshake_mgr.get_pending_requests()
        assert len(pending) == 1
        assert pending[0]['peer_id'] == peer_id
        assert pending[0]['channel_verified'] is True

    def test_approve_triggers_challenge(self, handshake_mgr):
        """pop_pending_request should return and remove the request."""
        peer_id = '02' + 'c' * 64
        handshake_mgr.store_pending_request(peer_id)

        req = handshake_mgr.pop_pending_request(peer_id)
        assert req is not None
        assert req['peer_id'] == peer_id

        # Should be gone now
        assert handshake_mgr.pop_pending_request(peer_id) is None

    def test_approve_unknown_peer_returns_none(self, handshake_mgr):
        """pop_pending_request for unknown peer should return None."""
        assert handshake_mgr.pop_pending_request('02' + 'f' * 64) is None

    def test_pending_returns_stored_requests(self, handshake_mgr):
        """get_pending_requests should return all stored requests."""
        handshake_mgr.store_pending_request('02' + 'a' * 64)
        handshake_mgr.store_pending_request('02' + 'b' * 64)

        pending = handshake_mgr.get_pending_requests()
        assert len(pending) == 2

    def test_pending_request_expiry(self, handshake_mgr):
        """expire_pending_requests should remove old entries."""
        peer_id = '02' + 'd' * 64
        handshake_mgr.store_pending_request(peer_id)
        # Manually backdate the request
        handshake_mgr._pending_requests[peer_id]['received_at'] = int(time.time()) - 90000

        expired = handshake_mgr.expire_pending_requests(max_age_seconds=86400)
        assert expired == 1
        assert len(handshake_mgr.get_pending_requests()) == 0

    def test_pending_request_cap_removes_db_fallback_for_oldest(self, handshake_mgr):
        """Cap eviction must delete persisted oldest requests, not just memory state."""
        oldest_peer = _peer_id_for_index(0)
        for index in range(MAX_PENDING_REQUESTS + 1):
            handshake_mgr.store_pending_request(_peer_id_for_index(index))

        pending = handshake_mgr.get_pending_requests()
        assert len(pending) == MAX_PENDING_REQUESTS
        assert oldest_peer not in {req["peer_id"] for req in pending}

    def test_outbound_hello_cap_removes_db_fallback_for_oldest(self, handshake_mgr):
        """Oldest outbound HELLO must not resurrect from DB after cap eviction."""
        oldest_peer = _peer_id_for_index(0)
        for index in range(MAX_OUTBOUND_HELLOS + 1):
            handshake_mgr.record_hello_sent(_peer_id_for_index(index))

        handshake_mgr._outbound_hello_sent.clear()

        assert handshake_mgr.has_pending_outbound_hello(oldest_peer) is False

    def test_pending_challenge_cap_removes_db_fallback_for_oldest(self, handshake_mgr):
        """Oldest pending challenge must not resurrect from DB after cap eviction."""
        oldest_peer = _peer_id_for_index(0)
        for index in range(MAX_PENDING_CHALLENGES + 1):
            handshake_mgr.generate_challenge(_peer_id_for_index(index), requirements=0, initial_tier='member')

        handshake_mgr._pending_challenges.clear()

        assert handshake_mgr.get_pending_challenge(oldest_peer) is None

    def test_hive_approve_preserves_pending_request_when_challenge_send_fails(
        self, database, mock_plugin, mock_rpc
    ):
        """A transient sendcustommsg failure should not consume the pending join request."""
        cl_hive = _load_cl_hive_main()
        our_pubkey = '02' + 'a' * 64
        peer_id = '02' + 'e' * 64

        database.add_member(our_pubkey, tier='member', joined_at=int(time.time()))
        handshake_mgr = HandshakeManager(mock_rpc, database, mock_plugin)
        handshake_mgr.store_pending_request(peer_id)

        mock_plugin.rpc = MagicMock()
        mock_plugin.rpc.call.side_effect = Exception("send failed")
        mock_plugin.log = MagicMock()

        cl_hive.database = database
        cl_hive.handshake_mgr = handshake_mgr
        cl_hive.our_pubkey = our_pubkey
        cl_hive._check_permission = lambda: None

        result = cl_hive.hive_approve(mock_plugin, peer_id)

        assert result["error"] == "Failed to send CHALLENGE: send failed"
        pending = handshake_mgr.get_pending_requests()
        assert any(req["peer_id"] == peer_id for req in pending)
        assert handshake_mgr.get_pending_challenge(peer_id) is None


# =============================================================================
# STATUS TESTS
# =============================================================================

class TestStatusRPC:
    """Test hive-status RPC command functionality."""

    def test_status_returns_correct_structure(self, database, config):
        """Status should return all required fields with correct types."""
        # Add some members (all single tier)
        database.add_member('02' + 'a' * 64, tier='member', joined_at=int(time.time()))
        database.add_member('02' + 'b' * 64, tier='member', joined_at=int(time.time()))
        database.add_member('02' + 'c' * 64, tier='member', joined_at=int(time.time()))

        members = database.get_all_members()

        status = {
            "status": "active",
            "governance": "recommendation_only",
            "members": {
                "total": len(members),
            },
            "limits": {
                "max_members": config.max_members,
                "market_share_cap": config.market_share_cap_pct,
            },
            "version": "2.2.3",
        }

        # Verify structure
        assert isinstance(status['status'], str)
        assert isinstance(status['governance'], str)
        assert isinstance(status['members'], dict)
        assert isinstance(status['members']['total'], int)
        assert status['members']['total'] == 3

    def test_status_genesis_required_when_empty(self, database, config):
        """Status should indicate genesis_required when no members."""
        members = database.get_all_members()

        status = "genesis_required" if not members else "active"
        assert status == "genesis_required"


# =============================================================================
# CONTRIBUTION TESTS
# =============================================================================

class TestContributionRPC:
    """Test hive-contribution RPC command functionality."""

    def test_contribution_stats_returned(self, database, mock_plugin, mock_rpc, config):
        """Contribution stats should be retrievable."""
        peer_id = '02' + 'a' * 64

        # Record some contributions
        database.record_contribution(peer_id, 'forwarded', 100000)
        database.record_contribution(peer_id, 'forwarded', 50000)
        database.record_contribution(peer_id, 'received', 75000)

        # Get stats
        stats = database.get_contribution_stats(peer_id)

        assert stats['forwarded'] == 150000
        assert stats['received'] == 75000

    def test_contribution_ratio_calculation(self, database):
        """Contribution ratio should be calculated correctly."""
        peer_id = '02' + 'b' * 64

        database.record_contribution(peer_id, 'forwarded', 200000)
        database.record_contribution(peer_id, 'received', 100000)

        ratio = database.get_contribution_ratio(peer_id)
        assert ratio == 2.0  # 200000 / 100000


# =============================================================================
# TOPOLOGY TESTS
# =============================================================================

class TestTopologyRPC:
    """Test hive-topology RPC command functionality."""

    def test_planner_log_retrieval(self, database):
        """Planner logs should be retrievable."""
        # Add some log entries
        database.log_planner_action(
            action_type='saturation_check',
            result='completed',
            details={'saturated_targets': 2}
        )
        database.log_planner_action(
            action_type='ignore',
            result='success',
            target='02' + 'x' * 64,
            details={'hive_share_pct': 0.25}
        )

        # Retrieve logs
        logs = database.get_planner_logs(limit=10)

        assert len(logs) == 2
        assert logs[0]['action_type'] in ('saturation_check', 'ignore')


# =============================================================================
# MEMBERS TESTS
# =============================================================================

class TestMembersRPC:
    """Test hive-members RPC command functionality."""

    def test_members_list_returned(self, database):
        """Members list should include all members."""
        database.add_member('02' + 'a' * 64, tier='member', joined_at=int(time.time()))
        database.add_member('02' + 'b' * 64, tier='member', joined_at=int(time.time()))
        database.add_member('02' + 'c' * 64, tier='member', joined_at=int(time.time()))

        members = database.get_all_members()

        assert len(members) == 3
        assert all(m['tier'] == 'member' for m in members)


# =============================================================================
# BAN TESTS
# =============================================================================

class TestBanRPC:
    """Test hive-ban RPC command functionality."""

    def test_ban_added_to_db(self, database):
        """Ban should be stored in the database."""
        peer_id = '02' + 'x' * 64
        reporter = '02' + 'a' * 64

        success = database.add_ban(
            peer_id=peer_id,
            reason='test ban',
            reporter=reporter,
            signature='test_sig'
        )

        assert success
        assert database.is_banned(peer_id)

    def test_ban_info_retrievable(self, database):
        """Ban info should be retrievable."""
        peer_id = '02' + 'y' * 64
        reporter = '02' + 'a' * 64

        database.add_ban(peer_id, 'spam', reporter, 'sig')

        info = database.get_ban_info(peer_id)
        assert info is not None
        assert info['reason'] == 'spam'
        assert info['reporter'] == reporter


# =============================================================================
# PERMISSION TESTS (Issue #25)
# =============================================================================

class TestPermissionModel:
    """Test permission model enforcement."""

    def test_member_permission_granted(self, database):
        """Any member should have permission for all commands."""
        member_pubkey = '02' + 'a' * 64
        database.add_member(member_pubkey, tier='member', joined_at=int(time.time()))

        member = database.get_member(member_pubkey)
        assert member is not None
        assert member['tier'] == 'member'

    def test_non_member_permission_denied(self, database):
        """Non-member should be denied."""
        member = database.get_member('02' + 'z' * 64)
        assert member is None


class TestMembershipAuditLog:
    """Test the membership audit log."""

    def test_membership_audit_log(self, database):
        """Audit log should record and retrieve events."""
        peer_id = '02' + 'a' * 64
        actor_id = '02' + 'b' * 64

        database.log_membership_event("joined", peer_id)
        database.log_membership_event("banned", peer_id, actor_peer_id=actor_id, reason="spam")

        log = database.get_membership_audit_log(peer_id=peer_id)
        assert len(log) == 2
        assert log[0]['event'] == 'banned'  # newest first
        assert log[0]['reason'] == 'spam'
        assert log[1]['event'] == 'joined'

    def test_audit_log_all_events(self, database):
        """Audit log without filter returns all events."""
        database.log_membership_event("joined", '02' + 'a' * 64)
        database.log_membership_event("joined", '02' + 'b' * 64)

        log = database.get_membership_audit_log()
        assert len(log) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
