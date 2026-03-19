"""
Tests for Strategic Positioning Module.

Tests cover:
- CorridorValue data class
- PositionRecommendation data class
- PositioningSummary data class
- RouteValueAnalyzer
- FleetPositioningStrategy
- StrategicPositioningManager
"""

import pytest
import time
from unittest.mock import MagicMock, patch

from modules.strategic_positioning import (
    CorridorValue,
    PositionRecommendation,
    PositioningSummary,
    RouteValueAnalyzer,
    FleetPositioningStrategy,
    StrategicPositioningManager,
    HIGH_VALUE_VOLUME_SATS_DAILY,
    MEDIUM_VALUE_VOLUME_SATS_DAILY,
    LOW_COMPETITION_THRESHOLD,
    MEDIUM_COMPETITION_THRESHOLD,
    EXCHANGE_PRIORITY_BONUS,
    MAX_MEMBERS_PER_TARGET,
    PRIORITY_EXCHANGES,
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
        self.corridor_manager = MockCorridorManager()


class MockCorridorManager:
    """Mock corridor manager for testing."""

    def __init__(self):
        self.assignments = []

    def get_all_assignments(self):
        return self.assignments


class MockYieldMetricsManager:
    """Mock yield metrics manager for testing."""

    def __init__(self):
        self.channel_metrics = {}

    def get_channel_yield_metrics(self, channel_id=None):
        if channel_id:
            return self.channel_metrics.get(channel_id, {})
        return self.channel_metrics


# =============================================================================
# CORRIDOR VALUE TESTS
# =============================================================================

class TestCorridorValue:
    """Tests for CorridorValue data class."""

    def test_corridor_value_defaults(self):
        """Test CorridorValue has correct defaults."""
        cv = CorridorValue(
            source_peer_id="source123",
            destination_peer_id="dest456",
        )
        assert cv.source_peer_id == "source123"
        assert cv.destination_peer_id == "dest456"
        assert cv.daily_volume_sats == 0
        assert cv.monthly_volume_sats == 0
        assert cv.competitor_count == 0
        assert cv.fleet_members_present == 0
        assert cv.value_score == 0.0
        assert cv.value_tier == "unknown"

    def test_corridor_value_to_dict(self):
        """Test CorridorValue to_dict method."""
        cv = CorridorValue(
            source_peer_id="source123",
            destination_peer_id="dest456",
            source_alias="Source Node",
            destination_alias="Dest Node",
            daily_volume_sats=5_000_000,
            competitor_count=3,
            value_score=0.85,
            value_tier="high",
        )
        result = cv.to_dict()
        assert result["source_peer_id"] == "source123"
        assert result["destination_peer_id"] == "dest456"
        assert result["daily_volume_sats"] == 5_000_000
        assert result["value_score"] == 0.85
        assert result["value_tier"] == "high"
        assert result["competitor_count"] == 3


class TestPositionRecommendation:
    """Tests for PositionRecommendation data class."""

    def test_position_recommendation_defaults(self):
        """Test PositionRecommendation has correct defaults."""
        rec = PositionRecommendation(
            target_peer_id="peer123",
            target_alias="Test Peer",
        )
        assert rec.target_peer_id == "peer123"
        assert rec.priority_score == 0.0
        assert rec.recommended_capacity_sats == 0
        assert rec.reason == ""

    def test_position_recommendation_to_dict(self):
        """Test PositionRecommendation to_dict method."""
        rec = PositionRecommendation(
            target_peer_id="peer123",
            target_alias="Test Peer",
            priority_score=0.9,
            recommended_capacity_sats=5_000_000,
            reason="High value corridor",
        )
        result = rec.to_dict()
        assert result["target_peer_id"] == "peer123"
        assert result["priority_score"] == 0.9
        assert result["recommended_capacity_sats"] == 5_000_000


class TestPositioningSummary:
    """Tests for PositioningSummary data class."""

    def test_positioning_summary_defaults(self):
        """Test PositioningSummary has correct defaults."""
        summary = PositioningSummary()
        assert summary.total_targets_analyzed == 0
        assert summary.high_value_corridors == 0
        assert summary.exchange_coverage_pct == 0.0
        assert summary.open_recommendations == 0

    def test_positioning_summary_to_dict(self):
        """Test PositioningSummary to_dict method."""
        summary = PositioningSummary(
            total_targets_analyzed=50,
            high_value_corridors=5,
            exchange_coverage_pct=60.0,
            open_recommendations=3,
        )
        result = summary.to_dict()
        assert result["total_targets_analyzed"] == 50
        assert result["high_value_corridors"] == 5
        assert result["exchange_coverage_pct"] == 60.0


# =============================================================================
# ROUTE VALUE ANALYZER TESTS
# =============================================================================

class TestRouteValueAnalyzer:
    """Tests for RouteValueAnalyzer."""

    def test_initialization(self):
        """Test RouteValueAnalyzer initializes correctly."""
        analyzer = RouteValueAnalyzer(plugin=MockPlugin())
        assert analyzer.plugin is not None
        assert analyzer._our_pubkey is None

    def test_set_our_pubkey(self):
        """Test setting our pubkey."""
        analyzer = RouteValueAnalyzer(plugin=MockPlugin())
        analyzer.set_our_pubkey("our123")
        assert analyzer._our_pubkey == "our123"

    def test_analyze_corridor_high_value(self):
        """Test analyzing a high-value corridor."""
        analyzer = RouteValueAnalyzer(plugin=MockPlugin())

        cv = analyzer.analyze_corridor(
            source_peer_id="source123",
            destination_peer_id="dest456",
            volume_sats=(HIGH_VALUE_VOLUME_SATS_DAILY + 1) * 30,
            destination_alias="Test Peer",
        )
        assert cv.value_tier == "high"

    def test_analyze_corridor_medium_value(self):
        """Test analyzing a medium-value corridor."""
        analyzer = RouteValueAnalyzer(plugin=MockPlugin())

        cv = analyzer.analyze_corridor(
            source_peer_id="source123",
            destination_peer_id="dest456",
            volume_sats=(MEDIUM_VALUE_VOLUME_SATS_DAILY + 1) * 30,
            destination_alias="Test Peer",
        )
        assert cv.value_tier == "medium"

    def test_analyze_corridor_low_value(self):
        """Test analyzing a low-value corridor."""
        analyzer = RouteValueAnalyzer(plugin=MockPlugin())

        cv = analyzer.analyze_corridor(
            source_peer_id="source123",
            destination_peer_id="dest456",
            volume_sats=100 * 30,
            destination_alias="Test Peer",
        )
        assert cv.value_tier == "low"

    def test_is_exchange(self):
        """Test exchange detection."""
        analyzer = RouteValueAnalyzer(plugin=MockPlugin())

        # Test with known exchange patterns - returns (is_exchange, priority)
        for name, data in PRIORITY_EXCHANGES.items():
            for pattern in data["alias_patterns"]:
                is_exch, priority = analyzer._is_exchange(pattern + "_test")
                assert is_exch is True

        # Test non-exchange
        is_exch, priority = analyzer._is_exchange("random_node")
        assert is_exch is False

    def test_find_valuable_corridors_empty(self):
        """Test finding valuable corridors with no data."""
        analyzer = RouteValueAnalyzer(plugin=MockPlugin())

        corridors = analyzer.find_valuable_corridors([])
        assert corridors == []

    def test_find_exchange_targets(self):
        """Test finding exchange targets."""
        analyzer = RouteValueAnalyzer(plugin=MockPlugin())

        targets = analyzer.find_exchange_targets()
        assert isinstance(targets, list)
        assert len(targets) > 0  # Should return priority exchanges


# =============================================================================
# FLEET POSITIONING STRATEGY TESTS
# =============================================================================

class TestFleetPositioningStrategy:
    """Tests for FleetPositioningStrategy."""

    def test_initialization(self):
        """Test FleetPositioningStrategy initializes correctly."""
        plugin = MockPlugin()
        analyzer = RouteValueAnalyzer(plugin=plugin)
        strategy = FleetPositioningStrategy(plugin=plugin, route_analyzer=analyzer)
        assert strategy.plugin is not None
        assert strategy.route_analyzer is not None

    def test_set_our_pubkey(self):
        """Test setting our pubkey propagates to route analyzer."""
        plugin = MockPlugin()
        analyzer = RouteValueAnalyzer(plugin=plugin)
        strategy = FleetPositioningStrategy(plugin=plugin, route_analyzer=analyzer)

        strategy.set_our_pubkey("our123")

        assert strategy._our_pubkey == "our123"
        assert strategy.route_analyzer._our_pubkey == "our123"

    def test_count_fleet_channels_to_target_no_state_manager(self):
        """Test counting fleet channels without state manager."""
        strategy = FleetPositioningStrategy(plugin=MockPlugin())

        count = strategy._count_fleet_channels_to_target("peer123")
        assert count == 0

    def test_count_fleet_channels_to_target_with_coverage(self):
        """Test counting fleet channels with coverage."""
        sm = MockStateManager()
        sm.set_peer_state("member1", capacity=5_000_000, topology=["peer123", "peer456"])
        sm.set_peer_state("member2", capacity=3_000_000, topology=["peer123"])

        strategy = FleetPositioningStrategy(
            plugin=MockPlugin(),
            state_manager=sm
        )

        count = strategy._count_fleet_channels_to_target("peer123")
        assert count == 2  # Both members have channels to peer123

    def test_select_best_member_for_target(self):
        """Test selecting best member for a target."""
        sm = MockStateManager()
        sm.set_peer_state("member1", capacity=5_000_000, topology=["peer123"])
        sm.set_peer_state("member2", capacity=3_000_000, topology=[])

        strategy = FleetPositioningStrategy(
            plugin=MockPlugin(),
            state_manager=sm
        )

        # member2 doesn't have the target, so it should be recommended
        best = strategy._select_best_member_for_target(
            target_peer_id="peer123"
        )
        assert best == "member2"

    def test_recommend_next_open_cooldown(self):
        """Test recommendation cooldown."""
        strategy = FleetPositioningStrategy(plugin=MockPlugin())

        recs = strategy.get_positioning_recommendations(count=5)
        assert isinstance(recs, list)

    def test_get_positioning_recommendations_empty(self):
        """Test getting recommendations with no corridors."""
        strategy = FleetPositioningStrategy(plugin=MockPlugin())

        recs = strategy.get_positioning_recommendations(count=5)
        assert len(recs) == 0


# =============================================================================
# STRATEGIC POSITIONING MANAGER TESTS
# =============================================================================

class TestStrategicPositioningManager:
    """Tests for StrategicPositioningManager."""

    def test_initialization(self):
        """Test StrategicPositioningManager initializes correctly."""
        plugin = MockPlugin()
        manager = StrategicPositioningManager(plugin=plugin)

        assert manager.plugin == plugin
        assert manager.route_analyzer is not None
        assert manager.positioning_strategy is not None

    def test_set_our_pubkey(self):
        """Test setting our pubkey propagates to all components."""
        plugin = MockPlugin()
        manager = StrategicPositioningManager(plugin=plugin)

        manager.set_our_pubkey("our123")

        assert manager._our_pubkey == "our123"
        assert manager.route_analyzer._our_pubkey == "our123"
        assert manager.positioning_strategy._our_pubkey == "our123"

    def test_get_valuable_corridors(self):
        """Test getting valuable corridors."""
        plugin = MockPlugin()
        manager = StrategicPositioningManager(plugin=plugin)

        corridors = manager.get_valuable_corridors()
        assert corridors == []

    def test_get_exchange_coverage(self):
        """Test getting exchange coverage."""
        plugin = MockPlugin()
        manager = StrategicPositioningManager(plugin=plugin)

        coverage = manager.get_exchange_coverage()

        assert "total_priority_exchanges" in coverage
        assert "covered_exchanges" in coverage
        assert "coverage_pct" in coverage
        assert "exchanges" in coverage

    def test_get_positioning_recommendations(self):
        """Test getting positioning recommendations."""
        plugin = MockPlugin()
        manager = StrategicPositioningManager(plugin=plugin)

        recs = manager.get_positioning_recommendations()
        assert isinstance(recs, list)

    def test_get_positioning_summary(self):
        """Test getting positioning summary."""
        plugin = MockPlugin()
        manager = StrategicPositioningManager(plugin=plugin)

        summary = manager.get_positioning_summary()

        assert "total_targets_analyzed" in summary
        assert "high_value_corridors" in summary
        assert "exchange_coverage_pct" in summary
        assert "open_recommendations" in summary

    def test_get_status(self):
        """Test getting positioning status."""
        plugin = MockPlugin()
        manager = StrategicPositioningManager(plugin=plugin)

        status = manager.get_status()

        assert status["enabled"] is True
        assert "summary" in status
        assert "thresholds" in status
        assert "priority_exchanges" in status

        # Check thresholds
        thresholds = status["thresholds"]
        assert thresholds["max_members_per_target"] == MAX_MEMBERS_PER_TARGET


# =============================================================================
# CONSTANT VALIDATION TESTS
# =============================================================================

class TestConstants:
    """Tests for module constants."""

    def test_volume_thresholds_ordered(self):
        """Test volume thresholds are properly ordered."""
        assert HIGH_VALUE_VOLUME_SATS_DAILY > MEDIUM_VALUE_VOLUME_SATS_DAILY

    def test_competition_thresholds_ordered(self):
        """Test competition thresholds are properly ordered."""
        assert MEDIUM_COMPETITION_THRESHOLD > LOW_COMPETITION_THRESHOLD

    def test_priority_exchanges_valid(self):
        """Test priority exchanges have required fields."""
        for name, data in PRIORITY_EXCHANGES.items():
            assert "alias_patterns" in data
            assert "priority" in data
            assert isinstance(data["alias_patterns"], list)
            assert 0 <= data["priority"] <= 1.0

    def test_bonuses_positive(self):
        """Test bonus multipliers are positive."""
        assert EXCHANGE_PRIORITY_BONUS > 1.0
        assert MAX_MEMBERS_PER_TARGET >= 1


# =============================================================================
# RPC COMMAND HANDLER TESTS
# =============================================================================

class TestRpcCommandHandlers:
    """Tests for RPC command handlers in rpc_commands.py."""

    def test_valuable_corridors_handler(self):
        """Test valuable_corridors RPC handler."""
        from modules.rpc_commands import valuable_corridors, HiveContext

        plugin = MockPlugin()
        manager = StrategicPositioningManager(plugin=plugin)

        ctx = MagicMock(spec=HiveContext)
        ctx.strategic_positioning_mgr = manager

        result = valuable_corridors(ctx, min_score=0.05)

        assert "corridors" in result
        assert "total_count" in result
        assert "by_value_tier" in result

    def test_valuable_corridors_handler_not_initialized(self):
        """Test valuable_corridors RPC handler when not initialized."""
        from modules.rpc_commands import valuable_corridors, HiveContext

        ctx = MagicMock(spec=HiveContext)
        ctx.strategic_positioning_mgr = None

        result = valuable_corridors(ctx)
        assert "error" in result

    def test_exchange_coverage_handler(self):
        """Test exchange_coverage RPC handler."""
        from modules.rpc_commands import exchange_coverage, HiveContext

        plugin = MockPlugin()
        manager = StrategicPositioningManager(plugin=plugin)

        ctx = MagicMock(spec=HiveContext)
        ctx.strategic_positioning_mgr = manager

        result = exchange_coverage(ctx)

        assert "total_priority_exchanges" in result
        assert "covered_exchanges" in result

    def test_positioning_recommendations_handler(self):
        """Test positioning_recommendations RPC handler."""
        from modules.rpc_commands import positioning_recommendations, HiveContext

        plugin = MockPlugin()
        manager = StrategicPositioningManager(plugin=plugin)

        ctx = MagicMock(spec=HiveContext)
        ctx.strategic_positioning_mgr = manager

        result = positioning_recommendations(ctx, count=3)

        assert "recommendations" in result
        assert "count" in result
        assert "by_priority" in result

    def test_positioning_summary_handler(self):
        """Test positioning_summary RPC handler."""
        from modules.rpc_commands import positioning_summary, HiveContext

        plugin = MockPlugin()
        manager = StrategicPositioningManager(plugin=plugin)

        ctx = MagicMock(spec=HiveContext)
        ctx.strategic_positioning_mgr = manager

        result = positioning_summary(ctx)

        assert "total_targets_analyzed" in result

    def test_positioning_status_handler(self):
        """Test positioning_status RPC handler."""
        from modules.rpc_commands import positioning_status, HiveContext

        plugin = MockPlugin()
        manager = StrategicPositioningManager(plugin=plugin)

        ctx = MagicMock(spec=HiveContext)
        ctx.strategic_positioning_mgr = manager

        result = positioning_status(ctx)

        assert result["enabled"] is True
        assert "thresholds" in result
        assert "priority_exchanges" in result
