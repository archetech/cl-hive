"""
RPC Command Handlers for cl-hive

This module contains the implementation logic for hive-* RPC commands.
The actual @plugin.method() decorators remain in cl-hive.py, which creates
thin wrappers that call these handler functions.

Design Pattern:
    - Each handler receives a HiveContext with all dependencies
    - Handlers are pure functions that can be easily tested
    - Permission checks are done via check_permission() helper
"""

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional



@dataclass
class HiveContext:
    """
    Context object holding all dependencies for RPC command handlers.

    This bundles the global state that commands need access to,
    making dependencies explicit and handlers testable.
    """
    database: Any  # HiveDatabase
    config: Any    # HiveConfig
    safe_plugin: Any  # ThreadSafePluginProxy
    our_pubkey: str
    planner: Any = None  # Planner
    quality_scorer: Any = None  # PeerQualityScorer
    bridge: Any = None  # Bridge
    intent_mgr: Any = None  # IntentManager
    membership_mgr: Any = None  # MembershipManager
    contribution_mgr: Any = None  # ContributionManager
    yield_metrics_mgr: Any = None  # YieldMetricsManager (Phase 1 - Metrics)
    liquidity_coordinator: Any = None  # LiquidityCoordinator (for competition detection)
    fee_coordination_mgr: Any = None  # FeeCoordinationManager (Phase 2 - Fee Coordination)
    rationalization_mgr: Any = None  # RationalizationManager (Channel Rationalization)
    strategic_positioning_mgr: Any = None  # StrategicPositioningManager (Phase 5 - Strategic Positioning)
    traffic_intel_mgr: Any = None  # TrafficIntelligenceManager (Phase 14 - Traffic Intelligence)
    peer_reputation_mgr: Any = None  # PeerReputationManager (fleet-aggregated peer quality)
    our_id: str = ""  # Our node pubkey (alias for our_pubkey for consistency)
    signing_backend: str = "unknown"
    log: Callable[[str, str], None] = None  # Logger function: (msg, level) -> None


def check_permission(ctx: HiveContext, required_tier: str = 'member') -> Optional[Dict[str, Any]]:
    """
    Check if the local node is a hive member.

    All members have equal privileges in the single-role model.

    Returns:
        None if permission granted, or error dict if denied.
    """
    if not ctx.our_pubkey or not ctx.database:
        return {"error": "Not initialized"}

    member = ctx.database.get_member(ctx.our_pubkey)
    if not member:
        return {"error": "Not a Hive member", "required_tier": required_tier}

    return None  # Permission granted


def _member_uptime_pct(ctx: HiveContext, peer_id: str, member_row: Dict[str, Any]) -> float:
    """Return live member uptime as a 0-100 percentage when possible."""
    if ctx.membership_mgr and ctx.database and peer_id and ctx.database.get_presence(peer_id):
        try:
            return round(ctx.membership_mgr.calculate_uptime(peer_id), 2)
        except Exception:
            pass

    uptime_raw = member_row.get("uptime_pct", 0.0)
    if uptime_raw <= 1.0:
        uptime_raw = round(uptime_raw * 100, 2)
    return uptime_raw


# =============================================================================
# VPN COMMANDS
# =============================================================================



# =============================================================================
# STATUS/CONFIG COMMANDS
# =============================================================================

def status(ctx: HiveContext) -> Dict[str, Any]:
    """
    Get current Hive status and membership info.

    Returns:
        Dict with hive state and member count.
    """
    if not ctx.database:
        return {"error": "Hive not initialized"}

    members = ctx.database.get_all_members()

    # Get our own membership status (used by cl-revenue-ops to detect hive mode)
    our_membership = {"tier": None, "joined_at": None}
    if ctx.our_pubkey:
        our_member = ctx.database.get_member(ctx.our_pubkey)
        if our_member:
            uptime_raw = _member_uptime_pct(ctx, ctx.our_pubkey, our_member)
            contribution_ratio = our_member.get("contribution_ratio", 0.0)
            # Enrich with live contribution ratio if available (Issue #59)
            if ctx.membership_mgr:
                contribution_ratio = ctx.membership_mgr.calculate_contribution_ratio(ctx.our_pubkey)
            our_membership = {
                "tier": our_member.get("tier"),
                "joined_at": our_member.get("joined_at"),
                "pubkey": ctx.our_pubkey,
                "uptime_pct": uptime_raw,
                "contribution_ratio": contribution_ratio,
            }

    return {
        "status": "active" if members else "no_members",
        "governance": "recommendation_only",
        "membership": our_membership,  # Our own membership for cl-revenue-ops detection
        "members": {
            "total": len(members),
        },
        "limits": {
            "max_members": ctx.config.max_members if ctx.config else 50,
            "market_share_cap": ctx.config.market_share_cap_pct if ctx.config else 0.20,
        },
        "signing_backend": str(ctx.signing_backend or "unknown"),
        "version": "2.2.6",
    }


def get_config(ctx: HiveContext) -> Dict[str, Any]:
    """
    Get current Hive configuration values.

    Shows all config options and their current values. Useful for verifying
    hot-reload changes made via `lightning-cli setconfig`.

    Returns:
        Dict with all current config values and metadata.
    """
    if not ctx.config:
        return {"error": "Hive not initialized"}

    return {
        "config_version": ctx.config._version,
        "hot_reload_enabled": True,
        "immutable": {
            "db_path": ctx.config.db_path,
        },
        "governance": "recommendation_only",
        "membership": {
            "membership_enabled": ctx.config.membership_enabled,
            "auto_join_enabled": ctx.config.auto_join_enabled,
            "max_members": ctx.config.max_members,
        },
        "protocol": {
            "market_share_cap_pct": ctx.config.market_share_cap_pct,
            "intent_hold_seconds": ctx.config.intent_hold_seconds,
            "intent_expire_seconds": ctx.config.intent_expire_seconds,
            "gossip_threshold_pct": ctx.config.gossip_threshold_pct,
            "heartbeat_interval": ctx.config.heartbeat_interval,
        },
        "planner": {
            "planner_interval": ctx.config.planner_interval,
            "planner_min_channel_sats": ctx.config.planner_min_channel_sats,
            "planner_max_channel_sats": ctx.config.planner_max_channel_sats,
            "planner_default_channel_sats": ctx.config.planner_default_channel_sats,
        },
        "vpn": {"enabled": False},  # VPN transport removed
    }


def members(ctx: HiveContext) -> Dict[str, Any]:
    """
    List all Hive members with their tier and stats.

    Returns:
        Dict with list of all members and their details.
    """
    if not ctx.database:
        return {"error": "Hive not initialized"}

    all_members = ctx.database.get_all_members()

    # Enrich with live contribution ratio from ledger (Issue #59)
    if ctx.membership_mgr:
        for m in all_members:
            peer_id = m.get("peer_id")
            if peer_id:
                m["contribution_ratio"] = ctx.membership_mgr.calculate_contribution_ratio(peer_id)
                m["uptime_pct"] = _member_uptime_pct(ctx, peer_id, m)
    else:
        for m in all_members:
            peer_id = m.get("peer_id")
            if peer_id:
                m["uptime_pct"] = _member_uptime_pct(ctx, peer_id, m)

    return {
        "count": len(all_members),
        "members": all_members,
    }


# =============================================================================
# ACTION MANAGEMENT COMMANDS
# =============================================================================



# =============================================================================
# Phase 4: Topology, Planner, and Query Commands
# =============================================================================

