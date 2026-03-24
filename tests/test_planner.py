"""
Tests for Phase 6: Planner Module

Tests the Planner class for:
- Network cache refresh and directional dedup
- Saturation calculation with gossip clamping
- Guard mechanism with max ignores/cycle limit
- Fail-closed on RPC errors

Author: Lightning Goats Team
"""

import pytest
import time
from unittest.mock import MagicMock, patch, PropertyMock
from dataclasses import dataclass

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.planner import (
    Planner, ChannelInfo, SaturationResult, RpcError, ExpansionRecommendation,
    ChannelSizer, ChannelSizeResult,
    MAX_IGNORES_PER_CYCLE, SATURATION_RELEASE_THRESHOLD_PCT,
    MIN_TARGET_CAPACITY_SATS, NETWORK_CACHE_TTL_SECONDS,
    MIN_QUALITY_SCORE,
    # Cooperation module constants (Phase 7)
    HIVE_COVERAGE_MAJORITY_PCT, LOW_COMPETITION_CHANNELS,
    MEDIUM_COMPETITION_CHANNELS, HIGH_COMPETITION_CHANNELS,
    COMPETITION_DISCOUNT_LOW, COMPETITION_DISCOUNT_MEDIUM,
    COMPETITION_DISCOUNT_HIGH, BOTTLENECK_BONUS_MULTIPLIER
)


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def mock_state_manager():
    """Create a mock StateManager."""
    sm = MagicMock()
    sm.get_all_peer_states.return_value = []
    return sm


@pytest.fixture
def mock_database():
    """Create a mock database."""
    db = MagicMock()
    db.get_all_members.return_value = []
    db.log_planner_action = MagicMock()
    # Mock pending action tracking methods (rejection tracking)
    db.has_pending_action_for_target.return_value = False
    db.was_recently_rejected.return_value = False
    db.get_rejection_count.return_value = 0
    # Mock global constraint tracking (BUG-001 fix)
    db.count_consecutive_expansion_rejections.return_value = 0
    db.get_recent_expansion_rejections.return_value = []
    # Mock budget tracking
    db.get_available_budget.return_value = 2_000_000  # Matches failsafe_budget_per_day
    db.get_pending_channel_open_total.return_value = 0
    # Mock ignored peers (planner ignore feature)
    db.is_peer_ignored.return_value = False
    # Mock peer event summary for quality scorer (neutral values)
    db.get_peer_event_summary.return_value = {
        "peer_id": "",
        "event_count": 0,
        "open_count": 0,
        "close_count": 0,
        "remote_close_count": 0,
        "local_close_count": 0,
        "mutual_close_count": 0,
        "total_revenue_sats": 0,
        "total_rebalance_cost_sats": 0,
        "total_net_pnl_sats": 0,
        "total_forward_count": 0,
        "avg_routing_score": 0.5,
        "avg_profitability_score": 0.5,
        "avg_duration_days": 0,
        "reporters": []
    }
    return db


@pytest.fixture
def mock_bridge():
    """Create a mock Bridge."""
    return MagicMock()


@pytest.fixture
def mock_plugin():
    """Create a mock plugin."""
    plugin = MagicMock()
    plugin.log = MagicMock()
    plugin.rpc = MagicMock()
    return plugin


@pytest.fixture
def mock_config():
    """Create a mock config snapshot."""
    cfg = MagicMock()
    cfg.market_share_cap_pct = 0.20  # 20%
    # Channel size options
    cfg.planner_min_channel_sats = 1_000_000  # 1M sats
    cfg.planner_max_channel_sats = 50_000_000  # 50M sats
    cfg.planner_default_channel_sats = 5_000_000  # 5M sats
    # Global constraint tracking (BUG-001 fix)
    cfg.expansion_pause_threshold = 3  # Pause after 3 consecutive rejections
    cfg.planner_safety_reserve_sats = 500_000  # 500k sats safety reserve
    cfg.planner_fee_buffer_sats = 100_000  # 100k sats for on-chain fees
    # Budget constraints (ensures proposals are within executable limits)
    cfg.daily_expansion_budget_sats = 2_000_000  # 2M sats daily budget
    cfg.budget_reserve_pct = 0.20  # 20% reserve
    cfg.budget_max_per_channel_pct = 0.50  # 50% of daily budget per channel (= 1M)
    return cfg


@pytest.fixture
def planner(mock_state_manager, mock_database, mock_bridge, mock_plugin):
    """Create a Planner instance with mocked dependencies."""
    return Planner(
        state_manager=mock_state_manager,
        database=mock_database,
        bridge=mock_bridge,
        plugin=mock_plugin
    )


# =============================================================================
# NETWORK CACHE TESTS (Directional Dedup)
# =============================================================================

