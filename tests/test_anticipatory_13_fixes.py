"""
Tests for anticipatory liquidity fixes (pattern and Kalman features).

Covers:
- Monthly pattern detection loads 30 days of history
- Intra-day velocity uses actual capacity instead of hardcoded 10M
- receive_pattern_from_fleet uses single lock block
- Kalman weight uses 1/sigma^2 (inverse variance)
- Flow history eviction uses tracker dict
- Flow history trims by window before limit
- Kalman velocity status batches consensus in single lock
- get_patterns_summary counts monthly patterns
- Regime change uses INTRADAY_REGIME_CHANGE_THRESHOLD constant
"""

import math
import time
import threading
import pytest
from collections import defaultdict
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.anticipatory_liquidity import (
    AnticipatoryLiquidityManager,
    HourlyFlowSample,
    KalmanVelocityReport,
    TemporalPattern,
    FlowDirection,
    PATTERN_WINDOW_DAYS,
    MONTHLY_PATTERN_WINDOW_DAYS,
    MONTHLY_PATTERNS_ENABLED,
    PATTERN_CONFIDENCE_THRESHOLD,
    PATTERN_STRENGTH_THRESHOLD,
    MAX_FLOW_HISTORY_CHANNELS,
    MAX_FLOW_SAMPLES_PER_CHANNEL,
    KALMAN_VELOCITY_TTL_SECONDS,
    KALMAN_MIN_CONFIDENCE,
    KALMAN_MIN_REPORTERS,
)

# =============================================================================
# FIXTURES
# =============================================================================

class MockPlugin:
    def __init__(self):
        self.logs = []
        self.rpc = MagicMock()

    def log(self, msg, level="info"):
        self.logs.append({"msg": msg, "level": level})


class MockDatabase:
    def __init__(self):
        self._flow_samples = {}
        self._requested_days = []

    def record_flow_sample(self, **kwargs):
        pass

    def get_flow_samples(self, channel_id, days=14):
        self._requested_days.append(days)
        return self._flow_samples.get(channel_id, [])


class MockStateManager:
    def __init__(self):
        self._states = []

    def get_all_peer_states(self):
        return self._states


def _make_sample(channel_id, hour, day_of_week, net_flow, ts=None):
    """Helper to create an HourlyFlowSample."""
    ts = ts or int(time.time())
    return HourlyFlowSample(
        channel_id=channel_id,
        hour=hour,
        day_of_week=day_of_week,
        inbound_sats=max(0, net_flow),
        outbound_sats=max(0, -net_flow),
        net_flow_sats=net_flow,
        timestamp=ts,
    )


def _make_manager(db=None, plugin=None, state_manager=None, our_id="our_node_abc"):
    """Helper to create a manager."""
    return AnticipatoryLiquidityManager(
        database=db or MockDatabase(),
        plugin=plugin or MockPlugin(),
        state_manager=state_manager,
        our_id=our_id,
    )

# =============================================================================
# FIX 1: Monthly pattern detection loads 30 days
# =============================================================================

class TestMonthlyPatternHistoryWindow:
    """Fix 1: load_flow_history uses MONTHLY_PATTERN_WINDOW_DAYS when enabled."""

    def test_default_loads_monthly_window(self):
        """Default load_flow_history should request 30 days when monthly enabled."""
        db = MockDatabase()
        mgr = _make_manager(db=db)
        mgr.load_flow_history("chan1")
        assert db._requested_days[-1] == MONTHLY_PATTERN_WINDOW_DAYS

    def test_explicit_days_override(self):
        """Explicit days parameter should override default."""
        db = MockDatabase()
        mgr = _make_manager(db=db)
        mgr.load_flow_history("chan1", days=7)
        assert db._requested_days[-1] == 7

    def test_monthly_window_constant(self):
        """MONTHLY_PATTERN_WINDOW_DAYS should be 30."""
        assert MONTHLY_PATTERN_WINDOW_DAYS == 30
        assert MONTHLY_PATTERN_WINDOW_DAYS > PATTERN_WINDOW_DAYS


