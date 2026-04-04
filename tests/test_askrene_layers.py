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

    def test_skips_remove_when_layer_missing(self):
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
                }
            ]
        }

        calls = []

        def _rpc_call(method, payload):
            calls.append((method, payload))
            if method == "askrene-listlayers":
                return {"layers": []}
            return {}

        plugin.rpc.call.side_effect = _rpc_call

        db = MockDatabase(members=[{"peer_id": "fleet_a"}])
        mgr = AskreneLayerManager(plugin, db, MockPeerReputationMgr())
        result = mgr._refresh_fleet_layer()

        assert result is True
        methods = [method for method, _ in calls]
        assert "askrene-listlayers" in methods
        assert "askrene-remove-layer" not in methods

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
            "bad_peer": MockReputation(score=10, force_closes=3, htlc_success=0.2),
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

    def test_borderline_peer_not_disabled(self):
        """Softened thresholds: 2 force closes or 40% HTLC success shouldn't disable."""
        plugin = MagicMock()
        plugin.rpc.call.return_value = {}

        reps = {
            "borderline": MockReputation(score=25, force_closes=2, htlc_success=0.4),
        }

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr(reps))
        mgr._refresh_reputation_layer()

        disable_calls = [
            c for c in plugin.rpc.call.call_args_list
            if c[0][0] == "askrene-disable-node"
        ]
        assert len(disable_calls) == 0

    def test_disabled_peer_reenables_after_expiry(self):
        """Peers re-enable after DISABLE_EXPIRY_SECONDS."""
        plugin = MagicMock()
        plugin.rpc.call.return_value = {}

        reps = {
            "bad_peer": MockReputation(score=10, force_closes=5, htlc_success=0.1),
        }

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr(reps))

        # First refresh: peer gets disabled
        mgr._refresh_reputation_layer()
        assert "bad_peer" in mgr._disabled_since

        # Simulate expiry by backdating the timestamp
        mgr._disabled_since["bad_peer"] = time.time() - mgr.DISABLE_EXPIRY_SECONDS - 1

        plugin.rpc.call.reset_mock()
        plugin.rpc.call.return_value = {}
        mgr._refresh_reputation_layer()

        # Should NOT have called disable-node this time (expired)
        disable_calls = [
            c for c in plugin.rpc.call.call_args_list
            if c[0][0] == "askrene-disable-node"
        ]
        assert len(disable_calls) == 0
        assert "bad_peer" not in mgr._disabled_since

    def test_closure_candidates_tracked(self):
        """Bad peers are tracked as closure candidates."""
        plugin = MagicMock()
        plugin.rpc.call.return_value = {}

        reps = {
            "bad_peer": MockReputation(score=10, force_closes=4, htlc_success=0.1),
            "good_peer": MockReputation(score=85, force_closes=0, htlc_success=0.99),
        }

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr(reps))
        mgr._refresh_reputation_layer()

        closures = mgr.get_closure_candidates()
        assert "bad_peer" in closures
        assert "good_peer" not in closures
        assert closures["bad_peer"]["force_closes"] == 4

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


class MockFlowCorridor:
    def __init__(self, source_peer_id, dest_peer_id, total_volume_sats):
        self.source_peer_id = source_peer_id
        self.destination_peer_id = dest_peer_id
        self.total_volume_sats = total_volume_sats


class MockCorridorAssignment:
    def __init__(self, corridor):
        self.corridor = corridor


