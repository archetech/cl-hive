"""
Tests for Channel Rationalization Module.

Tests cover:
- PeerCoverage data class
- CloseRecommendation data class
- RationalizationSummary data class
- RedundancyAnalyzer
- ChannelRationalizer
- RationalizationManager
"""

import pytest
import time
from unittest.mock import MagicMock, patch

from modules.channel_rationalization import (
    PeerCoverage,
    CloseRecommendation,
    RationalizationSummary,
    RedundancyAnalyzer,
    ChannelRationalizer,
    RationalizationManager,
    OWNERSHIP_DOMINANT_RATIO,
    OWNERSHIP_MIN_MARKERS,
    OWNERSHIP_MIN_STRENGTH,
    MAX_HEALTHY_REDUNDANCY,
    UNDERPERFORMER_MARKER_RATIO,
)


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

    def listpeerchannels(self):
        return {"channels": self.channels}


class MockStateManager:
    """Mock state manager for testing."""

    def __init__(self):
        self.peer_states = {}

    def get_peer_state(self, peer_id):
        return self.peer_states.get(peer_id)

    def get_all_peer_states(self):
        return list(self.peer_states.values())

    def set_peer_state(self, peer_id, capacity=0, topology=None):
        state = MagicMock()
        state.peer_id = peer_id
        state.capacity_sats = capacity
        state.topology = topology or []
        self.peer_states[peer_id] = state


class MockFeeCoordinationManager:
    """Mock fee coordination manager for testing."""

    def __init__(self):
        self.stigmergy = MockStigmergy()


class MockStigmergy:
    """Mock stigmergic marker store for testing."""

    def __init__(self):
        self.markers = []

    def get_all_markers(self):
        return self.markers

    def add_marker(self, depositor, source, destination, fee_ppm, success, volume_sats, strength=1.0):
        marker = MagicMock()
        marker.depositor = depositor
        marker.source_peer_id = source
        marker.destination_peer_id = destination
        marker.fee_ppm = fee_ppm
        marker.success = success
        marker.volume_sats = volume_sats
        marker.strength = strength
        self.markers.append(marker)


class MockGovernance:
    """Mock governance for testing."""

    def __init__(self):
        self.pending_actions = []

    def create_pending_action(self, action_type, data, source):
        self.pending_actions.append({
            "action_type": action_type,
            "data": data,
            "source": source
        })


# =============================================================================
# DATA CLASS TESTS
# =============================================================================

class TestPeerCoverage:
    """Test PeerCoverage data class."""

    def test_basic_creation(self):
        """Test creating a basic peer coverage."""
        coverage = PeerCoverage(peer_id="02" + "a" * 64)

        assert coverage.peer_id == "02" + "a" * 64
        assert coverage.members_with_channels == []
        assert coverage.redundancy_count == 0
        assert coverage.is_over_redundant is False

    def test_to_dict_serialization(self):
        """Test to_dict serialization."""
        coverage = PeerCoverage(
            peer_id="02" + "a" * 64,
            peer_alias="TestPeer",
            members_with_channels=["02" + "b" * 64, "02" + "c" * 64],
            member_marker_strength={"02" + "b" * 64: 5.0, "02" + "c" * 64: 1.0},
            member_marker_count={"02" + "b" * 64: 10, "02" + "c" * 64: 2},
            owner_member="02" + "b" * 64,
            ownership_confidence=0.83,
            redundancy_count=2,
            is_over_redundant=False
        )

        d = coverage.to_dict()

        assert d["peer_id"] == "02" + "a" * 64
        assert d["peer_alias"] == "TestPeer"
        assert len(d["members_with_channels"]) == 2
        assert d["owner_member"] == "02" + "b" * 64
        assert d["ownership_confidence"] == 0.83

    def test_over_redundant_flag(self):
        """Test over redundant detection."""
        coverage = PeerCoverage(
            peer_id="02" + "a" * 64,
            members_with_channels=["02" + "b" * 64, "02" + "c" * 64, "02" + "d" * 64],
            redundancy_count=3,
            is_over_redundant=True
        )

        assert coverage.is_over_redundant is True
        assert coverage.redundancy_count > MAX_HEALTHY_REDUNDANCY


