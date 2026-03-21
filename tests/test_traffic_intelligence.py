"""
Test Suite for Traffic Intelligence.

Tests fleet-shared traffic profiles, temporal conflict detection,
and fleet demand forecasting.
"""

import pytest
import time
import json
from unittest.mock import Mock, MagicMock, patch

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Mock pyln.client before importing modules
class MockRpcError(Exception):
    pass

mock_pyln = MagicMock()
mock_pyln.Plugin = MagicMock
mock_pyln.RpcError = MockRpcError
sys.modules['pyln'] = mock_pyln
sys.modules['pyln.client'] = mock_pyln

from modules.protocol import (
    HiveMessageType,
    validate_traffic_intelligence_batch,
    get_traffic_intelligence_batch_signing_payload,
    create_traffic_intelligence_batch,
    serialize,
    deserialize,
)


class TestTrafficIntelligenceProtocol:
    """Test protocol functions for TRAFFIC_INTELLIGENCE_BATCH."""

    def test_message_type_exists(self):
        """TRAFFIC_INTELLIGENCE_BATCH enum value is 32905."""
        assert HiveMessageType.TRAFFIC_INTELLIGENCE_BATCH == 32905

    def test_signing_payload_deterministic(self):
        """Signing payload is deterministic for same input."""
        payload = {
            "reporter_id": "abc123",
            "timestamp": 1000000,
            "signature": "sig",
            "profiles": [
                {"peer_id": "peer_a", "profile_type": "retail", "confidence": 0.9},
                {"peer_id": "peer_b", "profile_type": "wholesale", "confidence": 0.8},
            ],
        }
        sig1 = get_traffic_intelligence_batch_signing_payload(payload)
        sig2 = get_traffic_intelligence_batch_signing_payload(payload)
        assert sig1 == sig2
        assert "TRAFFIC_INTELLIGENCE_BATCH:" in sig1
        assert "abc123" in sig1

    def test_signing_payload_order_independent(self):
        """Signing payload is the same regardless of profiles order."""
        p1 = {"peer_id": "peer_a", "profile_type": "retail", "confidence": 0.9}
        p2 = {"peer_id": "peer_b", "profile_type": "wholesale", "confidence": 0.8}
        base = {"reporter_id": "abc", "timestamp": 1000, "signature": "s"}
        sig_ab = get_traffic_intelligence_batch_signing_payload({**base, "profiles": [p1, p2]})
        sig_ba = get_traffic_intelligence_batch_signing_payload({**base, "profiles": [p2, p1]})
        assert sig_ab == sig_ba

    def test_validate_valid_payload(self):
        """Valid payload passes validation."""
        payload = {
            "reporter_id": "a" * 66,
            "timestamp": int(time.time()),
            "signature": "validbase64sig",
            "profiles": [
                {
                    "peer_id": "b" * 66,
                    "profile_type": "retail",
                    "peak_hours_utc": [9, 10, 11],
                    "quiet_hours_utc": [1, 2, 3],
                    "avg_forward_size_sats": 50000.0,
                    "daily_volume_sats": 5000000.0,
                    "drain_direction": "outbound_heavy",
                    "confidence": 0.85,
                    "observation_window_hours": 168,
                },
            ],
        }
        assert validate_traffic_intelligence_batch(payload) is True

    def test_validate_rejects_missing_reporter(self):
        """Missing reporter_id fails validation."""
        payload = {
            "timestamp": int(time.time()),
            "signature": "sig",
            "profiles": [],
        }
        assert validate_traffic_intelligence_batch(payload) is False

    def test_validate_rejects_stale_timestamp(self):
        """Timestamp older than 48h fails validation."""
        payload = {
            "reporter_id": "a" * 66,
            "timestamp": int(time.time()) - (49 * 3600),
            "signature": "validbase64sig",
            "profiles": [],
        }
        assert validate_traffic_intelligence_batch(payload) is False

    def test_validate_rejects_bad_profile_type(self):
        """Invalid profile_type fails validation."""
        payload = {
            "reporter_id": "a" * 66,
            "timestamp": int(time.time()),
            "signature": "validbase64sig",
            "profiles": [{
                "peer_id": "b" * 66,
                "profile_type": "INVALID",
                "peak_hours_utc": [],
                "quiet_hours_utc": [],
                "avg_forward_size_sats": 100.0,
                "daily_volume_sats": 1000.0,
                "drain_direction": "balanced",
                "confidence": 0.5,
                "observation_window_hours": 24,
            }],
        }
        assert validate_traffic_intelligence_batch(payload) is False

    def test_validate_rejects_too_many_profiles(self):
        """More than 200 profiles fails validation."""
        payload = {
            "reporter_id": "a" * 66,
            "timestamp": int(time.time()),
            "signature": "validbase64sig",
            "profiles": [{"peer_id": f"peer_{i}", "profile_type": "retail",
                          "peak_hours_utc": [], "quiet_hours_utc": [],
                          "avg_forward_size_sats": 100.0, "daily_volume_sats": 1000.0,
                          "drain_direction": "balanced", "confidence": 0.5,
                          "observation_window_hours": 24} for i in range(201)],
        }
        assert validate_traffic_intelligence_batch(payload) is False

    def test_create_and_deserialize_roundtrip(self):
        """create + deserialize roundtrip preserves data."""
        profiles = [
            {"peer_id": "peer_a", "profile_type": "retail", "confidence": 0.9,
             "peak_hours_utc": [9, 10], "quiet_hours_utc": [1, 2],
             "avg_forward_size_sats": 50000.0, "daily_volume_sats": 5000000.0,
             "drain_direction": "outbound_heavy", "observation_window_hours": 168},
        ]
        msg_bytes = create_traffic_intelligence_batch(
            reporter_id="reporter_abc",
            timestamp=1000000,
            signature="test_sig",
            profiles=profiles,
        )
        assert msg_bytes is not None
        msg_type, payload = deserialize(msg_bytes)
        assert msg_type == HiveMessageType.TRAFFIC_INTELLIGENCE_BATCH
        assert payload["reporter_id"] == "reporter_abc"
        assert len(payload["profiles"]) == 1
        assert payload["profiles"][0]["profile_type"] == "retail"