class TestCorridorsLayer:
    def _make_plugin(self, assignments, channels):
        plugin = MagicMock()
        plugin.rpc.call.return_value = {}
        plugin.rpc.listpeerchannels.return_value = {"channels": channels}
        return plugin

    def _make_fee_coord_mgr(self, assignments):
        fee_coord_mgr = MagicMock()
        fee_coord_mgr.corridor_mgr.get_assignments.return_value = assignments
        return fee_coord_mgr

    def test_no_fee_coord_mgr_returns_false(self):
        mgr = AskreneLayerManager(MagicMock(), MockDatabase(), MockPeerReputationMgr())
        assert mgr._refresh_corridors_layer() is False

    def test_empty_assignments_returns_true(self):
        plugin = MagicMock()
        plugin.rpc.call.return_value = {}
        fee_coord_mgr = self._make_fee_coord_mgr([])
        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr(),
                                  fee_coordination_mgr=fee_coord_mgr)
        assert mgr._refresh_corridors_layer() is True

    def test_high_volume_corridor_gets_bias_8(self):
        corridor = MockFlowCorridor("peer_src", "peer_dst", 60_000_000)
        assignment = MockCorridorAssignment(corridor)
        channels = [
            {"state": "CHANNELD_NORMAL", "peer_id": "peer_src", "short_channel_id": "100x1x0"},
            {"state": "CHANNELD_NORMAL", "peer_id": "peer_dst", "short_channel_id": "200x1x0"},
        ]
        plugin = self._make_plugin([assignment], channels)
        fee_coord_mgr = self._make_fee_coord_mgr([assignment])

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr(),
                                  fee_coordination_mgr=fee_coord_mgr)
        result = mgr._refresh_corridors_layer()

        assert result is True
        bias_calls = [
            c for c in plugin.rpc.call.call_args_list
            if c[0][0] == "askrene-bias-channel"
        ]
        assert len(bias_calls) > 0
        assert all(c[0][1]["bias"] == 8 for c in bias_calls)

    def test_medium_volume_corridor_gets_bias_4(self):
        corridor = MockFlowCorridor("peer_src", "peer_dst", 25_000_000)
        assignment = MockCorridorAssignment(corridor)
        channels = [
            {"state": "CHANNELD_NORMAL", "peer_id": "peer_src", "short_channel_id": "100x1x0"},
        ]
        plugin = self._make_plugin([assignment], channels)
        fee_coord_mgr = self._make_fee_coord_mgr([assignment])

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr(),
                                  fee_coordination_mgr=fee_coord_mgr)
        mgr._refresh_corridors_layer()

        bias_calls = [
            c for c in plugin.rpc.call.call_args_list
            if c[0][0] == "askrene-bias-channel"
        ]
        assert all(c[0][1]["bias"] == 4 for c in bias_calls)

    def test_low_volume_corridor_gets_bias_2(self):
        corridor = MockFlowCorridor("peer_src", "peer_dst", 8_000_000)
        assignment = MockCorridorAssignment(corridor)
        channels = [
            {"state": "CHANNELD_NORMAL", "peer_id": "peer_src", "short_channel_id": "100x1x0"},
        ]
        plugin = self._make_plugin([assignment], channels)
        fee_coord_mgr = self._make_fee_coord_mgr([assignment])

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr(),
                                  fee_coordination_mgr=fee_coord_mgr)
        mgr._refresh_corridors_layer()

        bias_calls = [
            c for c in plugin.rpc.call.call_args_list
            if c[0][0] == "askrene-bias-channel"
        ]
        assert all(c[0][1]["bias"] == 2 for c in bias_calls)

    def test_very_low_volume_skips_bias(self):
        corridor = MockFlowCorridor("peer_src", "peer_dst", 1_000_000)
        assignment = MockCorridorAssignment(corridor)
        channels = [
            {"state": "CHANNELD_NORMAL", "peer_id": "peer_src", "short_channel_id": "100x1x0"},
        ]
        plugin = self._make_plugin([assignment], channels)
        fee_coord_mgr = self._make_fee_coord_mgr([assignment])

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr(),
                                  fee_coordination_mgr=fee_coord_mgr)
        mgr._refresh_corridors_layer()

        bias_calls = [
            c for c in plugin.rpc.call.call_args_list
            if c[0][0] == "askrene-bias-channel"
        ]
        assert len(bias_calls) == 0

    def test_bias_applied_both_directions(self):
        corridor = MockFlowCorridor("peer_src", "peer_dst", 60_000_000)
        assignment = MockCorridorAssignment(corridor)
        channels = [
            {"state": "CHANNELD_NORMAL", "peer_id": "peer_src", "short_channel_id": "100x1x0"},
        ]
        plugin = self._make_plugin([assignment], channels)
        fee_coord_mgr = self._make_fee_coord_mgr([assignment])

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr(),
                                  fee_coordination_mgr=fee_coord_mgr)
        mgr._refresh_corridors_layer()

        bias_calls = [
            c for c in plugin.rpc.call.call_args_list
            if c[0][0] == "askrene-bias-channel"
        ]
        # Both direction 0 and 1 for the one matching channel
        scid_dirs = {c[0][1]["short_channel_id_dir"] for c in bias_calls}
        assert "100x1x0/0" in scid_dirs
        assert "100x1x0/1" in scid_dirs