class TestCloseRecommendation:
    """Test CloseRecommendation data class."""

    def test_basic_creation(self):
        """Test creating a basic close recommendation."""
        rec = CloseRecommendation(
            member_id="02" + "a" * 64,
            peer_id="02" + "b" * 64,
            channel_id="123x1x0"
        )

        assert rec.member_id == "02" + "a" * 64
        assert rec.peer_id == "02" + "b" * 64
        assert rec.channel_id == "123x1x0"
        assert rec.urgency == "low"

    def test_to_dict_serialization(self):
        """Test to_dict serialization."""
        rec = CloseRecommendation(
            member_id="02" + "a" * 64,
            peer_id="02" + "b" * 64,
            channel_id="123x1x0",
            peer_alias="TestPeer",
            member_marker_strength=0.5,
            owner_marker_strength=10.0,
            owner_member="02" + "c" * 64,
            capacity_sats=5_000_000,
            channel_age_days=45,
            reason="Underperforming: 5% of owner's activity",
            confidence=0.75,
            urgency="medium",
            freed_capital_sats=5_000_000
        )

        d = rec.to_dict()

        assert d["channel_id"] == "123x1x0"
        assert d["member_marker_strength"] == 0.5
        assert d["owner_marker_strength"] == 10.0
        assert d["urgency"] == "medium"
        assert d["freed_capital_sats"] == 5_000_000


class TestRationalizationSummary:
    """Test RationalizationSummary data class."""

    def test_basic_creation(self):
        """Test creating a basic summary."""
        summary = RationalizationSummary(
            total_peers_analyzed=50,
            redundant_peers=15,
            over_redundant_peers=5,
            close_recommendations=3
        )

        assert summary.total_peers_analyzed == 50
        assert summary.redundant_peers == 15
        assert summary.over_redundant_peers == 5

    def test_to_dict_serialization(self):
        """Test to_dict serialization."""
        summary = RationalizationSummary(
            total_peers_analyzed=50,
            redundant_peers=15,
            well_owned_peers=10,
            contested_peers=3,
            orphan_peers=2,
            potential_freed_capital_sats=10_000_000
        )

        d = summary.to_dict()

        assert d["total_peers_analyzed"] == 50
        assert d["well_owned_peers"] == 10
        assert d["contested_peers"] == 3
        assert d["potential_freed_capital_sats"] == 10_000_000


# =============================================================================
# REDUNDANCY ANALYZER TESTS
# =============================================================================