from modules import protocol_handlers


class TestTrafficIntelligenceHandler:
    """Test protocol handler for TRAFFIC_INTELLIGENCE_BATCH."""

    def test_handler_exists(self):
        """handle_traffic_intelligence_batch function exists."""
        assert hasattr(protocol_handlers, 'handle_traffic_intelligence_batch')

    def test_handler_returns_continue_when_no_manager(self):
        """Handler returns continue when traffic_intel_mgr is None."""
        # Save and clear the global
        original = getattr(protocol_handlers, 'traffic_intel_mgr', None)
        protocol_handlers.traffic_intel_mgr = None
        try:
            result = protocol_handlers.handle_traffic_intelligence_batch(
                "peer_id", {}, Mock()
            )
            assert result == {"result": "continue"}
        finally:
            protocol_handlers.traffic_intel_mgr = original
class TestSizeAwareFeeEnrichment:
    """Phase 3c: Size-aware fee adjustment based on fleet traffic intelligence."""

    def test_large_forwards_get_discount(self):
        """Peers with large average forwards get a fee discount (0.9x)."""
        from modules.fee_coordination import FeeCoordinationManager
        mock_db = MagicMock()
        mock_plugin = MagicMock()
        mgr = FeeCoordinationManager(mock_db, mock_plugin)
        mock_traffic_intel = MagicMock()
        mock_traffic_intel.get_aggregated_profile.return_value = {
            "avg_forward_size_sats": 600000,
            "daily_volume_sats": 5000000,
            "confidence": 0.8,
        }
        mgr.traffic_intel_mgr = mock_traffic_intel
        multiplier = mgr.get_size_aware_adjustment("02" + "a" * 64)
        assert 0.85 <= multiplier <= 0.95

    def test_small_forwards_get_premium(self):
        """Peers with small average forwards get a fee premium (1.1x)."""
        from modules.fee_coordination import FeeCoordinationManager
        mock_db = MagicMock()
        mock_plugin = MagicMock()
        mgr = FeeCoordinationManager(mock_db, mock_plugin)
        mock_traffic_intel = MagicMock()
        mock_traffic_intel.get_aggregated_profile.return_value = {
            "avg_forward_size_sats": 5000,
            "daily_volume_sats": 2000000,
            "confidence": 0.8,
        }
        mgr.traffic_intel_mgr = mock_traffic_intel
        multiplier = mgr.get_size_aware_adjustment("02" + "a" * 64)
        assert 1.05 <= multiplier <= 1.15

    def test_high_volume_gets_floor_boost(self):
        """High-volume peers get +0.05 floor boost."""
        from modules.fee_coordination import FeeCoordinationManager
        mock_db = MagicMock()
        mock_plugin = MagicMock()
        mgr = FeeCoordinationManager(mock_db, mock_plugin)
        mock_traffic_intel = MagicMock()
        mock_traffic_intel.get_aggregated_profile.return_value = {
            "avg_forward_size_sats": 100000,
            "daily_volume_sats": 15000000,
            "confidence": 0.8,
        }
        mgr.traffic_intel_mgr = mock_traffic_intel
        multiplier = mgr.get_size_aware_adjustment("02" + "a" * 64)
        assert multiplier >= 1.04

    def test_no_traffic_data_returns_neutral(self):
        """No traffic data returns neutral 1.0 multiplier."""
        from modules.fee_coordination import FeeCoordinationManager
        mock_db = MagicMock()
        mock_plugin = MagicMock()
        mgr = FeeCoordinationManager(mock_db, mock_plugin)
        mock_traffic_intel = MagicMock()
        mock_traffic_intel.get_aggregated_profile.return_value = None
        mgr.traffic_intel_mgr = mock_traffic_intel
        multiplier = mgr.get_size_aware_adjustment("02" + "a" * 64)
        assert multiplier == 1.0

    def test_no_traffic_intel_mgr_returns_neutral(self):
        """No traffic_intel_mgr returns neutral 1.0 multiplier."""
        from modules.fee_coordination import FeeCoordinationManager
        mock_db = MagicMock()
        mock_plugin = MagicMock()
        mgr = FeeCoordinationManager(mock_db, mock_plugin)
        mgr.traffic_intel_mgr = None
        multiplier = mgr.get_size_aware_adjustment("02" + "a" * 64)
        assert multiplier == 1.0

    def test_multiplier_bounded(self):
        """Multiplier is always bounded to [0.8, 1.3]."""
        from modules.fee_coordination import FeeCoordinationManager
        mock_db = MagicMock()
        mock_plugin = MagicMock()
        mgr = FeeCoordinationManager(mock_db, mock_plugin)
        mock_traffic_intel = MagicMock()
        mock_traffic_intel.get_aggregated_profile.return_value = {
            "avg_forward_size_sats": 1,
            "daily_volume_sats": 100000000,
            "confidence": 1.0,
        }
        mgr.traffic_intel_mgr = mock_traffic_intel
        multiplier = mgr.get_size_aware_adjustment("02" + "a" * 64)
        assert 0.8 <= multiplier <= 1.3

    def test_low_confidence_returns_neutral(self):
        """Low confidence (<0.3) returns neutral 1.0 multiplier."""
        from modules.fee_coordination import FeeCoordinationManager
        mock_db = MagicMock()
        mock_plugin = MagicMock()
        mgr = FeeCoordinationManager(mock_db, mock_plugin)
        mock_traffic_intel = MagicMock()
        mock_traffic_intel.get_aggregated_profile.return_value = {
            "avg_forward_size_sats": 600000,
            "daily_volume_sats": 5000000,
            "confidence": 0.2,
        }
        mgr.traffic_intel_mgr = mock_traffic_intel
        multiplier = mgr.get_size_aware_adjustment("02" + "a" * 64)
        assert multiplier == 1.0

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
