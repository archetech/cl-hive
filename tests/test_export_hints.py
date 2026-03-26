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
    _derive_channel_open_hints,
)
from modules.fee_coordination import CorridorAssignment, FlowCorridor
from modules.quality_scorer import PeerQualityResult


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

    def test_traffic_confidence_defaults_for_members_when_no_manager(self):
        ctx = _make_ctx(traffic_intel_mgr=None)
        result = export_hints(ctx)
        # Members get default 0.5 confidence so hints aren't dead on consumer side
        assert result["hints"][PEER_A]["traffic_confidence"] == 0.5


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
    volume_routed_sats: int = 0


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

    def test_multiple_channels_use_dominant_volume_regardless_of_order(self):
        mgr = MagicMock()
        sink_metric = _MockYieldMetric(
            peer_id=PEER_A, flow_direction="sink", volume_routed_sats=5_000_000
        )
        source_metric = _MockYieldMetric(
            peer_id=PEER_A, flow_direction="source", volume_routed_sats=1_000_000
        )
        ctx = _make_ctx(yield_metrics_mgr=mgr)

        mgr.get_channel_yield_metrics.return_value = [sink_metric, source_metric]
        prefs = _derive_rebalance_preferences(ctx)
        assert prefs[PEER_A] == "sink"

        mgr.get_channel_yield_metrics.return_value = [source_metric, sink_metric]
        prefs = _derive_rebalance_preferences(ctx)
        assert prefs[PEER_A] == "sink"


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

    def test_traffic_confidence_defaults_for_members_when_no_profile(self):
        traffic_mgr = MagicMock()
        traffic_mgr.get_aggregated_profile.return_value = None

        ctx = _make_ctx(traffic_intel_mgr=traffic_mgr)
        result = export_hints(ctx)
        # Members get default 0.5 confidence even without traffic profile
        assert result["hints"][PEER_A]["traffic_confidence"] == 0.5

    def test_exports_peers_with_only_quality_or_traffic_data(self):
        db = MagicMock()
        db.get_all_members.return_value = []

        scorer = MagicMock()
        quality_result = PeerQualityResult(
            peer_id=PEER_A,
            overall_score=0.81,
            reliability_score=0.8,
            profitability_score=0.8,
            routing_score=0.8,
            consistency_score=0.8,
            confidence=0.7,
            recommendation="good",
            factors={},
        )
        scorer.get_scored_peers.return_value = [quality_result]

        def _score_peer(peer_id, days=90):
            if peer_id == PEER_A:
                return quality_result
            return PeerQualityResult(
                peer_id=peer_id,
                overall_score=0.0,
                reliability_score=0.0,
                profitability_score=0.0,
                routing_score=0.0,
                consistency_score=0.0,
                confidence=0.0,
                recommendation="neutral",
                factors={},
            )

        scorer.calculate_score.side_effect = _score_peer

        traffic_mgr = MagicMock()
        traffic_mgr.get_all_profiles.return_value = [{"peer_id": PEER_B, "confidence": 0.73}]
        traffic_mgr.get_aggregated_profile.side_effect = (
            lambda peer_id: {"confidence": 0.73} if peer_id == PEER_B else None
        )

        ctx = _make_ctx(
            database=db,
            quality_scorer=scorer,
            traffic_intel_mgr=traffic_mgr,
        )
        result = export_hints(ctx)

        assert PEER_A in result["hints"]
        assert result["hints"][PEER_A]["peer_quality_score"] == 0.81
        assert result["hints"][PEER_A]["member"] is False

        assert PEER_B in result["hints"]
        assert result["hints"][PEER_B]["traffic_confidence"] == 0.73
        assert result["hints"][PEER_B]["member"] is False


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

    def test_no_cln_execution_calls(self):
        """Verify export_hints never calls setchannel, close, or fundchannel."""
        ctx = _make_ctx()
        # safe_plugin is the RPC proxy — verify no execution RPCs
        export_hints(ctx)
        if hasattr(ctx.safe_plugin, 'setchannel'):
            ctx.safe_plugin.setchannel.assert_not_called()
        if hasattr(ctx.safe_plugin, 'close'):
            ctx.safe_plugin.close.assert_not_called()
        if hasattr(ctx.safe_plugin, 'call'):
            for call_args in ctx.safe_plugin.call.call_args_list:
                method = call_args[0][0] if call_args[0] else ""
                assert method not in (
                    "fundchannel", "multifundchannel", "setchannel",
                    "close", "revenue-policy", "revenue-rebalance",
                ), f"export_hints called forbidden RPC: {method}"

    def test_no_execution_calls_with_planner(self):
        """Verify hints with planner still never execute."""
        ctx = _make_planner_ctx()
        export_hints(ctx)
        ctx.database.add_member.assert_not_called()
        ctx.database.remove_member.assert_not_called()


