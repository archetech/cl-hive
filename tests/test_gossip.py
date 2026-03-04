"""
Tests for Gossip module - Boltz activity in gossip state (F1 fix).

Tests that the gossip payload correctly includes Boltz swap activity
information for fleet-wide coordination.
"""

import pytest
from unittest.mock import MagicMock

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.state_manager import StateManager
from modules.gossip import GossipManager


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def mock_database():
    """Create a mock database for testing."""
    db = MagicMock()
    db.get_all_hive_states.return_value = []
    db.update_hive_state.return_value = None
    return db


@pytest.fixture
def mock_plugin():
    """Create a mock plugin for logging."""
    plugin = MagicMock()
    plugin.log = MagicMock()
    return plugin


@pytest.fixture
def state_manager(mock_database, mock_plugin):
    """Create a StateManager with mocked dependencies."""
    return StateManager(mock_database, mock_plugin)


@pytest.fixture
def gossip_manager(state_manager, mock_plugin):
    """Create a GossipManager with mocked dependencies."""
    return GossipManager(state_manager, mock_plugin, heartbeat_interval=300)


# =============================================================================
# BOLTZ ACTIVITY GOSSIP TESTS
# =============================================================================

class TestGossipBoltzActivity:
    """Tests for Boltz activity in gossip payload."""

    def test_gossip_payload_includes_boltz_activity(self, gossip_manager):
        """Gossip payload should include boltz_activity when provided."""
        payload = gossip_manager.create_gossip_payload(
            our_pubkey="02" + "a" * 64,
            capacity_sats=1000000,
            available_sats=500000,
            fee_policy={"base_fee": 0, "fee_rate": 100},
            topology=["peer1"],
            boltz_activity={"pending_swaps": 1, "daily_spend_sats": 500, "last_swap_ts": 1709510400}
        )
        assert "boltz_activity" in payload
        assert payload["boltz_activity"]["pending_swaps"] == 1
        assert payload["boltz_activity"]["daily_spend_sats"] == 500
        assert payload["boltz_activity"]["last_swap_ts"] == 1709510400

    def test_gossip_payload_boltz_activity_defaults_empty(self, gossip_manager):
        """Boltz activity should default to empty dict when not provided."""
        payload = gossip_manager.create_gossip_payload(
            our_pubkey="02" + "a" * 64,
            capacity_sats=1000000,
            available_sats=500000,
            fee_policy={"base_fee": 0, "fee_rate": 100},
            topology=["peer1"],
        )
        assert payload.get("boltz_activity") == {}

    def test_gossip_payload_boltz_activity_none_becomes_empty(self, gossip_manager):
        """Explicitly passing None for boltz_activity should result in empty dict."""
        payload = gossip_manager.create_gossip_payload(
            our_pubkey="02" + "a" * 64,
            capacity_sats=1000000,
            available_sats=500000,
            fee_policy={"base_fee": 0, "fee_rate": 100},
            topology=["peer1"],
            boltz_activity=None
        )
        assert payload["boltz_activity"] == {}

    def test_gossip_payload_boltz_activity_with_zero_values(self, gossip_manager):
        """Boltz activity with all zero values should be preserved."""
        boltz = {"pending_swaps": 0, "daily_spend_sats": 0, "last_swap_ts": 0}
        payload = gossip_manager.create_gossip_payload(
            our_pubkey="02" + "a" * 64,
            capacity_sats=1000000,
            available_sats=500000,
            fee_policy={"base_fee": 0, "fee_rate": 100},
            topology=["peer1"],
            boltz_activity=boltz
        )
        assert payload["boltz_activity"]["pending_swaps"] == 0
        assert payload["boltz_activity"]["daily_spend_sats"] == 0
        assert payload["boltz_activity"]["last_swap_ts"] == 0

    def test_gossip_payload_preserves_other_fields_with_boltz(self, gossip_manager):
        """Adding boltz_activity should not affect other payload fields."""
        payload = gossip_manager.create_gossip_payload(
            our_pubkey="02" + "a" * 64,
            capacity_sats=1000000,
            available_sats=500000,
            fee_policy={"base_fee": 0, "fee_rate": 100},
            topology=["peer1"],
            budget_available_sats=100000,
            addresses=["1.2.3.4:9735"],
            boltz_activity={"pending_swaps": 2, "daily_spend_sats": 1000, "last_swap_ts": 1709510400}
        )
        assert payload["capacity_sats"] == 1000000
        assert payload["available_sats"] == 500000
        assert payload["budget_available_sats"] == 100000
        assert payload["addresses"] == ["1.2.3.4:9735"]
        assert payload["boltz_activity"]["pending_swaps"] == 2
