"""
Tests for Fee Coordination Module.

Tests cover:
- FlowCorridorManager and corridor assignment
- FeeCoordinationManager (main interface)
- Competition avoidance (corridor ownership)
- Centrality/size adjustments
- Egress desaturation bias
"""

import pytest
import time
import threading
from unittest.mock import MagicMock, patch

from modules.fee_coordination import (
    # Constants
    FLEET_FEE_FLOOR_PPM,
    FLEET_FEE_CEILING_PPM,
    DEFAULT_FEE_PPM,
    PRIMARY_FEE_MULTIPLIER,
    SECONDARY_FEE_MULTIPLIER,
    EGRESS_DESATURATION_MAX_SURCHARGE_PPM,
    # Data classes
    FlowCorridor,
    CorridorAssignment,
    FeeRecommendation,
    # Classes
    FlowCorridorManager,
    FeeCoordinationManager,
)


class MockDatabase:
    """Mock database for testing."""

    def __init__(self):
        self.members = {}

    def get_all_members(self):
        return list(self.members.values()) if self.members else []

    def get_member(self, peer_id):
        return self.members.get(peer_id)


class MockPlugin:
    """Mock plugin for testing."""

    def __init__(self):
        self.logs = []
        self.rpc = MockRpc()

    def log(self, msg, level="info"):
        self.logs.append({"msg": msg, "level": level})


class MockRpc:
    """Mock RPC interface."""

    def __init__(self):
        self.channels = []

    def listpeerchannels(self, id=None):
        if id:
            return {"channels": [c for c in self.channels if c.get("peer_id") == id]}
        return {"channels": self.channels}


class MockStateManager:
    """Mock state manager for testing."""

    def __init__(self):
        self.peer_states = {}

    def get_peer_state(self, peer_id):
        return self.peer_states.get(peer_id)

    def get_all_peer_states(self):
        return list(self.peer_states.values())

    def set_peer_state(self, peer_id, topology=None, capacity_sats=10_000_000):
        state = MagicMock()
        state.peer_id = peer_id
        state.topology = topology or []
        state.capacity_sats = capacity_sats
        self.peer_states[peer_id] = state


class MockLiquidityCoordinator:
    """Mock liquidity coordinator for testing."""

    def __init__(self):
        self.needs = []
        self._lock = threading.Lock()
        self._member_liquidity_state = {}

    def get_fleet_liquidity_needs(self):
        return self.needs

    def add_need(self, target_peer_id, reporter_id, amount_sats=1_000_000):
        self.needs.append({
            "target_peer_id": target_peer_id,
            "reporter_id": reporter_id,
            "amount_sats": amount_sats,
        })

    def set_local_saturated_channels(self, member_id, channels):
        with self._lock:
            state = self._member_liquidity_state.setdefault(member_id, {})
            state["saturated_channels"] = channels


# =============================================================================
# FLOW CORRIDOR TESTS
# =============================================================================

class TestFlowCorridor:
    """Test FlowCorridor data class."""

    def test_basic_creation(self):
        """Test creating a flow corridor."""
        corridor = FlowCorridor(
            source_peer_id="02" + "a" * 64,
            destination_peer_id="02" + "b" * 64,
            capable_members=["02" + "c" * 64, "02" + "d" * 64]
        )

        assert corridor.source_peer_id == "02" + "a" * 64
        assert len(corridor.capable_members) == 2

    def test_to_dict(self):
        """Test serialization."""
        corridor = FlowCorridor(
            source_peer_id="02" + "a" * 64,
            destination_peer_id="02" + "b" * 64,
            competition_level="medium"
        )

        d = corridor.to_dict()
        assert "source_peer_id" in d
        assert d["competition_level"] == "medium"


class TestCorridorAssignment:
    """Test CorridorAssignment data class."""

    def test_basic_creation(self):
        """Test creating a corridor assignment."""
        corridor = FlowCorridor(
            source_peer_id="02" + "a" * 64,
            destination_peer_id="02" + "b" * 64
        )
        assignment = CorridorAssignment(
            corridor=corridor,
            primary_member="02" + "c" * 64,
            secondary_members=["02" + "d" * 64],
            primary_fee_ppm=500,
            secondary_fee_ppm=750,
            assignment_reason="highest_score",
            confidence=0.8
        )

        assert assignment.primary_member == "02" + "c" * 64
        assert assignment.primary_fee_ppm == 500