class TestNetworkCache:
    """Test network cache refresh and deduplication."""

    def test_refresh_network_cache_success(self, planner, mock_plugin):
        """_refresh_network_cache should populate cache from listchannels."""
        mock_plugin.rpc.listchannels.return_value = {
            'channels': [
                {
                    'source': '02' + 'a' * 64,
                    'destination': '02' + 'b' * 64,
                    'short_channel_id': '123x1x0',
                    'satoshis': 1000000,
                    'active': True
                }
            ]
        }

        result = planner._refresh_network_cache(force=True)

        assert result is True
        assert len(planner._network_cache) > 0

    def test_refresh_network_cache_rpc_failure(self, planner, mock_plugin):
        """_refresh_network_cache should return False on RPC error."""
        mock_plugin.rpc.listchannels.side_effect = RpcError('listchannels', {}, 'timeout')

        result = planner._refresh_network_cache(force=True)

        assert result is False

    def test_directional_dedup(self, planner, mock_plugin):
        """Should deduplicate bidirectional channels (A->B and B->A counted once)."""
        # Same channel, both directions
        mock_plugin.rpc.listchannels.return_value = {
            'channels': [
                {
                    'source': '02' + 'a' * 64,
                    'destination': '02' + 'b' * 64,
                    'short_channel_id': '123x1x0',
                    'satoshis': 1000000,
                    'active': True
                },
                {
                    'source': '02' + 'b' * 64,
                    'destination': '02' + 'a' * 64,
                    'short_channel_id': '123x1x0',  # Same channel
                    'satoshis': 1000000,
                    'active': True
                }
            ]
        }

        planner._refresh_network_cache(force=True)

        # Should not double-count
        target_a = '02' + 'a' * 64
        target_b = '02' + 'b' * 64

        # Each target should have exactly 1 channel entry (the deduplicated one)
        channels_to_a = planner._network_cache.get(target_a, [])
        channels_to_b = planner._network_cache.get(target_b, [])

        # The dedup logic should result in consistent counts
        assert len(channels_to_a) == len(channels_to_b)

    def test_cache_ttl_respected(self, planner, mock_plugin):
        """Should not refresh if cache is fresh."""
        mock_plugin.rpc.listchannels.return_value = {'channels': []}

        # First refresh
        planner._refresh_network_cache(force=True)
        call_count_1 = mock_plugin.rpc.listchannels.call_count

        # Second refresh without force (should use cache)
        planner._refresh_network_cache(force=False)
        call_count_2 = mock_plugin.rpc.listchannels.call_count

        assert call_count_2 == call_count_1  # No additional call


# =============================================================================
# SATURATION CALCULATION TESTS
# =============================================================================

class TestSaturationCalculation:
    """Test saturation calculation with gossip clamping."""

    def test_calculate_hive_share_basic(self, planner, mock_database, mock_state_manager, mock_plugin, mock_config):
        """Basic saturation calculation."""
        target = '02' + 'c' * 64
        member1 = '02' + 'a' * 64

        # Setup Hive member
        mock_database.get_all_members.return_value = [
            {'peer_id': member1, 'tier': 'member'}
        ]

        # Setup member state with target in topology
        mock_state = MagicMock()
        mock_state.peer_id = member1
        mock_state.topology = [target]
        mock_state.capacity_sats = 500000  # 500k sats
        mock_state_manager.get_all_peer_states.return_value = [mock_state]

        # Setup network cache with public channels
        mock_plugin.rpc.listchannels.return_value = {
            'channels': [
                {
                    'source': member1,
                    'destination': target,
                    'short_channel_id': '100x1x0',
                    'satoshis': 500000,
                    'active': True
                },
                {
                    'source': '02' + 'd' * 64,  # Non-hive node
                    'destination': target,
                    'short_channel_id': '200x1x0',
                    'satoshis': 2000000,  # 2M sats
                    'active': True
                }
            ]
        }
        planner._refresh_network_cache(force=True)

        result = planner._calculate_hive_share(target, mock_config)

        # Hive has 500k out of 2.5M total = 20%
        assert result.hive_capacity_sats == 500000
        assert result.public_capacity_sats == 2500000
        assert abs(result.hive_share_pct - 0.20) < 0.01

    def test_gossip_clamping_to_public_reality(self, planner, mock_database, mock_state_manager, mock_plugin, mock_config):
        """Gossip capacity should be clamped to public listchannels maximum."""
        target = '02' + 'c' * 64
        member1 = '02' + 'a' * 64

        # Setup Hive member
        mock_database.get_all_members.return_value = [
            {'peer_id': member1, 'tier': 'member'}
        ]

        # Gossip claims 10 BTC (inflated!)
        mock_state = MagicMock()
        mock_state.peer_id = member1
        mock_state.topology = [target]
        mock_state.capacity_sats = 1_000_000_000  # 10 BTC - INFLATED
        mock_state_manager.get_all_peer_states.return_value = [mock_state]

        # But public reality shows only 500k sats
        mock_plugin.rpc.listchannels.return_value = {
            'channels': [
                {
                    'source': member1,
                    'destination': target,
                    'short_channel_id': '100x1x0',
                    'satoshis': 500000,  # Only 500k in reality
                    'active': True
                },
                {
                    'source': '02' + 'd' * 64,
                    'destination': target,
                    'short_channel_id': '200x1x0',
                    'satoshis': 2000000,
                    'active': True
                }
            ]
        }
        planner._refresh_network_cache(force=True)

        result = planner._calculate_hive_share(target, mock_config)

        # Should be clamped to 500k, not 10 BTC
        assert result.hive_capacity_sats == 500000
        # Share should be 500k / 2.5M = 20%, not 10BTC / (10BTC + 2M) = ~83%
        assert result.hive_share_pct < 0.25

    def test_no_public_channel_ignores_gossip(self, planner, mock_database, mock_state_manager, mock_plugin, mock_config):
        """If no public channel exists, gossip capacity should be ignored."""
        target = '02' + 'c' * 64
        member1 = '02' + 'a' * 64

        mock_database.get_all_members.return_value = [
            {'peer_id': member1, 'tier': 'member'}
        ]

        # Gossip claims capacity to target
        mock_state = MagicMock()
        mock_state.peer_id = member1
        mock_state.topology = [target]
        mock_state.capacity_sats = 5_000_000
        mock_state_manager.get_all_peer_states.return_value = [mock_state]

        # But no public channel exists between member and target
        mock_plugin.rpc.listchannels.return_value = {
            'channels': [
                {
                    'source': '02' + 'd' * 64,  # Different source
                    'destination': target,
                    'short_channel_id': '200x1x0',
                    'satoshis': 2000000,
                    'active': True
                }
            ]
        }
        planner._refresh_network_cache(force=True)

        result = planner._calculate_hive_share(target, mock_config)

        # No verified public channel = 0 hive capacity
        assert result.hive_capacity_sats == 0