def reinit_bridge(ctx: HiveContext) -> Dict[str, Any]:
    """
    Re-attempt bridge initialization if it failed at startup.

    Permission: Member only
    """
    perm_error = check_permission(ctx, 'member')
    if perm_error:
        return perm_error

    if not ctx.bridge:
        return {"error": "Bridge module not initialized"}

    # Import BridgeStatus here to avoid circular imports
    from modules.bridge import BridgeStatus

    previous_status = ctx.bridge.status.value
    new_status = ctx.bridge.reinitialize()

    return {
        "previous_status": previous_status,
        "new_status": new_status.value,
        "revenue_ops_version": ctx.bridge._revenue_ops_version,
        "message": (
            "Bridge enabled successfully" if new_status == BridgeStatus.ENABLED
            else "Bridge still disabled - check cl-revenue-ops installation"
        )
    }


def topology(ctx: HiveContext) -> Dict[str, Any]:
    """
    Get current topology analysis from the Planner.

    Returns:
        Dict with saturated targets, planner stats, and config.
    """
    if not ctx.planner:
        return {"error": "Planner not initialized"}
    if not ctx.config:
        return {"error": "Config not initialized"}

    # Take config snapshot
    cfg = ctx.config.snapshot()

    # Refresh network cache before analysis
    ctx.planner._refresh_network_cache(force=True)

    # Get saturated targets
    saturated = ctx.planner.get_saturated_targets(cfg)
    saturated_list = [
        {
            "target": r.target[:16] + "...",
            "target_full": r.target,
            "hive_capacity_sats": r.hive_capacity_sats,
            "public_capacity_sats": r.public_capacity_sats,
            "hive_share_pct": round(r.hive_share_pct * 100, 2),
        }
        for r in saturated
    ]

    # Get planner stats
    stats = ctx.planner.get_planner_stats()

    return {
        "saturated_targets": saturated_list,
        "saturated_count": len(saturated_list),
        "ignored_peers": stats.get("ignored_peers", []),
        "ignored_count": stats.get("ignored_peers_count", 0),
        "network_cache_size": stats.get("network_cache_size", 0),
        "network_cache_age_seconds": stats.get("network_cache_age_seconds", 0),
        "config": {
            "market_share_cap_pct": cfg.market_share_cap_pct,
            "planner_interval_seconds": cfg.planner_interval,
            "governance": "recommendation_only",
        }
    }


def planner_log(ctx: HiveContext, limit: int = 50) -> Dict[str, Any]:
    """
    Get recent Planner decision logs.

    Args:
        limit: Maximum number of log entries to return (default: 50)

    Returns:
        Dict with log entries and count.
    """
    if not ctx.database:
        return {"error": "Database not initialized"}

    # Bound limit to prevent excessive queries
    limit = min(max(1, limit), 500)

    logs = ctx.database.get_planner_logs(limit=limit)
    return {
        "count": len(logs),
        "limit": limit,
        "logs": logs,
    }


def expansion_recommendations(ctx: HiveContext, limit: int = 10) -> Dict[str, Any]:
    """
    Get expansion recommendations with cooperation module intelligence.

    Returns detailed recommendations integrating:
    - Hive coverage diversity (% of members with channels)
    - Network competition (peer channel count)
    - Bottleneck detection (from liquidity_coordinator)
    - Channel rationalization recommendations

    Args:
        limit: Maximum number of recommendations to return (default: 10)

    Returns:
        Dict with expansion recommendations and coverage summary.
    """
    if not ctx.planner:
        return {"error": "Planner not initialized"}
    if not ctx.config:
        return {"error": "Config not initialized"}

    # Take config snapshot
    cfg = ctx.config.snapshot()

    # Refresh network cache
    ctx.planner._refresh_network_cache(force=True)

    # Get underserved targets (already uses cooperation modules)
    underserved = ctx.planner.get_underserved_targets(cfg)

    # Bound limit
    limit = min(max(1, limit), 50)
    underserved = underserved[:limit]

    # Build detailed recommendations
    recommendations = []
    coverage_stats = {
        "well_covered_peers": 0,
        "partially_covered_peers": 0,
        "uncovered_peers": 0,
        "bottleneck_peers": 0
    }

    for target_result in underserved:
        # Get full expansion recommendation
        rec = ctx.planner.get_expansion_recommendation(target_result.target, cfg)

        # Update coverage stats
        if rec.hive_coverage_pct >= 0.60:
            coverage_stats["well_covered_peers"] += 1
        elif rec.hive_coverage_pct >= 0.20:
            coverage_stats["partially_covered_peers"] += 1
        else:
            coverage_stats["uncovered_peers"] += 1

        if rec.is_bottleneck:
            coverage_stats["bottleneck_peers"] += 1

        # Get node alias if available
        alias = target_result.target[:12] + "..."
        try:
            if ctx.safe_plugin:
                node_info = ctx.safe_plugin.rpc.listnodes(id=target_result.target)
                nodes = node_info.get("nodes", [])
                if nodes and nodes[0].get("alias"):
                    alias = nodes[0]["alias"]
        except Exception:
            pass

        recommendations.append({
            "target": target_result.target[:16] + "...",
            "target_full": target_result.target,
            "alias": alias,
            "recommendation": rec.recommendation_type,
            "score": round(rec.score, 4),
            "hive_coverage": f"{rec.hive_members_count}/{len(ctx.planner._get_hive_members())} members ({rec.hive_coverage_pct:.0%})",
            "hive_coverage_pct": round(rec.hive_coverage_pct * 100, 1),
            "hive_members_count": rec.hive_members_count,
            "competition_level": rec.competition_level,
            "network_channels": rec.network_channels,
            "is_bottleneck": rec.is_bottleneck,
            "reasoning": rec.reasoning,
            "details": rec.details,
            "quality_score": round(target_result.quality_score, 3),
            "quality_recommendation": target_result.quality_recommendation
        })

    return {
        "recommendations": recommendations,
        "count": len(recommendations),
        "coverage_summary": coverage_stats,
        "cooperation_modules": {
            "liquidity_coordinator": ctx.planner.liquidity_coordinator is not None,
            "health_aggregator": ctx.planner.health_aggregator is not None
        }
    }


def intent_status(ctx: HiveContext) -> Dict[str, Any]:
    """
    Get current intent status (local and remote intents).

    Returns:
        Dict with pending intents and stats.
    """
    if not ctx.planner or not ctx.planner.intent_manager:
        return {"error": "Intent manager not initialized"}

    intent_mgr = ctx.planner.intent_manager
    stats = intent_mgr.get_intent_stats()

    # Get pending local intents from DB
    pending = ctx.database.get_pending_intents() if ctx.database else []

    # Get remote intents from cache
    remote = intent_mgr.get_remote_intents()

    return {
        "local_pending": len(pending),
        "local_intents": pending,
        "remote_cached": len(remote),
        "remote_intents": [r.to_dict() for r in remote],
        "stats": stats
    }


def contribution(ctx: HiveContext, peer_id: str = None) -> Dict[str, Any]:
    """
    View contribution stats for a peer or self.

    Args:
        peer_id: Optional peer to view (defaults to self)

    Returns:
        Dict with contribution statistics.
    """
    if not ctx.contribution_mgr or not ctx.database:
        return {"error": "Contribution tracking not available"}

    target_id = peer_id or ctx.our_pubkey
    if not target_id:
        return {"error": "No peer specified and our_pubkey not available"}

    # Get contribution stats
    stats = ctx.contribution_mgr.get_contribution_stats(target_id)

    # Get member info
    member = ctx.database.get_member(target_id)

    # Get leech status
    leech_status = ctx.contribution_mgr.check_leech_status(target_id)

    result = {
        "peer_id": target_id,
        "forwarded_msat": stats["forwarded"],
        "received_msat": stats["received"],
        "contribution_ratio": round(stats["ratio"], 4),
        "is_leech": leech_status["is_leech"],
    }

    if member:
        result["tier"] = member.get("tier")
        uptime_raw = member.get("uptime_pct", 0.0)
        # Normalize to 0-100 scale (DB stores 0.0-1.0)
        if uptime_raw is not None and uptime_raw <= 1.0:
            uptime_raw = round(uptime_raw * 100, 2)
        result["uptime_pct"] = uptime_raw

    return result




