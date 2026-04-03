"""Tests for AskreneLayerManager."""

import time
from unittest.mock import MagicMock, call
import pytest

from modules.askrene_layers import AskreneLayerManager


class MockPeerReputationMgr:
    def __init__(self, reps=None):
        self._reps = reps or {}

    def get_all_reputations(self):
        return self._reps


class MockReputation:
    def __init__(self, score=50, force_closes=0, htlc_success=1.0):
        self.reputation_score = score
        self.total_force_closes = force_closes
        self.avg_htlc_success = htlc_success


class MockDatabase:
    def __init__(self, members=None):
        self._members = members or []

    def get_all_members(self):
        return self._members


class TestAskreneLayerManagerInit:
    def test_defaults(self):
        mgr = AskreneLayerManager(MagicMock(), MockDatabase(), MockPeerReputationMgr())
        assert mgr.available is False
        assert mgr.is_available() is False

    def test_no_plugin(self):
        mgr = AskreneLayerManager(None, MockDatabase(), MockPeerReputationMgr())
        result = mgr.refresh_all()
        assert result["hive-fleet"] is False
        assert result["hive-reputation"] is False


class TestFleetLayer:
    def test_creates_layer_with_zero_fee_and_capacity(self):
        plugin = MagicMock()
        plugin.rpc.getinfo.return_value = {"id": "our_id"}
        plugin.rpc.listpeerchannels.return_value = {
            "channels": [
                {
                    "state": "CHANNELD_NORMAL",
                    "peer_id": "fleet_a",
                    "short_channel_id": "100x1x0",
                    "to_us_msat": 500000000,
                    "total_msat": 1000000000,
                },
                {
                    "state": "CHANNELD_NORMAL",
                    "peer_id": "external",
                    "short_channel_id": "200x1x0",
                    "to_us_msat": 300000000,
                    "total_msat": 1000000000,
                },
            ]
        }
        plugin.rpc.call.return_value = {}

        db = MockDatabase(members=[{"peer_id": "fleet_a"}])
        mgr = AskreneLayerManager(plugin, db, MockPeerReputationMgr())
        result = mgr._refresh_fleet_layer()

        assert result is True
        assert mgr.available is True

        # Verify askrene calls were made
        call_args = [c[0] for c in plugin.rpc.call.call_args_list]
        methods = [args[0] for args in call_args]
        assert "askrene-create-layer" in methods
        assert "askrene-update-channel" in methods
        assert "askrene-inform-channel" in methods
        assert "askrene-bias-node" in methods
        assert "askrene-age" in methods

    def test_fails_gracefully_when_askrene_unavailable(self):
        plugin = MagicMock()
        plugin.rpc.getinfo.return_value = {"id": "our_id"}
        plugin.rpc.call.side_effect = Exception("askrene not available")

        mgr = AskreneLayerManager(plugin, MockDatabase([{"peer_id": "fleet_a"}]), MockPeerReputationMgr())
        result = mgr._refresh_fleet_layer()

        assert result is False
        assert mgr.available is False


class TestReputationLayer:
    def test_disables_bad_peers(self):
        plugin = MagicMock()
        plugin.rpc.call.return_value = {}

        reps = {
            "bad_peer": MockReputation(score=10, force_closes=3, htlc_success=0.3),
            "good_peer": MockReputation(score=85, force_closes=0, htlc_success=0.99),
        }

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr(reps))
        result = mgr._refresh_reputation_layer()

        assert result is True

        # Check disable-node was called for bad_peer
        disable_calls = [
            c for c in plugin.rpc.call.call_args_list
            if c[0][0] == "askrene-disable-node"
        ]
        assert len(disable_calls) == 1
        assert disable_calls[0][0][1]["node"] == "bad_peer"

        # Check bias-node was called for good_peer with positive bias
        bias_calls = [
            c for c in plugin.rpc.call.call_args_list
            if c[0][0] == "askrene-bias-node" and c[0][1].get("node") == "good_peer"
        ]
        assert len(bias_calls) == 2  # in + out
        assert all(c[0][1]["bias"] == 5 for c in bias_calls)

    def test_no_reputation_data_succeeds(self):
        plugin = MagicMock()
        plugin.rpc.call.return_value = {}

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr())
        result = mgr._refresh_reputation_layer()
        assert result is True

    def test_no_reputation_manager(self):
        mgr = AskreneLayerManager(MagicMock(), MockDatabase(), None)
        result = mgr._refresh_reputation_layer()
        assert result is False


class TestRefreshAll:
    def test_returns_both_results(self):
        plugin = MagicMock()
        plugin.rpc.getinfo.return_value = {"id": "our_id"}
        plugin.rpc.listpeerchannels.return_value = {"channels": []}
        plugin.rpc.call.return_value = {}

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr())
        results = mgr.refresh_all()

        assert "hive-fleet" in results
        assert "hive-reputation" in results