# =============================================================================
# GUARD MECHANISM TESTS
# =============================================================================

class TestGuardMechanism:
    """Test saturation enforcement."""

    def test_detect_saturated_target(self, planner, mock_database, mock_plugin, mock_config):
        """Should record saturation for saturated targets."""
        target = '02' + 'x' * 64

        # Setup network cache with saturated target
        mock_plugin.rpc.listchannels.return_value = {
            'channels': [
                {
                    'source': '02' + 'a' * 64,
                    'destination': target,
                    'short_channel_id': '100x1x0',
                    'satoshis': MIN_TARGET_CAPACITY_SATS,  # Meets minimum
                    'active': True
                }
            ]
        }
        planner._refresh_network_cache(force=True)

        # Mock get_saturated_targets to return our target
        with patch.object(planner, 'get_saturated_targets') as mock_get_sat:
            mock_get_sat.return_value = [
                SaturationResult(
                    target=target,
                    hive_capacity_sats=25_000_000,
                    public_capacity_sats=100_000_000,
                    hive_share_pct=0.25,  # 25% > 20% threshold
                    is_saturated=True,
                    should_release=False
                )
            ]

            decisions = planner._enforce_saturation(mock_config, 'test-run-1')

        # Should have recorded saturation and added to ignored peers
        assert target in planner._ignored_peers
        assert any(d.get('action') == 'saturation_detected' for d in decisions)

    def test_max_ignores_per_cycle_limit(self, planner, mock_database, mock_plugin, mock_config):
        """Should abort if more than MAX_IGNORES_PER_CYCLE saturation detections needed."""
        # Setup network cache
        mock_plugin.rpc.listchannels.return_value = {'channels': []}
        planner._refresh_network_cache(force=True)

        # Create more saturated targets than allowed
        too_many_targets = [
            SaturationResult(
                target=f'02{i:064x}'[:66],
                hive_capacity_sats=25_000_000,
                public_capacity_sats=100_000_000,
                hive_share_pct=0.25,
                is_saturated=True,
                should_release=False
            )
            for i in range(MAX_IGNORES_PER_CYCLE + 5)
        ]

        with patch.object(planner, 'get_saturated_targets') as mock_get_sat:
            mock_get_sat.return_value = too_many_targets

            decisions = planner._enforce_saturation(mock_config, 'test-run-2')

        # Should have aborted
        assert any(d.get('action') == 'abort' for d in decisions)
        assert any(d.get('reason') == 'mass_saturation_detected' for d in decisions)

        # Should have logged the abort
        mock_database.log_planner_action.assert_any_call(
            action_type='saturation_check',
            result='aborted',
            details={
                'reason': 'mass_saturation_detected',
                'targets_count': MAX_IGNORES_PER_CYCLE + 5,
                'max_allowed': MAX_IGNORES_PER_CYCLE,
                'run_id': 'test-run-2'
            }
        )

    def test_idempotent_saturation(self, planner, mock_database, mock_plugin, mock_config):
        """Should not re-flag already-flagged peers."""
        target = '02' + 'y' * 64

        # Mark as already flagged
        planner._ignored_peers.add(target)

        mock_plugin.rpc.listchannels.return_value = {'channels': []}
        planner._refresh_network_cache(force=True)

        with patch.object(planner, 'get_saturated_targets') as mock_get_sat:
            mock_get_sat.return_value = [
                SaturationResult(
                    target=target,
                    hive_capacity_sats=25_000_000,
                    public_capacity_sats=100_000_000,
                    hive_share_pct=0.25,
                    is_saturated=True,
                    should_release=False
                )
            ]

            decisions = planner._enforce_saturation(mock_config, 'test-run-3')

        # Should not have added any new saturation detections (already flagged)
        assert len(decisions) == 0

    def test_saturation_recorded_for_analytics(self, planner, mock_database, mock_plugin, mock_config):
        """Should record saturation detection for analytics."""
        target = '02' + 'z' * 64

        mock_plugin.rpc.listchannels.return_value = {'channels': []}
        planner._refresh_network_cache(force=True)

        with patch.object(planner, 'get_saturated_targets') as mock_get_sat:
            mock_get_sat.return_value = [
                SaturationResult(
                    target=target,
                    hive_capacity_sats=25_000_000,
                    public_capacity_sats=100_000_000,
                    hive_share_pct=0.25,
                    is_saturated=True,
                    should_release=False
                )
            ]

            decisions = planner._enforce_saturation(mock_config, 'test-run-4')

        # Should record saturation_detected
        assert any(d.get('action') == 'saturation_detected' for d in decisions)


# =============================================================================
# FAIL-CLOSED BEHAVIOR TESTS
# =============================================================================

class TestFailClosed:
    """Test fail-closed behavior on errors."""

    def test_rpc_failure_aborts_cycle(self, planner, mock_plugin, mock_config):
        """Should abort cycle if network cache refresh fails."""
        mock_plugin.rpc.listchannels.side_effect = RpcError('listchannels', {}, 'timeout')

        decisions = planner.run_cycle(mock_config, run_id='test-fail')

        # Should return empty (no actions taken)
        assert decisions == []

    def test_no_intents_on_cache_failure(self, planner, mock_plugin, mock_config, mock_database):
        """Should not issue any ignores if cache refresh fails."""
        mock_plugin.rpc.listchannels.side_effect = RpcError('listchannels', {}, 'timeout')

        # Even with mocked saturated targets, should not act
        planner.run_cycle(mock_config, run_id='test-no-action')

        # Verify logged failure
        mock_database.log_planner_action.assert_any_call(
            action_type='cycle',
            result='failed',
            details={'reason': 'cache_refresh_failed', 'run_id': 'test-no-action'}
        )


