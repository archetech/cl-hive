"""
Test Suite for Traffic Intelligence.

Tests fleet-shared traffic profiles, temporal conflict detection,
and fleet demand forecasting.
"""

import pytest
import time
import json
import threading
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

from modules.database import HiveDatabase


@pytest.fixture
def db(tmp_path):
    """Create a temporary database."""
    db_path = str(tmp_path / "test_traffic.db")
    mock_plugin = MagicMock()
    mock_plugin.log = MagicMock()
    database = HiveDatabase(db_path, mock_plugin)
    database.initialize()
    return database


class TestTrafficIntelligenceDatabase:
    """Test DB operations for fleet_traffic_intelligence table."""

    def test_save_traffic_profile(self, db):
        """save_traffic_profile stores and retrieves a profile."""
        db.save_traffic_profile(
            peer_id="peer_aaa",
            reporter_id="reporter_111",
            profile_type="retail",
            peak_hours_utc=json.dumps([9, 10, 11, 14, 15, 16]),
            quiet_hours_utc=json.dumps([1, 2, 3, 4, 5]),
            avg_forward_size_sats=50000.0,
            daily_volume_sats=5000000.0,
            drain_direction="outbound_heavy",
            confidence=0.85,
            observation_window_hours=168,
            received_at=time.time(),
            ttl_hours=168.0,
        )
        profiles = db.get_traffic_profiles_for_peer("peer_aaa")
        assert len(profiles) == 1
        assert profiles[0]["profile_type"] == "retail"
        assert profiles[0]["reporter_id"] == "reporter_111"

    def test_save_traffic_profile_upsert(self, db):
        """save_traffic_profile overwrites on same (peer_id, reporter_id)."""
        now = time.time()
        db.save_traffic_profile(
            peer_id="peer_aaa", reporter_id="reporter_111",
            profile_type="retail", peak_hours_utc="[]", quiet_hours_utc="[]",
            avg_forward_size_sats=100.0, daily_volume_sats=1000.0,
            drain_direction="balanced", confidence=0.5,
            observation_window_hours=24, received_at=now, ttl_hours=168.0,
        )
        db.save_traffic_profile(
            peer_id="peer_aaa", reporter_id="reporter_111",
            profile_type="wholesale", peak_hours_utc="[]", quiet_hours_utc="[]",
            avg_forward_size_sats=500000.0, daily_volume_sats=50000000.0,
            drain_direction="inbound_heavy", confidence=0.9,
            observation_window_hours=168, received_at=now + 1, ttl_hours=168.0,
        )
        profiles = db.get_traffic_profiles_for_peer("peer_aaa")
        assert len(profiles) == 1
        assert profiles[0]["profile_type"] == "wholesale"

    def test_get_traffic_profiles_for_peer_filters(self, db):
        """get_traffic_profiles_for_peer returns only matching peer."""
        now = time.time()
        for peer in ["peer_aaa", "peer_bbb"]:
            db.save_traffic_profile(
                peer_id=peer, reporter_id="reporter_111",
                profile_type="retail", peak_hours_utc="[]", quiet_hours_utc="[]",
                avg_forward_size_sats=100.0, daily_volume_sats=1000.0,
                drain_direction="balanced", confidence=0.5,
                observation_window_hours=24, received_at=now, ttl_hours=168.0,
            )
        assert len(db.get_traffic_profiles_for_peer("peer_aaa")) == 1
        assert len(db.get_traffic_profiles_for_peer("peer_bbb")) == 1
        assert len(db.get_traffic_profiles_for_peer("peer_ccc")) == 0

    def test_get_all_traffic_profiles(self, db):
        """get_all_traffic_profiles returns all non-expired profiles."""
        now = time.time()
        db.save_traffic_profile(
            peer_id="peer_aaa", reporter_id="reporter_111",
            profile_type="retail", peak_hours_utc="[]", quiet_hours_utc="[]",
            avg_forward_size_sats=100.0, daily_volume_sats=1000.0,
            drain_direction="balanced", confidence=0.5,
            observation_window_hours=24, received_at=now, ttl_hours=168.0,
        )
        db.save_traffic_profile(
            peer_id="peer_bbb", reporter_id="reporter_222",
            profile_type="burst", peak_hours_utc="[]", quiet_hours_utc="[]",
            avg_forward_size_sats=200.0, daily_volume_sats=2000.0,
            drain_direction="balanced", confidence=0.6,
            observation_window_hours=48, received_at=now, ttl_hours=168.0,
        )
        profiles = db.get_all_traffic_profiles()
        assert len(profiles) == 2

    def test_cleanup_expired_traffic_profiles(self, db):
        """cleanup_expired_traffic_profiles removes stale profiles."""
        old_time = time.time() - (200 * 3600)  # 200 hours ago
        db.save_traffic_profile(
            peer_id="peer_old", reporter_id="reporter_111",
            profile_type="retail", peak_hours_utc="[]", quiet_hours_utc="[]",
            avg_forward_size_sats=100.0, daily_volume_sats=1000.0,
            drain_direction="balanced", confidence=0.5,
            observation_window_hours=24, received_at=old_time, ttl_hours=168.0,
        )
        db.save_traffic_profile(
            peer_id="peer_new", reporter_id="reporter_111",
            profile_type="retail", peak_hours_utc="[]", quiet_hours_utc="[]",
            avg_forward_size_sats=100.0, daily_volume_sats=1000.0,
            drain_direction="balanced", confidence=0.5,
            observation_window_hours=24, received_at=time.time(), ttl_hours=168.0,
        )
        deleted = db.cleanup_expired_traffic_profiles()
        assert deleted == 1
        assert len(db.get_all_traffic_profiles()) == 1


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