# =============================================================================
# YIELD METRICS COMMANDS (Phase 1 - Metrics & Measurement)
# =============================================================================

def yield_metrics(ctx: HiveContext, channel_id: str = None,
                  period_days: int = 30) -> Dict[str, Any]:
    """
    Get yield metrics for channels.

    Shows ROI, capital efficiency, turn rate, and flow characteristics.

    Args:
        ctx: HiveContext
        channel_id: Optional specific channel (None for all)
        period_days: Analysis period in days (default: 30)

    Returns:
        Dict with channel yield metrics.
    """
    if not ctx.yield_metrics_mgr:
        return {"error": "Yield metrics manager not initialized"}

    try:
        metrics = ctx.yield_metrics_mgr.get_channel_yield_metrics(
            channel_id=channel_id,
            period_days=period_days
        )

        return {
            "status": "ok",
            "period_days": period_days,
            "channel_count": len(metrics),
            "channels": [m.to_dict() for m in metrics]
        }
    except Exception as e:
        return {"error": f"Failed to get yield metrics: {e}"}


def yield_summary(ctx: HiveContext, period_days: int = 30) -> Dict[str, Any]:
    """
    Get aggregated yield summary for the fleet.

    Shows total revenue, ROI, and channel health distribution.

    Args:
        ctx: HiveContext
        period_days: Analysis period in days (default: 30)

    Returns:
        Dict with fleet yield summary.
    """
    if not ctx.yield_metrics_mgr:
        return {"error": "Yield metrics manager not initialized"}

    try:
        summary = ctx.yield_metrics_mgr.get_fleet_yield_summary(
            period_days=period_days
        )

        return {
            "status": "ok",
            **summary.to_dict()
        }
    except Exception as e:
        return {"error": f"Failed to get yield summary: {e}"}


def velocity_prediction(ctx: HiveContext, channel_id: str,
                        hours: int = 24) -> Dict[str, Any]:
    """
    Predict channel balance at future time based on flow velocity.

    Shows depletion/saturation risk and recommended actions.

    Args:
        ctx: HiveContext
        channel_id: Channel to predict
        hours: Hours into the future to predict (default: 24)

    Returns:
        Dict with velocity prediction.
    """
    if not ctx.yield_metrics_mgr:
        return {"error": "Yield metrics manager not initialized"}

    if not channel_id:
        return {"error": "channel_id is required"}

    try:
        prediction = ctx.yield_metrics_mgr.predict_channel_state(
            channel_id=channel_id,
            hours=hours
        )

        if not prediction:
            return {"error": "Insufficient data for prediction"}

        return {
            "status": "ok",
            **prediction.to_dict()
        }
    except Exception as e:
        return {"error": f"Failed to predict channel state: {e}"}


def critical_velocity_channels(ctx: HiveContext,
                               threshold_hours: int = 24) -> Dict[str, Any]:
    """
    Get channels with critical velocity (depleting/filling rapidly).

    These channels need urgent attention (fee changes or rebalancing).

    Args:
        ctx: HiveContext
        threshold_hours: Alert if depletion/saturation within this time

    Returns:
        Dict with critical velocity channels.
    """
    if not ctx.yield_metrics_mgr:
        return {"error": "Yield metrics manager not initialized"}

    try:
        critical = ctx.yield_metrics_mgr.get_critical_velocity_channels(
            threshold_hours=threshold_hours
        )

        return {
            "status": "ok",
            "threshold_hours": threshold_hours,
            "critical_count": len(critical),
            "channels": [p.to_dict() for p in critical]
        }
    except Exception as e:
        return {"error": f"Failed to get critical velocity channels: {e}"}




# =============================================================================
# PHASE 2: FEE COORDINATION RPC COMMANDS
# =============================================================================

def fee_recommendation(
    ctx: HiveContext,
    channel_id: str,
    current_fee: int = 500,
    local_balance_pct: float = 0.5,
    source: str = None,
    destination: str = None
) -> Dict[str, Any]:
    """
    Get coordinated fee recommendation for a channel.

    Combines corridor assignment, centrality, and size-aware signals.

    Args:
        ctx: HiveContext
        channel_id: Channel ID to get recommendation for
        current_fee: Current fee in ppm (default: 500)
        local_balance_pct: Current local balance percentage (default: 0.5)
        source: Source peer hint for corridor lookup
        destination: Destination peer hint for corridor lookup

    Returns:
        Dict with fee recommendation and reasoning.
    """
    if not ctx.fee_coordination_mgr:
        return {"error": "Fee coordination not initialized"}

    try:
        # Get peer_id from channel if possible
        peer_id = ""
        if ctx.safe_plugin:
            try:
                channels = ctx.safe_plugin.rpc.listpeerchannels()
                for ch in channels.get("channels", []):
                    if ch.get("short_channel_id") == channel_id:
                        peer_id = ch.get("peer_id", "")
                        break
            except Exception:
                pass

        recommendation = ctx.fee_coordination_mgr.get_fee_recommendation(
            channel_id=channel_id,
            peer_id=peer_id,
            current_fee=current_fee,
            local_balance_pct=local_balance_pct,
            source_hint=source,
            destination_hint=destination
        )

        return recommendation.to_dict()

    except Exception as e:
        return {"error": f"Failed to get fee recommendation: {e}"}


def egress_desaturation_bias(
    ctx: HiveContext,
    channel_id: str = None,
    peer_id: str = None
) -> Dict[str, Any]:
    """
    Report whether a local non-hive exit should be surcharged to favor a
    saturated local hive-member egress.

    Args:
        ctx: HiveContext
        channel_id: Optional channel ID to inspect
        peer_id: Optional peer ID to inspect

    Returns:
        Structured bias payload with match status and surcharge recommendation.
    """
    if not ctx.fee_coordination_mgr:
        return {"error": "Fee coordination not initialized"}

    if not channel_id and not peer_id:
        return {"error": "channel_id or peer_id is required"}

    try:
        return ctx.fee_coordination_mgr.get_egress_desaturation_bias(
            channel_id=channel_id,
            peer_id=peer_id,
        )
    except Exception as e:
        return {"error": f"Failed to get egress desaturation bias: {e}"}


def corridor_assignments(ctx: HiveContext, force_refresh: bool = False) -> Dict[str, Any]:
    """
    Get flow corridor assignments for the fleet.

    Shows which member is primary for each (source, destination) pair.

    Args:
        ctx: HiveContext
        force_refresh: Force refresh of cached assignments

    Returns:
        Dict with corridor assignments and statistics.
    """
    if not ctx.fee_coordination_mgr:
        return {"error": "Fee coordination not initialized"}

    try:
        assignments = ctx.fee_coordination_mgr.corridor_mgr.get_assignments(
            force_refresh=force_refresh
        )

        # Categorize by competition level
        by_level = {
            "none": [], "low": [], "medium": [], "high": []
        }
        for a in assignments:
            level = a.corridor.competition_level
            if level in by_level:
                by_level[level].append(a.to_dict())

        return {
            "total_corridors": len(assignments),
            "by_competition_level": {
                level: len(items) for level, items in by_level.items()
            },
            "assignments": [a.to_dict() for a in assignments],
            "our_primary_corridors": [
                a.to_dict() for a in assignments
                if a.primary_member == ctx.our_pubkey
            ]
        }

    except Exception as e:
        return {"error": f"Failed to get corridor assignments: {e}"}




def get_routing_intelligence(ctx: HiveContext, scid: str = None) -> Dict[str, Any]:
    """
    Get routing intelligence based on corridor assignments.

    Args:
        ctx: HiveContext
        scid: Optional specific channel short_channel_id (unused, kept for compat).

    Returns:
        Dict with corridor assignment data.
    """
    if not ctx.fee_coordination_mgr:
        return {"error": "Fee coordination not initialized"}

    try:
        assignments = ctx.fee_coordination_mgr.corridor_mgr.get_assignments()
        now = time.time()

        return {
            "corridor_assignments": len(assignments),
            "assignments": [a.to_dict() for a in assignments[:20]],
            "timestamp": int(now),
        }

    except Exception as e:
        return {"error": f"Failed to get routing intelligence: {e}"}