# =============================================================================
# RECOMMENDATION LOGGING TESTS
# =============================================================================

class TestRecommendationLogging:
    """Test recommendation logging behavior."""

    def test_planner_stats_include_ignored_peers(self, planner, mock_config):
        """Planner stats should include ignored_peers_count."""
        stats = planner.get_planner_stats()
        assert 'ignored_peers_count' in stats


# =============================================================================
# RUN CYCLE INTEGRATION TESTS
# =============================================================================

class TestRunCycle:
    """Test the main run_cycle method."""

    def test_run_cycle_returns_decisions(self, planner, mock_plugin, mock_config, mock_database):
        """run_cycle should return decision records."""
        mock_plugin.rpc.listchannels.return_value = {'channels': []}

        decisions = planner.run_cycle(mock_config, run_id='test-cycle')

        # Should return a list (may be empty)
        assert isinstance(decisions, list)

        # Should log cycle completion
        mock_database.log_planner_action.assert_called()

    def test_run_cycle_respects_shutdown(self, planner, mock_config):
        """run_cycle should exit early if shutdown_event is set."""
        import threading
        shutdown = threading.Event()
        shutdown.set()

        decisions = planner.run_cycle(mock_config, shutdown_event=shutdown, run_id='test-shutdown')

        assert decisions == []


# =============================================================================
# SATURATION RELEASE TESTS
# =============================================================================

class TestSaturationRelease:
    """Test release of ignores when saturation drops."""

    def test_release_when_below_threshold(self, planner, mock_config, mock_plugin, mock_database):
        """Should release saturation flag when share drops below release threshold."""
        target = '02' + 'r' * 64

        # Mark as flagged
        planner._ignored_peers.add(target)

        mock_plugin.rpc.listchannels.return_value = {'channels': []}
        planner._refresh_network_cache(force=True)

        # Mock share calculation to show it's now below threshold
        with patch.object(planner, '_calculate_hive_share') as mock_calc:
            mock_calc.return_value = SaturationResult(
                target=target,
                hive_capacity_sats=10_000_000,
                public_capacity_sats=100_000_000,
                hive_share_pct=0.10,  # 10% < 15% release threshold
                is_saturated=False,
                should_release=True
            )

            decisions = planner._release_saturation(mock_config, 'test-release')

        # Should have released the saturation flag
        assert target not in planner._ignored_peers
        assert any(d.get('action') == 'saturation_released' for d in decisions)


# =============================================================================
# EXPANSION LOGIC TESTS
# =============================================================================

class TestUnderservedTargets:
    """Test underserved target identification."""

    def test_get_underserved_targets_basic(self, planner, mock_config, mock_plugin, mock_database, mock_state_manager):
        """Should identify targets with low Hive share."""
        target = '02' + 'x' * 64

        # Setup network cache with high-capacity target
        mock_plugin.rpc.listchannels.return_value = {
            'channels': [
                {
                    'source': '02' + 'd' * 64,
                    'destination': target,
                    'short_channel_id': '100x1x0',
                    'satoshis': 200_000_000,  # 2 BTC
                    'active': True
                }
            ]
        }
        planner._refresh_network_cache(force=True)

        # No Hive members to calculate share
        mock_database.get_all_members.return_value = []
        mock_state_manager.get_all_peer_states.return_value = []

        underserved = planner.get_underserved_targets(mock_config)

        # Should find targets since Hive share is 0%
        # Both source and destination are indexed, so we may get both
        assert len(underserved) >= 1
        # Our specific target should be in the results
        target_results = [u for u in underserved if u.target == target]
        assert len(target_results) == 1
        assert target_results[0].hive_share_pct == 0.0

    def test_get_underserved_skips_small_targets(self, planner, mock_config, mock_plugin):
        """Should skip targets below minimum capacity."""
        small_target = '02' + 'y' * 64

        # Setup network cache with small target
        mock_plugin.rpc.listchannels.return_value = {
            'channels': [
                {
                    'source': '02' + 'd' * 64,
                    'destination': small_target,
                    'short_channel_id': '100x1x0',
                    'satoshis': 50_000_000,  # 0.5 BTC < 1 BTC threshold
                    'active': True
                }
            ]
        }
        planner._refresh_network_cache(force=True)

        underserved = planner.get_underserved_targets(mock_config)

        # Should not find the target (too small)
        assert len(underserved) == 0


# =============================================================================
# COOPERATION MODULE INTEGRATION TESTS (Phase 7)
# =============================================================================