from modules.traffic_intelligence import TrafficIntelligenceManager


@pytest.fixture
def traffic_mgr(db):
    """Create a TrafficIntelligenceManager with test database."""
    plugin = Mock()
    plugin.log = Mock()
    plugin.rpc = MagicMock()
    mgr = TrafficIntelligenceManager(
        database=db,
        plugin=plugin,
        our_pubkey="our_node_pubkey_abc123",
    )
    return mgr


class TestTrafficIntelligenceManager:
    """Test TrafficIntelligenceManager core methods."""

    def test_store_local_profile(self, traffic_mgr, db):
        """store_local_profile saves to database."""
        result = traffic_mgr.store_local_profile(
            peer_id="peer_aaa",
            profile_type="retail",
            peak_hours_utc=[9, 10, 11, 14, 15, 16],
            quiet_hours_utc=[1, 2, 3, 4, 5],
            avg_forward_size_sats=50000.0,
            daily_volume_sats=5000000.0,
            drain_direction="outbound_heavy",
            confidence=0.85,
            observation_window_hours=168,
        )
        assert result is True
        profiles = db.get_traffic_profiles_for_peer("peer_aaa")
        assert len(profiles) == 1
        assert profiles[0]["profile_type"] == "retail"
        assert profiles[0]["reporter_id"] == "our_node_pubkey_abc123"

    def test_store_local_profile_rejects_invalid_type(self, traffic_mgr):
        """store_local_profile rejects invalid profile_type."""
        result = traffic_mgr.store_local_profile(
            peer_id="peer_aaa", profile_type="INVALID",
            peak_hours_utc=[], quiet_hours_utc=[],
            avg_forward_size_sats=100.0, daily_volume_sats=1000.0,
            drain_direction="balanced", confidence=0.5,
            observation_window_hours=24,
        )
        assert result is False

    def test_get_aggregated_profile_single_reporter(self, traffic_mgr):
        """get_aggregated_profile with one reporter returns its data."""
        traffic_mgr.store_local_profile(
            peer_id="peer_aaa", profile_type="retail",
            peak_hours_utc=[9, 10, 11], quiet_hours_utc=[1, 2, 3],
            avg_forward_size_sats=50000.0, daily_volume_sats=5000000.0,
            drain_direction="outbound_heavy", confidence=0.85,
            observation_window_hours=168,
        )
        agg = traffic_mgr.get_aggregated_profile("peer_aaa")
        assert agg is not None
        assert agg["profile_type"] == "retail"
        assert agg["confidence"] == 0.85
        assert 9 in agg["peak_hours_utc"]

    def test_get_aggregated_profile_multiple_reporters(self, traffic_mgr, db):
        """get_aggregated_profile merges multiple reporters."""
        now = time.time()
        # Our report
        traffic_mgr.store_local_profile(
            peer_id="peer_aaa", profile_type="retail",
            peak_hours_utc=[9, 10, 11], quiet_hours_utc=[1, 2, 3],
            avg_forward_size_sats=50000.0, daily_volume_sats=5000000.0,
            drain_direction="outbound_heavy", confidence=0.9,
            observation_window_hours=168,
        )
        # Remote report with different peak hours
        db.save_traffic_profile(
            peer_id="peer_aaa", reporter_id="remote_node_xyz",
            profile_type="wholesale", peak_hours_utc=json.dumps([14, 15, 16]),
            quiet_hours_utc=json.dumps([4, 5, 6]),
            avg_forward_size_sats=200000.0, daily_volume_sats=20000000.0,
            drain_direction="inbound_heavy", confidence=0.7,
            observation_window_hours=168, received_at=now, ttl_hours=168.0,
        )
        agg = traffic_mgr.get_aggregated_profile("peer_aaa")
        assert agg is not None
        # Highest confidence reporter's profile_type wins
        assert agg["profile_type"] == "retail"
        # Peak hours are union of both reporters
        assert 9 in agg["peak_hours_utc"]
        assert 14 in agg["peak_hours_utc"]

    def test_get_aggregated_profile_nonexistent_peer(self, traffic_mgr):
        """get_aggregated_profile returns None for unknown peer."""
        assert traffic_mgr.get_aggregated_profile("unknown_peer") is None

    def test_get_all_profiles_no_filter(self, traffic_mgr):
        """get_all_profiles returns all stored profiles."""
        for peer in ["peer_aaa", "peer_bbb"]:
            traffic_mgr.store_local_profile(
                peer_id=peer, profile_type="retail",
                peak_hours_utc=[], quiet_hours_utc=[],
                avg_forward_size_sats=100.0, daily_volume_sats=1000.0,
                drain_direction="balanced", confidence=0.5,
                observation_window_hours=24,
            )
        profiles = traffic_mgr.get_all_profiles()
        assert len(profiles) == 2

    def test_get_all_profiles_filter_by_type(self, traffic_mgr):
        """get_all_profiles filters by profile_type."""
        traffic_mgr.store_local_profile(
            peer_id="peer_aaa", profile_type="retail",
            peak_hours_utc=[], quiet_hours_utc=[],
            avg_forward_size_sats=100.0, daily_volume_sats=1000.0,
            drain_direction="balanced", confidence=0.5,
            observation_window_hours=24,
        )
        traffic_mgr.store_local_profile(
            peer_id="peer_bbb", profile_type="wholesale",
            peak_hours_utc=[], quiet_hours_utc=[],
            avg_forward_size_sats=500000.0, daily_volume_sats=50000000.0,
            drain_direction="balanced", confidence=0.5,
            observation_window_hours=24,
        )
        retail = traffic_mgr.get_all_profiles(profile_type="retail")
        assert len(retail) == 1
        assert retail[0]["profile_type"] == "retail"

    def test_cleanup_expired(self, traffic_mgr, db):
        """cleanup_expired_profiles delegates to database."""
        old_time = time.time() - (200 * 3600)
        db.save_traffic_profile(
            peer_id="peer_old", reporter_id="reporter_111",
            profile_type="retail", peak_hours_utc="[]", quiet_hours_utc="[]",
            avg_forward_size_sats=100.0, daily_volume_sats=1000.0,
            drain_direction="balanced", confidence=0.5,
            observation_window_hours=24, received_at=old_time, ttl_hours=168.0,
        )
        deleted = traffic_mgr.cleanup_expired_profiles()
        assert deleted == 1