def fee_coordination_status(ctx: HiveContext) -> Dict[str, Any]:
    """
    Get overall fee coordination status.

    Comprehensive view of all Phase 2 coordination systems.

    Args:
        ctx: HiveContext

    Returns:
        Dict with fee coordination status.
    """
    if not ctx.fee_coordination_mgr:
        return {"error": "Fee coordination not initialized"}

    try:
        return ctx.fee_coordination_mgr.get_coordination_status()

    except Exception as e:
        return {"error": f"Failed to get coordination status: {e}"}


# =============================================================================
# YIELD OPTIMIZATION PHASE 3: COST REDUCTION
# =============================================================================
# =============================================================================
# CHANNEL RATIONALIZATION COMMANDS
# =============================================================================

def coverage_analysis(
    ctx: HiveContext,
    peer_id: str = None
) -> Dict[str, Any]:
    """
    Analyze fleet coverage for redundant channels.

    Shows which fleet members have channels to the same peers
    and determines ownership based on routing activity.

    Args:
        ctx: HiveContext
        peer_id: Specific peer to analyze, or None for all redundant peers

    Returns:
        Dict with coverage analysis showing ownership and redundancy.
    """
    if not ctx.rationalization_mgr:
        return {"error": "Rationalization not initialized"}

    try:
        return ctx.rationalization_mgr.analyze_coverage(peer_id=peer_id)

    except Exception as e:
        return {"error": f"Failed to analyze coverage: {e}"}


def close_recommendations(
    ctx: HiveContext,
    our_node_only: bool = False
) -> Dict[str, Any]:
    """
    Get channel close recommendations for underperforming redundant channels.

    Uses corridor assignments to determine which member "owns" each peer
    relationship. Recommends closes for members with redundant channels.

    Args:
        ctx: HiveContext
        our_node_only: If True, only return recommendations for our node

    Returns:
        Dict with close recommendations sorted by urgency.
    """
    if not ctx.rationalization_mgr:
        return {"error": "Rationalization not initialized"}

    try:
        recommendations = ctx.rationalization_mgr.get_close_recommendations(
            for_our_node_only=our_node_only
        )

        # Summarize
        by_urgency = {"high": 0, "medium": 0, "low": 0}
        total_freed = 0
        for rec in recommendations:
            by_urgency[rec.get("urgency", "low")] += 1
            total_freed += rec.get("freed_capital_sats", 0)

        return {
            "recommendations": recommendations,
            "count": len(recommendations),
            "by_urgency": by_urgency,
            "potential_freed_capital_sats": total_freed,
            "potential_freed_btc": round(total_freed / 100_000_000, 4)
        }

    except Exception as e:
        return {"error": f"Failed to get close recommendations: {e}"}




def rationalization_summary(ctx: HiveContext) -> Dict[str, Any]:
    """
    Get summary of channel rationalization analysis.

    Shows fleet coverage health: well-owned peers, contested peers,
    orphan peers, and recommended closes.

    Args:
        ctx: HiveContext

    Returns:
        Dict with rationalization summary.
    """
    if not ctx.rationalization_mgr:
        return {"error": "Rationalization not initialized"}

    try:
        return ctx.rationalization_mgr.get_summary()

    except Exception as e:
        return {"error": f"Failed to get rationalization summary: {e}"}


def rationalization_status(ctx: HiveContext) -> Dict[str, Any]:
    """
    Get channel rationalization status.

    Shows overall health metrics and configuration thresholds.

    Args:
        ctx: HiveContext

    Returns:
        Dict with rationalization status.
    """
    if not ctx.rationalization_mgr:
        return {"error": "Rationalization not initialized"}

    try:
        return ctx.rationalization_mgr.get_status()

    except Exception as e:
        return {"error": f"Failed to get rationalization status: {e}"}


# =============================================================================
# YIELD OPTIMIZATION PHASE 5: STRATEGIC POSITIONING
# =============================================================================
# Position fleet on critical network paths:
# - RouteValueAnalyzer: High-value corridors with limited competition
# - FleetPositioningStrategy: Coordinated channel opens (max 2 per target)

def valuable_corridors(
    ctx: HiveContext,
    min_score: float = 0.05
) -> Dict[str, Any]:
    """
    Get high-value routing corridors for strategic positioning.

    Corridors are scored by: Volume × Margin × (1/Competition)
    Higher scores indicate better positioning opportunities.

    Args:
        ctx: HiveContext
        min_score: Minimum value score to include (default: 0.05)

    Returns:
        Dict with valuable corridors sorted by score.
    """
    if not ctx.strategic_positioning_mgr:
        return {"error": "Strategic positioning not initialized"}

    try:
        corridors = ctx.strategic_positioning_mgr.get_valuable_corridors(
            min_score=min_score
        )

        # Categorize by value tier
        by_tier = {"high": [], "medium": [], "low": []}
        for c in corridors:
            tier = c.get("value_tier", "low")
            if tier in by_tier:
                by_tier[tier].append(c)

        return {
            "corridors": corridors,
            "total_count": len(corridors),
            "by_value_tier": {
                tier: len(items) for tier, items in by_tier.items()
            },
            "min_score_filter": min_score
        }

    except Exception as e:
        return {"error": f"Failed to get valuable corridors: {e}"}


def exchange_coverage(ctx: HiveContext) -> Dict[str, Any]:
    """
    Get priority exchange connectivity status.

    Shows which major Lightning exchanges the fleet is connected to
    and which still need channels.

    Args:
        ctx: HiveContext

    Returns:
        Dict with exchange coverage analysis.
    """
    if not ctx.strategic_positioning_mgr:
        return {"error": "Strategic positioning not initialized"}

    try:
        return ctx.strategic_positioning_mgr.get_exchange_coverage()

    except Exception as e:
        return {"error": f"Failed to get exchange coverage: {e}"}


def positioning_recommendations(
    ctx: HiveContext,
    count: int = 5
) -> Dict[str, Any]:
    """
    Get channel open recommendations for strategic positioning.

    Recommends where to open channels for maximum routing value,
    considering existing fleet coverage and competition.

    Args:
        ctx: HiveContext
        count: Number of recommendations to return (default: 5)

    Returns:
        Dict with positioning recommendations sorted by priority.
    """
    if not ctx.strategic_positioning_mgr:
        return {"error": "Strategic positioning not initialized"}

    try:
        recommendations = ctx.strategic_positioning_mgr.get_positioning_recommendations(
            count=count
        )

        # Summarize by priority tier
        by_tier = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for rec in recommendations:
            tier = rec.get("priority_tier", "low")
            if tier in by_tier:
                by_tier[tier] += 1

        return {
            "recommendations": recommendations,
            "count": len(recommendations),
            "by_priority": by_tier
        }

    except Exception as e:
        return {"error": f"Failed to get positioning recommendations: {e}"}




def positioning_summary(ctx: HiveContext) -> Dict[str, Any]:
    """
    Get summary of strategic positioning analysis.

    Shows high-value corridors, exchange coverage, and recommended actions.

    Args:
        ctx: HiveContext

    Returns:
        Dict with positioning summary.
    """
    if not ctx.strategic_positioning_mgr:
        return {"error": "Strategic positioning not initialized"}

    try:
        return ctx.strategic_positioning_mgr.get_positioning_summary()

    except Exception as e:
        return {"error": f"Failed to get positioning summary: {e}"}


