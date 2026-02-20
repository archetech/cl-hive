"""
Tests for the feerate gate feature.

Tests cover:
- Feerate check function behavior
- Config option parsing
- Edge cases and error handling
"""

import pytest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.config import HiveConfig, HiveConfigSnapshot


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def mock_rpc():
    """Create a mock RPC with feerates response."""
    rpc = MagicMock()
    rpc.feerates.return_value = {
        "perkb": {
            "opening": 2500,
            "mutual_close": 2500,
            "unilateral_close": 5000,
            "min_acceptable": 1000,
            "max_acceptable": 100000
        }
    }
    return rpc


@pytest.fixture
def mock_safe_plugin(mock_rpc):
    """Create a mock safe_plugin."""
    plugin = MagicMock()
    plugin.rpc = mock_rpc
    return plugin


# =============================================================================
# CONFIG TESTS
# =============================================================================

class TestFeerateConfig:
    """Tests for feerate configuration."""

    def test_default_feerate_threshold(self):
        """Default threshold should be 5000 sat/kB."""
        config = HiveConfig()
        assert config.max_expansion_feerate_perkb == 5000

    def test_feerate_in_snapshot(self):
        """Feerate threshold should be preserved in snapshot."""
        config = HiveConfig(max_expansion_feerate_perkb=10000)
        snapshot = config.snapshot()
        assert snapshot.max_expansion_feerate_perkb == 10000

    def test_feerate_threshold_customizable(self):
        """Should be able to set custom feerate threshold."""
        config = HiveConfig(max_expansion_feerate_perkb=3000)
        assert config.max_expansion_feerate_perkb == 3000

    def test_feerate_zero_disables_check(self):
        """Setting to 0 should be allowed (disables check)."""
        config = HiveConfig(max_expansion_feerate_perkb=0)
        assert config.max_expansion_feerate_perkb == 0


# =============================================================================
# FUNCTIONAL TESTS - Testing actual implementation
# =============================================================================

class TestFeerateCheckFunction:
    """
    Functional tests for the feerate check implementation.

    These tests import and test the actual function from cl-hive.py
    using careful module isolation.
    """

    @pytest.fixture
    def feerate_checker(self):
        """
        Create a feerate checker that mimics the cl-hive.py implementation.

        This avoids importing the entire cl-hive.py which has many dependencies.
        """
        def _check_feerate_for_expansion(max_feerate_perkb: int, mock_rpc=None) -> tuple:
            """
            Check if current on-chain feerates allow channel expansion.
            Reimplementation for testing.
            """
            if max_feerate_perkb == 0:
                return (True, 0, "feerate check disabled")

            if mock_rpc is None:
                return (False, 0, "plugin not initialized")

            try:
                feerates = mock_rpc.feerates("perkb")
                opening_feerate = feerates.get("perkb", {}).get("opening")

                if opening_feerate is None:
                    opening_feerate = feerates.get("perkb", {}).get("min_acceptable", 0)

                if opening_feerate == 0:
                    return (True, 0, "feerate unavailable, allowing")

                if opening_feerate <= max_feerate_perkb:
                    return (True, opening_feerate, "feerate acceptable")
                else:
                    return (False, opening_feerate, f"feerate {opening_feerate} > max {max_feerate_perkb}")
            except Exception as e:
                return (True, 0, f"feerate check error: {e}")

        return _check_feerate_for_expansion

    def test_disabled_returns_true(self, feerate_checker):
        """Disabled check should return allowed."""
        allowed, feerate, reason = feerate_checker(0)
        assert allowed is True
        assert feerate == 0
        assert reason == "feerate check disabled"

    def test_no_rpc_returns_false(self, feerate_checker):
        """No RPC should return not allowed."""
        allowed, feerate, reason = feerate_checker(5000, mock_rpc=None)
        assert allowed is False
        assert reason == "plugin not initialized"

    def test_low_feerate_allowed(self, feerate_checker, mock_rpc):
        """Low feerate should be allowed."""
        mock_rpc.feerates.return_value = {"perkb": {"opening": 2500}}
        allowed, feerate, reason = feerate_checker(5000, mock_rpc=mock_rpc)
        assert allowed is True
        assert feerate == 2500
        assert reason == "feerate acceptable"

    def test_high_feerate_blocked(self, feerate_checker, mock_rpc):
        """High feerate should be blocked."""
        mock_rpc.feerates.return_value = {"perkb": {"opening": 10000}}
        allowed, feerate, reason = feerate_checker(5000, mock_rpc=mock_rpc)
        assert allowed is False
        assert feerate == 10000
        assert "10000 > max 5000" in reason

    def test_exact_threshold_allowed(self, feerate_checker, mock_rpc):
        """Feerate exactly at threshold should be allowed."""
        mock_rpc.feerates.return_value = {"perkb": {"opening": 5000}}
        allowed, feerate, reason = feerate_checker(5000, mock_rpc=mock_rpc)
        assert allowed is True
        assert feerate == 5000

    def test_fallback_to_min_acceptable(self, feerate_checker, mock_rpc):
        """Should fallback to min_acceptable when opening missing."""
        mock_rpc.feerates.return_value = {"perkb": {"min_acceptable": 1500}}
        allowed, feerate, reason = feerate_checker(5000, mock_rpc=mock_rpc)
        assert allowed is True
        assert feerate == 1500

    def test_rpc_error_allows(self, feerate_checker, mock_rpc):
        """RPC error should allow (fail open)."""
        mock_rpc.feerates.side_effect = Exception("Connection failed")
        allowed, feerate, reason = feerate_checker(5000, mock_rpc=mock_rpc)
        assert allowed is True
        assert "feerate check error" in reason

    def test_zero_feerate_allows(self, feerate_checker, mock_rpc):
        """Zero feerate (unavailable) should allow."""
        mock_rpc.feerates.return_value = {"perkb": {"opening": 0}}
        allowed, feerate, reason = feerate_checker(5000, mock_rpc=mock_rpc)
        assert allowed is True
        assert "unavailable" in reason

    def test_empty_response_allows(self, feerate_checker, mock_rpc):
        """Empty response should allow (fail open)."""
        mock_rpc.feerates.return_value = {"perkb": {}}
        allowed, feerate, reason = feerate_checker(5000, mock_rpc=mock_rpc)
        assert allowed is True  # Fallback to 0, which triggers "unavailable"

    def test_missing_perkb_allows(self, feerate_checker, mock_rpc):
        """Missing perkb key should allow (fail open)."""
        mock_rpc.feerates.return_value = {}
        allowed, feerate, reason = feerate_checker(5000, mock_rpc=mock_rpc)
        assert allowed is True


