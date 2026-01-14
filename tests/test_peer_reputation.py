"""
Tests for Peer Reputation functionality (Phase 5 - Advanced Cooperation).

Tests cover:
- PeerReputationManager class
- PEER_REPUTATION payload validation
- Reputation aggregation with outlier detection
- Rate limiting
- Database integration
"""

import pytest
import time
from unittest.mock import MagicMock, patch

from modules.peer_reputation import (
    PeerReputationManager,
    AggregatedReputation,
    MIN_REPORTERS_FOR_CONFIDENCE,
    OUTLIER_DEVIATION_THRESHOLD,
    REPUTATION_STALENESS_HOURS,
)
from modules.protocol import (
    validate_peer_reputation_payload,
    get_peer_reputation_signing_payload,
    create_peer_reputation,
    PEER_REPUTATION_RATE_LIMIT,
    VALID_WARNINGS,
    MAX_WARNINGS_COUNT,
)


class MockDatabase:
    """Mock database for testing."""

    def __init__(self):
        self.peer_reputation = []
        self.members = {}

    def get_member(self, peer_id):
        return self.members.get(peer_id)

    def get_all_members(self):
        return list(self.members.values()) if self.members else []

    def store_peer_reputation(self, **kwargs):
        self.peer_reputation.append(kwargs)

    def get_peer_reputation_reports(self, peer_id, max_age_hours=168):
        return [r for r in self.peer_reputation if r.get("peer_id") == peer_id]

    def get_all_peer_reputation_reports(self, max_age_hours=168):
        return self.peer_reputation

    def get_peer_reputation_reporters(self, peer_id):
        reporters = set()
        for r in self.peer_reputation:
            if r.get("peer_id") == peer_id:
                reporters.add(r.get("reporter_id"))
        return list(reporters)

    def cleanup_old_peer_reputation(self, max_age_hours=168):
        return 0


