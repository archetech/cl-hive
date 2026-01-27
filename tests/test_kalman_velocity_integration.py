"""
Tests for Kalman velocity integration between cl-revenue-ops and cl-hive.

Tests the KalmanVelocityReport dataclass and the integration methods in
AnticipatoryLiquidityManager.
"""
import math
import pytest
import time


class TestKalmanVelocityReport:
    """Tests for KalmanVelocityReport dataclass."""

    def test_default_initialization(self):
        """Test basic initialization."""
        from modules.anticipatory_liquidity import KalmanVelocityReport

        report = KalmanVelocityReport(
            channel_id="123x1x0",
            peer_id="02abc123",
            reporter_id="03def456",
            velocity_pct_per_hour=-0.02,
            uncertainty=0.005,
            flow_ratio=-0.3,
            confidence=0.85,
            is_regime_change=False
        )

        assert report.channel_id == "123x1x0"
        assert report.velocity_pct_per_hour == -0.02
        assert report.uncertainty == 0.005
        assert report.confidence == 0.85
        assert report.timestamp > 0

    def test_is_stale_fresh_report(self):
        """Test that fresh reports are not stale."""
        from modules.anticipatory_liquidity import KalmanVelocityReport

        report = KalmanVelocityReport(
            channel_id="123x1x0",
            peer_id="02abc123",
            reporter_id="03def456",
            velocity_pct_per_hour=0.01,
            uncertainty=0.003,
            flow_ratio=0.2,
            confidence=0.9,
            is_regime_change=False,
            timestamp=int(time.time())
        )

        assert not report.is_stale(ttl_seconds=3600)

    def test_is_stale_old_report(self):
        """Test that old reports are stale."""
        from modules.anticipatory_liquidity import KalmanVelocityReport

        old_timestamp = int(time.time()) - 7200  # 2 hours ago

        report = KalmanVelocityReport(
            channel_id="123x1x0",
            peer_id="02abc123",
            reporter_id="03def456",
            velocity_pct_per_hour=0.01,
            uncertainty=0.003,
            flow_ratio=0.2,
            confidence=0.9,
            is_regime_change=False,
            timestamp=old_timestamp
        )

        assert report.is_stale(ttl_seconds=3600)

    def test_to_dict(self):
        """Test serialization to dict."""
        from modules.anticipatory_liquidity import KalmanVelocityReport

        report = KalmanVelocityReport(
            channel_id="123x1x0",
            peer_id="02abc123def456",
            reporter_id="03def456789abc",
            velocity_pct_per_hour=-0.015,
            uncertainty=0.004,
            flow_ratio=-0.25,
            confidence=0.88,
            is_regime_change=True
        )

        d = report.to_dict()

        assert d["channel_id"] == "123x1x0"
        assert d["velocity_pct_per_hour"] == pytest.approx(-0.015, abs=0.0001)
        assert d["uncertainty"] == pytest.approx(0.004, abs=0.0001)
        assert d["flow_ratio"] == pytest.approx(-0.25, abs=0.01)
        assert d["confidence"] == pytest.approx(0.88, abs=0.01)
        assert d["is_regime_change"] is True
        assert "age_seconds" in d