class TestFleetFeePriors:
    """Validate fleet_fee_median export from corridor assignments."""

    def test_secondary_members_use_secondary_fee_prior(self):
        corridor_mgr = MagicMock()
        corridor_mgr.get_assignments.return_value = [
            CorridorAssignment(
                corridor=FlowCorridor(
                    source_peer_id="source",
                    destination_peer_id="dest",
                    capable_members=[PEER_A, PEER_B],
                ),
                primary_member=PEER_A,
                secondary_members=[PEER_B],
                primary_fee_ppm=500,
                secondary_fee_ppm=900,
                assignment_reason="test",
                confidence=0.9,
            )
        ]
        fee_coordination_mgr = MagicMock(corridor_mgr=corridor_mgr)

        ctx = _make_ctx(fee_coordination_mgr=fee_coordination_mgr)
        result = export_hints(ctx)

        assert result["hints"][PEER_B]["fleet_fee_median"] == 900


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


# =============================================================================
# CHANNEL-OPEN HINT DERIVATION
# =============================================================================

@dataclass
class _MockUnderservedResult:
    target: str
    public_capacity_sats: int = 500_000_000
    hive_share_pct: float = 0.02
    score: float = 2.0
    quality_score: float = 0.7
    quality_confidence: float = 0.6
    quality_recommendation: str = "good"


@dataclass
class _MockExpansionRec:
    target: str
    recommendation_type: str = "open_channel"
    score: float = 1.5
    reasoning: str = "underserved"
    details: dict = None
    hive_coverage_pct: float = 0.20
    hive_members_count: int = 1
    network_channels: int = 40
    is_bottleneck: bool = False
    competition_level: str = "low"

    def __post_init__(self):
        if self.details is None:
            self.details = {}


@dataclass
class _MockSizeResult:
    recommended_size_sats: int = 5_000_000
    factors: dict = None
    reasoning: str = "default"

    def __post_init__(self):
        if self.factors is None:
            self.factors = {}


def _make_planner_ctx(underserved=None, expansion_rec=None, size_result=None):
    """Create context with planner mocks for channel-open hint tests."""
    planner = MagicMock()
    config = MagicMock()
    cfg_snapshot = MagicMock()
    cfg_snapshot.planner_min_channel_sats = 1_000_000
    cfg_snapshot.planner_default_channel_sats = 5_000_000
    cfg_snapshot.planner_max_channel_sats = 50_000_000
    cfg_snapshot.market_share_cap_pct = 0.20
    config.snapshot.return_value = cfg_snapshot

    if underserved is None:
        underserved = [_MockUnderservedResult(target=PEER_C)]
    planner.get_underserved_targets.return_value = underserved

    if expansion_rec is None:
        expansion_rec = _MockExpansionRec(target=PEER_C)
    planner.get_expansion_recommendation.return_value = expansion_rec

    sizer = MagicMock()
    if size_result is None:
        size_result = _MockSizeResult()
    sizer.calculate_size.return_value = size_result
    planner.channel_sizer = sizer

    return _make_ctx(planner=planner, config=config)