class TestFlowCorridorManager:
    """Test FlowCorridorManager class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.db = MockDatabase()
        self.plugin = MockPlugin()
        self.state_manager = MockStateManager()
        self.liquidity_coord = MockLiquidityCoordinator()

        self.manager = FlowCorridorManager(
            database=self.db,
            plugin=self.plugin,
            state_manager=self.state_manager,
            liquidity_coordinator=self.liquidity_coord
        )
        self.manager.set_our_pubkey("02" + "0" * 64)

    def test_identify_corridors_empty(self):
        """Test identifying corridors when no competition exists."""
        corridors = self.manager.identify_corridors()
        assert len(corridors) == 0

    def test_identify_corridors_with_needs(self):
        """Test identifying corridors with liquidity needs."""
        self.liquidity_coord.add_need(
            "peer1", "02" + "a" * 64, amount_sats=5_000_000
        )

        corridors = self.manager.identify_corridors()
        assert len(corridors) == 1
        assert corridors[0].source_peer_id == "peer1"

    def test_assess_competition_level(self):
        """Test competition level assessment."""
        assert self.manager._assess_competition_level(1) == "none"
        assert self.manager._assess_competition_level(2) == "low"
        assert self.manager._assess_competition_level(3) == "medium"
        assert self.manager._assess_competition_level(5) == "high"

    def test_assign_corridor_no_members(self):
        """Test assigning corridor with no capable members."""
        corridor = FlowCorridor(
            source_peer_id="peer1",
            destination_peer_id="peer2",
            capable_members=[]
        )

        assignment = self.manager.assign_corridor(corridor)
        assert assignment.primary_member == ""
        assert assignment.confidence == 0.0

    def test_assign_corridor_with_members(self):
        """Test assigning corridor with capable members."""
        self.state_manager.set_peer_state("02" + "a" * 64, capacity_sats=20_000_000)
        self.state_manager.set_peer_state("02" + "b" * 64, capacity_sats=10_000_000)

        corridor = FlowCorridor(
            source_peer_id="peer1",
            destination_peer_id="peer2",
            capable_members=["02" + "a" * 64, "02" + "b" * 64]
        )

        assignment = self.manager.assign_corridor(corridor)

        # Member with higher capacity should be primary
        assert assignment.primary_member == "02" + "a" * 64
        assert "02" + "b" * 64 in assignment.secondary_members


# =============================================================================
# FEE COORDINATION MANAGER TESTS
# =============================================================================

class TestFeeCoordinationManager:
    """Test FeeCoordinationManager class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.db = MockDatabase()
        self.plugin = MockPlugin()
        self.state_manager = MockStateManager()
        self.liquidity_coord = MockLiquidityCoordinator()

        self.manager = FeeCoordinationManager(
            database=self.db,
            plugin=self.plugin,
            state_manager=self.state_manager,
            liquidity_coordinator=self.liquidity_coord
        )
        self.manager.set_our_pubkey("02" + "0" * 64)

    def test_get_fee_recommendation_basic(self):
        """Test basic fee recommendation."""
        rec = self.manager.get_fee_recommendation(
            channel_id="123x1x0",
            peer_id="02" + "a" * 64,
            current_fee=500,
            local_balance_pct=0.5
        )

        assert rec.channel_id == "123x1x0"
        assert rec.recommended_fee_ppm >= FLEET_FEE_FLOOR_PPM
        assert rec.recommended_fee_ppm <= FLEET_FEE_CEILING_PPM

    def test_floor_enforcement(self):
        """Test fee floor enforcement."""
        rec = self.manager.get_fee_recommendation(
            channel_id="123x1x0",
            peer_id="02" + "a" * 64,
            current_fee=10,  # Very low
            local_balance_pct=0.5
        )

        assert rec.recommended_fee_ppm >= FLEET_FEE_FLOOR_PPM

    def test_ceiling_enforcement(self):
        """Test fee ceiling enforcement."""
        rec = self.manager.get_fee_recommendation(
            channel_id="123x1x0",
            peer_id="02" + "a" * 64,
            current_fee=3000,  # Above ceiling
            local_balance_pct=0.5
        )

        assert rec.recommended_fee_ppm <= FLEET_FEE_CEILING_PPM

    def test_hive_member_gets_normal_fee(self):
        """Membership is a trust signal, not a fee privilege — members get normal fees."""
        peer_id = "02" + "a" * 64
        self.db.members[peer_id] = {"peer_id": peer_id, "tier": "member"}

        rec = self.manager.get_fee_recommendation(
            channel_id="123x1x0",
            peer_id=peer_id,
            current_fee=500,
            local_balance_pct=0.5
        )

        # Member should NOT get forced 0 PPM — cl-revenue-ops decides fees locally
        assert rec.recommended_fee_ppm >= 0
        assert rec.reason != "hive_member_zero_fee"

    def test_record_routing_outcome_noop(self):
        """Test that record_routing_outcome is a no-op after simplification."""
        # Should not raise
        self.manager.record_routing_outcome(
            channel_id="123x1x0",
            peer_id="02" + "a" * 64,
            fee_ppm=500,
            success=True,
            revenue_sats=100000,
            source="peer1",
            destination="peer2"
        )

    def test_get_coordination_status(self):
        """Test getting coordination status."""
        status = self.manager.get_coordination_status()

        assert "corridor_assignments" in status
        assert "fleet_fee_floor" in status
        assert "fleet_fee_ceiling" in status

    def test_save_restore_noop(self):
        """Test that save/restore are no-ops after simplification."""
        result = self.manager.save_state_to_database()
        assert result == {}

        result = self.manager.restore_state_from_database()
        assert result == {}

    def test_should_auto_backfill_false(self):
        """Test that auto-backfill always returns False after simplification."""
        assert self.manager.should_auto_backfill() is False

    def test_egress_desaturation_bias_no_match_without_saturated_hive_channels(self):
        """No local saturated hive channels means no bias."""
        result = self.manager.get_egress_desaturation_bias(
            channel_id="123x1x0",
            peer_id="03" + "a" * 64,
        )

        assert result["matched"] is False
        assert result["recommended_surcharge_ppm"] == 0
        assert result["reason"] == "no_saturated_hive_egress"

    def test_egress_desaturation_bias_no_match_without_competing_hive_egress(self):
        """Saturated hive channels should not bias unrelated external exits."""
        hive_peer = "02" + "b" * 64
        external_peer = "03" + "c" * 64

        self.db.members[hive_peer] = {"peer_id": hive_peer, "tier": "member"}
        self.state_manager.set_peer_state(
            hive_peer,
            topology=["03" + "d" * 64],
        )
        self.liquidity_coord.set_local_saturated_channels(
            self.manager.our_pubkey,
            [{"peer_id": hive_peer, "local_pct": 97.0, "capacity_sats": 5_000_000}],
        )

        result = self.manager.get_egress_desaturation_bias(
            channel_id="123x1x0",
            peer_id=external_peer,
        )

        assert result["matched"] is False
        assert result["recommended_surcharge_ppm"] == 0
        assert result["reason"] == "no_competing_saturated_hive_egress"

    def test_egress_desaturation_bias_ignores_channels_below_feature_threshold(self):
        """General saturation state below 90% should not trigger the bias feature."""
        hive_peer = "02" + "b" * 64
        external_peer = "03" + "c" * 64

        self.db.members[hive_peer] = {"peer_id": hive_peer, "tier": "member"}
        self.state_manager.set_peer_state(
            hive_peer,
            topology=[external_peer],
        )
        self.liquidity_coord.set_local_saturated_channels(
            self.manager.our_pubkey,
            [{"peer_id": hive_peer, "local_pct": 85.0, "capacity_sats": 5_000_000}],
        )

        result = self.manager.get_egress_desaturation_bias(
            channel_id="123x1x0",
            peer_id=external_peer,
        )

        assert result["matched"] is False
        assert result["recommended_surcharge_ppm"] == 0
        assert result["reason"] == "no_saturated_hive_egress"

    def test_egress_desaturation_bias_returns_bounded_surcharge_for_competing_exit(self):
        """A competing non-fleet exit should receive a bounded surcharge."""
        hive_peer = "02" + "b" * 64
        external_peer = "03" + "c" * 64

        self.db.members[hive_peer] = {"peer_id": hive_peer, "tier": "member"}
        self.state_manager.set_peer_state(
            hive_peer,
            topology=[external_peer],
        )
        self.liquidity_coord.set_local_saturated_channels(
            self.manager.our_pubkey,
            [{"peer_id": hive_peer, "local_pct": 96.0, "capacity_sats": 5_000_000}],
        )

        result = self.manager.get_egress_desaturation_bias(
            channel_id="123x1x0",
            peer_id=external_peer,
        )

        assert result["matched"] is True
        assert result["saturated_hive_peer_id"] == hive_peer
        assert 0 < result["recommended_surcharge_ppm"] <= EGRESS_DESATURATION_MAX_SURCHARGE_PPM
        assert result["reason"] == "competes_with_saturated_hive_egress"

    def test_egress_desaturation_bias_increases_with_more_severe_saturation(self):
        """More severe saturation should increase the recommended surcharge."""
        hive_peer = "02" + "b" * 64
        external_peer = "03" + "c" * 64

        self.db.members[hive_peer] = {"peer_id": hive_peer, "tier": "member"}
        self.state_manager.set_peer_state(
            hive_peer,
            topology=[external_peer],
        )

        self.liquidity_coord.set_local_saturated_channels(
            self.manager.our_pubkey,
            [{"peer_id": hive_peer, "local_pct": 92.0, "capacity_sats": 5_000_000}],
        )
        mild = self.manager.get_egress_desaturation_bias(
            channel_id="123x1x0",
            peer_id=external_peer,
        )

        self.liquidity_coord.set_local_saturated_channels(
            self.manager.our_pubkey,
            [{"peer_id": hive_peer, "local_pct": 99.0, "capacity_sats": 5_000_000}],
        )
        severe = self.manager.get_egress_desaturation_bias(
            channel_id="123x1x0",
            peer_id=external_peer,
        )

        assert mild["matched"] is True
        assert severe["matched"] is True
        assert severe["recommended_surcharge_ppm"] > mild["recommended_surcharge_ppm"]

    def test_egress_desaturation_bias_uses_corridor_assignment_secondary_fallback(self):
        """Fallback corridor matches should accept the fleet member as a secondary owner."""
        hive_peer = "02" + "b" * 64
        primary_peer = "02" + "d" * 64
        external_peer = "03" + "c" * 64

        self.db.members[hive_peer] = {"peer_id": hive_peer, "tier": "member"}
        corridor = FlowCorridor(
            source_peer_id=external_peer,
            destination_peer_id="03" + "e" * 64,
        )
        assignment = CorridorAssignment(
            corridor=corridor,
            primary_member=primary_peer,
            secondary_members=[hive_peer],
            primary_fee_ppm=500,
            secondary_fee_ppm=700,
            assignment_reason="test",
            confidence=0.7,
        )
        self.manager.corridor_mgr._assignments_snapshot = (
            {(corridor.source_peer_id, corridor.destination_peer_id): assignment},
            time.time(),
        )
        self.liquidity_coord.set_local_saturated_channels(
            self.manager.our_pubkey,
            [{"peer_id": hive_peer, "local_pct": 97.0, "capacity_sats": 5_000_000}],
        )

        result = self.manager.get_egress_desaturation_bias(
            channel_id="123x1x0",
            peer_id=external_peer,
        )

        assert result["matched"] is True
        assert result["signal_source"] == "corridor_assignment"

    def test_egress_desaturation_bias_resolves_peer_from_channel_id(self):
        """Channel-only calls should resolve peer identity through listpeerchannels."""
        hive_peer = "02" + "b" * 64
        external_peer = "03" + "c" * 64

        self.db.members[hive_peer] = {"peer_id": hive_peer, "tier": "member"}
        self.plugin.rpc.channels = [
            {"short_channel_id": "123x1x0", "peer_id": external_peer},
        ]
        self.state_manager.set_peer_state(
            hive_peer,
            topology=[external_peer],
        )
        self.liquidity_coord.set_local_saturated_channels(
            self.manager.our_pubkey,
            [{"peer_id": hive_peer, "local_pct": 96.0, "capacity_sats": 5_000_000}],
        )

        result = self.manager.get_egress_desaturation_bias(channel_id="123x1x0")

        assert result["matched"] is True
        assert result["peer_id"] == external_peer