def positioning_status(ctx: HiveContext) -> Dict[str, Any]:
    """
    Get strategic positioning status.

    Shows overall status, thresholds, and priority exchanges.

    Args:
        ctx: HiveContext

    Returns:
        Dict with positioning status.
    """
    if not ctx.strategic_positioning_mgr:
        return {"error": "Strategic positioning not initialized"}

    try:
        return ctx.strategic_positioning_mgr.get_status()

    except Exception as e:
        return {"error": f"Failed to get positioning status: {e}"}


# =============================================================================
# NETWORK METRICS COMMANDS
# =============================================================================

def network_metrics(ctx: HiveContext, member_id: str = None) -> Dict[str, Any]:
    """
    Get network position metrics for hive members.

    These metrics include centrality, unique peers, bridge score, hive centrality,
    and rebalance hub scores. Used for fair share calculations and routing optimization.

    Args:
        ctx: HiveContext
        member_id: Specific member pubkey (omit for all members)

    Returns:
        Dict with network metrics for the specified member(s).
    """
    from . import network_metrics as nm

    calculator = nm.get_calculator()
    if not calculator:
        return {"error": "Network metrics calculator not initialized"}

    try:
        if member_id:
            metrics = calculator.get_member_metrics(member_id)
            if not metrics:
                return {"error": f"No metrics available for member {member_id[:16]}..."}
            return {"metrics": metrics.to_dict()}
        else:
            all_metrics = calculator.get_all_metrics()
            members = [m.to_dict() for m in all_metrics.values()]
            # Sort by rebalance hub score for consistency
            members.sort(key=lambda x: x.get("rebalance_hub_score", 0), reverse=True)
            return {
                "member_count": len(members),
                "members": members
            }

    except Exception as e:
        return {"error": f"Failed to get network metrics: {e}"}


def rebalance_hubs(
    ctx: HiveContext,
    top_n: int = 3,
    exclude_members: List[str] = None
) -> Dict[str, Any]:
    """
    Get the best zero-fee rebalance intermediaries in the hive.

    Nodes with high hive centrality make good rebalance hubs because they
    have channels to many other hive members. Routing rebalances through
    these nodes is free (0 ppm fees within hive).

    Args:
        ctx: HiveContext
        top_n: Number of top hubs to return (default: 3)
        exclude_members: Member IDs to exclude (e.g., source/dest of rebalance)

    Returns:
        Dict with ranked list of best rebalance hubs.
    """
    from . import network_metrics as nm

    calculator = nm.get_calculator()
    if not calculator:
        return {"error": "Network metrics calculator not initialized"}

    try:
        hubs = calculator.get_rebalance_hubs(top_n=top_n, exclude_members=exclude_members)
        hub_list = []
        for hub in hubs:
            hub_dict = hub.to_dict()
            # Get alias if available from state manager
            if getattr(ctx, 'state_manager', None):
                state = ctx.state_manager.get_peer_state(hub.member_id)
                if state and hasattr(state, 'alias') and state.alias:
                    hub_dict['alias'] = state.alias
            hub_list.append(hub_dict)

        return {
            "count": len(hub_list),
            "hubs": hub_list
        }

    except Exception as e:
        return {"error": f"Failed to get rebalance hubs: {e}"}




def fleet_health(ctx: HiveContext) -> Dict[str, Any]:
    """
    Get overall fleet connectivity health metrics.

    Returns aggregated metrics showing how well-connected the fleet is
    internally. Includes health score (0-100) and letter grade.

    Args:
        ctx: HiveContext

    Returns:
        Dict with fleet health metrics.
    """
    from . import network_metrics as nm

    calculator = nm.get_calculator()
    if not calculator:
        return {"error": "Network metrics calculator not initialized"}

    try:
        return calculator.get_fleet_health()

    except Exception as e:
        return {"error": f"Failed to get fleet health: {e}"}


def connectivity_alerts(ctx: HiveContext) -> Dict[str, Any]:
    """
    Check for fleet connectivity issues that need attention.

    Returns alerts for isolated members, disconnected members,
    low hub availability, and other connectivity problems.

    Args:
        ctx: HiveContext

    Returns:
        Dict with list of alerts sorted by severity.
    """
    from . import network_metrics as nm

    calculator = nm.get_calculator()
    if not calculator:
        return {"error": "Network metrics calculator not initialized"}

    try:
        alerts = calculator.check_connectivity_alerts()
        critical = sum(1 for a in alerts if a.get("severity") == "critical")
        warnings = sum(1 for a in alerts if a.get("severity") == "warning")
        info = sum(1 for a in alerts if a.get("severity") == "info")

        return {
            "alert_count": len(alerts),
            "critical_count": critical,
            "warning_count": warnings,
            "info_count": info,
            "alerts": alerts
        }

    except Exception as e:
        return {"error": f"Failed to check connectivity: {e}"}


def member_connectivity(ctx: HiveContext, member_id: str) -> Dict[str, Any]:
    """
    Get detailed connectivity report for a specific member.

    Shows how well-connected this member is within the fleet,
    comparison to fleet average, and recommendations for improvement.

    Args:
        ctx: HiveContext
        member_id: Member's public key

    Returns:
        Dict with connectivity details and recommendations.
    """
    from . import network_metrics as nm

    calculator = nm.get_calculator()
    if not calculator:
        return {"error": "Network metrics calculator not initialized"}

    try:
        return calculator.get_member_connectivity_report(member_id)

    except Exception as e:
        return {"error": f"Failed to get member connectivity: {e}"}




def get_peer_quality(ctx: HiveContext, peer_id: str = None) -> Dict[str, Any]:
    """
    Get peer quality assessments from the hive's collective intelligence.

    Returns quality ratings based on uptime, routing success, fee stability,
    and fleet-wide reputation. Used by cl-revenue-ops to adjust optimization
    intensity - don't invest heavily in bad peers.

    Args:
        ctx: HiveContext
        peer_id: Optional specific peer ID. If None, returns all peers.

    Returns:
        Dict with peer quality assessments:
        {
            "peers": {
                "03abc...": {
                    "quality": "good",
                    "quality_score": 0.85,
                    "reasons": ["high_uptime", "good_routing_partner"],
                    "recommendation": "expand",
                    "last_assessed": 1707600000
                }
            }
        }
    """
    if not ctx.quality_scorer:
        return {"error": "Quality scorer not initialized"}

    try:
        peers_data = {}

        # Get peers to assess
        peer_list = []
        if peer_id:
            peer_list = [peer_id]
        elif ctx.safe_plugin:
            # Get all connected peers
            channels = ctx.safe_plugin.rpc.listpeerchannels()
            peer_list = list(set(
                ch.get('peer_id') for ch in channels.get('channels', [])
                if ch.get('peer_id')
            ))

        for pid in peer_list:
            # Get quality score from quality_scorer
            score_result = ctx.quality_scorer.score_peer(pid)

            quality_score = score_result.quality_score if score_result else 0.5
            recommendation = score_result.quality_recommendation if score_result else "maintain"

            # Classify quality tier
            if quality_score >= 0.7:
                quality = "good"
            elif quality_score >= 0.4:
                quality = "neutral"
            else:
                quality = "avoid"

            # Build reasons list
            reasons = []
            if score_result:
                if hasattr(score_result, 'uptime_score') and score_result.uptime_score >= 0.9:
                    reasons.append("high_uptime")
                if hasattr(score_result, 'success_rate_score') and score_result.success_rate_score >= 0.8:
                    reasons.append("good_routing_partner")
                if hasattr(score_result, 'fee_stability_score') and score_result.fee_stability_score >= 0.8:
                    reasons.append("stable_fees")
                if hasattr(score_result, 'force_close_penalty') and score_result.force_close_penalty > 0:
                    reasons.append("force_close_history")
                if quality_score < 0.4:
                    reasons.append("low_quality_score")

            # Get last assessment time from peer reputation manager
            last_assessed = None
            if ctx.database:
                # Check for peer events
                events = ctx.database.get_peer_events(peer_id=pid, limit=1)
                if events:
                    last_assessed = events[0].get('timestamp')

            peers_data[pid] = {
                "quality": quality,
                "quality_score": round(quality_score, 3),
                "reasons": reasons,
                "recommendation": recommendation,
                "last_assessed": last_assessed or int(time.time()),
            }

        return {"peers": peers_data}

    except Exception as e:
        return {"error": f"Failed to get peer quality: {e}"}