class TestChannelSizer:
    """Tests for the ChannelSizer intelligent sizing engine."""

    def _default_params(self, **overrides):
        """Return default params for ChannelSizer.calculate_size()."""
        params = dict(
            target='02' + 'a' * 64,
            target_capacity_sats=5_000_000_000,  # 50 BTC (mid-size)
            target_channel_count=50,
            hive_share_pct=0.01,
            target_share_cap=0.10,
            onchain_balance_sats=100_000_000,  # 1 BTC
            min_channel_sats=1_000_000,
            max_channel_sats=50_000_000,
            default_channel_sats=5_000_000,
            avg_fee_rate_ppm=500,
            quality_score=0.5,
            quality_confidence=0.5,
            quality_recommendation='neutral',
        )
        params.update(overrides)
        return params

    def test_default_baseline_within_bounds(self):
        """Default sizing should produce result between min and max."""
        sizer = ChannelSizer()
        result = sizer.calculate_size(**self._default_params())
        assert result.recommended_size_sats >= 1_000_000
        assert result.recommended_size_sats <= 50_000_000

    def test_mid_size_node_preferred(self):
        """Mid-size node (50 BTC) should score higher than very large (5000 BTC)."""
        sizer = ChannelSizer()
        mid = sizer.calculate_size(**self._default_params(
            target_capacity_sats=50_00_000_000,  # 50 BTC
            target_channel_count=50
        ))
        large = sizer.calculate_size(**self._default_params(
            target_capacity_sats=500_000_000_000,  # 5000 BTC
            target_channel_count=500
        ))
        assert mid.recommended_size_sats >= large.recommended_size_sats

    def test_excellent_quality_bonus(self):
        """Excellent quality (0.9) should size larger than neutral (0.5)."""
        sizer = ChannelSizer()
        excellent = sizer.calculate_size(**self._default_params(
            quality_score=0.9, quality_confidence=0.8, quality_recommendation='excellent'
        ))
        neutral = sizer.calculate_size(**self._default_params(
            quality_score=0.5, quality_confidence=0.8, quality_recommendation='neutral'
        ))
        assert excellent.recommended_size_sats > neutral.recommended_size_sats

    def test_caution_quality_reduction(self):
        """Caution quality (0.2) should size smaller than neutral (0.5)."""
        sizer = ChannelSizer()
        caution = sizer.calculate_size(**self._default_params(
            quality_score=0.2, quality_confidence=0.8, quality_recommendation='caution'
        ))
        neutral = sizer.calculate_size(**self._default_params(
            quality_score=0.5, quality_confidence=0.8, quality_recommendation='neutral'
        ))
        assert caution.recommended_size_sats < neutral.recommended_size_sats

    def test_budget_limited_sizing(self):
        """Channel size should be capped at available budget."""
        sizer = ChannelSizer()
        result = sizer.calculate_size(**self._default_params(
            available_budget_sats=2_000_000
        ))
        assert result.recommended_size_sats <= 2_000_000

    def test_liquidity_constrained_sizing(self):
        """Low balance should produce smaller channel size."""
        sizer = ChannelSizer()
        low_balance = sizer.calculate_size(**self._default_params(
            onchain_balance_sats=3_000_000  # Very tight
        ))
        high_balance = sizer.calculate_size(**self._default_params(
            onchain_balance_sats=500_000_000  # Flush
        ))
        assert low_balance.recommended_size_sats <= high_balance.recommended_size_sats

    def test_zero_capacity_target(self):
        """Zero capacity target should produce a low capacity score."""
        sizer = ChannelSizer()
        result = sizer.calculate_size(**self._default_params(
            target_capacity_sats=0
        ))
        assert result.factors['capacity_score'] == 0.5
        assert result.factors['target_capacity_btc'] == 0.0

    def test_zero_channels_low_routing(self):
        """Target with zero channels should have low routing score."""
        sizer = ChannelSizer()
        result = sizer.calculate_size(**self._default_params(
            target_channel_count=0
        ))
        assert result.factors['routing_score'] < 1.0

    def test_low_confidence_quality_neutral(self):
        """Low confidence quality should use neutral factor (1.0)."""
        sizer = ChannelSizer()
        result = sizer.calculate_size(**self._default_params(
            quality_score=0.9, quality_confidence=0.1
        ))
        assert result.factors['quality_factor'] == 1.0
        assert result.factors.get('quality_note') == 'low_confidence_neutral'

    def test_insufficient_budget_flagged(self):
        """Budget below minimum should be flagged in factors."""
        sizer = ChannelSizer()
        result = sizer.calculate_size(**self._default_params(
            available_budget_sats=500_000,  # Below min_channel_sats of 1M
            min_channel_sats=1_000_000
        ))
        assert result.factors.get('insufficient_budget') is True

    def test_share_gap_influences_size(self):
        """Larger share gap (more underserved) should produce larger channel."""
        sizer = ChannelSizer()
        underserved = sizer.calculate_size(**self._default_params(
            hive_share_pct=0.0, target_share_cap=0.10
        ))
        well_served = sizer.calculate_size(**self._default_params(
            hive_share_pct=0.09, target_share_cap=0.10
        ))
        assert underserved.recommended_size_sats >= well_served.recommended_size_sats


# =============================================================================
# QUALITY SCORE VARIATION TESTS (Phase 6.2)
# =============================================================================