class TestPeerReputationPayload:
    """Test PEER_REPUTATION payload validation."""

    def test_valid_payload(self):
        """Test that valid payload passes validation."""
        payload = {
            "reporter_id": "02" + "a" * 64,
            "peer_id": "03" + "b" * 64,
            "timestamp": int(time.time()),
            "signature": "valid_signature_here",
            "uptime_pct": 0.95,
            "response_time_ms": 500,
            "force_close_count": 0,
            "fee_stability": 0.9,
            "htlc_success_rate": 0.98,
            "channel_age_days": 30,
            "total_routed_sats": 1000000,
            "warnings": [],
            "observation_days": 7,
        }
        assert validate_peer_reputation_payload(payload) is True

    def test_missing_reporter(self):
        """Test that missing reporter fails validation."""
        payload = {
            "peer_id": "03" + "b" * 64,
            "timestamp": int(time.time()),
            "signature": "valid_signature_here",
        }
        assert validate_peer_reputation_payload(payload) is False

    def test_missing_peer_id(self):
        """Test that missing peer_id fails validation."""
        payload = {
            "reporter_id": "02" + "a" * 64,
            "timestamp": int(time.time()),
            "signature": "valid_signature_here",
        }
        assert validate_peer_reputation_payload(payload) is False

    def test_invalid_uptime_pct(self):
        """Test that uptime outside 0-1 fails validation."""
        payload = {
            "reporter_id": "02" + "a" * 64,
            "peer_id": "03" + "b" * 64,
            "timestamp": int(time.time()),
            "signature": "valid_signature_here",
            "uptime_pct": 1.5,  # Invalid - must be 0-1
        }
        assert validate_peer_reputation_payload(payload) is False

    def test_invalid_htlc_success_rate(self):
        """Test that HTLC rate outside 0-1 fails validation."""
        payload = {
            "reporter_id": "02" + "a" * 64,
            "peer_id": "03" + "b" * 64,
            "timestamp": int(time.time()),
            "signature": "valid_signature_here",
            "htlc_success_rate": -0.1,  # Invalid - must be 0-1
        }
        assert validate_peer_reputation_payload(payload) is False

    def test_negative_response_time(self):
        """Test that negative response time fails validation."""
        payload = {
            "reporter_id": "02" + "a" * 64,
            "peer_id": "03" + "b" * 64,
            "timestamp": int(time.time()),
            "signature": "valid_signature_here",
            "response_time_ms": -100,
        }
        assert validate_peer_reputation_payload(payload) is False

    def test_valid_warnings(self):
        """Test that valid warnings pass validation."""
        payload = {
            "reporter_id": "02" + "a" * 64,
            "peer_id": "03" + "b" * 64,
            "timestamp": int(time.time()),
            "signature": "valid_signature_here",
            "warnings": ["fee_spike", "force_close"],
        }
        assert validate_peer_reputation_payload(payload) is True

    def test_invalid_warnings(self):
        """Test that invalid warning code fails validation."""
        payload = {
            "reporter_id": "02" + "a" * 64,
            "peer_id": "03" + "b" * 64,
            "timestamp": int(time.time()),
            "signature": "valid_signature_here",
            "warnings": ["invalid_warning_code"],
        }
        assert validate_peer_reputation_payload(payload) is False

    def test_too_many_warnings(self):
        """Test that too many warnings fails validation."""
        payload = {
            "reporter_id": "02" + "a" * 64,
            "peer_id": "03" + "b" * 64,
            "timestamp": int(time.time()),
            "signature": "valid_signature_here",
            "warnings": list(VALID_WARNINGS)[:MAX_WARNINGS_COUNT + 1],
        }
        # This will fail because we're trying to have more warnings than allowed
        payload["warnings"] = ["fee_spike"] * (MAX_WARNINGS_COUNT + 1)
        assert validate_peer_reputation_payload(payload) is False

    def test_short_signature(self):
        """Test that short signature fails validation."""
        payload = {
            "reporter_id": "02" + "a" * 64,
            "peer_id": "03" + "b" * 64,
            "timestamp": int(time.time()),
            "signature": "short",  # Too short
        }
        assert validate_peer_reputation_payload(payload) is False


class TestPeerReputationSigningPayload:
    """Test peer reputation signing payload generation."""

    def test_signing_payload_deterministic(self):
        """Test that signing payload is deterministic."""
        payload = {
            "reporter_id": "02" + "a" * 64,
            "peer_id": "03" + "b" * 64,
            "timestamp": 1700000000,
            "uptime_pct": 0.95,
            "htlc_success_rate": 0.98,
            "force_close_count": 0,
        }
        sig1 = get_peer_reputation_signing_payload(payload)
        sig2 = get_peer_reputation_signing_payload(payload)
        assert sig1 == sig2

    def test_signing_payload_contains_essential_fields(self):
        """Test that signing payload contains essential fields."""
        payload = {
            "reporter_id": "02" + "a" * 64,
            "peer_id": "03" + "b" * 64,
            "timestamp": 1700000000,
            "uptime_pct": 0.95,
            "htlc_success_rate": 0.98,
            "force_close_count": 1,
        }
        sig = get_peer_reputation_signing_payload(payload)
        assert payload["reporter_id"] in sig
        assert payload["peer_id"] in sig
        assert "1700000000" in sig