def get_channel_flags(ctx: HiveContext, scid: str = None) -> Dict[str, Any]:
    """
    Get special flags for channels.

    Returns flags identifying hive-internal channels that should be excluded
    from optimization (always 0 fee) or have other special treatment.

    Args:
        ctx: HiveContext
        scid: Optional specific channel SCID. If None, returns all channels.

    Returns:
        Dict with channel flags:
        {
            "channels": {
                "932263x1883x0": {
                    "is_hive_internal": false,
                    "is_hive_member": false,
                    "fixed_fee": null,
                    "exclude_from_optimization": false
                }
            }
        }
    """
    if not ctx.database:
        return {"error": "Database not initialized"}

    try:
        channels_data = {}

        # Get all hive members
        members = ctx.database.get_all_members()
        member_ids = set(m.get('peer_id') for m in members if m.get('peer_id'))

        # Get all channels
        if ctx.safe_plugin:
            channels = ctx.safe_plugin.rpc.listpeerchannels()

            for ch in channels.get('channels', []):
                ch_scid = ch.get('short_channel_id')
                if not ch_scid:
                    continue

                # Skip if specific scid requested and this isn't it
                if scid and ch_scid != scid:
                    continue

                peer_id = ch.get('peer_id', '')
                is_hive_member = peer_id in member_ids

                # Check if this is a hive-internal channel (between hive members)
                # Both ends must be hive members
                is_hive_internal = is_hive_member  # Our end is hive, check peer

                # Hive internal channels should have 0 fee
                fixed_fee = 0 if is_hive_internal else None
                exclude_from_optimization = is_hive_internal

                channels_data[ch_scid] = {
                    "is_hive_internal": is_hive_internal,
                    "is_hive_member": is_hive_member,
                    "fixed_fee": fixed_fee,
                    "exclude_from_optimization": exclude_from_optimization,
                    "peer_id": peer_id[:16] + "..." if peer_id else None,
                }

        return {"channels": channels_data}

    except Exception as e:
        return {"error": f"Failed to get channel flags: {e}"}




def get_nnlb_opportunities(ctx: HiveContext, min_amount: int = 50000) -> Dict[str, Any]:
    """
    Get Nearest-Neighbor Load Balancing opportunities.

    Returns low-cost rebalance opportunities between fleet members where
    the rebalance can be done at zero or minimal fee through hive-internal
    channels.

    Args:
        ctx: HiveContext
        min_amount: Minimum amount in sats to consider (default: 50000)

    Returns:
        Dict with NNLB opportunities:
        {
            "opportunities": [
                {
                    "source_scid": "932263x1883x0",
                    "sink_scid": "931308x1256x0",
                    "amount_sats": 200000,
                    "estimated_cost_sats": 0,
                    "path_hops": 1,
                    "is_hive_internal": true
                }
            ]
        }
    """
    if not ctx.liquidity_coordinator:
        return {"error": "Liquidity coordinator not initialized"}

    try:
        opportunities = []

        if ctx.liquidity_coordinator:
            # Use liquidity coordinator's circular flow detection
            if hasattr(ctx.liquidity_coordinator, 'get_circular_rebalance_opportunities'):
                circ_opps = ctx.liquidity_coordinator.get_circular_rebalance_opportunities()
                for opp in circ_opps:
                    if opp.get('amount_sats', 0) >= min_amount:
                        opportunities.append({
                            "source_scid": opp.get('from_channel'),
                            "sink_scid": opp.get('to_channel'),
                            "amount_sats": opp.get('amount_sats', 0),
                            "estimated_cost_sats": opp.get('cost_sats', 0),
                            "path_hops": opp.get('hops', 1),
                            "is_hive_internal": opp.get('is_hive_internal', True),
                        })

        # Sort by amount descending
        opportunities.sort(key=lambda x: x['amount_sats'], reverse=True)

        return {"opportunities": opportunities[:20]}  # Limit to 20

    except Exception as e:
        return {"error": f"Failed to get NNLB opportunities: {e}"}


def get_channel_ages(ctx: HiveContext, scid: str = None) -> Dict[str, Any]:
    """
    Get channel age information.

    Returns age and maturity classification for channels. Used by
    cl-revenue-ops to adjust exploration vs exploitation in Thompson
    sampling - new channels need more exploration, mature channels
    should exploit known-good fees.

    Args:
        ctx: HiveContext
        scid: Optional specific channel SCID. If None, returns all channels.

    Returns:
        Dict with channel ages:
        {
            "channels": {
                "932263x1883x0": {
                    "age_days": 45,
                    "maturity": "mature",
                    "first_forward_days_ago": 40,
                    "total_forwards": 250
                }
            }
        }
    """
    if not ctx.safe_plugin:
        return {"error": "Plugin not initialized"}

    try:
        channels_data = {}
        now = int(time.time())

        # Get all channels
        channels = ctx.safe_plugin.rpc.listpeerchannels()

        # Fetch blockheight once (constant across iterations)
        current_block = 0
        try:
            info = ctx.safe_plugin.rpc.getinfo()
            current_block = info.get('blockheight', 0)
        except Exception:
            pass

        for ch in channels.get('channels', []):
            ch_scid = ch.get('short_channel_id')
            if not ch_scid:
                continue

            # Skip if specific scid requested and this isn't it
            if scid and ch_scid != scid:
                continue

            # Calculate age from funding confirmation
            # SCID format: blockheight x txindex x output
            # We can derive approximate age from blockheight
            try:
                parts = ch_scid.split('x')
                if len(parts) >= 3:
                    funding_block = int(parts[0])

                    blocks_old = current_block - funding_block
                    # Approximate 10 minutes per block
                    age_days = (blocks_old * 10) / (60 * 24)
                    age_days = max(0, age_days)
                else:
                    age_days = 0
            except (ValueError, TypeError):
                age_days = 0

            # Classify maturity
            if age_days < 14:
                maturity = "new"
            elif age_days < 60:
                maturity = "developing"
            else:
                maturity = "mature"

            # Get forward statistics if available from database
            first_forward_days_ago = None
            total_forwards = 0

            if ctx.database:
                # Check peer events for forward activity
                peer_id = ch.get('peer_id', '')
                if peer_id:
                    events = ctx.database.get_peer_events(
                        peer_id=peer_id,
                        event_type='forward',
                        limit=1000
                    )
                    if events:
                        total_forwards = len(events)
                        oldest_event = min(e.get('timestamp', now) for e in events)
                        first_forward_days_ago = (now - oldest_event) / 86400

            channels_data[ch_scid] = {
                "age_days": round(age_days, 1),
                "maturity": maturity,
                "first_forward_days_ago": round(first_forward_days_ago, 1) if first_forward_days_ago else None,
                "total_forwards": total_forwards,
            }

        return {"channels": channels_data}

    except Exception as e:
        return {"error": f"Failed to get channel ages: {e}"}


# =============================================================================
# DID CREDENTIAL COMMANDS (Phase 16)
# =============================================================================



# =============================================================================
# Phase 14 – Traffic Intelligence RPCs
# =============================================================================