class TestTrafficIntelligenceGossip:
    """Test gossip creation and handling."""

    def test_create_batch_message(self, traffic_mgr):
        """create_traffic_intelligence_batch_message creates signed bytes."""
        traffic_mgr.store_local_profile(
            peer_id="peer_aaa", profile_type="retail",
            peak_hours_utc=[9, 10], quiet_hours_utc=[1, 2],
            avg_forward_size_sats=50000.0, daily_volume_sats=5000000.0,
            drain_direction="outbound_heavy", confidence=0.85,
            observation_window_hours=168,
        )
        rpc = MagicMock()
        rpc.signmessage.return_value = {"zbase": "fakesig123abc"}
        msg = traffic_mgr.create_traffic_intelligence_batch_message(rpc)
        assert msg is not None
        rpc.signmessage.assert_called_once()

    def test_create_batch_message_no_profiles(self, traffic_mgr):
        """create_traffic_intelligence_batch_message returns None with no data."""
        rpc = MagicMock()
        msg = traffic_mgr.create_traffic_intelligence_batch_message(rpc)
        assert msg is None

    def test_handle_batch_valid(self, traffic_mgr, db):
        """handle_traffic_intelligence_batch stores remote profiles."""
        sender = "remote_node_xyz"
        db.add_member(sender, tier="full")
        payload = {
            "reporter_id": sender,
            "timestamp": int(time.time()),
            "signature": "valid_sig_long_enough",
            "profiles": [{
                "peer_id": "peer_ext",
                "profile_type": "wholesale",
                "peak_hours_utc": [14, 15, 16],
                "quiet_hours_utc": [2, 3, 4],
                "avg_forward_size_sats": 200000.0,
                "daily_volume_sats": 20000000.0,
                "drain_direction": "inbound_heavy",
                "confidence": 0.8,
                "observation_window_hours": 168,
            }],
        }
        rpc = MagicMock()
        rpc.checkmessage.return_value = {"verified": True, "pubkey": sender}
        result = traffic_mgr.handle_traffic_intelligence_batch(sender, payload, rpc)
        assert result.get("success") is True
        assert result.get("profiles_stored") == 1
        profiles = db.get_traffic_profiles_for_peer("peer_ext")
        assert len(profiles) == 1

    def test_handle_batch_rejects_nonmember(self, traffic_mgr):
        """handle_traffic_intelligence_batch rejects non-member."""
        payload = {
            "reporter_id": "stranger",
            "timestamp": int(time.time()),
            "signature": "sig_long_enough_here",
            "profiles": [],
        }
        rpc = MagicMock()
        result = traffic_mgr.handle_traffic_intelligence_batch("stranger", payload, rpc)
        assert "error" in result

    def test_handle_batch_rejects_bad_signature(self, traffic_mgr, db):
        """handle_traffic_intelligence_batch rejects invalid signature."""
        sender = "remote_node_xyz"
        db.add_member(sender, tier="full")
        payload = {
            "reporter_id": sender,
            "timestamp": int(time.time()),
            "signature": "bad_sig_long_enough",
            "profiles": [],
        }
        rpc = MagicMock()
        rpc.checkmessage.return_value = {"verified": False}
        result = traffic_mgr.handle_traffic_intelligence_batch(sender, payload, rpc)
        assert result.get("error") == "invalid_signature"

    def test_handle_batch_rejects_reporter_mismatch(self, traffic_mgr, db):
        """handle_traffic_intelligence_batch rejects if reporter != sender."""
        sender = "real_sender"
        db.add_member(sender, tier="full")
        payload = {
            "reporter_id": "impersonator",
            "timestamp": int(time.time()),
            "signature": "sig_long_enough_here",
            "profiles": [],
        }
        rpc = MagicMock()
        result = traffic_mgr.handle_traffic_intelligence_batch(sender, payload, rpc)
        assert result.get("error") == "reporter_mismatch"