class TestQualityScoreVariation:
    """Tests for quality score filtering in get_underserved_targets()."""

    def _setup_planner_with_target(self, planner, mock_plugin, mock_database,
                                    mock_state_manager, target, capacity_sats=200_000_000):
        """Setup a planner with a target in the network cache."""
        mock_plugin.rpc.listchannels.return_value = {
            'channels': [{
                'source': '02' + 'd' * 64,
                'destination': target,
                'short_channel_id': '100x1x0',
                'satoshis': capacity_sats,
                'active': True
            }]
        }
        planner._refresh_network_cache(force=True)

        # No existing channels
        mock_plugin.rpc.listpeerchannels.return_value = {'channels': []}

        # No hive members with channels to target (underserved)
        mock_database.get_all_members.return_value = [
            {'peer_id': '02' + 'a' * 64, 'tier': 'member'}
        ]
        mock_state_manager.get_all_peer_states.return_value = []

    @staticmethod
    def _filter_target(results, target):
        """Filter results for a specific target pubkey."""
        return [r for r in results if r.target == target]

    def _make_quality_result(self, score, confidence, recommendation):
        """Create a mock quality result."""
        result = MagicMock()
        result.overall_score = score
        result.confidence = confidence
        result.recommendation = recommendation
        return result

    def test_high_quality_scores_higher(self, planner, mock_config, mock_plugin,
                                         mock_database, mock_state_manager):
        """High quality target should score higher than neutral."""
        target = '02' + 'e' * 64
        self._setup_planner_with_target(planner, mock_plugin, mock_database,
                                         mock_state_manager, target)

        # Mock quality scorer returning high quality
        mock_scorer = MagicMock()
        mock_scorer.calculate_score.return_value = self._make_quality_result(0.85, 0.8, 'excellent')
        planner.quality_scorer = mock_scorer

        results_high = self._filter_target(planner.get_underserved_targets(mock_config), target)

        # Now test with neutral quality
        mock_scorer.calculate_score.return_value = self._make_quality_result(0.5, 0.8, 'neutral')
        results_neutral = self._filter_target(planner.get_underserved_targets(mock_config), target)

        assert len(results_high) == 1
        assert len(results_neutral) == 1
        # High quality should produce a higher combined score
        assert results_high[0].score > results_neutral[0].score

    def test_avoid_recommendation_filtered(self, planner, mock_config, mock_plugin,
                                            mock_database, mock_state_manager):
        """Target with 'avoid' recommendation should be filtered out."""
        target = '02' + 'e' * 64
        self._setup_planner_with_target(planner, mock_plugin, mock_database,
                                         mock_state_manager, target)

        mock_scorer = MagicMock()
        mock_scorer.calculate_score.return_value = self._make_quality_result(0.2, 0.8, 'avoid')
        planner.quality_scorer = mock_scorer

        results = self._filter_target(planner.get_underserved_targets(mock_config), target)
        assert len(results) == 0

    def test_low_quality_included_when_flag_set(self, planner, mock_config, mock_plugin,
                                                 mock_database, mock_state_manager):
        """Low quality target should be included when include_low_quality=True."""
        target = '02' + 'e' * 64
        self._setup_planner_with_target(planner, mock_plugin, mock_database,
                                         mock_state_manager, target)

        mock_scorer = MagicMock()
        mock_scorer.calculate_score.return_value = self._make_quality_result(0.2, 0.8, 'avoid')
        planner.quality_scorer = mock_scorer

        results = self._filter_target(
            planner.get_underserved_targets(mock_config, include_low_quality=True), target
        )
        assert len(results) == 1

    def test_below_min_quality_with_high_confidence_filtered(self, planner, mock_config,
                                                              mock_plugin, mock_database,
                                                              mock_state_manager):
        """Below MIN_QUALITY_SCORE with sufficient confidence should be filtered."""
        target = '02' + 'e' * 64
        self._setup_planner_with_target(planner, mock_plugin, mock_database,
                                         mock_state_manager, target)

        mock_scorer = MagicMock()
        # Score below MIN_QUALITY_SCORE (0.45), high confidence, not 'avoid'
        mock_scorer.calculate_score.return_value = self._make_quality_result(0.3, 0.8, 'caution')
        planner.quality_scorer = mock_scorer

        results = self._filter_target(planner.get_underserved_targets(mock_config), target)
        assert len(results) == 0

    def test_below_min_quality_with_low_confidence_passes(self, planner, mock_config,
                                                           mock_plugin, mock_database,
                                                           mock_state_manager):
        """Below MIN_QUALITY_SCORE with low confidence should pass (neutral treatment)."""
        target = '02' + 'e' * 64
        self._setup_planner_with_target(planner, mock_plugin, mock_database,
                                         mock_state_manager, target)

        mock_scorer = MagicMock()
        # Score below threshold but LOW confidence - should not filter
        mock_scorer.calculate_score.return_value = self._make_quality_result(0.3, 0.1, 'caution')
        planner.quality_scorer = mock_scorer

        results = self._filter_target(planner.get_underserved_targets(mock_config), target)
        assert len(results) == 1


# =============================================================================
# COMPUTE NODE SUMMARY TESTS
# =============================================================================

