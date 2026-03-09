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