from datetime import datetime, timezone


class TestRebalanceConflictCheck:
    """Test temporal rebalance conflict detection."""

    def test_no_conflict_no_data(self, traffic_mgr):
        """No conflict when no traffic data exists."""
        result = traffic_mgr.check_rebalance_conflict(
            peer_id="unknown_peer",
            direction="outbound",
            amount_sats=100000,
        )
        assert result["conflict"] is False
        assert result["peer_in_peak_hours"] is False

    def test_peak_hour_detection(self, traffic_mgr):
        """Detects when peer is in peak hours."""
        current_hour = datetime.now(timezone.utc).hour
        traffic_mgr.store_local_profile(
            peer_id="peer_aaa", profile_type="retail",
            peak_hours_utc=[current_hour],
            quiet_hours_utc=[(current_hour + 12) % 24],
            avg_forward_size_sats=50000.0, daily_volume_sats=5000000.0,
            drain_direction="outbound_heavy", confidence=0.85,
            observation_window_hours=168,
        )
        result = traffic_mgr.check_rebalance_conflict(
            peer_id="peer_aaa", direction="outbound", amount_sats=100000,
        )
        assert result["peer_in_peak_hours"] is True

    def test_suggested_window_from_quiet_hours(self, traffic_mgr):
        """Suggests rebalance window from quiet hours."""
        current_hour = datetime.now(timezone.utc).hour
        quiet = [(current_hour + 6) % 24, (current_hour + 7) % 24]
        traffic_mgr.store_local_profile(
            peer_id="peer_aaa", profile_type="retail",
            peak_hours_utc=[current_hour],
            quiet_hours_utc=quiet,
            avg_forward_size_sats=50000.0, daily_volume_sats=5000000.0,
            drain_direction="outbound_heavy", confidence=0.85,
            observation_window_hours=168,
        )
        result = traffic_mgr.check_rebalance_conflict(
            peer_id="peer_aaa", direction="outbound", amount_sats=100000,
        )
        assert result["suggested_window_utc"] is not None
        assert len(result["suggested_window_utc"]) == 2

    def test_conflict_response_structure(self, traffic_mgr):
        """Response has all expected fields."""
        result = traffic_mgr.check_rebalance_conflict(
            peer_id="any_peer", direction="outbound", amount_sats=100000,
        )
        assert "conflict" in result
        assert "conflicting_member" in result
        assert "peer_in_peak_hours" in result
        assert "suggested_window_utc" in result
        assert "fleet_drain_forecast_sats" in result


class TestFleetDemandForecast:
    """Test fleet demand forecasting."""

    def test_forecast_no_data(self, traffic_mgr):
        """Forecast returns empty structure when no data."""
        forecast = traffic_mgr.get_fleet_demand_forecast(hours_ahead=6)
        assert "members" in forecast
        assert isinstance(forecast["members"], list)

    def test_forecast_structure(self, traffic_mgr):
        """Forecast response has expected top-level fields."""
        forecast = traffic_mgr.get_fleet_demand_forecast(hours_ahead=6)
        assert "members" in forecast
        assert "generated_at" in forecast
        assert "hours_ahead" in forecast


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