class TestRedundancyAnalyzer:
    """Test RedundancyAnalyzer class."""

    def test_initialization(self):
        """Test basic initialization."""
        plugin = MockPlugin()
        state_manager = MockStateManager()

        analyzer = RedundancyAnalyzer(
            plugin=plugin,
            state_manager=state_manager
        )

        assert analyzer.plugin == plugin
        assert analyzer.state_manager == state_manager

    def test_set_our_pubkey(self):
        """Test setting our pubkey."""
        plugin = MockPlugin()
        analyzer = RedundancyAnalyzer(plugin=plugin)

        analyzer.set_our_pubkey("02" + "a" * 64)

        assert analyzer._our_pubkey == "02" + "a" * 64

    def test_get_fleet_members_empty(self):
        """Test getting fleet members when empty."""
        plugin = MockPlugin()
        state_manager = MockStateManager()

        analyzer = RedundancyAnalyzer(
            plugin=plugin,
            state_manager=state_manager
        )

        members = analyzer._get_fleet_members()

        assert len(members) == 0

    def test_get_fleet_members_with_state(self):
        """Test getting fleet members from state."""
        plugin = MockPlugin()
        state_manager = MockStateManager()

        # Add fleet members
        state_manager.set_peer_state(
            "02" + "a" * 64,
            topology=["02" + "x" * 64]
        )
        state_manager.set_peer_state(
            "02" + "b" * 64,
            topology=["02" + "y" * 64]
        )

        analyzer = RedundancyAnalyzer(
            plugin=plugin,
            state_manager=state_manager
        )

        members = analyzer._get_fleet_members()

        assert len(members) == 2

    def test_analyze_peer_coverage_no_members(self):
        """Test analyzing coverage with no members."""
        plugin = MockPlugin()
        state_manager = MockStateManager()

        analyzer = RedundancyAnalyzer(
            plugin=plugin,
            state_manager=state_manager
        )

        coverage = analyzer.analyze_peer_coverage("02" + "x" * 64)

        assert coverage.peer_id == "02" + "x" * 64
        assert len(coverage.members_with_channels) == 0

    def test_analyze_peer_coverage_single_member(self):
        """Test analyzing coverage with single member."""
        plugin = MockPlugin()
        state_manager = MockStateManager()

        external_peer = "02" + "x" * 64
        member = "02" + "a" * 64

        # Member has channel to external peer
        state_manager.set_peer_state(member, topology=[external_peer])

        analyzer = RedundancyAnalyzer(
            plugin=plugin,
            state_manager=state_manager
        )

        coverage = analyzer.analyze_peer_coverage(external_peer)

        assert len(coverage.members_with_channels) == 1
        assert member in coverage.members_with_channels
        assert coverage.redundancy_count == 1
        assert coverage.is_over_redundant is False

    def test_analyze_peer_coverage_redundant(self):
        """Test analyzing coverage with redundant members."""
        plugin = MockPlugin()
        state_manager = MockStateManager()
        fee_coord_mgr = MockFeeCoordinationManager()

        external_peer = "02" + "x" * 64
        member_a = "02" + "a" * 64
        member_b = "02" + "b" * 64
        member_c = "02" + "c" * 64

        # Multiple members have channels to same external peer
        state_manager.set_peer_state(member_a, topology=[external_peer])
        state_manager.set_peer_state(member_b, topology=[external_peer])
        state_manager.set_peer_state(member_c, topology=[external_peer])

        analyzer = RedundancyAnalyzer(
            plugin=plugin,
            state_manager=state_manager,
            fee_coordination_mgr=fee_coord_mgr
        )

        coverage = analyzer.analyze_peer_coverage(external_peer)

        assert len(coverage.members_with_channels) == 3
        assert coverage.redundancy_count == 3
        assert coverage.is_over_redundant is True

    def test_determine_ownership_by_markers(self):
        """Test ownership determination based on markers."""
        plugin = MockPlugin()
        state_manager = MockStateManager()
        fee_coord_mgr = MockFeeCoordinationManager()

        external_peer = "02" + "x" * 64
        member_a = "02" + "a" * 64  # Will be owner (strong markers)
        member_b = "02" + "b" * 64  # Weak markers

        state_manager.set_peer_state(member_a, topology=[external_peer])
        state_manager.set_peer_state(member_b, topology=[external_peer])

        # Member A has strong markers
        for i in range(5):
            fee_coord_mgr.stigmergy.add_marker(
                depositor=member_a,
                source=external_peer,
                destination="02" + "y" * 64,
                fee_ppm=500,
                success=True,
                volume_sats=1_000_000,
                strength=2.0
            )

        # Member B has weak markers
        fee_coord_mgr.stigmergy.add_marker(
            depositor=member_b,
            source=external_peer,
            destination="02" + "z" * 64,
            fee_ppm=500,
            success=True,
            volume_sats=100_000,
            strength=0.5
        )

        analyzer = RedundancyAnalyzer(
            plugin=plugin,
            state_manager=state_manager,
            fee_coordination_mgr=fee_coord_mgr
        )

        coverage = analyzer.analyze_peer_coverage(external_peer)

        # Member A should own this peer
        assert coverage.owner_member == member_a
        assert coverage.ownership_confidence > 0.6

    def test_get_redundant_peers(self):
        """Test getting all redundant peers."""
        plugin = MockPlugin()
        state_manager = MockStateManager()

        # External peers
        external_a = "02" + "x" * 64
        external_b = "02" + "y" * 64

        # Fleet members
        member_a = "02" + "a" * 64
        member_b = "02" + "b" * 64

        # Both members have channels to external_a (redundant)
        # Only member_a has channel to external_b (not redundant)
        state_manager.set_peer_state(member_a, topology=[external_a, external_b])
        state_manager.set_peer_state(member_b, topology=[external_a])

        analyzer = RedundancyAnalyzer(
            plugin=plugin,
            state_manager=state_manager
        )

        redundant = analyzer.get_redundant_peers()

        # Only external_a should be flagged as redundant
        assert len(redundant) == 1
        assert redundant[0].peer_id == external_a