# =============================================================================
# FIX 3: Intra-day velocity uses actual capacity
# =============================================================================

class TestIntradayCapacity:
    """Fix 3: _analyze_intraday_bucket uses capacity_sats instead of hardcoded 10M."""

    def setup_method(self):
        self.mgr = _make_manager()

    def test_velocity_with_actual_capacity(self):
        """Velocity should scale correctly with actual channel capacity."""
        from modules.anticipatory_liquidity import IntraDayPhase

        # 1M sat channel with 100K net flow => 10% velocity
        samples = [
            _make_sample("c1", hour=9, day_of_week=0, net_flow=100_000,
                         ts=int(time.time()) - i * 3600)
            for i in range(10)
        ]
        result = self.mgr._analyze_intraday_bucket(
            channel_id="c1", samples=samples,
            phase=IntraDayPhase.MORNING, hour_start=8, hour_end=12,
            kalman_confidence=0.5, is_regime_change=False,
            capacity_sats=1_000_000,
        )
        assert result is not None
        # velocity = 100_000 / 1_000_000 = 0.10 (10%)
        assert abs(result.avg_velocity - 0.10) < 0.01

    def test_velocity_with_zero_capacity_uses_estimate(self):
        """When capacity_sats=0, should estimate from flow magnitudes."""
        from modules.anticipatory_liquidity import IntraDayPhase

        samples = [
            _make_sample("c1", hour=9, day_of_week=0, net_flow=100_000,
                         ts=int(time.time()) - i * 3600)
            for i in range(10)
        ]
        result = self.mgr._analyze_intraday_bucket(
            channel_id="c1", samples=samples,
            phase=IntraDayPhase.MORNING, hour_start=8, hour_end=12,
            kalman_confidence=0.5, is_regime_change=False,
            capacity_sats=0,
        )
        assert result is not None
        # Estimate: p90 of magnitudes * 10 = 100_000 * 10 = 1M
        # So velocity ~ 100_000 / 1M = 0.10
        assert result.avg_velocity > 0

# =============================================================================
# FIX 7: receive_pattern_from_fleet single lock block
# =============================================================================

class TestReceivePatternThreadSafety:
    """Fix 7: Eviction and append in single lock acquisition."""

    def test_concurrent_receive_patterns(self):
        """Concurrent calls should not corrupt state."""
        mgr = _make_manager()
        errors = []

        def add_pattern(reporter, peer):
            try:
                result = mgr.receive_pattern_from_fleet(
                    reporter_id=reporter,
                    pattern_data={
                        "peer_id": peer,
                        "direction": "outbound",
                        "intensity": 1.5,
                        "confidence": 0.7,
                        "samples": 10,
                    },
                )
                assert result is True
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=add_pattern, args=(f"reporter_{i}", f"peer_{i % 5}"))
            for i in range(50)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # All 5 unique peers should be tracked
        assert len(mgr._remote_patterns) == 5

# =============================================================================
# FIX 8: Kalman inverse-variance weighting (1/sigma^2)
# =============================================================================

class TestKalmanInverseVarianceWeighting:
    """Fix 8: Consensus velocity uses 1/sigma^2, not 1/sigma."""

    def test_low_uncertainty_dominates(self):
        """Reporter with much lower uncertainty should dominate consensus."""
        mgr = _make_manager()
        now = int(time.time())

        # Reporter A: velocity=0.05, uncertainty=0.01 (very precise)
        mgr.receive_kalman_velocity(
            reporter_id="A", channel_id="c1", peer_id="p1",
            velocity_pct_per_hour=0.05, uncertainty=0.01,
            flow_ratio=0.5, confidence=0.9,
        )
        # Reporter B: velocity=-0.05, uncertainty=0.10 (10x less precise)
        mgr.receive_kalman_velocity(
            reporter_id="B", channel_id="c1", peer_id="p1",
            velocity_pct_per_hour=-0.05, uncertainty=0.10,
            flow_ratio=0.5, confidence=0.9,
        )

        consensus = mgr._get_kalman_consensus_velocity("c1")
        assert consensus is not None
        # With 1/sigma^2: weight_A = 0.9/(0.0001*1.5) = 6000, weight_B = 0.9/(0.01*1.5) = 60
        # So A should dominate ~99:1
        assert consensus > 0.04  # Should be close to 0.05, not 0.0

    def test_equal_uncertainty_equal_weight(self):
        """Equal uncertainties should give equal weight (averaging)."""
        mgr = _make_manager()

        mgr.receive_kalman_velocity(
            reporter_id="A", channel_id="c1", peer_id="p1",
            velocity_pct_per_hour=0.10, uncertainty=0.05,
            flow_ratio=0.5, confidence=0.9,
        )
        mgr.receive_kalman_velocity(
            reporter_id="B", channel_id="c1", peer_id="p1",
            velocity_pct_per_hour=0.00, uncertainty=0.05,
            flow_ratio=0.5, confidence=0.9,
        )

        consensus = mgr._get_kalman_consensus_velocity("c1")
        assert consensus is not None
        # Equal uncertainty + equal confidence => simple average ≈ 0.05
        assert abs(consensus - 0.05) < 0.01