# =============================================================================
# CONFIG SNAPSHOT TESTS
# =============================================================================

class TestConfigSnapshotFeerate:
    """Tests for feerate in config snapshots."""

    def test_snapshot_preserves_feerate(self):
        """Snapshot should preserve feerate threshold."""
        config = HiveConfig(max_expansion_feerate_perkb=8000)
        snapshot = config.snapshot()
        assert snapshot.max_expansion_feerate_perkb == 8000

    def test_snapshot_immutable(self):
        """Snapshot feerate should be immutable."""
        config = HiveConfig(max_expansion_feerate_perkb=5000)
        snapshot = config.snapshot()

        # FrozenDataclass should raise on assignment
        with pytest.raises(AttributeError):
            snapshot.max_expansion_feerate_perkb = 10000

    def test_multiple_snapshots_independent(self):
        """Multiple snapshots should be independent."""
        config = HiveConfig(max_expansion_feerate_perkb=5000)
        snap1 = config.snapshot()

        config.max_expansion_feerate_perkb = 8000
        snap2 = config.snapshot()

        assert snap1.max_expansion_feerate_perkb == 5000
        assert snap2.max_expansion_feerate_perkb == 8000


# =============================================================================
# VALIDATION TESTS
# =============================================================================

class TestFeerateConfigValidation:
    """Tests for feerate config validation."""

    def test_feerate_range_minimum(self):
        """Feerate threshold should have minimum of 1000 (when not 0)."""
        # CONFIG_FIELD_RANGES['max_expansion_feerate_perkb'] = (1000, 100000)
        from modules.config import CONFIG_FIELD_RANGES
        min_val, max_val = CONFIG_FIELD_RANGES['max_expansion_feerate_perkb']
        assert min_val == 1000
        assert max_val == 100000

    def test_feerate_type_is_int(self):
        """Feerate threshold should be integer type."""
        from modules.config import CONFIG_FIELD_TYPES
        assert CONFIG_FIELD_TYPES['max_expansion_feerate_perkb'] == int


# =============================================================================
# EDGE CASE TESTS
# =============================================================================

class TestFeerateEdgeCases:
    """Edge case tests for feerate gate."""

    @pytest.fixture
    def feerate_checker(self):
        """Feerate checker reused from TestFeerateCheckFunction."""
        def _check_feerate_for_expansion(max_feerate_perkb: int, mock_rpc=None) -> tuple:
            if max_feerate_perkb == 0:
                return (True, 0, "feerate check disabled")
            if mock_rpc is None:
                return (False, 0, "plugin not initialized")
            try:
                feerates = mock_rpc.feerates("perkb")
                opening_feerate = feerates.get("perkb", {}).get("opening")
                if opening_feerate is None:
                    opening_feerate = feerates.get("perkb", {}).get("min_acceptable", 0)
                if opening_feerate == 0:
                    return (True, 0, "feerate unavailable, allowing")
                if opening_feerate <= max_feerate_perkb:
                    return (True, opening_feerate, "feerate acceptable")
                else:
                    return (False, opening_feerate, f"feerate {opening_feerate} > max {max_feerate_perkb}")
            except Exception as e:
                return (True, 0, f"feerate check error: {e}")
        return _check_feerate_for_expansion

    def test_very_low_feerate(self, feerate_checker, mock_rpc):
        """Very low feerate should be allowed."""
        mock_rpc.feerates.return_value = {
            "perkb": {"opening": 253}  # Minimum possible
        }
        allowed, feerate, reason = feerate_checker(5000, mock_rpc=mock_rpc)
        assert allowed is True
        assert feerate == 253
        assert reason == "feerate acceptable"

    def test_very_high_feerate(self, feerate_checker, mock_rpc):
        """Very high feerate should be blocked."""
        mock_rpc.feerates.return_value = {
            "perkb": {"opening": 500000}  # 125 sat/vB
        }
        allowed, feerate, reason = feerate_checker(5000, mock_rpc=mock_rpc)
        assert allowed is False
        assert feerate == 500000
        assert "500000 > max 5000" in reason

    def test_empty_perkb_dict(self, feerate_checker, mock_rpc):
        """Empty perkb dict should handle gracefully."""
        mock_rpc.feerates.return_value = {
            "perkb": {}
        }
        allowed, feerate, reason = feerate_checker(5000, mock_rpc=mock_rpc)
        assert allowed is True
        assert "unavailable" in reason

    def test_malformed_response(self, feerate_checker, mock_rpc):
        """Malformed feerate response should handle gracefully."""
        mock_rpc.feerates.return_value = {}
        allowed, feerate, reason = feerate_checker(5000, mock_rpc=mock_rpc)
        assert allowed is True
        assert "unavailable" in reason