def report_traffic_profile(
    ctx,
    peer_id: str = "",
    profile_type: str = "mixed",
    peak_hours_utc: list = None,
    quiet_hours_utc: list = None,
    avg_forward_size_sats: float = 0.0,
    daily_volume_sats: float = 0.0,
    drain_direction: str = "balanced",
    confidence: float = 0.5,
    observation_window_hours: int = 24,
):
    """
    Receive traffic profile from cl-revenue-ops.

    Permission: None (local integration)
    """
    if not ctx.database or not ctx.traffic_intel_mgr:
        return {"error": "Traffic intelligence not initialized"}

    if not peer_id:
        return {"error": "peer_id is required"}

    try:
        ok = ctx.traffic_intel_mgr.store_local_profile(
            peer_id=peer_id,
            profile_type=profile_type,
            peak_hours_utc=peak_hours_utc or [],
            quiet_hours_utc=quiet_hours_utc or [],
            avg_forward_size_sats=avg_forward_size_sats,
            daily_volume_sats=daily_volume_sats,
            drain_direction=drain_direction,
            confidence=confidence,
            observation_window_hours=observation_window_hours,
        )
        if ok:
            return {"status": "accepted", "peer_id": peer_id}
        else:
            return {"error": "Failed to store profile (validation failed)"}
    except Exception as e:
        return {"error": f"Failed to store profile: {e}"}


def get_traffic_intelligence(
    ctx,
    peer_id: str = None,
    profile_type: str = None,
):
    """
    Query aggregated fleet traffic intelligence.

    Permission: None (local query)
    """
    if not ctx.traffic_intel_mgr:
        return {"error": "Traffic intelligence not initialized"}

    try:
        if peer_id:
            agg = ctx.traffic_intel_mgr.get_aggregated_profile(peer_id)
            if agg:
                return {"profiles": [agg]}
            return {"profiles": []}
        else:
            profiles = ctx.traffic_intel_mgr.get_all_profiles(
                profile_type=profile_type,
            )
            return {"profiles": profiles}
    except Exception as e:
        return {"error": f"Query failed: {e}"}


def check_rebalance_conflict(
    ctx,
    peer_id: str = "",
    direction: str = "outbound",
    amount_sats: int = 0,
):
    """
    Check if rebalancing through a peer conflicts with fleet activity.

    Permission: None (local query)
    """
    if not ctx.traffic_intel_mgr:
        return {"error": "Traffic intelligence not initialized"}

    if not peer_id:
        return {"error": "peer_id is required"}

    try:
        return ctx.traffic_intel_mgr.check_rebalance_conflict(
            peer_id=peer_id,
            direction=direction,
            amount_sats=amount_sats,
        )
    except Exception as e:
        return {"error": f"Conflict check failed: {e}"}


def get_fleet_demand_forecast(ctx, hours_ahead: int = 6):
    """
    Get fleet-wide demand forecast.

    Permission: None (local query)
    """
    if not ctx.traffic_intel_mgr:
        return {"error": "Traffic intelligence not initialized"}

    hours_ahead = max(1, min(hours_ahead, 168))

    try:
        return ctx.traffic_intel_mgr.get_fleet_demand_forecast(
            hours_ahead=hours_ahead,
        )
    except Exception as e:
        return {"error": f"Forecast failed: {e}"}


# =============================================================================
# EXPORT HINTS (Local trusted integration surface for cl-revenue-ops)
# =============================================================================

_DEFAULT_HINTS_TTL = 900  # 15 minutes


def _derive_corridor_roles(ctx: HiveContext) -> Dict[str, str]:
    """Derive per-peer corridor role from corridor assignments.

    Returns dict mapping peer_id -> one of "owner", "secondary", "contested", "none".
    A peer is "owner" if it is the primary member on any corridor,
    "secondary" if it appears only as secondary, and "contested" if it
    appears as both primary and secondary on different corridors.
    """
    roles: Dict[str, str] = {}
    if not ctx.fee_coordination_mgr:
        return roles

    try:
        corridor_mgr = getattr(ctx.fee_coordination_mgr, "corridor_mgr", None)
        if not corridor_mgr:
            return roles
        assignments = corridor_mgr.get_assignments()
    except Exception:
        return roles

    for a in assignments:
        # Primary member
        pm = a.primary_member
        if pm:
            prev = roles.get(pm)
            if prev is None:
                roles[pm] = "owner"
            elif prev == "secondary":
                roles[pm] = "contested"
        # Secondary members
        for sm in (a.secondary_members or []):
            prev = roles.get(sm)
            if prev is None:
                roles[sm] = "secondary"
            elif prev == "owner":
                roles[sm] = "contested"

    return roles


def _derive_competition_bias(ctx: HiveContext) -> Dict[str, int]:
    """Derive per-peer competition bias from corridor competition levels.

    Returns dict mapping peer_id -> one of -1, 0, 1.
    -1 = high competition (back off), 0 = neutral, 1 = low competition (lean in).
    """
    biases: Dict[str, int] = {}
    if not ctx.fee_coordination_mgr:
        return biases

    try:
        corridor_mgr = getattr(ctx.fee_coordination_mgr, "corridor_mgr", None)
        if not corridor_mgr:
            return biases
        assignments = corridor_mgr.get_assignments()
    except Exception:
        return biases

    # Collect competition signals per peer (from corridors they participate in)
    peer_signals: Dict[str, list] = {}
    for a in assignments:
        level = getattr(a.corridor, "competition_level", "none")
        participants = set()
        if a.primary_member:
            participants.add(a.primary_member)
        for sm in (a.secondary_members or []):
            participants.add(sm)
        for pid in participants:
            peer_signals.setdefault(pid, []).append(level)

    for pid, signals in peer_signals.items():
        high_count = sum(1 for s in signals if s in ("high", "medium"))
        low_count = sum(1 for s in signals if s in ("none", "low"))
        if high_count > low_count:
            biases[pid] = -1
        elif low_count > high_count:
            biases[pid] = 1
        else:
            biases[pid] = 0

    return biases


def _derive_rebalance_preferences(ctx: HiveContext) -> Dict[str, str]:
    """Derive per-peer rebalance preference from yield metrics flow direction.

    Returns dict mapping peer_id -> one of "source", "sink", "neutral".
    """
    prefs: Dict[str, str] = {}
    if not ctx.yield_metrics_mgr:
        return prefs

    try:
        metrics = ctx.yield_metrics_mgr.get_channel_yield_metrics()
    except Exception:
        return prefs

    peer_flow_weights: Dict[str, Dict[str, float]] = {}
    for m in metrics:
        peer_id = getattr(m, "peer_id", None)
        if not peer_id:
            continue
        direction = getattr(m, "flow_direction", "balanced")
        if direction not in ("source", "sink"):
            continue

        weight = getattr(m, "volume_routed_sats", 0) or getattr(m, "flow_intensity", 0)
        try:
            weight = float(weight)
        except (TypeError, ValueError):
            weight = 0.0
        if weight <= 0:
            weight = 1.0

        totals = peer_flow_weights.setdefault(peer_id, {"source": 0.0, "sink": 0.0})
        totals[direction] += weight

    for peer_id, totals in peer_flow_weights.items():
        if totals["source"] > totals["sink"]:
            prefs[peer_id] = "source"
        elif totals["sink"] > totals["source"]:
            prefs[peer_id] = "sink"

    return prefs