class TestTrafficLayer:
    def _make_profile(self, peer_id, drain_direction, confidence):
        return {"peer_id": peer_id, "drain_direction": drain_direction, "confidence": confidence}

    def test_no_traffic_intel_mgr_returns_false(self):
        mgr = AskreneLayerManager(MagicMock(), MockDatabase(), MockPeerReputationMgr())
        assert mgr._refresh_traffic_layer() is False

    def test_empty_profiles_returns_true(self):
        plugin = MagicMock()
        plugin.rpc.call.return_value = {}
        traffic_mgr = MagicMock()
        traffic_mgr.get_all_profiles.return_value = []
        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr(),
                                  traffic_intel_mgr=traffic_mgr)
        assert mgr._refresh_traffic_layer() is True

    def test_inbound_heavy_biases_direction_0(self):
        plugin = MagicMock()
        plugin.rpc.call.return_value = {}
        plugin.rpc.listpeerchannels.return_value = {
            "channels": [
                {"state": "CHANNELD_NORMAL", "peer_id": "peer_a", "short_channel_id": "100x1x0"},
            ]
        }
        traffic_mgr = MagicMock()
        traffic_mgr.get_all_profiles.return_value = [
            self._make_profile("peer_a", "inbound_heavy", 0.8)
        ]

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr(),
                                  traffic_intel_mgr=traffic_mgr)
        mgr._refresh_traffic_layer()

        bias_calls = [
            c for c in plugin.rpc.call.call_args_list
            if c[0][0] == "askrene-bias-channel"
        ]
        assert len(bias_calls) == 1
        assert bias_calls[0][0][1]["short_channel_id_dir"] == "100x1x0/0"

    def test_outbound_heavy_biases_direction_1(self):
        plugin = MagicMock()
        plugin.rpc.call.return_value = {}
        plugin.rpc.listpeerchannels.return_value = {
            "channels": [
                {"state": "CHANNELD_NORMAL", "peer_id": "peer_b", "short_channel_id": "200x1x0"},
            ]
        }
        traffic_mgr = MagicMock()
        traffic_mgr.get_all_profiles.return_value = [
            self._make_profile("peer_b", "outbound_heavy", 0.9)
        ]

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr(),
                                  traffic_intel_mgr=traffic_mgr)
        mgr._refresh_traffic_layer()

        bias_calls = [
            c for c in plugin.rpc.call.call_args_list
            if c[0][0] == "askrene-bias-channel"
        ]
        assert len(bias_calls) == 1
        assert bias_calls[0][0][1]["short_channel_id_dir"] == "200x1x0/1"

    def test_low_confidence_skips_bias(self):
        plugin = MagicMock()
        plugin.rpc.call.return_value = {}
        plugin.rpc.listpeerchannels.return_value = {
            "channels": [
                {"state": "CHANNELD_NORMAL", "peer_id": "peer_a", "short_channel_id": "100x1x0"},
            ]
        }
        traffic_mgr = MagicMock()
        traffic_mgr.get_all_profiles.return_value = [
            self._make_profile("peer_a", "inbound_heavy", 0.2)  # Below 0.3 threshold
        ]

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr(),
                                  traffic_intel_mgr=traffic_mgr)
        mgr._refresh_traffic_layer()

        bias_calls = [
            c for c in plugin.rpc.call.call_args_list
            if c[0][0] == "askrene-bias-channel"
        ]
        assert len(bias_calls) == 0

    def test_balanced_drain_skips_bias(self):
        plugin = MagicMock()
        plugin.rpc.call.return_value = {}
        plugin.rpc.listpeerchannels.return_value = {
            "channels": [
                {"state": "CHANNELD_NORMAL", "peer_id": "peer_a", "short_channel_id": "100x1x0"},
            ]
        }
        traffic_mgr = MagicMock()
        traffic_mgr.get_all_profiles.return_value = [
            self._make_profile("peer_a", "balanced", 0.9)
        ]

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr(),
                                  traffic_intel_mgr=traffic_mgr)
        mgr._refresh_traffic_layer()

        bias_calls = [
            c for c in plugin.rpc.call.call_args_list
            if c[0][0] == "askrene-bias-channel"
        ]
        assert len(bias_calls) == 0

    def test_askrene_age_called(self):
        plugin = MagicMock()
        plugin.rpc.call.return_value = {}
        plugin.rpc.listpeerchannels.return_value = {"channels": []}
        traffic_mgr = MagicMock()
        traffic_mgr.get_all_profiles.return_value = [
            self._make_profile("peer_x", "inbound_heavy", 0.8)
        ]

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr(),
                                  traffic_intel_mgr=traffic_mgr)
        mgr._refresh_traffic_layer()

        age_calls = [
            c for c in plugin.rpc.call.call_args_list
            if c[0][0] == "askrene-age"
        ]
        assert len(age_calls) >= 1
        assert age_calls[0][0][1]["layer"] == "hive-traffic"


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

    def test_returns_all_four_layer_keys(self):
        plugin = MagicMock()
        plugin.rpc.getinfo.return_value = {"id": "our_id"}
        plugin.rpc.listpeerchannels.return_value = {"channels": []}
        plugin.rpc.call.return_value = {}

        fee_coord_mgr = MagicMock()
        fee_coord_mgr.corridor_mgr.get_assignments.return_value = []
        traffic_mgr = MagicMock()
        traffic_mgr.get_all_profiles.return_value = []

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr(),
                                  fee_coordination_mgr=fee_coord_mgr,
                                  traffic_intel_mgr=traffic_mgr)
        results = mgr.refresh_all()

        assert "hive-fleet" in results
        assert "hive-reputation" in results
        assert "hive-corridors" in results
        assert "hive-traffic" in results