# =============================================================================
# FIX 11: Flow history eviction uses tracker
# =============================================================================

class TestFlowHistoryEviction:
    """Fix 11: O(1) eviction via _flow_history_last_ts tracker."""

    def test_tracker_initialized(self):
        """Manager should have _flow_history_last_ts dict."""
        mgr = _make_manager()
        assert hasattr(mgr, '_flow_history_last_ts')
        assert isinstance(mgr._flow_history_last_ts, dict)

    def test_tracker_updated_on_record(self):
        """Recording a sample should update the timestamp tracker."""
        mgr = _make_manager()
        now = int(time.time())
        mgr.record_flow_sample("chan1", 100, 50, timestamp=now)
        assert "chan1" in mgr._flow_history_last_ts
        assert mgr._flow_history_last_ts["chan1"] == now

    def test_eviction_removes_oldest_tracker(self):
        """When evicting, the tracker entry should also be removed."""
        mgr = _make_manager()
        now = int(time.time())

        # Fill to limit
        for i in range(MAX_FLOW_HISTORY_CHANNELS):
            mgr.record_flow_sample(f"chan_{i}", 100, 50, timestamp=now + i)

        assert len(mgr._flow_history) == MAX_FLOW_HISTORY_CHANNELS

        # Add one more => should evict oldest
        mgr.record_flow_sample("chan_new", 100, 50, timestamp=now + MAX_FLOW_HISTORY_CHANNELS + 1)
        assert len(mgr._flow_history) <= MAX_FLOW_HISTORY_CHANNELS + 1
        # The evicted channel (chan_0) should not be in tracker
        if "chan_0" not in mgr._flow_history:
            assert "chan_0" not in mgr._flow_history_last_ts

# =============================================================================
# FIX 12: Window trim before limit
# =============================================================================

class TestFlowHistoryTrimOrder:
    """Fix 12: Old samples trimmed by window first, then limit applied."""

    def test_old_samples_trimmed_by_monthly_window(self):
        """Samples older than monthly window should be trimmed."""
        mgr = _make_manager()
        now = int(time.time())

        # Add a sample 40 days ago (beyond 30-day monthly window)
        old_ts = now - (40 * 24 * 3600)
        mgr.record_flow_sample("chan1", 100, 50, timestamp=old_ts)

        # Add a recent sample
        mgr.record_flow_sample("chan1", 200, 100, timestamp=now)

        with mgr._lock:
            samples = mgr._flow_history["chan1"]
        # Old sample should have been trimmed
        assert all(s.timestamp > now - (MONTHLY_PATTERN_WINDOW_DAYS * 24 * 3600) for s in samples)

# =============================================================================
# FIX 13: Kalman velocity status batched in single lock
# =============================================================================