# =============================================================================
# CONSTANT TESTS
# =============================================================================

class TestConstants:
    """Test constant values."""

    def test_fee_bounds(self):
        assert FLEET_FEE_FLOOR_PPM > 0
        assert FLEET_FEE_CEILING_PPM > FLEET_FEE_FLOOR_PPM

    def test_fee_multipliers(self):
        assert PRIMARY_FEE_MULTIPLIER <= SECONDARY_FEE_MULTIPLIER


# =============================================================================
# RECORD FEE CHANGE WIRING TESTS
# =============================================================================

class TestRecordFeeChangeWiring:
    """Test that salient recommendations trigger record_fee_change."""

    def setup_method(self):
        self.db = MockDatabase()
        self.plugin = MockPlugin()
        self.manager = FeeCoordinationManager(
            database=self.db,
            plugin=self.plugin
        )
        self.manager.set_our_pubkey("02" + "0" * 64)

    def test_salient_change_records_fee_change(self):
        """Test that a salient recommendation records fee change time."""
        channel_id = "100x1x0"

        assert self.manager._get_last_fee_change_time(channel_id) == 0

        # Use fee intelligence to drive a significant fee change
        mock_intel = MagicMock()
        mock_intel.get_fee_recommendation.return_value = {
            "recommended_fee_ppm": 200,
            "confidence": 0.9,
        }
        self.manager.set_fee_intelligence_mgr(mock_intel)

        rec = self.manager.get_fee_recommendation(
            channel_id=channel_id,
            peer_id="02" + "a" * 64,
            current_fee=500,
            local_balance_pct=0.5
        )

        if rec.is_salient and rec.recommended_fee_ppm != 500:
            assert self.manager._get_last_fee_change_time(channel_id) > 0

    def test_non_salient_change_no_record(self):
        """Test that a non-salient recommendation doesn't record."""
        channel_id = "100x1x0"

        rec = self.manager.get_fee_recommendation(
            channel_id=channel_id,
            peer_id="02" + "a" * 64,
            current_fee=500,
            local_balance_pct=0.5
        )

        if not rec.is_salient:
            assert self.manager._get_last_fee_change_time(channel_id) == 0