# =============================================================================
# CHANNEL RATIONALIZER TESTS
# =============================================================================

class TestChannelRationalizer:
    """Test ChannelRationalizer class."""

    def test_initialization(self):
        """Test basic initialization."""
        plugin = MockPlugin()

        rationalizer = ChannelRationalizer(plugin=plugin)

        assert rationalizer.plugin == plugin
        assert rationalizer.redundancy_analyzer is not None

    def test_set_our_pubkey(self):
        """Test setting our pubkey propagates."""
        plugin = MockPlugin()
        rationalizer = ChannelRationalizer(plugin=plugin)

        pubkey = "02" + "a" * 64
        rationalizer.set_our_pubkey(pubkey)

        assert rationalizer._our_pubkey == pubkey
        assert rationalizer.redundancy_analyzer._our_pubkey == pubkey

    def test_should_recommend_close_for_owner(self):
        """Test that owners don't get close recommendations."""
        plugin = MockPlugin()
        rationalizer = ChannelRationalizer(plugin=plugin)

        member = "02" + "a" * 64
        coverage = PeerCoverage(
            peer_id="02" + "x" * 64,
            owner_member=member,
            member_marker_strength={member: 10.0}
        )

        should_close, reason, conf, urg, connectivity_impact = rationalizer._should_recommend_close(
            coverage, member
        )

        assert should_close is False

    def test_should_recommend_close_for_underperformer(self):
        """Test close recommendation for underperformer."""
        plugin = MockPlugin()
        rationalizer = ChannelRationalizer(plugin=plugin)

        owner = "02" + "a" * 64
        underperformer = "02" + "b" * 64

        coverage = PeerCoverage(
            peer_id="02" + "x" * 64,
            members_with_channels=[owner, underperformer],
            owner_member=owner,
            ownership_confidence=0.8,
            member_marker_strength={
                owner: 10.0,        # Strong
                underperformer: 0.5  # Weak (<10% of owner)
            },
            redundancy_count=2,
            is_over_redundant=False
        )

        should_close, reason, conf, urg, connectivity_impact = rationalizer._should_recommend_close(
            coverage, underperformer
        )

        assert should_close is True
        assert "Underperforming" in reason

    def test_generate_close_recommendations_empty(self):
        """Test generating recommendations with no redundancy."""
        plugin = MockPlugin()
        state_manager = MockStateManager()

        rationalizer = ChannelRationalizer(
            plugin=plugin,
            state_manager=state_manager
        )

        recommendations = rationalizer.generate_close_recommendations()

        assert len(recommendations) == 0

    def test_get_my_close_recommendations(self):
        """Test filtering recommendations for our node."""
        plugin = MockPlugin()
        state_manager = MockStateManager()
        fee_coord_mgr = MockFeeCoordinationManager()

        our_pubkey = "02" + "a" * 64
        other_member = "02" + "b" * 64
        external_peer = "02" + "x" * 64

        # Setup: Both have channel, but other_member owns it
        state_manager.set_peer_state(our_pubkey, topology=[external_peer])
        state_manager.set_peer_state(other_member, topology=[external_peer])

        # Other member has strong markers (owns it)
        for i in range(5):
            fee_coord_mgr.stigmergy.add_marker(
                depositor=other_member,
                source=external_peer,
                destination="02" + "z" * 64,
                fee_ppm=500,
                success=True,
                volume_sats=1_000_000,
                strength=2.0
            )

        rationalizer = ChannelRationalizer(
            plugin=plugin,
            state_manager=state_manager,
            fee_coordination_mgr=fee_coord_mgr
        )
        rationalizer.set_our_pubkey(our_pubkey)

        my_recs = rationalizer.get_my_close_recommendations()

        # We should get a recommendation to close our channel
        # (if the ownership threshold is met)
        for rec in my_recs:
            assert rec.member_id == our_pubkey

    def test_create_pending_actions(self):
        """Test creating pending actions for recommendations."""
        plugin = MockPlugin()
        governance = MockGovernance()

        rationalizer = ChannelRationalizer(
            plugin=plugin,
            governance=governance
        )

        recommendations = [
            CloseRecommendation(
                member_id="02" + "a" * 64,
                peer_id="02" + "x" * 64,
                channel_id="123x1x0",
                confidence=0.75,
                urgency="medium",
                reason="Underperforming",
                freed_capital_sats=5_000_000
            )
        ]

        created = rationalizer.create_pending_actions(recommendations)

        assert created == 1
        assert len(governance.pending_actions) == 1
        assert governance.pending_actions[0]["action_type"] == "close_recommendation"