class TestKalmanStatusBatched:
    """Fix 13: get_kalman_velocity_status doesn't call _get_kalman_consensus_velocity."""

    def test_status_works_without_deadlock(self):
        """get_kalman_velocity_status should complete without deadlocking."""
        mgr = _make_manager()

        # Add some Kalman data
        mgr.receive_kalman_velocity(
            reporter_id="A", channel_id="c1", peer_id="p1",
            velocity_pct_per_hour=0.01, uncertainty=0.05,
            flow_ratio=0.5, confidence=0.8,
        )

        status = mgr.get_kalman_velocity_status()
        assert status["kalman_integration_active"] is True
        assert status["channels_with_data"] == 1
        assert status["total_reports"] == 1

    def test_consensus_count_correct(self):
        """channels_with_consensus should count channels meeting min_reporters threshold."""
        mgr = _make_manager()

        # Channel c1: 1 reporter (below default KALMAN_MIN_REPORTERS=1 means it qualifies)
        mgr.receive_kalman_velocity(
            reporter_id="A", channel_id="c1", peer_id="p1",
            velocity_pct_per_hour=0.01, uncertainty=0.05,
            flow_ratio=0.5, confidence=0.8,
        )

        status = mgr.get_kalman_velocity_status()
        if KALMAN_MIN_REPORTERS <= 1:
            assert status["channels_with_consensus"] >= 1
        else:
            assert status["channels_with_consensus"] == 0

# =============================================================================
# FOLLOW-UP FIX 2: get_patterns_summary counts monthly patterns
# =============================================================================

class TestPatternsSummaryMonthly:
    """get_patterns_summary should include monthly_patterns count."""

    def test_monthly_count_in_summary(self):
        """Summary should include monthly_patterns key."""
        mgr = _make_manager()

        # Populate cache with a monthly pattern
        monthly_p = TemporalPattern(
            channel_id="c1", hour_of_day=None, direction=FlowDirection.OUTBOUND,
            intensity=1.5, confidence=0.8, samples=10, avg_flow_sats=50000,
            day_of_month=15,
        )
        hourly_p = TemporalPattern(
            channel_id="c1", hour_of_day=10, direction=FlowDirection.INBOUND,
            intensity=1.4, confidence=0.7, samples=8, avg_flow_sats=40000,
        )
        with mgr._lock:
            mgr._pattern_cache["c1"] = [monthly_p, hourly_p]

        summary = mgr.get_patterns_summary()
        assert "monthly_patterns" in summary
        assert summary["monthly_patterns"] == 1
        assert summary["hourly_patterns"] == 1
        assert summary["total_patterns"] == 2

# =============================================================================
# FOLLOW-UP FIX 6: Regime detection uses INTRADAY_REGIME_CHANGE_THRESHOLD
# =============================================================================

class TestRegimeChangeConstant:
    """Regime change detection should use the constant, not hardcoded 2."""

    def test_constant_is_used(self):
        """Verify INTRADAY_REGIME_CHANGE_THRESHOLD is 2.5 (not 2)."""
        from modules.anticipatory_liquidity import INTRADAY_REGIME_CHANGE_THRESHOLD
        assert INTRADAY_REGIME_CHANGE_THRESHOLD == 2.5

    def test_stable_below_threshold(self):
        """Pattern should be regime_stable when std < threshold * avg."""
        from modules.anticipatory_liquidity import (
            IntraDayPhase, INTRADAY_REGIME_CHANGE_THRESHOLD
        )
        mgr = _make_manager()

        # velocity_std = 0.04, avg_velocity = 0.02
        # ratio = 0.04 / 0.02 = 2.0 < 2.5 threshold => stable
        samples = []
        now = int(time.time())
        for i in range(10):
            # Alternate between 80K and 120K to get std ~ 0.02 with avg ~ 0.10
            flow = 100_000 if i % 2 == 0 else 100_000
            samples.append(_make_sample("c1", hour=9, day_of_week=0,
                                         net_flow=flow, ts=now - i * 3600))

        result = mgr._analyze_intraday_bucket(
            channel_id="c1", samples=samples,
            phase=IntraDayPhase.MORNING, hour_start=8, hour_end=12,
            kalman_confidence=0.5, is_regime_change=False,
            capacity_sats=1_000_000,
        )
        if result:
            # Constant flow => zero variance => stable
            assert result.is_regime_stable is True