class TestPeerReputationManager:
    """Test PeerReputationManager class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_db = MockDatabase()
        self.mock_plugin = MagicMock()
        self.our_pubkey = "02" + "0" * 64
        self.rep_mgr = PeerReputationManager(
            database=self.mock_db,
            plugin=self.mock_plugin,
            our_pubkey=self.our_pubkey
        )

        # Add members
        self.member1 = "02" + "a" * 64
        self.member2 = "02" + "b" * 64
        self.member3 = "02" + "c" * 64
        self.mock_db.members[self.member1] = {
            "peer_id": self.member1,
            "tier": "member"
        }
        self.mock_db.members[self.member2] = {
            "peer_id": self.member2,
            "tier": "member"
        }
        self.mock_db.members[self.member3] = {
            "peer_id": self.member3,
            "tier": "member"
        }

        # External peer
        self.external_peer = "03" + "x" * 64

    def test_handle_peer_reputation_valid(self):
        """Test handling a valid peer reputation report."""
        mock_rpc = MagicMock()
        mock_rpc.checkmessage.return_value = {
            "verified": True,
            "pubkey": self.member1
        }

        payload = {
            "reporter_id": self.member1,
            "peer_id": self.external_peer,
            "timestamp": int(time.time()),
            "signature": "valid_signature_here",
            "uptime_pct": 0.95,
            "htlc_success_rate": 0.98,
            "force_close_count": 0,
            "warnings": [],
        }

        result = self.rep_mgr.handle_peer_reputation(
            self.member1, payload, mock_rpc
        )

        assert result.get("success") is True
        assert result.get("stored") is True
        assert len(self.mock_db.peer_reputation) == 1

    def test_handle_peer_reputation_non_member(self):
        """Test rejecting report from non-member."""
        mock_rpc = MagicMock()
        non_member = "02" + "z" * 64

        payload = {
            "reporter_id": non_member,
            "peer_id": self.external_peer,
            "timestamp": int(time.time()),
            "signature": "valid_signature_here",
        }

        result = self.rep_mgr.handle_peer_reputation(
            non_member, payload, mock_rpc
        )

        assert result.get("error") == "reporter not a member"
        assert len(self.mock_db.peer_reputation) == 0

    def test_handle_peer_reputation_invalid_signature(self):
        """Test rejecting report with invalid signature."""
        mock_rpc = MagicMock()
        mock_rpc.checkmessage.return_value = {"verified": False}

        payload = {
            "reporter_id": self.member1,
            "peer_id": self.external_peer,
            "timestamp": int(time.time()),
            "signature": "invalid_signature",
        }

        result = self.rep_mgr.handle_peer_reputation(
            self.member1, payload, mock_rpc
        )

        assert "signature" in result.get("error", "").lower()
        assert len(self.mock_db.peer_reputation) == 0

    def test_handle_peer_reputation_rate_limited(self):
        """Test rate limiting of peer reputation reports."""
        mock_rpc = MagicMock()
        mock_rpc.checkmessage.return_value = {
            "verified": True,
            "pubkey": self.member1
        }

        max_reports, period = PEER_REPUTATION_RATE_LIMIT

        # Send reports up to rate limit
        for i in range(max_reports):
            payload = {
                "reporter_id": self.member1,
                "peer_id": self.external_peer,
                "timestamp": int(time.time()),
                "signature": f"signature_for_report_{i:02d}",
            }
            result = self.rep_mgr.handle_peer_reputation(
                self.member1, payload, mock_rpc
            )
            assert result.get("success") is True

        # Next report should be rate limited
        payload = {
            "reporter_id": self.member1,
            "peer_id": self.external_peer,
            "timestamp": int(time.time()),
            "signature": "extra_signature_here",
        }
        result = self.rep_mgr.handle_peer_reputation(
            self.member1, payload, mock_rpc
        )

        assert result.get("error") == "rate limited"

    def test_reputation_aggregation(self):
        """Test aggregation of multiple reputation reports."""
        # Add reports from multiple members
        now = int(time.time())
        self.mock_db.peer_reputation = [
            {
                "reporter_id": self.member1,
                "peer_id": self.external_peer,
                "timestamp": now,
                "uptime_pct": 0.95,
                "htlc_success_rate": 0.98,
                "fee_stability": 0.9,
                "force_close_count": 0,
                "warnings": [],
            },
            {
                "reporter_id": self.member2,
                "peer_id": self.external_peer,
                "timestamp": now,
                "uptime_pct": 0.90,
                "htlc_success_rate": 0.95,
                "fee_stability": 0.85,
                "force_close_count": 0,
                "warnings": [],
            },
            {
                "reporter_id": self.member3,
                "peer_id": self.external_peer,
                "timestamp": now,
                "uptime_pct": 0.92,
                "htlc_success_rate": 0.96,
                "fee_stability": 0.88,
                "force_close_count": 0,
                "warnings": [],
            },
        ]

        self.rep_mgr._update_aggregation(self.external_peer)

        rep = self.rep_mgr.get_reputation(self.external_peer)
        assert rep is not None
        assert len(rep.reporters) == 3
        assert rep.confidence == "high"  # 3+ reporters
        assert rep.avg_uptime > 0.9
        assert rep.avg_htlc_success > 0.95

    def test_outlier_filtering(self):
        """Test that outliers are filtered from aggregation."""
        now = int(time.time())

        # Two normal reports and one outlier
        self.mock_db.peer_reputation = [
            {
                "reporter_id": self.member1,
                "peer_id": self.external_peer,
                "timestamp": now,
                "uptime_pct": 0.95,
                "htlc_success_rate": 0.98,
            },
            {
                "reporter_id": self.member2,
                "peer_id": self.external_peer,
                "timestamp": now,
                "uptime_pct": 0.93,
                "htlc_success_rate": 0.97,
            },
            {
                "reporter_id": self.member3,
                "peer_id": self.external_peer,
                "timestamp": now,
                "uptime_pct": 0.50,  # Outlier - significantly different
                "htlc_success_rate": 0.40,  # Outlier
            },
        ]

        self.rep_mgr._update_aggregation(self.external_peer)

        rep = self.rep_mgr.get_reputation(self.external_peer)
        assert rep is not None
        # Outlier should be filtered, so avg should be close to normal values
        assert rep.avg_uptime > 0.9
        assert rep.avg_htlc_success > 0.9

    def test_our_data_weighted_higher(self):
        """Test that our own observations are weighted higher."""
        now = int(time.time())

        # Our report has different values
        self.mock_db.peer_reputation = [
            {
                "reporter_id": self.our_pubkey,
                "peer_id": self.external_peer,
                "timestamp": now,
                "uptime_pct": 0.80,  # Our observation
                "htlc_success_rate": 0.85,
                "fee_stability": 0.8,
            },
            {
                "reporter_id": self.member1,
                "peer_id": self.external_peer,
                "timestamp": now,
                "uptime_pct": 0.95,
                "htlc_success_rate": 0.98,
                "fee_stability": 0.95,
            },
            {
                "reporter_id": self.member2,
                "peer_id": self.external_peer,
                "timestamp": now,
                "uptime_pct": 0.95,
                "htlc_success_rate": 0.98,
                "fee_stability": 0.95,
            },
        ]

        self.rep_mgr._update_aggregation(self.external_peer)

        rep = self.rep_mgr.get_reputation(self.external_peer)
        assert rep is not None
        # With our data weighted 2x, average should be pulled toward our values
        # Without weighting: avg_uptime = (0.80 + 0.95 + 0.95) / 3 = 0.90
        # With 2x weight: avg_uptime = (0.80 + 0.80 + 0.95 + 0.95) / 4 = 0.875
        assert rep.avg_uptime < 0.90

    def test_warning_aggregation(self):
        """Test aggregation of warnings."""
        now = int(time.time())

        self.mock_db.peer_reputation = [
            {
                "reporter_id": self.member1,
                "peer_id": self.external_peer,
                "timestamp": now,
                "warnings": ["fee_spike", "force_close"],
            },
            {
                "reporter_id": self.member2,
                "peer_id": self.external_peer,
                "timestamp": now,
                "warnings": ["fee_spike"],  # Same warning
            },
            {
                "reporter_id": self.member3,
                "peer_id": self.external_peer,
                "timestamp": now,
                "warnings": ["slow_response"],
            },
        ]

        self.rep_mgr._update_aggregation(self.external_peer)

        rep = self.rep_mgr.get_reputation(self.external_peer)
        assert rep is not None
        assert "fee_spike" in rep.warnings
        assert rep.warnings["fee_spike"] == 2  # Reported twice
        assert "force_close" in rep.warnings
        assert "slow_response" in rep.warnings

    def test_reputation_score_calculation(self):
        """Test reputation score calculation."""
        now = int(time.time())

        # Good peer
        self.mock_db.peer_reputation = [
            {
                "reporter_id": self.member1,
                "peer_id": self.external_peer,
                "timestamp": now,
                "uptime_pct": 1.0,
                "htlc_success_rate": 1.0,
                "fee_stability": 1.0,
                "force_close_count": 0,
                "warnings": [],
            },
        ]

        self.rep_mgr._update_aggregation(self.external_peer)

        rep = self.rep_mgr.get_reputation(self.external_peer)
        assert rep is not None
        # Perfect metrics should give high score
        assert rep.reputation_score > 70

    def test_reputation_score_with_penalties(self):
        """Test reputation score with penalties."""
        now = int(time.time())

        # Bad peer
        bad_peer = "03" + "y" * 64
        self.mock_db.peer_reputation = [
            {
                "reporter_id": self.member1,
                "peer_id": bad_peer,
                "timestamp": now,
                "uptime_pct": 0.5,
                "htlc_success_rate": 0.5,
                "fee_stability": 0.5,
                "force_close_count": 3,
                "warnings": ["fee_spike", "force_close", "slow_response"],
            },
        ]

        self.rep_mgr._update_aggregation(bad_peer)

        rep = self.rep_mgr.get_reputation(bad_peer)
        assert rep is not None
        # Poor metrics + force closes + warnings should give low score
        assert rep.reputation_score < 50

    def test_get_low_reputation_peers(self):
        """Test getting low reputation peers."""
        now = int(time.time())

        good_peer = "03" + "g" * 64
        bad_peer = "03" + "b" * 64

        self.mock_db.peer_reputation = [
            {
                "reporter_id": self.member1,
                "peer_id": good_peer,
                "timestamp": now,
                "uptime_pct": 1.0,
                "htlc_success_rate": 1.0,
                "fee_stability": 1.0,
            },
            {
                "reporter_id": self.member1,
                "peer_id": bad_peer,
                "timestamp": now,
                "uptime_pct": 0.3,
                "htlc_success_rate": 0.3,
                "fee_stability": 0.3,
                "force_close_count": 5,
            },
        ]

        self.rep_mgr._update_aggregation(good_peer)
        self.rep_mgr._update_aggregation(bad_peer)

        low_rep = self.rep_mgr.get_low_reputation_peers(threshold=40)
        assert len(low_rep) == 1
        assert low_rep[0].peer_id == bad_peer

    def test_get_peers_with_warnings(self):
        """Test getting peers with warnings."""
        now = int(time.time())

        warned_peer = "03" + "w" * 64
        clean_peer = "03" + "c" * 64

        self.mock_db.peer_reputation = [
            {
                "reporter_id": self.member1,
                "peer_id": warned_peer,
                "timestamp": now,
                "warnings": ["fee_spike"],
            },
            {
                "reporter_id": self.member1,
                "peer_id": clean_peer,
                "timestamp": now,
                "warnings": [],
            },
        ]

        self.rep_mgr._update_aggregation(warned_peer)
        self.rep_mgr._update_aggregation(clean_peer)

        warned = self.rep_mgr.get_peers_with_warnings()
        assert len(warned) == 1
        assert warned[0].peer_id == warned_peer

    def test_reputation_stats(self):
        """Test reputation statistics."""
        now = int(time.time())

        # Add some peers
        for i in range(5):
            peer = f"03{'x' * 63}{i}"
            self.mock_db.peer_reputation.append({
                "reporter_id": self.member1,
                "peer_id": peer,
                "timestamp": now,
                "uptime_pct": 0.9,
                "htlc_success_rate": 0.9,
            })
            self.rep_mgr._update_aggregation(peer)

        stats = self.rep_mgr.get_reputation_stats()

        assert stats["total_peers_tracked"] == 5
        assert stats["avg_reputation_score"] > 0

    def test_cleanup_stale_data(self):
        """Test cleanup of stale reputation data."""
        # Add old aggregation
        old_timestamp = int(time.time()) - (REPUTATION_STALENESS_HOURS + 1) * 3600

        self.rep_mgr._aggregated[self.external_peer] = AggregatedReputation(
            peer_id=self.external_peer,
            last_update=old_timestamp
        )

        assert len(self.rep_mgr._aggregated) == 1

        cleaned = self.rep_mgr.cleanup_stale_data()

        assert cleaned == 1
        assert len(self.rep_mgr._aggregated) == 0

    def test_confidence_levels(self):
        """Test confidence level calculation."""
        now = int(time.time())

        # Single reporter = low confidence
        single_peer = "03" + "s" * 64
        self.mock_db.peer_reputation = [
            {
                "reporter_id": self.member1,
                "peer_id": single_peer,
                "timestamp": now,
            },
        ]
        self.rep_mgr._update_aggregation(single_peer)
        rep = self.rep_mgr.get_reputation(single_peer)
        assert rep.confidence == "low"

        # Two reporters = medium confidence
        two_peer = "03" + "t" * 64
        self.mock_db.peer_reputation = [
            {"reporter_id": self.member1, "peer_id": two_peer, "timestamp": now},
            {"reporter_id": self.member2, "peer_id": two_peer, "timestamp": now},
        ]
        self.rep_mgr._update_aggregation(two_peer)
        rep = self.rep_mgr.get_reputation(two_peer)
        assert rep.confidence == "medium"

        # Three+ reporters = high confidence
        three_peer = "03" + "h" * 64
        self.mock_db.peer_reputation = [
            {"reporter_id": self.member1, "peer_id": three_peer, "timestamp": now},
            {"reporter_id": self.member2, "peer_id": three_peer, "timestamp": now},
            {"reporter_id": self.member3, "peer_id": three_peer, "timestamp": now},
        ]
        self.rep_mgr._update_aggregation(three_peer)
        rep = self.rep_mgr.get_reputation(three_peer)
        assert rep.confidence == "high"


class TestCreatePeerReputation:
    """Test peer reputation message creation."""

    def test_create_peer_reputation(self):
        """Test creating a signed peer reputation message."""
        mock_rpc = MagicMock()
        mock_rpc.signmessage.return_value = {"signature": "base64sig", "zbase": "zbasesig"}

        reporter_id = "02" + "a" * 64
        peer_id = "03" + "b" * 64

        msg = create_peer_reputation(
            reporter_id=reporter_id,
            peer_id=peer_id,
            rpc=mock_rpc,
            uptime_pct=0.95,
            htlc_success_rate=0.98,
            warnings=["fee_spike"]
        )

        assert msg is not None
        assert isinstance(msg, bytes)
        assert mock_rpc.signmessage.called


class TestAggregatedReputation:
    """Test AggregatedReputation dataclass."""

    def test_aggregated_reputation_defaults(self):
        """Test AggregatedReputation default values."""
        rep = AggregatedReputation(peer_id="03" + "a" * 64)

        assert rep.avg_uptime == 1.0
        assert rep.avg_htlc_success == 1.0
        assert rep.avg_fee_stability == 1.0
        assert rep.avg_response_time_ms == 0
        assert rep.total_force_closes == 0
        assert len(rep.reporters) == 0
        assert rep.report_count == 0
        assert rep.confidence == "low"
        assert rep.reputation_score == 50