class TestKalmanVelocityReceive:
    """Tests for receiving Kalman velocity reports."""

    def test_receive_kalman_velocity_basic(self, mock_manager):
        """Test receiving a basic Kalman velocity report."""
        success = mock_manager.receive_kalman_velocity(
            reporter_id="03reporter123",
            channel_id="123x1x0",
            peer_id="02peer456",
            velocity_pct_per_hour=-0.02,
            uncertainty=0.005,
            flow_ratio=-0.3,
            confidence=0.85,
            is_regime_change=False
        )

        assert success

        # Verify stored
        reports = mock_manager._kalman_velocities.get("123x1x0", [])
        assert len(reports) == 1
        assert reports[0].velocity_pct_per_hour == -0.02

    def test_receive_kalman_velocity_updates_existing(self, mock_manager):
        """Test that reports from same reporter update existing."""
        # First report
        mock_manager.receive_kalman_velocity(
            reporter_id="03reporter123",
            channel_id="123x1x0",
            peer_id="02peer456",
            velocity_pct_per_hour=-0.02,
            uncertainty=0.005,
            flow_ratio=-0.3,
            confidence=0.85,
            is_regime_change=False
        )

        # Second report from same reporter
        mock_manager.receive_kalman_velocity(
            reporter_id="03reporter123",
            channel_id="123x1x0",
            peer_id="02peer456",
            velocity_pct_per_hour=-0.03,
            uncertainty=0.004,
            flow_ratio=-0.35,
            confidence=0.90,
            is_regime_change=False
        )

        # Should still be only 1 report (updated)
        reports = mock_manager._kalman_velocities.get("123x1x0", [])
        assert len(reports) == 1
        assert reports[0].velocity_pct_per_hour == -0.03

    def test_receive_multiple_reporters(self, mock_manager):
        """Test receiving reports from multiple reporters."""
        mock_manager.receive_kalman_velocity(
            reporter_id="03reporter1",
            channel_id="123x1x0",
            peer_id="02peer456",
            velocity_pct_per_hour=-0.02,
            uncertainty=0.005,
            flow_ratio=-0.3,
            confidence=0.85,
            is_regime_change=False
        )

        mock_manager.receive_kalman_velocity(
            reporter_id="03reporter2",
            channel_id="123x1x0",
            peer_id="02peer456",
            velocity_pct_per_hour=-0.018,
            uncertainty=0.004,
            flow_ratio=-0.28,
            confidence=0.90,
            is_regime_change=False
        )

        reports = mock_manager._kalman_velocities.get("123x1x0", [])
        assert len(reports) == 2

    def test_receive_validates_confidence(self, mock_manager):
        """Test that invalid confidence values are clamped."""
        success = mock_manager.receive_kalman_velocity(
            reporter_id="03reporter123",
            channel_id="123x1x0",
            peer_id="02peer456",
            velocity_pct_per_hour=-0.02,
            uncertainty=0.005,
            flow_ratio=-0.3,
            confidence=1.5,  # Invalid - should be clamped to 1.0
            is_regime_change=False
        )

        assert success
        reports = mock_manager._kalman_velocities.get("123x1x0", [])
        assert reports[0].confidence == 1.0


class TestKalmanConsensusVelocity:
    """Tests for consensus velocity calculation."""

    def test_consensus_single_reporter(self, mock_manager):
        """Test consensus with single reporter."""
        mock_manager.receive_kalman_velocity(
            reporter_id="03reporter1",
            channel_id="123x1x0",
            peer_id="02peer456",
            velocity_pct_per_hour=-0.02,
            uncertainty=0.005,
            flow_ratio=-0.3,
            confidence=0.85,
            is_regime_change=False
        )

        velocity = mock_manager._get_kalman_consensus_velocity("123x1x0")
        assert velocity == pytest.approx(-0.02, abs=0.001)

    def test_consensus_multiple_reporters(self, mock_manager):
        """Test consensus averages multiple reporters."""
        mock_manager.receive_kalman_velocity(
            reporter_id="03reporter1",
            channel_id="123x1x0",
            peer_id="02peer456",
            velocity_pct_per_hour=-0.02,
            uncertainty=0.005,
            flow_ratio=-0.3,
            confidence=0.9,
            is_regime_change=False
        )

        mock_manager.receive_kalman_velocity(
            reporter_id="03reporter2",
            channel_id="123x1x0",
            peer_id="02peer456",
            velocity_pct_per_hour=-0.01,
            uncertainty=0.005,
            flow_ratio=-0.2,
            confidence=0.9,
            is_regime_change=False
        )

        velocity = mock_manager._get_kalman_consensus_velocity("123x1x0")
        # Should be somewhere between -0.02 and -0.01
        assert -0.02 < velocity < -0.01

    def test_consensus_weights_by_uncertainty(self, mock_manager):
        """Test that lower uncertainty reports have higher weight."""
        # Low uncertainty reporter
        mock_manager.receive_kalman_velocity(
            reporter_id="03reporter1",
            channel_id="123x1x0",
            peer_id="02peer456",
            velocity_pct_per_hour=-0.02,
            uncertainty=0.001,  # Very low uncertainty
            flow_ratio=-0.3,
            confidence=0.9,
            is_regime_change=False
        )

        # High uncertainty reporter
        mock_manager.receive_kalman_velocity(
            reporter_id="03reporter2",
            channel_id="123x1x0",
            peer_id="02peer456",
            velocity_pct_per_hour=-0.01,
            uncertainty=0.1,  # High uncertainty
            flow_ratio=-0.2,
            confidence=0.9,
            is_regime_change=False
        )

        velocity = mock_manager._get_kalman_consensus_velocity("123x1x0")
        # Should be closer to -0.02 (low uncertainty reporter)
        assert velocity < -0.015

    def test_consensus_ignores_low_confidence(self, mock_manager):
        """Test that low confidence reports are ignored."""
        mock_manager.receive_kalman_velocity(
            reporter_id="03reporter1",
            channel_id="123x1x0",
            peer_id="02peer456",
            velocity_pct_per_hour=-0.02,
            uncertainty=0.005,
            flow_ratio=-0.3,
            confidence=0.1,  # Below threshold
            is_regime_change=False
        )

        velocity = mock_manager._get_kalman_consensus_velocity("123x1x0")
        # Should be None since confidence is below threshold
        assert velocity is None

    def test_consensus_returns_none_for_no_data(self, mock_manager):
        """Test consensus returns None when no data."""
        velocity = mock_manager._get_kalman_consensus_velocity("nonexistent")
        assert velocity is None