def _derive_channel_open_hints(ctx: HiveContext) -> Dict[str, Dict[str, Any]]:
    """Derive per-peer channel-opening advisory hints from planner topology.

    Returns dict mapping peer_id -> channel_open_hint dict with:
        open_preference: "open" | "neutral" | "avoid"
        topology_confidence: 0.0 to 1.0
        suggested_size_bucket: "small" | "medium" | "large"
        reason: "underserved_corridor" | "improve_coverage" | "reduce_overlap" |
                "member_connectivity" | "none"
    """
    hints: Dict[str, Dict[str, Any]] = {}
    if not ctx.planner or not ctx.config:
        return hints

    try:
        cfg = ctx.config.snapshot()
        underserved = ctx.planner.get_underserved_targets(cfg)
    except Exception:
        return hints

    # Size bucket boundaries from config
    min_sats = getattr(cfg, "planner_min_channel_sats", 1_000_000)
    default_sats = getattr(cfg, "planner_default_channel_sats", 5_000_000)
    max_sats = getattr(cfg, "planner_max_channel_sats", 50_000_000)
    # Thresholds: small < low_thresh, medium < high_thresh, large >= high_thresh
    low_thresh = min_sats + (default_sats - min_sats) // 2
    high_thresh = default_sats + (max_sats - default_sats) // 2

    for ur in underserved:
        try:
            rec = ctx.planner.get_expansion_recommendation(ur.target, cfg)
        except Exception:
            continue

        # open_preference
        if rec.recommendation_type == "open_channel":
            open_pref = "open"
        elif rec.hive_coverage_pct >= 0.50:
            open_pref = "avoid"
        else:
            open_pref = "neutral"

        # topology_confidence: blend quality confidence + data availability
        data_confidence = min(1.0, ur.score / 3.0) if ur.score > 0 else 0.0
        topology_confidence = round(
            0.5 * ur.quality_confidence + 0.5 * data_confidence, 2
        )

        # reason
        if rec.is_bottleneck:
            reason = "improve_coverage"
        elif rec.hive_members_count == 0:
            reason = "member_connectivity"
        elif rec.hive_coverage_pct >= 0.50:
            reason = "reduce_overlap"
        elif ur.hive_share_pct < 0.03:
            reason = "underserved_corridor"
        else:
            reason = "none"

        # suggested_size_bucket from recommended size
        try:
            size_result = ctx.planner.channel_sizer.calculate_size(
                target=ur.target,
                target_capacity_sats=ur.public_capacity_sats,
                target_channel_count=rec.network_channels,
                hive_share_pct=ur.hive_share_pct,
                target_share_cap=getattr(cfg, "market_share_cap_pct", 0.20),
                onchain_balance_sats=0,  # Unknown at hint time
                min_channel_sats=min_sats,
                max_channel_sats=max_sats,
                default_channel_sats=default_sats,
            )
            size_sats = size_result.recommended_size_sats
        except Exception:
            size_sats = default_sats

        if size_sats < low_thresh:
            size_bucket = "small"
        elif size_sats >= high_thresh:
            size_bucket = "large"
        else:
            size_bucket = "medium"

        # Downgrade to neutral if confidence is very low
        if topology_confidence < 0.15 and open_pref == "open":
            open_pref = "neutral"

        hints[ur.target] = {
            "open_preference": open_pref,
            "topology_confidence": topology_confidence,
            "suggested_size_bucket": size_bucket,
            "reason": reason,
        }

    return hints


def export_hints(ctx: HiveContext, ttl_seconds: int = _DEFAULT_HINTS_TTL) -> Dict[str, Any]:
    """
    Export compact short-lived per-peer hints for trusted local consumers.

    This is a read-only distillation of fleet coordination state.
    cl-revenue-ops may poll this locally and use the hints as bounded
    soft biases in its own local decision logic.

    Returns:
        Dict with generated_at, ttl_seconds, and per-peer hints map.
    """
    if not ctx.database:
        return {"error": "Hive not initialized"}

    now = int(time.time())
    hints: Dict[str, Dict[str, Any]] = {}

    # Collect all known peer_ids from membership + our channel peers
    members = ctx.database.get_all_members()
    member_set = {m["peer_id"] for m in members}

    # Pre-compute bulk lookups
    corridor_roles = _derive_corridor_roles(ctx)
    competition_biases = _derive_competition_bias(ctx)
    rebalance_prefs = _derive_rebalance_preferences(ctx)
    channel_open_hints = _derive_channel_open_hints(ctx)

    # Build the set of peers to export hints for: members + any peer
    # we have corridor/quality/traffic data about
    all_peers = set(member_set)
    all_peers.update(corridor_roles.keys())
    all_peers.update(rebalance_prefs.keys())
    all_peers.update(channel_open_hints.keys())
    if ctx.quality_scorer:
        try:
            scored_peers = ctx.quality_scorer.get_scored_peers(days=90)
            all_peers.update(
                result.peer_id
                for result in scored_peers
                if getattr(result, "peer_id", None) and getattr(result, "confidence", 0.0) > 0.0
            )
        except Exception:
            pass
    if ctx.traffic_intel_mgr:
        try:
            profiles = ctx.traffic_intel_mgr.get_all_profiles()
            all_peers.update(
                profile.get("peer_id")
                for profile in profiles
                if isinstance(profile, dict) and profile.get("peer_id")
            )
        except Exception:
            pass

    # Exclude ourselves
    all_peers.discard(ctx.our_pubkey)

    for peer_id in all_peers:
        hint: Dict[str, Any] = {}
        is_member = peer_id in member_set
        hint["member"] = is_member

        # Corridor role
        role = corridor_roles.get(peer_id, "none")
        hint["corridor_role"] = role

        # Competition bias
        hint["competition_bias"] = competition_biases.get(peer_id, 0)

        # Peer quality score
        if ctx.quality_scorer:
            try:
                qr = ctx.quality_scorer.calculate_score(peer_id, days=90)
                if qr.confidence > 0.0:
                    hint["peer_quality_score"] = round(qr.overall_score, 2)
            except Exception:
                pass

        # Traffic confidence
        if ctx.traffic_intel_mgr:
            try:
                profile = ctx.traffic_intel_mgr.get_aggregated_profile(peer_id)
                if profile:
                    hint["traffic_confidence"] = round(
                        profile.get("confidence", 0.0), 2
                    )
            except Exception:
                pass

        # Default traffic_confidence for peers with other hint data but no
        # traffic profile. Without this, corridor/competition/rebalance hints
        # are dead on the consumer side (traffic_confidence gates all biases).
        if "traffic_confidence" not in hint:
            if is_member:
                hint["traffic_confidence"] = 0.5
            elif role != "none" or peer_id in rebalance_prefs or "peer_quality_score" in hint:
                hint["traffic_confidence"] = 0.3

        # Fleet fee median (for downstream prior initialization)
        if ctx.fee_coordination_mgr:
            try:
                corridor_mgr = getattr(ctx.fee_coordination_mgr, "corridor_mgr", None)
                if corridor_mgr:
                    assignments = corridor_mgr.get_assignments()
                    # Find corridors involving this peer and get their avg fee
                    peer_fees = []
                    for a in assignments:
                        if a.primary_member == peer_id and a.primary_fee_ppm > 0:
                            peer_fees.append(a.primary_fee_ppm)
                        if peer_id in (a.secondary_members or []) and a.secondary_fee_ppm > 0:
                            peer_fees.append(a.secondary_fee_ppm)
                    if peer_fees:
                        peer_fees.sort()
                        hint["fleet_fee_median"] = peer_fees[len(peer_fees) // 2]
            except Exception:
                pass

        # Rebalance preference
        pref = rebalance_prefs.get(peer_id, "neutral")
        hint["rebalance_preference"] = pref

        # Network centrality (routing importance)
        try:
            from . import network_metrics as nm
            calculator = nm.get_calculator()
            if calculator:
                metrics = calculator.get_member_metrics(peer_id)
                if metrics:
                    hint["external_centrality"] = round(metrics.external_centrality, 6)
        except Exception:
            pass

        # Peer reputation score (fleet-aggregated quality)
        if ctx.peer_reputation_mgr:
            try:
                rep = ctx.peer_reputation_mgr.get_reputation(peer_id)
                if rep:
                    hint["reputation_score"] = rep.reputation_score
            except Exception:
                pass

        # Channel-opening advisory hint (omitted if no topology data)
        ch_hint = channel_open_hints.get(peer_id)
        if ch_hint:
            hint["channel_open_hint"] = ch_hint

        hints[peer_id] = hint

    return {
        "generated_at": now,
        "ttl_seconds": ttl_seconds,
        "peer_count": len(hints),
        "hints": hints,
    }