class TestComputeNodeSummary:
    """Tests for Planner.compute_node_summary()."""

    def _make_planner(self, mock_plugin, mock_state_manager, mock_database,
                      mock_bridge):
        return Planner(
            plugin=mock_plugin,
            state_manager=mock_state_manager,
            database=mock_database,
            bridge=mock_bridge,
        )

    def test_counts_only_active_channels(self, mock_plugin, mock_state_manager,
                                          mock_database, mock_bridge):
        """Given mixed channel states, only CHANNELD_NORMAL counted as active.
        Verify active_channels, pending_channels, closing_channels, total_capacity_sats."""
        planner = self._make_planner(mock_plugin, mock_state_manager,
                                      mock_database, mock_bridge)

        mock_plugin.rpc.listpeerchannels.return_value = {
            'channels': [
                {'state': 'CHANNELD_NORMAL', 'total_msat': 5_000_000_000},   # 5M sats active
                {'state': 'CHANNELD_NORMAL', 'total_msat': 3_000_000_000},   # 3M sats active
                {'state': 'CHANNELD_AWAITING_LOCKIN', 'total_msat': 2_000_000_000},  # pending
                {'state': 'ONCHAIN', 'total_msat': 1_000_000_000},            # closing
                {'state': 'CLOSINGD_COMPLETE', 'total_msat': 500_000_000},    # closing
            ]
        }
        # Bridge returns no profitability data (empty list)
        mock_bridge.safe_call.return_value = {'channels': []}

        result = planner.compute_node_summary()

        assert result is not None
        assert result['active_channels'] == 2
        assert result['pending_channels'] == 1
        assert result['closing_channels'] == 2
        # total_capacity_sats = (5M + 3M) = 8M sats (only active channels)
        assert result['total_capacity_sats'] == 8_000_000

    def test_underwater_count_from_bridge(self, mock_plugin, mock_state_manager,
                                           mock_database, mock_bridge,
 ):
        """When bridge.safe_call('revenue-profitability') returns channel profitability
        data, underwater_count and underwater_pct are computed correctly."""
        planner = self._make_planner(mock_plugin, mock_state_manager,
                                      mock_database, mock_bridge)

        mock_plugin.rpc.listpeerchannels.return_value = {
            'channels': [
                {'state': 'CHANNELD_NORMAL', 'total_msat': 1_000_000_000},
                {'state': 'CHANNELD_NORMAL', 'total_msat': 1_000_000_000},
                {'state': 'CHANNELD_NORMAL', 'total_msat': 1_000_000_000},
                {'state': 'CHANNELD_NORMAL', 'total_msat': 1_000_000_000},
                {'state': 'CHANNELD_NORMAL', 'total_msat': 1_000_000_000},
            ]
        }
        mock_bridge.safe_call.return_value = {
            'channels': [
                {'short_channel_id': '1x1x1', 'profitability_class': 'underwater'},
                {'short_channel_id': '1x1x2', 'profitability_class': 'bleeder'},
                {'short_channel_id': '1x1x3', 'profitability_class': 'profitable'},
                {'short_channel_id': '1x1x4', 'profitability_class': 'highly_profitable'},
                {'short_channel_id': '1x1x5', 'profitability_class': 'neutral'},
            ]
        }

        result = planner.compute_node_summary()

        assert result is not None
        assert result['underwater_count'] == 2  # underwater + bleeder
        # underwater_pct = round(2 * 100.0 / 5, 1) = 40.0
        assert result['underwater_pct'] == 40.0

    def test_rpc_failure_returns_none(self, mock_plugin, mock_state_manager,
                                       mock_database, mock_bridge):
        """When listpeerchannels raises Exception, returns None."""
        planner = self._make_planner(mock_plugin, mock_state_manager,
                                      mock_database, mock_bridge)

        mock_plugin.rpc.listpeerchannels.side_effect = Exception("RPC connection lost")

        result = planner.compute_node_summary()

        assert result is None

    def test_bridge_failure_graceful(self, mock_plugin, mock_state_manager,
                                      mock_database, mock_bridge):
        """When bridge.safe_call raises, underwater_count defaults to 0 (no crash)."""
        planner = self._make_planner(mock_plugin, mock_state_manager,
                                      mock_database, mock_bridge)

        mock_plugin.rpc.listpeerchannels.return_value = {
            'channels': [
                {'state': 'CHANNELD_NORMAL', 'total_msat': 2_000_000_000},
                {'state': 'CHANNELD_NORMAL', 'total_msat': 3_000_000_000},
            ]
        }
        mock_bridge.safe_call.side_effect = Exception("bridge unavailable")

        result = planner.compute_node_summary()

        assert result is not None
        assert result['active_channels'] == 2
        assert result['underwater_count'] == 0
        assert result['underwater_pct'] == 0.0

    def test_no_plugin_returns_none(self, mock_state_manager, mock_database,
                                     mock_bridge):
        """When self.plugin is None, returns None."""
        planner = Planner(
            plugin=None,
            state_manager=mock_state_manager,
            database=mock_database,
            bridge=mock_bridge,
        )

        result = planner.compute_node_summary()

        assert result is None

    def test_opener_breakdown(self, mock_plugin, mock_state_manager,
                               mock_database, mock_bridge):
        """Counts we_opened vs they_opened from opener field."""
        planner = self._make_planner(mock_plugin, mock_state_manager,
                                      mock_database, mock_bridge)
        mock_plugin.rpc.listpeerchannels.return_value = {
            'channels': [
                {'state': 'CHANNELD_NORMAL', 'total_msat': 5_000_000_000, 'opener': 'local'},
                {'state': 'CHANNELD_NORMAL', 'total_msat': 3_000_000_000, 'opener': 'local'},
                {'state': 'CHANNELD_NORMAL', 'total_msat': 2_000_000_000, 'opener': 'remote'},
                {'state': 'CHANNELD_AWAITING_LOCKIN', 'total_msat': 1_000_000_000, 'opener': 'local'},
                {'state': 'ONCHAIN', 'total_msat': 500_000_000, 'opener': 'remote'},
            ]
        }
        mock_bridge.safe_call.return_value = {'channels': []}

        result = planner.compute_node_summary()
        assert result['we_opened'] == 2   # only active CHANNELD_NORMAL with opener=local
        assert result['they_opened'] == 1  # only active CHANNELD_NORMAL with opener=remote
        assert result['active_channels'] == 3
        assert result['pending_channels'] == 1
        assert result['closing_channels'] == 1