class TestKalmanVelocityQuery:
    """Tests for querying Kalman velocity data."""

    def test_query_returns_aggregated_data(self, mock_manager):
        """Test that query returns properly aggregated data."""
        mock_manager.receive_kalman_velocity(
            reporter_id="03reporter1",
            channel_id="123x1x0",
            peer_id="02peer456",
            velocity_pct_per_hour=-0.02,
            uncertainty=0.005,
            flow_ratio=-0.3,
            confidence=0.85,
            is_regime_change=False
        )

        result = mock_manager.query_kalman_velocity("123x1x0")

        assert result is not None
        assert result["status"] == "ok"
        assert result["channel_id"] == "123x1x0"
        assert "velocity_pct_per_hour" in result
        assert "uncertainty" in result
        assert "reporters" in result
        assert result["reporters"] >= 1

    def test_query_returns_none_for_no_data(self, mock_manager):
        """Test query returns None when no data."""
        result = mock_manager.query_kalman_velocity("nonexistent")
        assert result is None


class TestCalculateVelocityWithKalman:
    """Tests for _calculate_velocity with Kalman integration."""

    def test_calculate_velocity_uses_kalman_when_available(self, mock_manager):
        """Test that _calculate_velocity prefers Kalman data."""
        # Add Kalman data
        mock_manager.receive_kalman_velocity(
            reporter_id="03reporter1",
            channel_id="123x1x0",
            peer_id="02peer456",
            velocity_pct_per_hour=-0.02,
            uncertainty=0.005,
            flow_ratio=-0.3,
            confidence=0.85,
            is_regime_change=False
        )

        velocity = mock_manager._calculate_velocity("123x1x0", 10_000_000)

        # Should use Kalman velocity
        assert velocity == pytest.approx(-0.02, abs=0.005)

    def test_calculate_velocity_falls_back_to_simple(self, mock_manager):
        """Test fallback to simple calculation when no Kalman data."""
        # No Kalman data added
        velocity = mock_manager._calculate_velocity("123x1x0", 10_000_000)

        # Should return 0 (no samples)
        assert velocity == 0.0


class TestKalmanStatusAndCleanup:
    """Tests for status and cleanup methods."""

    def test_get_kalman_velocity_status(self, mock_manager):
        """Test status returns proper statistics."""
        mock_manager.receive_kalman_velocity(
            reporter_id="03reporter1",
            channel_id="123x1x0",
            peer_id="02peer456",
            velocity_pct_per_hour=-0.02,
            uncertainty=0.005,
            flow_ratio=-0.3,
            confidence=0.85,
            is_regime_change=False
        )

        status = mock_manager.get_kalman_velocity_status()

        assert status["kalman_integration_active"] is True
        assert status["total_reports"] >= 1
        assert status["channels_with_data"] >= 1

    def test_cleanup_stale_kalman_data(self, mock_manager):
        """Test cleanup removes stale data."""
        # Add old report
        from modules.anticipatory_liquidity import KalmanVelocityReport

        old_report = KalmanVelocityReport(
            channel_id="123x1x0",
            peer_id="02peer456",
            reporter_id="03reporter1",
            velocity_pct_per_hour=-0.02,
            uncertainty=0.005,
            flow_ratio=-0.3,
            confidence=0.85,
            is_regime_change=False,
            timestamp=int(time.time()) - 7200  # 2 hours old
        )

        mock_manager._kalman_velocities["123x1x0"].append(old_report)

        cleaned = mock_manager.cleanup_stale_kalman_data()

        assert cleaned == 1
        assert "123x1x0" not in mock_manager._kalman_velocities


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_manager():
    """Create a mock AnticipatoryLiquidityManager."""
    from modules.anticipatory_liquidity import AnticipatoryLiquidityManager

    class MockDatabase:
        def record_flow_sample(self, **kwargs):
            pass

        def get_flow_samples(self, **kwargs):
            return []

    manager = AnticipatoryLiquidityManager(
        database=MockDatabase(),
        plugin=None,
        state_manager=None,
        our_id="03test123"
    )

    return manager
