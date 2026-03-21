"""
Tests for hive-export-hints RPC: compact per-peer hint export.

Verifies:
- Valid response shape and required top-level fields
- Normalized field ranges and enum values
- Neutral defaults when managers are missing
- No side effects from calling the RPC
- Stable behavior with partial peer state
"""

import time
from dataclasses import dataclass
from unittest.mock import MagicMock, PropertyMock

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.rpc_commands import (
    HiveContext,
    export_hints,
    _derive_corridor_roles,
    _derive_competition_bias,
    _derive_rebalance_preferences,
)


PEER_A = "02" + "a" * 64
PEER_B = "03" + "b" * 64
PEER_C = "02" + "c" * 64
OUR_PUBKEY = "02" + "ff" * 32


def _make_ctx(**overrides):
    """Create a minimal HiveContext with sensible defaults."""
    db = MagicMock()
    db.get_all_members.return_value = [
        {"peer_id": PEER_A, "tier": "member"},
        {"peer_id": PEER_B, "tier": "member"},
    ]
    defaults = dict(
        database=db,
        config=MagicMock(),
        safe_plugin=MagicMock(),
        our_pubkey=OUR_PUBKEY,
    )
    defaults.update(overrides)
    return HiveContext(**defaults)


# =============================================================================
# RESPONSE SHAPE
# =============================================================================

class TestResponseShape:
    """Test that export_hints returns the correct top-level structure."""

    def test_top_level_fields(self):
        ctx = _make_ctx()
        result = export_hints(ctx)

        assert "generated_at" in result
        assert "ttl_seconds" in result
        assert "peer_count" in result
        assert "hints" in result
        assert isinstance(result["hints"], dict)

    def test_generated_at_is_recent(self):
        ctx = _make_ctx()
        before = int(time.time())
        result = export_hints(ctx)
        after = int(time.time())

        assert before <= result["generated_at"] <= after

    def test_default_ttl(self):
        ctx = _make_ctx()
        result = export_hints(ctx)
        assert result["ttl_seconds"] == 900

    def test_custom_ttl(self):
        ctx = _make_ctx()
        result = export_hints(ctx, ttl_seconds=300)
        assert result["ttl_seconds"] == 300

    def test_peer_count_matches_hints(self):
        ctx = _make_ctx()
        result = export_hints(ctx)
        assert result["peer_count"] == len(result["hints"])

    def test_excludes_our_pubkey(self):
        ctx = _make_ctx()
        result = export_hints(ctx)
        assert OUR_PUBKEY not in result["hints"]


# =============================================================================
# PER-PEER HINT FIELDS
# =============================================================================

class TestHintFields:
    """Test individual hint field values and defaults."""

    def test_member_flag_true_for_members(self):
        ctx = _make_ctx()
        result = export_hints(ctx)
        assert result["hints"][PEER_A]["member"] is True
        assert result["hints"][PEER_B]["member"] is True

    def test_corridor_role_default_none(self):
        ctx = _make_ctx()
        result = export_hints(ctx)
        assert result["hints"][PEER_A]["corridor_role"] == "none"

    def test_competition_bias_default_zero(self):
        ctx = _make_ctx()
        result = export_hints(ctx)
        assert result["hints"][PEER_A]["competition_bias"] == 0

    def test_rebalance_preference_default_neutral(self):
        ctx = _make_ctx()
        result = export_hints(ctx)
        assert result["hints"][PEER_A]["rebalance_preference"] == "neutral"

    def test_quality_score_omitted_when_no_scorer(self):
        ctx = _make_ctx(quality_scorer=None)
        result = export_hints(ctx)
        assert "peer_quality_score" not in result["hints"][PEER_A]

    def test_traffic_confidence_omitted_when_no_manager(self):
        ctx = _make_ctx(traffic_intel_mgr=None)
        result = export_hints(ctx)
        assert "traffic_confidence" not in result["hints"][PEER_A]


# =============================================================================
# CORRIDOR ROLE DERIVATION
# =============================================================================

@dataclass
class _MockCorridor:
    competition_level: str = "none"


@dataclass
class _MockAssignment:
    corridor: _MockCorridor
    primary_member: str = ""
    secondary_members: list = None

    def __post_init__(self):
        if self.secondary_members is None:
            self.secondary_members = []


