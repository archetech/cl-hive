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
    validate_peer_reputation_snapshot_payload,
    get_peer_reputation_snapshot_signing_payload,
    create_peer_reputation_snapshot,
    PEER_REPUTATION_SNAPSHOT_RATE_LIMIT,
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


class TestPeerReputationSnapshot:
    """Test PEER_REPUTATION_SNAPSHOT message handling."""

    def test_snapshot_payload_validation(self):
        """Test PEER_REPUTATION_SNAPSHOT payload validation."""
        from modules.protocol import validate_peer_reputation_snapshot_payload

        now = int(time.time())
        valid_payload = {
            "reporter_id": "02" + "a" * 64,
            "timestamp": now,
            "signature": "testsig12345",
            "peers": [
                {
                    "peer_id": "03" + "b" * 64,
                    "uptime_pct": 0.99,
                    "response_time_ms": 150,
                    "force_close_count": 0,
                    "fee_stability": 0.95,
                    "htlc_success_rate": 0.98,
                    "channel_age_days": 90,
                    "total_routed_sats": 1000000,
                    "warnings": [],
                    "observation_days": 7
                }
            ]
        }

        assert validate_peer_reputation_snapshot_payload(valid_payload) is True

    def test_snapshot_rejects_invalid_uptime(self):
        """Test that invalid uptime values are rejected."""
        from modules.protocol import validate_peer_reputation_snapshot_payload

        now = int(time.time())
        invalid_payload = {
            "reporter_id": "02" + "a" * 64,
            "timestamp": now,
            "signature": "testsig12345",
            "peers": [
                {
                    "peer_id": "03" + "b" * 64,
                    "uptime_pct": 1.5,  # Invalid - > 1
                }
            ]
        }

        assert validate_peer_reputation_snapshot_payload(invalid_payload) is False

    def test_snapshot_rejects_invalid_warnings(self):
        """Test that invalid warning codes are rejected."""
        from modules.protocol import validate_peer_reputation_snapshot_payload

        now = int(time.time())
        invalid_payload = {
            "reporter_id": "02" + "a" * 64,
            "timestamp": now,
            "signature": "testsig12345",
            "peers": [
                {
                    "peer_id": "03" + "b" * 64,
                    "warnings": ["invalid_warning_code"],  # Not in VALID_WARNINGS
                }
            ]
        }

        assert validate_peer_reputation_snapshot_payload(invalid_payload) is False

    def test_snapshot_rejects_too_many_peers(self):
        """Test that snapshots with too many peers are rejected."""
        from modules.protocol import (
            validate_peer_reputation_snapshot_payload,
            MAX_PEERS_IN_REPUTATION_SNAPSHOT
        )

        now = int(time.time())
        too_many_peers = {
            "reporter_id": "02" + "a" * 64,
            "timestamp": now,
            "signature": "testsig12345",
            "peers": [
                {
                    "peer_id": f"03{'x' * 63}{i:x}",
                    "uptime_pct": 0.99,
                }
                for i in range(MAX_PEERS_IN_REPUTATION_SNAPSHOT + 1)
            ]
        }

        assert validate_peer_reputation_snapshot_payload(too_many_peers) is False

    def test_snapshot_signing_deterministic(self):
        """Test that snapshot signing payload is deterministic."""
        from modules.protocol import get_peer_reputation_snapshot_signing_payload

        now = int(time.time())
        payload = {
            "reporter_id": "02" + "a" * 64,
            "timestamp": now,
            "peers": [
                {"peer_id": "03" + "b" * 64, "uptime_pct": 0.99},
                {"peer_id": "03" + "c" * 64, "uptime_pct": 0.95},
            ]
        }

        # Different order should produce same signing payload (sorted by peer_id)
        payload_reordered = {
            "reporter_id": "02" + "a" * 64,
            "timestamp": now,
            "peers": [
                {"peer_id": "03" + "c" * 64, "uptime_pct": 0.95},
                {"peer_id": "03" + "b" * 64, "uptime_pct": 0.99},
            ]
        }

        sig1 = get_peer_reputation_snapshot_signing_payload(payload)
        sig2 = get_peer_reputation_snapshot_signing_payload(payload_reordered)

        assert sig1 == sig2

    def test_snapshot_rate_limiting(self):
        """Test snapshot rate limiting."""
        from modules.protocol import PEER_REPUTATION_SNAPSHOT_RATE_LIMIT
        from modules.peer_reputation import PeerReputationManager

        mock_db = MagicMock()
        mock_plugin = MagicMock()

        mgr = PeerReputationManager(
            database=mock_db,
            plugin=mock_plugin,
            our_pubkey="02" + "a" * 64
        )

        sender_id = "02" + "b" * 64

        # Should allow first few snapshots
        for i in range(PEER_REPUTATION_SNAPSHOT_RATE_LIMIT[0]):
            allowed = mgr._check_rate_limit(
                sender_id,
                mgr._snapshot_rate,
                PEER_REPUTATION_SNAPSHOT_RATE_LIMIT
            )
            mgr._record_message(sender_id, mgr._snapshot_rate)
            assert allowed is True

        # Should reject the next one
        allowed = mgr._check_rate_limit(
            sender_id,
            mgr._snapshot_rate,
            PEER_REPUTATION_SNAPSHOT_RATE_LIMIT
        )
        assert allowed is False