class TestChannelOpenHintDerivation:
    """Test _derive_channel_open_hints helper."""

    def test_open_preference_for_underserved(self):
        ctx = _make_planner_ctx()
        hints = _derive_channel_open_hints(ctx)
        assert PEER_C in hints
        assert hints[PEER_C]["open_preference"] == "open"

    def test_avoid_when_well_covered(self):
        rec = _MockExpansionRec(
            target=PEER_C,
            recommendation_type="no_action",
            hive_coverage_pct=0.60,
        )
        ctx = _make_planner_ctx(expansion_rec=rec)
        hints = _derive_channel_open_hints(ctx)
        assert hints[PEER_C]["open_preference"] == "avoid"

    def test_neutral_when_no_planner(self):
        ctx = _make_ctx(planner=None)
        hints = _derive_channel_open_hints(ctx)
        assert hints == {}

    def test_topology_confidence_range(self):
        ctx = _make_planner_ctx()
        hints = _derive_channel_open_hints(ctx)
        conf = hints[PEER_C]["topology_confidence"]
        assert 0.0 <= conf <= 1.0

    def test_reason_underserved_corridor(self):
        ur = _MockUnderservedResult(target=PEER_C, hive_share_pct=0.01)
        ctx = _make_planner_ctx(underserved=[ur])
        hints = _derive_channel_open_hints(ctx)
        assert hints[PEER_C]["reason"] == "underserved_corridor"

    def test_reason_improve_coverage_for_bottleneck(self):
        rec = _MockExpansionRec(target=PEER_C, is_bottleneck=True)
        ctx = _make_planner_ctx(expansion_rec=rec)
        hints = _derive_channel_open_hints(ctx)
        assert hints[PEER_C]["reason"] == "improve_coverage"

    def test_reason_reduce_overlap(self):
        rec = _MockExpansionRec(
            target=PEER_C,
            recommendation_type="no_action",
            hive_coverage_pct=0.60,
        )
        ctx = _make_planner_ctx(expansion_rec=rec)
        hints = _derive_channel_open_hints(ctx)
        assert hints[PEER_C]["reason"] == "reduce_overlap"

    def test_reason_member_connectivity(self):
        rec = _MockExpansionRec(target=PEER_C, hive_members_count=0)
        ur = _MockUnderservedResult(target=PEER_C, hive_share_pct=0.04)
        ctx = _make_planner_ctx(underserved=[ur], expansion_rec=rec)
        hints = _derive_channel_open_hints(ctx)
        assert hints[PEER_C]["reason"] == "member_connectivity"

    def test_size_bucket_small(self):
        size = _MockSizeResult(recommended_size_sats=1_500_000)
        ctx = _make_planner_ctx(size_result=size)
        hints = _derive_channel_open_hints(ctx)
        assert hints[PEER_C]["suggested_size_bucket"] == "small"

    def test_size_bucket_medium(self):
        size = _MockSizeResult(recommended_size_sats=10_000_000)
        ctx = _make_planner_ctx(size_result=size)
        hints = _derive_channel_open_hints(ctx)
        assert hints[PEER_C]["suggested_size_bucket"] == "medium"

    def test_size_bucket_large(self):
        size = _MockSizeResult(recommended_size_sats=40_000_000)
        ctx = _make_planner_ctx(size_result=size)
        hints = _derive_channel_open_hints(ctx)
        assert hints[PEER_C]["suggested_size_bucket"] == "large"

    def test_low_confidence_downgrades_to_neutral(self):
        ur = _MockUnderservedResult(
            target=PEER_C, score=0.1, quality_confidence=0.05,
        )
        ctx = _make_planner_ctx(underserved=[ur])
        hints = _derive_channel_open_hints(ctx)
        assert hints[PEER_C]["open_preference"] == "neutral"


class TestChannelOpenHintInExport:
    """Test channel_open_hint integration in export_hints response."""

    def test_channel_open_hint_included(self):
        ctx = _make_planner_ctx()
        result = export_hints(ctx)
        # PEER_C should appear in hints with channel_open_hint
        assert PEER_C in result["hints"]
        assert "channel_open_hint" in result["hints"][PEER_C]
        ch = result["hints"][PEER_C]["channel_open_hint"]
        assert ch["open_preference"] in ("open", "neutral", "avoid")

    def test_channel_open_hint_omitted_when_no_topology_data(self):
        ctx = _make_ctx()  # No planner
        result = export_hints(ctx)
        for hint in result["hints"].values():
            assert "channel_open_hint" not in hint

    def test_channel_open_hint_field_enums(self):
        ctx = _make_planner_ctx()
        result = export_hints(ctx)
        for hint in result["hints"].values():
            if "channel_open_hint" in hint:
                ch = hint["channel_open_hint"]
                assert ch["open_preference"] in ("open", "neutral", "avoid")
                assert ch["suggested_size_bucket"] in ("small", "medium", "large")
                assert ch["reason"] in (
                    "underserved_corridor", "improve_coverage",
                    "reduce_overlap", "member_connectivity", "none",
                )
                assert 0.0 <= ch["topology_confidence"] <= 1.0

    def test_no_side_effects_with_channel_hints(self):
        ctx = _make_planner_ctx()
        export_hints(ctx)
        # Planner should only be called for reads
        ctx.database.add_member.assert_not_called()
        ctx.database.remove_member.assert_not_called()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