# =============================================================================
# RATIONALIZATION MANAGER TESTS
# =============================================================================

class TestRationalizationManager:
    """Test RationalizationManager class."""

    def test_initialization(self):
        """Test basic initialization."""
        plugin = MockPlugin()

        manager = RationalizationManager(plugin=plugin)

        assert manager.plugin == plugin
        assert manager.rationalizer is not None

    def test_set_our_pubkey(self):
        """Test setting our pubkey propagates."""
        plugin = MockPlugin()
        manager = RationalizationManager(plugin=plugin)

        pubkey = "02" + "a" * 64
        manager.set_our_pubkey(pubkey)

        assert manager._our_pubkey == pubkey
        assert manager.rationalizer._our_pubkey == pubkey

    def test_analyze_coverage_specific_peer(self):
        """Test analyzing coverage for specific peer."""
        plugin = MockPlugin()
        state_manager = MockStateManager()

        external_peer = "02" + "x" * 64
        member = "02" + "a" * 64

        state_manager.set_peer_state(member, topology=[external_peer])

        manager = RationalizationManager(
            plugin=plugin,
            state_manager=state_manager
        )

        result = manager.analyze_coverage(peer_id=external_peer)

        assert result["peer_id"] == external_peer
        assert "coverage" in result

    def test_get_close_recommendations(self):
        """Test getting close recommendations."""
        plugin = MockPlugin()

        manager = RationalizationManager(plugin=plugin)

        recs = manager.get_close_recommendations()

        assert isinstance(recs, list)

    def test_get_summary(self):
        """Test getting rationalization summary."""
        plugin = MockPlugin()

        manager = RationalizationManager(plugin=plugin)

        summary = manager.get_summary()

        assert "total_peers_analyzed" in summary
        assert "redundant_peers" in summary
        assert "close_recommendations" in summary

    def test_get_status(self):
        """Test getting rationalization status."""
        plugin = MockPlugin()

        manager = RationalizationManager(plugin=plugin)

        status = manager.get_status()

        assert status["enabled"] is True
        assert "summary" in status
        assert "health" in status
        assert "thresholds" in status


# =============================================================================
# CONSTANTS TESTS
# =============================================================================

class TestConstants:
    """Test constant values."""

    def test_ownership_thresholds(self):
        """Verify ownership thresholds are reasonable."""
        assert 0 < OWNERSHIP_DOMINANT_RATIO < 1
        assert OWNERSHIP_DOMINANT_RATIO == 0.6  # 60%
        assert OWNERSHIP_MIN_MARKERS >= 1
        assert OWNERSHIP_MIN_STRENGTH > 0

    def test_redundancy_thresholds(self):
        """Verify redundancy thresholds are reasonable."""
        assert MAX_HEALTHY_REDUNDANCY >= 2
        assert MAX_HEALTHY_REDUNDANCY == 2  # Up to 2 members per peer is healthy

    def test_underperformer_threshold(self):
        """Verify underperformer threshold is reasonable."""
        assert 0 < UNDERPERFORMER_MARKER_RATIO < 1
        assert UNDERPERFORMER_MARKER_RATIO == 0.1  # <10% = underperformer