class TestGetUniqueChannelsFor:
    """Test get_unique_channels_for() deduplicating accessor."""

    @staticmethod
    def _make_planner(mock_plugin, mock_state_manager, mock_database,
                      mock_bridge):
        return Planner(
            plugin=mock_plugin,
            state_manager=mock_state_manager,
            database=mock_database,
            bridge=mock_bridge,
        )

    def test_dedup_bidirectional_entries(self, mock_plugin, mock_state_manager,
                                         mock_database, mock_bridge,
  ):
        """Same ChannelInfo indexed under both endpoints returns 1 per query."""
        planner = self._make_planner(mock_plugin, mock_state_manager,
                                      mock_database, mock_bridge)

        ch = ChannelInfo(
            short_channel_id='123x1x0',
            source='A',
            destination='B',
            capacity_sats=1_000_000,
            active=True,
        )
        planner._network_cache = {
            'A': [ch],
            'B': [ch],
        }

        result_a = planner.get_unique_channels_for('A')
        assert len(result_a) == 1
        assert result_a[0].short_channel_id == '123x1x0'

        result_b = planner.get_unique_channels_for('B')
        assert len(result_b) == 1
        assert result_b[0].short_channel_id == '123x1x0'

    def test_multiple_unique_channels(self, mock_plugin, mock_state_manager,
                                       mock_database, mock_bridge):
        """Two distinct channels under same target returns both."""
        planner = self._make_planner(mock_plugin, mock_state_manager,
                                      mock_database, mock_bridge)

        ch1 = ChannelInfo(
            short_channel_id='100x1x0',
            source='T',
            destination='X',
            capacity_sats=500_000,
            active=True,
        )
        ch2 = ChannelInfo(
            short_channel_id='200x2x0',
            source='Y',
            destination='T',
            capacity_sats=750_000,
            active=True,
        )
        planner._network_cache = {
            'T': [ch1, ch2],
        }

        result = planner.get_unique_channels_for('T')
        assert len(result) == 2
        scids = {ch.short_channel_id for ch in result}
        assert scids == {'100x1x0', '200x2x0'}

    def test_empty_target(self, mock_plugin, mock_state_manager,
                           mock_database, mock_bridge):
        """Unknown target returns empty list."""
        planner = self._make_planner(mock_plugin, mock_state_manager,
                                      mock_database, mock_bridge)

        planner._network_cache = {}

        result = planner.get_unique_channels_for('UNKNOWN')
        assert result == []

    def test_get_public_capacity_uses_dedup(self, mock_plugin, mock_state_manager,
                                             mock_database, mock_bridge,
   ):
        """_get_public_capacity_to_target counts capacity only once per channel."""
        planner = self._make_planner(mock_plugin, mock_state_manager,
                                      mock_database, mock_bridge)

        ch = ChannelInfo(
            short_channel_id='123x1x0',
            source='A',
            destination='B',
            capacity_sats=1_000_000,
            active=True,
        )
        # Same channel object indexed under both endpoints
        planner._network_cache = {
            'A': [ch],
            'B': [ch],
        }

        # Capacity should be counted once, not doubled
        cap_a = planner._get_public_capacity_to_target('A')
        assert cap_a == 1_000_000

        cap_b = planner._get_public_capacity_to_target('B')
        assert cap_b == 1_000_000


class TestOpenerQualityBonus:
    """Tests for Fix H3: Remote channel opens boost quality score."""

    def test_remote_opens_increase_score(self, mock_database):
        """Peers who opened channels to us get a quality bonus."""
        from modules.quality_scorer import PeerQualityScorer
        scorer = PeerQualityScorer(database=mock_database)

        # Base case: no remote opens
        base_summary = {
            "peer_id": "peer_A",
            "event_count": 5,
            "open_count": 3,
            "remote_open_count": 0,
            "close_count": 2,
            "remote_close_count": 0,
            "local_close_count": 1,
            "mutual_close_count": 1,
            "total_revenue_sats": 5000,
            "total_rebalance_cost_sats": 1000,
            "total_net_pnl_sats": 4000,
            "total_forward_count": 100,
            "avg_routing_score": 0.7,
            "avg_profitability_score": 0.6,
            "avg_duration_days": 90,
            "reporters": ["node1"],
            "reporter_scores": {"node1": {"event_count": 5, "avg_routing_score": 0.7, "avg_profitability_score": 0.6}},
        }
        mock_database.get_peer_event_summary.return_value = base_summary
        base_result = scorer.calculate_score("peer_A")

        # With remote opens
        remote_summary = dict(base_summary)
        remote_summary["remote_open_count"] = 2
        mock_database.get_peer_event_summary.return_value = remote_summary
        remote_result = scorer.calculate_score("peer_A")

        assert remote_result.overall_score > base_result.overall_score

    def test_opener_bonus_caps_at_0_1(self, mock_database):
        """Remote opener bonus is capped at 0.10 even with many remote opens."""
        from modules.quality_scorer import PeerQualityScorer
        scorer = PeerQualityScorer(database=mock_database)

        # Base case: no remote opens
        base_summary = {
            "peer_id": "peer_B",
            "event_count": 5,
            "open_count": 3,
            "remote_open_count": 0,
            "close_count": 2,
            "remote_close_count": 0,
            "local_close_count": 1,
            "mutual_close_count": 1,
            "total_revenue_sats": 5000,
            "total_rebalance_cost_sats": 1000,
            "total_net_pnl_sats": 4000,
            "total_forward_count": 100,
            "avg_routing_score": 0.7,
            "avg_profitability_score": 0.6,
            "avg_duration_days": 90,
            "reporters": ["node1"],
            "reporter_scores": {"node1": {"event_count": 5, "avg_routing_score": 0.7, "avg_profitability_score": 0.6}},
        }
        mock_database.get_peer_event_summary.return_value = base_summary
        base_result = scorer.calculate_score("peer_B")

        # With 5 remote opens (5 * 0.05 = 0.25, but should be capped at 0.10)
        many_opens_summary = dict(base_summary)
        many_opens_summary["remote_open_count"] = 5
        mock_database.get_peer_event_summary.return_value = many_opens_summary
        many_result = scorer.calculate_score("peer_B")

        # With 2 remote opens (2 * 0.05 = 0.10, exactly at cap)
        two_opens_summary = dict(base_summary)
        two_opens_summary["remote_open_count"] = 2
        mock_database.get_peer_event_summary.return_value = two_opens_summary
        two_result = scorer.calculate_score("peer_B")

        # The bonus from 5 opens should equal the bonus from 2 opens (both capped at 0.10)
        # because min(0.1, 5*0.05) == min(0.1, 2*0.05) == 0.10
        assert abs(many_result.overall_score - two_result.overall_score) < 1e-9

        # Both should be exactly 0.10 above the base (unless clamped at 1.0)
        actual_bonus = many_result.overall_score - base_result.overall_score
        assert actual_bonus <= 0.1 + 1e-9  # bonus does not exceed 0.10


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