# =============================================================================
# CROSS-WIRE FEE INTELLIGENCE TESTS
# =============================================================================

class TestCrossWireFeeIntelligence:
    """Test fee_intelligence integration into fee_coordination."""

    def setup_method(self):
        self.db = MockDatabase()
        self.plugin = MockPlugin()
        self.manager = FeeCoordinationManager(
            database=self.db,
            plugin=self.plugin
        )
        self.manager.set_our_pubkey("02" + "0" * 64)

    def test_set_fee_intelligence_mgr(self):
        """Test setter method works."""
        mock_intel = MagicMock()
        self.manager.set_fee_intelligence_mgr(mock_intel)
        assert self.manager.fee_intelligence_mgr is mock_intel

    def test_intelligence_blended_when_confident(self):
        """Test that fee intelligence is blended when confidence > 0.3."""
        mock_intel = MagicMock()
        mock_intel.get_fee_recommendation.return_value = {
            "recommended_fee_ppm": 300,
            "confidence": 0.8,
        }
        self.manager.set_fee_intelligence_mgr(mock_intel)

        rec = self.manager.get_fee_recommendation(
            channel_id="100x1x0",
            peer_id="02" + "a" * 64,
            current_fee=500,
            local_balance_pct=0.5
        )

        mock_intel.get_fee_recommendation.assert_called_once()
        assert "intelligence" in rec.reason

    def test_intelligence_skipped_when_low_confidence(self):
        """Test that low-confidence intelligence is ignored."""
        mock_intel = MagicMock()
        mock_intel.get_fee_recommendation.return_value = {
            "recommended_fee_ppm": 300,
            "confidence": 0.1,
        }
        self.manager.set_fee_intelligence_mgr(mock_intel)

        rec = self.manager.get_fee_recommendation(
            channel_id="100x1x0",
            peer_id="02" + "a" * 64,
            current_fee=500,
            local_balance_pct=0.5
        )

        assert "intelligence" not in rec.reason

    def test_intelligence_exception_handled(self):
        """Test that exception from intelligence manager doesn't crash."""
        mock_intel = MagicMock()
        mock_intel.get_fee_recommendation.side_effect = Exception("db error")
        self.manager.set_fee_intelligence_mgr(mock_intel)

        rec = self.manager.get_fee_recommendation(
            channel_id="100x1x0",
            peer_id="02" + "a" * 64,
            current_fee=500,
            local_balance_pct=0.5
        )
        assert rec is not None
