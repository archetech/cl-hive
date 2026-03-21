"""
Tests for Gossip module.

Boltz activity tests removed - Boltz is a local execution concern,
not fleet coordination state.
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
# GOSSIP PAYLOAD TESTS
# =============================================================================

class TestGossipPayload:
    """Tests for gossip payload creation."""

    def test_gossip_payload_basic_fields(self, gossip_manager):
        """Gossip payload should include all required fields."""
        payload = gossip_manager.create_gossip_payload(
            our_pubkey="02" + "a" * 64,
            capacity_sats=1000000,
            available_sats=500000,
            fee_policy={"base_fee": 0, "fee_rate": 100},
            topology=["peer1"],
        )
        assert payload["capacity_sats"] == 1000000
        assert payload["available_sats"] == 500000
        assert payload["fee_policy"] == {"base_fee": 0, "fee_rate": 100}
        assert payload["topology"] == ["peer1"]

    def test_gossip_payload_no_boltz_activity(self, gossip_manager):
        """Gossip payload should not contain boltz_activity field."""
        payload = gossip_manager.create_gossip_payload(
            our_pubkey="02" + "a" * 64,
            capacity_sats=1000000,
            available_sats=500000,
            fee_policy={"base_fee": 0, "fee_rate": 100},
            topology=["peer1"],
        )
        assert "boltz_activity" not in payload

    def test_gossip_payload_with_budget_and_addresses(self, gossip_manager):
        """Budget and address fields should be included in payload."""
        payload = gossip_manager.create_gossip_payload(
            our_pubkey="02" + "a" * 64,
            capacity_sats=1000000,
            available_sats=500000,
            fee_policy={"base_fee": 0, "fee_rate": 100},
            topology=["peer1"],
            budget_available_sats=100000,
            addresses=["1.2.3.4:9735"],
        )
        assert payload["budget_available_sats"] == 100000
        assert payload["addresses"] == ["1.2.3.4:9735"]