class TestCorridorRoles:
    """Test corridor role derivation logic."""

    def test_owner_role(self):
        mgr = MagicMock()
        mgr.corridor_mgr.get_assignments.return_value = [
            _MockAssignment(
                corridor=_MockCorridor(),
                primary_member=PEER_A,
                secondary_members=[PEER_B],
            )
        ]
        ctx = _make_ctx(fee_coordination_mgr=mgr)
        roles = _derive_corridor_roles(ctx)
        assert roles[PEER_A] == "owner"
        assert roles[PEER_B] == "secondary"

    def test_contested_role(self):
        mgr = MagicMock()
        mgr.corridor_mgr.get_assignments.return_value = [
            _MockAssignment(
                corridor=_MockCorridor(),
                primary_member=PEER_A,
                secondary_members=[],
            ),
            _MockAssignment(
                corridor=_MockCorridor(),
                primary_member=PEER_B,
                secondary_members=[PEER_A],
            ),
        ]
        ctx = _make_ctx(fee_coordination_mgr=mgr)
        roles = _derive_corridor_roles(ctx)
        assert roles[PEER_A] == "contested"

    def test_empty_when_no_manager(self):
        ctx = _make_ctx(fee_coordination_mgr=None)
        roles = _derive_corridor_roles(ctx)
        assert roles == {}


# =============================================================================
# COMPETITION BIAS DERIVATION
# =============================================================================

class TestCompetitionBias:
    """Test competition bias derivation."""

    def test_high_competition_gives_negative_bias(self):
        mgr = MagicMock()
        mgr.corridor_mgr.get_assignments.return_value = [
            _MockAssignment(
                corridor=_MockCorridor(competition_level="high"),
                primary_member=PEER_A,
            ),
            _MockAssignment(
                corridor=_MockCorridor(competition_level="high"),
                primary_member=PEER_A,
            ),
        ]
        ctx = _make_ctx(fee_coordination_mgr=mgr)
        biases = _derive_competition_bias(ctx)
        assert biases[PEER_A] == -1

    def test_low_competition_gives_positive_bias(self):
        mgr = MagicMock()
        mgr.corridor_mgr.get_assignments.return_value = [
            _MockAssignment(
                corridor=_MockCorridor(competition_level="low"),
                primary_member=PEER_A,
            ),
        ]
        ctx = _make_ctx(fee_coordination_mgr=mgr)
        biases = _derive_competition_bias(ctx)
        assert biases[PEER_A] == 1


# =============================================================================
# REBALANCE PREFERENCE DERIVATION
# =============================================================================

@dataclass
class _MockYieldMetric:
    peer_id: str
    flow_direction: str = "balanced"


class TestRebalancePreference:
    """Test rebalance preference derivation from yield metrics."""

    def test_sink_direction(self):
        mgr = MagicMock()
        mgr.get_channel_yield_metrics.return_value = [
            _MockYieldMetric(peer_id=PEER_A, flow_direction="sink"),
        ]
        ctx = _make_ctx(yield_metrics_mgr=mgr)
        prefs = _derive_rebalance_preferences(ctx)
        assert prefs[PEER_A] == "sink"

    def test_source_direction(self):
        mgr = MagicMock()
        mgr.get_channel_yield_metrics.return_value = [
            _MockYieldMetric(peer_id=PEER_A, flow_direction="source"),
        ]
        ctx = _make_ctx(yield_metrics_mgr=mgr)
        prefs = _derive_rebalance_preferences(ctx)
        assert prefs[PEER_A] == "source"

    def test_balanced_not_in_prefs(self):
        mgr = MagicMock()
        mgr.get_channel_yield_metrics.return_value = [
            _MockYieldMetric(peer_id=PEER_A, flow_direction="balanced"),
        ]
        ctx = _make_ctx(yield_metrics_mgr=mgr)
        prefs = _derive_rebalance_preferences(ctx)
        assert PEER_A not in prefs


# =============================================================================
# QUALITY SCORE AND TRAFFIC CONFIDENCE
# =============================================================================

class TestQualityAndTraffic:
    """Test quality score and traffic confidence when managers are available."""

    def test_quality_score_included(self):
        scorer = MagicMock()
        score_result = MagicMock()
        score_result.overall_score = 0.82
        score_result.confidence = 0.5
        scorer.calculate_score.return_value = score_result

        ctx = _make_ctx(quality_scorer=scorer)
        result = export_hints(ctx)
        assert result["hints"][PEER_A]["peer_quality_score"] == 0.82

    def test_quality_score_omitted_when_zero_confidence(self):
        scorer = MagicMock()
        score_result = MagicMock()
        score_result.overall_score = 0.5
        score_result.confidence = 0.0
        scorer.calculate_score.return_value = score_result

        ctx = _make_ctx(quality_scorer=scorer)
        result = export_hints(ctx)
        assert "peer_quality_score" not in result["hints"][PEER_A]

    def test_traffic_confidence_included(self):
        traffic_mgr = MagicMock()
        traffic_mgr.get_aggregated_profile.return_value = {
            "confidence": 0.74,
            "profile_type": "retail",
        }

        ctx = _make_ctx(traffic_intel_mgr=traffic_mgr)
        result = export_hints(ctx)
        assert result["hints"][PEER_A]["traffic_confidence"] == 0.74

    def test_traffic_confidence_omitted_when_no_profile(self):
        traffic_mgr = MagicMock()
        traffic_mgr.get_aggregated_profile.return_value = None

        ctx = _make_ctx(traffic_intel_mgr=traffic_mgr)
        result = export_hints(ctx)
        assert "traffic_confidence" not in result["hints"][PEER_A]


# =============================================================================
# NO SIDE EFFECTS
# =============================================================================

class TestNoSideEffects:
    """Verify the RPC is read-only and safe to call repeatedly."""

    def test_no_database_writes(self):
        ctx = _make_ctx()
        export_hints(ctx)
        # Verify no write methods were called
        ctx.database.add_member.assert_not_called()
        ctx.database.remove_member.assert_not_called()
        ctx.database.add_ban.assert_not_called()

    def test_idempotent_results(self):
        ctx = _make_ctx()
        r1 = export_hints(ctx)
        r2 = export_hints(ctx)
        assert r1["hints"].keys() == r2["hints"].keys()
        assert r1["ttl_seconds"] == r2["ttl_seconds"]

    def test_error_when_db_missing(self):
        ctx = _make_ctx(database=None)
        result = export_hints(ctx)
        assert "error" in result


# =============================================================================
# FIELD RANGE VALIDATION
# =============================================================================

class TestFieldRanges:
    """Validate that exported field values are within expected ranges/enums."""

    def test_corridor_role_enum(self):
        valid_roles = {"owner", "secondary", "contested", "none"}
        ctx = _make_ctx()
        result = export_hints(ctx)
        for hint in result["hints"].values():
            assert hint["corridor_role"] in valid_roles

    def test_competition_bias_range(self):
        ctx = _make_ctx()
        result = export_hints(ctx)
        for hint in result["hints"].values():
            assert hint["competition_bias"] in (-1, 0, 1)

    def test_rebalance_preference_enum(self):
        valid_prefs = {"source", "sink", "neutral"}
        ctx = _make_ctx()
        result = export_hints(ctx)
        for hint in result["hints"].values():
            assert hint["rebalance_preference"] in valid_prefs

    def test_quality_score_range(self):
        scorer = MagicMock()
        score_result = MagicMock()
        score_result.overall_score = 0.82
        score_result.confidence = 0.5
        scorer.calculate_score.return_value = score_result

        ctx = _make_ctx(quality_scorer=scorer)
        result = export_hints(ctx)
        for hint in result["hints"].values():
            if "peer_quality_score" in hint:
                assert 0.0 <= hint["peer_quality_score"] <= 1.0

    def test_traffic_confidence_range(self):
        traffic_mgr = MagicMock()
        traffic_mgr.get_aggregated_profile.return_value = {"confidence": 0.5}

        ctx = _make_ctx(traffic_intel_mgr=traffic_mgr)
        result = export_hints(ctx)
        for hint in result["hints"].values():
            if "traffic_confidence" in hint:
                assert 0.0 <= hint["traffic_confidence"] <= 1.0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
