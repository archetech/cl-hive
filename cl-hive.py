#!/usr/bin/env python3
"""
cl-hive: Distributed Swarm Intelligence for Lightning Node Fleets

This plugin implements "The Hive" protocol, enabling independent Lightning nodes
to function as a coordinated swarm. It provides:
- Zero-cost capital teleportation between fleet members
- Coordinated topology optimization (anti-overlap)
- Distributed immunity via shared ban lists
- Intent Lock protocol for conflict-free coordination

ARCHITECTURE:
-------------
cl-hive is a COORDINATION layer that sits ABOVE cl-revenue-ops.
It uses cl-revenue-ops PolicyManager for fee control (strategy=hive)
and the Strategic Rebalance Exemption for load balancing.

    cl-hive (Coordination)
         │
         ▼
    cl-revenue-ops (Execution)
         │
         ▼
    Core Lightning

DEPENDENCIES:
- cl-revenue-ops v1.4.0+ (PolicyManager with HIVE strategy)
- pyln-client: Core Lightning plugin framework

Author: Lightning Goats Team
License: MIT
"""

import json
import inspect
import multiprocessing
import os
import queue
import signal
import threading
import time
import traceback
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional, Any, List

from pyln.client import Plugin, RpcError

# Import our modules
from modules.config import HiveConfig
from modules.database import HiveDatabase
from modules.protocol import (
    HIVE_MAGIC, HiveMessageType,
    MAX_MESSAGE_BYTES, is_hive_message, deserialize, serialize,
    validate_promotion_request, validate_vouch, validate_promotion,
    validate_member_left, validate_ban_proposal, validate_ban_vote,
    validate_peer_available, create_peer_available,
    validate_expansion_nominate, validate_expansion_elect, validate_expansion_decline,
    create_expansion_nominate, create_expansion_elect, create_expansion_decline,
    get_expansion_nominate_signing_payload, get_expansion_elect_signing_payload,
    get_expansion_decline_signing_payload,
    VOUCH_TTL_SECONDS, MAX_VOUCHES_IN_PROMOTION,
    create_challenge, create_welcome,
    # Signed message validation (security hardening)
    validate_gossip, validate_state_hash, validate_full_sync, validate_intent_abort,
    get_gossip_signing_payload, get_state_hash_signing_payload,
    get_full_sync_signing_payload, get_intent_signing_payload, get_intent_abort_signing_payload,
    get_peer_available_signing_payload, compute_states_hash,
    # Settlement offer broadcast
    create_settlement_offer, get_settlement_offer_signing_payload,
    # MCF (Min-Cost Max-Flow) optimization
    validate_mcf_needs_batch, validate_mcf_solution_broadcast,
    validate_mcf_assignment_ack, validate_mcf_completion_report,
    get_mcf_needs_batch_signing_payload, get_mcf_solution_signing_payload,
    get_mcf_assignment_ack_signing_payload, get_mcf_completion_signing_payload,
    create_mcf_needs_batch,
    # Phase D: Reliable delivery
    create_msg_ack, validate_msg_ack,
    IMPLICIT_ACK_MAP, IMPLICIT_ACK_MATCH_FIELD,
    RELIABLE_MESSAGE_TYPES,
)
from modules.handshake import HandshakeManager, Ticket, CHALLENGE_TTL_SECONDS
from modules.state_manager import StateManager, HivePeerState
from modules.gossip import GossipManager
from modules.intent_manager import IntentManager, Intent, IntentType
from modules.bridge import Bridge, BridgeStatus, CircuitOpenError
from modules.contribution import ContributionManager
from modules.membership import MembershipManager, MembershipTier
from modules.planner import Planner, ChannelSizer
from modules.quality_scorer import PeerQualityScorer
from modules.cooperative_expansion import CooperativeExpansionManager
from modules.governance import DecisionEngine
from modules.vpn_transport import VPNTransportManager
from modules.fee_intelligence import FeeIntelligenceManager
from modules.traffic_intelligence import TrafficIntelligenceManager
from modules.liquidity_coordinator import LiquidityCoordinator
from modules.splice_coordinator import SpliceCoordinator
from modules.health_aggregator import HealthScoreAggregator, HealthTier
from modules.routing_intelligence import HiveRoutingMap
from modules.peer_reputation import PeerReputationManager
from modules.routing_pool import RoutingPool
from modules.settlement import SettlementManager
from modules.yield_metrics import YieldMetricsManager
from modules.fee_coordination import FeeCoordinationManager
from modules.cost_reduction import CostReductionManager
from modules.channel_rationalization import RationalizationManager
from modules.strategic_positioning import StrategicPositioningManager
from modules.anticipatory_liquidity import AnticipatoryLiquidityManager
from modules.task_manager import TaskManager
from modules.splice_manager import SpliceManager
from modules.relay import RelayManager
from modules.idempotency import check_and_record, generate_event_id
from modules.outbox import OutboxManager
from modules.did_credentials import DIDCredentialManager
from modules.management_schemas import ManagementSchemaRegistry
from modules.cashu_escrow import CashuEscrowManager
from modules.nostr_transport import ExternalCommsTransport, TransportInterface
from modules.identity_adapter import IdentityInterface, RemoteArchonIdentity
from modules.phase6_ingest import parse_injected_hive_packet
from modules.marketplace import MarketplaceManager
from modules.liquidity_marketplace import LiquidityMarketplaceManager
from modules import network_metrics
from modules.plugin_options import (
    RateLimiter, _parse_bool, _parse_setconfig_value,
    OPTION_TO_CONFIG_MAP, VPN_OPTIONS, register_options,
)
from modules.rpc_pool import RpcLockTimeoutError, RpcPool, RpcPoolProxy
from modules.log_writer import BatchedLogWriter
from modules import rpc_pool as _rpc_pool_mod
from modules import protocol_handlers
from modules import background_loops
from modules.rpc_commands import (
    HiveContext,
    status as rpc_status,
    get_config as rpc_get_config,
    members as rpc_members,
    vpn_status as rpc_vpn_status,
    expansion_recommendations as rpc_expansion_recommendations,
    vpn_add_peer as rpc_vpn_add_peer,
    vpn_remove_peer as rpc_vpn_remove_peer,
    pending_actions as rpc_pending_actions,
    approve_action as rpc_approve_action,
    reject_action as rpc_reject_action,
    budget_summary as rpc_budget_summary,
    set_mode as rpc_set_mode,
    enable_expansions as rpc_enable_expansions,
    pending_bans as rpc_pending_bans,
    # Phase 4: Topology, Planner, and Query Commands
    reinit_bridge as rpc_reinit_bridge,
    topology as rpc_topology,
    planner_log as rpc_planner_log,
    intent_status as rpc_intent_status,
    contribution as rpc_contribution,
    expansion_status as rpc_expansion_status,
    # Phase 0: Routing Pool (Collective Economics)
    pool_status as rpc_pool_status,
    pool_member_status as rpc_pool_member_status,
    pool_snapshot as rpc_pool_snapshot,
    pool_distribution as rpc_pool_distribution,
    pool_settle as rpc_pool_settle,
    pool_record_revenue as rpc_pool_record_revenue,
    # Phase 1: Yield Metrics & Measurement
    yield_metrics as rpc_yield_metrics,
    yield_summary as rpc_yield_summary,
    velocity_prediction as rpc_velocity_prediction,
    critical_velocity_channels as rpc_critical_velocity_channels,
    internal_competition as rpc_internal_competition,
    # Phase 2: Fee Coordination
    fee_recommendation as rpc_fee_recommendation,
    corridor_assignments as rpc_corridor_assignments,
    stigmergic_markers as rpc_stigmergic_markers,
    deposit_marker as rpc_deposit_marker,
    defense_status as rpc_defense_status,
    broadcast_warning as rpc_broadcast_warning,
    pheromone_levels as rpc_pheromone_levels,
    get_routing_intelligence as rpc_get_routing_intelligence,
    fee_coordination_status as rpc_fee_coordination_status,
    # Phase 3 - Cost Reduction
    rebalance_recommendations as rpc_rebalance_recommendations,
    fleet_rebalance_path as rpc_fleet_rebalance_path,
    record_rebalance_outcome as rpc_record_rebalance_outcome,
    circular_flow_status as rpc_circular_flow_status,
    cost_reduction_status as rpc_cost_reduction_status,
    execute_hive_circular_rebalance as rpc_execute_hive_circular_rebalance,
    # Phase 15 - MCF Optimization
    mcf_status as rpc_mcf_status,
    mcf_solve as rpc_mcf_solve,
    mcf_assignments as rpc_mcf_assignments,
    mcf_optimized_path as rpc_mcf_optimized_path,
    # Channel Rationalization
    coverage_analysis as rpc_coverage_analysis,
    close_recommendations as rpc_close_recommendations,
    create_close_actions as rpc_create_close_actions,
    rationalization_summary as rpc_rationalization_summary,
    rationalization_status as rpc_rationalization_status,
    # Phase 5 - Strategic Positioning
    valuable_corridors as rpc_valuable_corridors,
    exchange_coverage as rpc_exchange_coverage,
    positioning_recommendations as rpc_positioning_recommendations,
    flow_recommendations as rpc_flow_recommendations,
    report_flow_intensity as rpc_report_flow_intensity,
    positioning_summary as rpc_positioning_summary,
    positioning_status as rpc_positioning_status,
    # Network Metrics
    network_metrics as rpc_network_metrics,
    rebalance_hubs as rpc_rebalance_hubs,
    rebalance_path as rpc_rebalance_path,
    # Fleet Health Monitoring
    fleet_health as rpc_fleet_health,
    connectivity_alerts as rpc_connectivity_alerts,
    member_connectivity as rpc_member_connectivity,
    # Promotion Criteria
    neophyte_rankings as rpc_neophyte_rankings,
    # Revenue Ops Integration
    get_defense_status as rpc_get_defense_status,
    get_peer_quality as rpc_get_peer_quality,
    get_fee_change_outcomes as rpc_get_fee_change_outcomes,
    get_channel_flags as rpc_get_channel_flags,
    get_mcf_targets as rpc_get_mcf_targets,
    get_nnlb_opportunities as rpc_get_nnlb_opportunities,
    get_channel_ages as rpc_get_channel_ages,
    # DID Credentials (Phase 16)
    did_issue_credential as rpc_did_issue_credential,
    did_list_credentials as rpc_did_list_credentials,
    did_revoke_credential as rpc_did_revoke_credential,
    did_get_reputation as rpc_did_get_reputation,
    did_list_profiles as rpc_did_list_profiles,
    # Management Schemas (Phase 2)
    schema_list as rpc_schema_list,
    schema_validate as rpc_schema_validate,
    mgmt_credential_issue as rpc_mgmt_credential_issue,
    mgmt_credential_list as rpc_mgmt_credential_list,
    mgmt_credential_revoke as rpc_mgmt_credential_revoke,
    # Phase 4A: Cashu Escrow
    escrow_create as rpc_escrow_create,
    escrow_list as rpc_escrow_list,
    escrow_redeem as rpc_escrow_redeem,
    escrow_refund as rpc_escrow_refund,
    escrow_get_receipt as rpc_escrow_get_receipt,
    escrow_complete as rpc_escrow_complete,
    # Phase 4B: Extended Settlements
    bond_post as rpc_bond_post,
    bond_status as rpc_bond_status,
    settlement_obligations_list as rpc_settlement_obligations_list,
    settlement_net as rpc_settlement_net,
    dispute_file as rpc_dispute_file,
    dispute_vote as rpc_dispute_vote,
    dispute_status as rpc_dispute_status,
    credit_tier_info as rpc_credit_tier_info,
    # Phase 5B: Advisor marketplace
    marketplace_discover as rpc_marketplace_discover,
    marketplace_profile as rpc_marketplace_profile,
    marketplace_propose as rpc_marketplace_propose,
    marketplace_accept as rpc_marketplace_accept,
    marketplace_trial as rpc_marketplace_trial,
    marketplace_terminate as rpc_marketplace_terminate,
    marketplace_status as rpc_marketplace_status,
    # Phase 5C: Liquidity marketplace
    liquidity_discover as rpc_liquidity_discover,
    liquidity_offer as rpc_liquidity_offer,
    liquidity_request as rpc_liquidity_request,
    liquidity_lease as rpc_liquidity_lease,
    liquidity_heartbeat as rpc_liquidity_heartbeat,
    liquidity_lease_status as rpc_liquidity_lease_status,
    liquidity_terminate as rpc_liquidity_terminate,
    # Phase 14: Traffic Intelligence
    report_traffic_profile as rpc_report_traffic_profile,
    get_traffic_intelligence as rpc_get_traffic_intelligence,
    check_rebalance_conflict as rpc_check_rebalance_conflict,
    get_fleet_demand_forecast as rpc_get_fleet_demand_forecast,
)

# Initialize the plugin
plugin = Plugin()

# =============================================================================
# GRACEFUL SHUTDOWN SUPPORT
# =============================================================================
# This event signals all background threads to exit cleanly.
# When `lightning-cli plugin stop cl-hive` is called, CLN sends SIGTERM.

shutdown_event = threading.Event()

# Global RPC pool instance (initialized in init)
_rpc_pool: Optional[RpcPool] = None

# Bounded thread pool for message dispatch (prevents unbounded thread creation)
_msg_executor: Optional[ThreadPoolExecutor] = None


_batched_log_writer: Optional["BatchedLogWriter"] = None


# =============================================================================
# GLOBAL INSTANCES (initialized in init)
# =============================================================================

database: Optional[HiveDatabase] = None
config: Optional[HiveConfig] = None
# Note: We use the global 'plugin' object directly for RPC calls.
# pyln-client is inherently thread-safe (opens new socket per call).
handshake_mgr: Optional[HandshakeManager] = None
state_manager: Optional[StateManager] = None
gossip_mgr: Optional[GossipManager] = None
intent_mgr: Optional[IntentManager] = None
bridge: Optional[Bridge] = None
membership_mgr: Optional[MembershipManager] = None
contribution_mgr: Optional[ContributionManager] = None
planner: Optional[Planner] = None
decision_engine: Optional[DecisionEngine] = None
vpn_transport: Optional[VPNTransportManager] = None
coop_expansion: Optional[CooperativeExpansionManager] = None
fee_intel_mgr: Optional[FeeIntelligenceManager] = None
traffic_intel_mgr: Optional[TrafficIntelligenceManager] = None
health_aggregator: Optional[HealthScoreAggregator] = None
liquidity_coord: Optional[LiquidityCoordinator] = None
splice_coord: Optional[SpliceCoordinator] = None
routing_map: Optional[HiveRoutingMap] = None
peer_reputation_mgr: Optional[PeerReputationManager] = None
routing_pool: Optional[RoutingPool] = None
settlement_mgr: Optional[SettlementManager] = None
yield_metrics_mgr: Optional[YieldMetricsManager] = None
fee_coordination_mgr: Optional[FeeCoordinationManager] = None
cost_reduction_mgr: Optional[CostReductionManager] = None
rationalization_mgr: Optional[RationalizationManager] = None
strategic_positioning_mgr: Optional[StrategicPositioningManager] = None
anticipatory_liquidity_mgr: Optional[AnticipatoryLiquidityManager] = None
quality_scorer_mgr: Optional[PeerQualityScorer] = None
task_mgr: Optional[TaskManager] = None
splice_mgr: Optional[SpliceManager] = None
relay_mgr: Optional[RelayManager] = None
outbox_mgr: Optional[OutboxManager] = None
did_credential_mgr: Optional[DIDCredentialManager] = None
management_schema_registry: Optional[ManagementSchemaRegistry] = None
cashu_escrow_mgr: Optional[CashuEscrowManager] = None
nostr_transport: Optional[TransportInterface] = None
identity_adapter: Optional[IdentityInterface] = None
marketplace_mgr: Optional[MarketplaceManager] = None
liquidity_mgr: Optional[LiquidityMarketplaceManager] = None
policy_engine: Optional[Any] = None
our_pubkey: Optional[str] = None
phase6_optional_plugins: Dict[str, Any] = {
    "cl_hive_comms": {"installed": False, "active": False, "name": ""},
    "cl_hive_archon": {"installed": False, "active": False, "name": ""},
    "warnings": [],
}

# Startup timestamp for lightweight health endpoint (Phase 4)
_start_time: float = time.time()

# Fee tracking for real-time gossip (Settlement Phase)
_local_fees_earned_sats: int = 0
_local_fees_forward_count: int = 0
_local_fees_period_start: int = 0
_local_fees_last_broadcast: int = 0
_local_fees_last_broadcast_amount: int = 0  # Tracks fees at last broadcast
_local_rebalance_costs_sats: int = 0  # Rebalance costs for net profit settlement (Issue #42)
_local_fees_lock = threading.Lock()

# Fee broadcast thresholds
FEE_BROADCAST_MIN_SATS = 10  # Minimum cumulative fee change to trigger broadcast
FEE_BROADCAST_MIN_INTERVAL = 30  # Minimum seconds between broadcasts


def _load_fee_tracking_state() -> None:
    """
    Load persisted fee tracking state from database on startup.

    This prevents loss of accumulated fees when the plugin restarts.
    """
    global _local_fees_earned_sats, _local_fees_forward_count
    global _local_fees_period_start, _local_fees_last_broadcast
    global _local_fees_last_broadcast_amount

    if not database:
        return

    saved = database.load_local_fee_tracking()
    if not saved:
        return

    now = int(time.time())

    # Check if saved state is from the current settlement period
    # (Weekly periods aligned to Monday 00:00 UTC)
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    days_since_monday = dt.weekday()
    current_week_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    current_week_start = int(current_week_start.timestamp() - (days_since_monday * 86400))

    saved_period_start = saved.get("period_start_ts", 0)

    with _local_fees_lock:
        if saved_period_start >= current_week_start:
            # Same settlement period - restore the state
            _local_fees_earned_sats = saved.get("earned_sats", 0)
            _local_fees_forward_count = saved.get("forward_count", 0)
            _local_fees_period_start = saved_period_start
            _local_fees_last_broadcast = saved.get("last_broadcast_ts", 0)
            _local_fees_last_broadcast_amount = saved.get("last_broadcast_amount", 0)

            plugin.log(
                f"cl-hive: Restored fee tracking - {_local_fees_earned_sats} sats, "
                f"{_local_fees_forward_count} forwards from period {saved_period_start}",
                level="info"
            )
        else:
            # New settlement period - start fresh but log the old data
            plugin.log(
                f"cl-hive: Fee tracking from previous period "
                f"({saved.get('earned_sats', 0)} sats) - starting new period",
                level="info"
            )


def _save_fee_tracking_state() -> None:
    """
    Persist current fee tracking state to database.

    Called after every fee update to prevent loss on crash.
    """
    if not database:
        return

    # Read under lock but save outside to minimize lock time
    with _local_fees_lock:
        earned = _local_fees_earned_sats
        count = _local_fees_forward_count
        period_start = _local_fees_period_start
        last_broadcast = _local_fees_last_broadcast
        last_amount = _local_fees_last_broadcast_amount

    database.save_local_fee_tracking(
        earned_sats=earned,
        forward_count=count,
        period_start_ts=period_start,
        last_broadcast_ts=last_broadcast,
        last_broadcast_amount=last_amount
    )


# =============================================================================
# RATE LIMITER (Security Enhancement)
# =============================================================================
# RateLimiter class moved to modules/plugin_options.py

# Global rate limiter for PEER_AVAILABLE messages
peer_available_limiter: Optional[RateLimiter] = None

# Phase 4B per-peer sliding-window limits (count, window_seconds)
PHASE4B_RATE_LIMITS = {
    "SETTLEMENT_RECEIPT": (30, 3600),
    "BOND_POSTING": (5, 3600),
    "BOND_SLASH": (5, 3600),
    "NETTING_PROPOSAL": (10, 3600),
    "NETTING_ACK": (10, 3600),
    "VIOLATION_REPORT": (5, 3600),
    "ARBITRATION_VOTE": (5, 3600),
}
_phase4b_rate_windows: Dict[tuple, List[int]] = {}
_phase4b_rate_lock = threading.Lock()

# Track latest verified netting proposals by settlement window.
_phase4b_netting_proposals: Dict[str, Dict[str, Any]] = {}
_phase4b_netting_lock = threading.Lock()


def _check_permission(required_tier: str) -> Optional[Dict[str, Any]]:
    """
    Check if the local node has the required tier for an RPC command.

    Permission model (from IMPLEMENTATION_PLAN.md Section 8.5):
    - Admin Only: hive-genesis, hive-invite, hive-ban, hive-set-mode
    - Member Only: hive-vouch, hive-approve, hive-reject
    - Any Tier: hive-status, hive-members, hive-contribution, hive-topology

    Args:
        required_tier: 'member' (full member) or 'neophyte' (any member)

    Returns:
        None if permission granted, or error dict if denied
    """
    if not our_pubkey or not database:
        return {"error": "Not initialized"}

    member = database.get_member(our_pubkey)
    if not member:
        return {"error": "Not a Hive member", "required_tier": required_tier}

    current_tier = member.get('tier', 'neophyte')

    if required_tier == 'member':
        if current_tier != 'member':
            return {
                "error": "permission_denied",
                "message": "This command requires full member privileges",
                "current_tier": current_tier,
                "required_tier": "member"
            }
    # 'neophyte' tier means any member (including neophytes) can use the command

    return None  # Permission granted


def _get_hive_context() -> HiveContext:
    """
    Create a HiveContext with all current global dependencies.

    This bundles the global state for RPC command handlers in modules/rpc_commands.py.
    Note: Some globals may not be initialized yet if init() hasn't completed.

    The safe_plugin field receives the global plugin object directly - pyln-client
    is inherently thread-safe (opens new socket per RPC call).
    """
    # These globals are always defined (may be None before init())
    _database = database if database is not None else None
    _config = config if config is not None else None
    _our_pubkey = our_pubkey if our_pubkey is not None else None
    _vpn_transport = vpn_transport if vpn_transport is not None else None
    _planner = planner if planner is not None else None
    _bridge = bridge if bridge is not None else None
    _intent_mgr = intent_mgr if intent_mgr is not None else None
    _membership_mgr = membership_mgr if membership_mgr is not None else None
    _coop_expansion = coop_expansion if coop_expansion is not None else None
    _contribution_mgr = contribution_mgr if contribution_mgr is not None else None
    _routing_pool = routing_pool if routing_pool is not None else None
    _yield_metrics_mgr = yield_metrics_mgr if yield_metrics_mgr is not None else None
    _liquidity_coord = liquidity_coord if liquidity_coord is not None else None
    _fee_coordination_mgr = fee_coordination_mgr if fee_coordination_mgr is not None else None
    _cost_reduction_mgr = cost_reduction_mgr if cost_reduction_mgr is not None else None
    _rationalization_mgr = rationalization_mgr if rationalization_mgr is not None else None
    _strategic_positioning_mgr = strategic_positioning_mgr if strategic_positioning_mgr is not None else None
    _anticipatory_liquidity_mgr = anticipatory_liquidity_mgr if anticipatory_liquidity_mgr is not None else None
    _nostr_transport = nostr_transport if nostr_transport is not None else None
    _identity_adapter = identity_adapter if identity_adapter is not None else None
    _phase6_plugins = phase6_optional_plugins if isinstance(phase6_optional_plugins, dict) else {}
    _comms_active = bool(_phase6_plugins.get("cl_hive_comms", {}).get("active"))
    _archon_active = bool(_phase6_plugins.get("cl_hive_archon", {}).get("active"))
    _signing_backend = "unknown"
    if isinstance(_identity_adapter, RemoteArchonIdentity):
        _signing_backend = "cl-hive-archon"
    elif _identity_adapter is None:
        _signing_backend = "none"

    # Create a log wrapper that calls plugin.log
    def _log(msg: str, level: str = 'info'):
        plugin.log(msg, level=level)

    return HiveContext(
        database=_database,
        config=_config,
        safe_plugin=plugin,  # Direct plugin access - pyln-client is thread-safe per-call
        our_pubkey=_our_pubkey,
        vpn_transport=_vpn_transport,
        planner=_planner,
        quality_scorer=quality_scorer_mgr if quality_scorer_mgr is not None else None,
        bridge=_bridge,
        intent_mgr=_intent_mgr,
        membership_mgr=_membership_mgr,
        coop_expansion_mgr=_coop_expansion,
        contribution_mgr=_contribution_mgr,
        routing_pool=_routing_pool,
        yield_metrics_mgr=_yield_metrics_mgr,
        liquidity_coordinator=_liquidity_coord,
        fee_coordination_mgr=_fee_coordination_mgr,
        cost_reduction_mgr=_cost_reduction_mgr,
        rationalization_mgr=_rationalization_mgr,
        strategic_positioning_mgr=_strategic_positioning_mgr,
        anticipatory_manager=_anticipatory_liquidity_mgr,
        did_credential_mgr=did_credential_mgr,
        management_schema_registry=management_schema_registry,
        cashu_escrow_mgr=cashu_escrow_mgr,
        nostr_transport=_nostr_transport,
        marketplace_mgr=marketplace_mgr,
        liquidity_mgr=liquidity_mgr,
        traffic_intel_mgr=traffic_intel_mgr,
        nostr_transport_enabled=bool(_nostr_transport),
        comms_active=_comms_active,
        archon_active=_archon_active,
        signing_backend=_signing_backend,
        policy_engine=policy_engine,
        our_id=_our_pubkey or "",
        log=_log,
    )


# =============================================================================
# PLUGIN OPTIONS
# =============================================================================
# Options, config maps, and parsers moved to modules/plugin_options.py
register_options(plugin)


def _detect_phase6_optional_plugins(plugin_obj: Plugin) -> Dict[str, Any]:
    """
    Detect optional Phase 6 sibling plugins.

    This is used for runtime capability selection and status reporting.
    When cl-hive-comms is absent, external transport features are disabled.
    The result is cached in the global phase6_optional_plugins map.
    """
    result: Dict[str, Any] = {
        "cl_hive_comms": {"installed": False, "active": False, "name": ""},
        "cl_hive_archon": {"installed": False, "active": False, "name": ""},
        "warnings": [],
    }

    try:
        try:
            plugins_resp = plugin_obj.rpc.plugin("list")
        except Exception:
            plugins_resp = plugin_obj.rpc.listplugins()

        for entry in plugins_resp.get("plugins", []):
            raw_name = (
                entry.get("name")
                or entry.get("path")
                or entry.get("plugin")
                or ""
            )
            normalized = os.path.basename(str(raw_name)).lower()
            is_active = bool(entry.get("active", False))

            if "cl-hive-comms" in normalized:
                result["cl_hive_comms"] = {
                    "installed": True,
                    "active": is_active,
                    "name": raw_name,
                }
            elif "cl-hive-archon" in normalized:
                result["cl_hive_archon"] = {
                    "installed": True,
                    "active": is_active,
                    "name": raw_name,
                }

        if result["cl_hive_archon"]["active"] and not result["cl_hive_comms"]["active"]:
            result["warnings"].append(
                "cl-hive-archon is active while cl-hive-comms is inactive; "
                "this is not a supported Phase 6 stack."
            )
    except Exception as e:
        result["warnings"].append(f"optional plugin detection failed: {e}")

    return result


def _reload_config_from_cln(plugin_obj: Plugin) -> Dict[str, Any]:
    """
    Reload all hive config options from CLN's current values.

    Call this after using `lightning-cli setconfig` to sync the internal
    config object with CLN's option values.

    Returns dict with list of updated options and any errors.
    """
    global config, vpn_transport

    results = {"updated": [], "errors": [], "vpn_reconfigured": False}

    # Reload standard config options
    for option_name, (attr_name, attr_type) in OPTION_TO_CONFIG_MAP.items():
        try:
            val = plugin_obj.get_option(option_name)
            if val is None:
                continue

            parsed_value = _parse_setconfig_value(val, attr_type)
            old_value = getattr(config, attr_name, None)

            if old_value != parsed_value:
                setattr(config, attr_name, parsed_value)
                results["updated"].append({
                    "option": option_name,
                    "attr": attr_name,
                    "old": old_value,
                    "new": parsed_value
                })

        except (ValueError, TypeError) as e:
            results["errors"].append({"option": option_name, "error": str(e)})

    # Increment config version if anything changed
    if results["updated"]:
        config._version += 1

        # Normalize and validate the new config
        config._normalize()
        validation_error = config.validate()
        if validation_error:
            results["errors"].append({"validation": validation_error})

    # Reload VPN options if VPN transport is active
    if vpn_transport is not None:
        try:
            vpn_result = vpn_transport.configure(
                mode=plugin_obj.get_option('hive-transport-mode'),
                vpn_subnets=plugin_obj.get_option('hive-vpn-subnets'),
                vpn_bind=plugin_obj.get_option('hive-vpn-bind'),
                vpn_peers=plugin_obj.get_option('hive-vpn-peers'),
                required_messages=plugin_obj.get_option('hive-vpn-required-messages')
            )
            results["vpn_reconfigured"] = True
            results["vpn_mode"] = vpn_result.get('mode', 'unknown')
        except Exception as e:
            results["errors"].append({"vpn": str(e)})

    return results


# =============================================================================
# EXTERNAL TRANSPORT PUMP (Coordinated Mode)
# =============================================================================


def _submit_hive_message(peer_id: str, msg_type: HiveMessageType, msg_payload: Dict[str, Any], plugin_obj: Plugin) -> bool:
    """Apply common policy checks and dispatch a validated Hive message."""
    if not peer_id or msg_type is None or not isinstance(msg_payload, dict):
        return False

    # VPN Transport Policy Check
    if vpn_transport and vpn_transport.is_enabled():
        accept, reason = vpn_transport.should_accept_hive_message(
            peer_id=peer_id,
            message_type=msg_type.name if msg_type else "",
        )
        if not accept:
            plugin_obj.log(
                f"cl-hive: VPN policy rejected {msg_type.name} from {peer_id[:16]}...: {reason}",
                level='info'
            )
            return False

    # Dispatch to a background thread so ingress paths return immediately.
    if _msg_executor is not None:
        _msg_executor.submit(_dispatch_hive_message, peer_id, msg_type, msg_payload, plugin_obj)
    else:
        threading.Thread(
            target=_dispatch_hive_message,
            args=(peer_id, msg_type, msg_payload, plugin_obj),
            daemon=True,
        ).start()
    return True


def _handle_external_transport_dm(envelope: Dict[str, Any]) -> None:
    """Decode injected payloads from comms and feed existing Hive dispatch path."""
    try:
        if not isinstance(envelope, dict):
            return

        packet = envelope.get("payload")
        if not isinstance(packet, dict):
            plaintext = envelope.get("plaintext")
            if isinstance(plaintext, str) and plaintext:
                packet = {"raw_plaintext": plaintext}
            else:
                return

        transport_sender = str(envelope.get("pubkey") or "")
        if not transport_sender:
            plugin.log("cl-hive: dropped injected packet (missing authenticated sender)", level="warn")
            return

        claimed_sender = str(packet.get("sender") or "")
        if claimed_sender and claimed_sender != transport_sender:
            plugin.log("cl-hive: dropped injected packet (sender mismatch)", level="warn")
            return

        packet = dict(packet)
        packet["sender"] = transport_sender

        peer_id, msg_type, msg_payload = parse_injected_hive_packet(packet)
        if msg_type is None or not isinstance(msg_payload, dict):
            plugin.log("cl-hive: dropped injected packet (unrecognized format)", level="debug")
            return
        if not peer_id:
            plugin.log("cl-hive: dropped injected packet (missing sender)", level="debug")
            return

        _submit_hive_message(peer_id, msg_type, msg_payload, plugin)
    except Exception as exc:
        plugin.log(f"cl-hive: external transport DM handling error: {exc}", level="warn")


def _external_transport_pump():
    """Drain injected packets from ExternalCommsTransport and dispatch to DM callbacks."""
    while not shutdown_event.is_set():
        try:
            if nostr_transport and isinstance(nostr_transport, ExternalCommsTransport):
                nostr_transport.process_inbound()
        except Exception as exc:
            plugin.log(f"cl-hive: external transport pump error: {exc}", level="warn")
        shutdown_event.wait(0.1)


# =============================================================================
# INITIALIZATION
# =============================================================================

@plugin.init()
def init(options: Dict[str, Any], configuration: Dict[str, Any], plugin: Plugin, **kwargs):
    """
    Initialize the cl-hive plugin.
    
    Steps:
    1. Parse and validate options
    2. Initialize database
    3. Initialize handshake manager
    4. Verify cl-revenue-ops dependency
    5. Set up signal handlers for graceful shutdown

    Note: pyln-client is inherently thread-safe (opens new socket per RPC call),
    so no RPC locking is needed. The global 'plugin' object is used directly.
    """
    global database, config, handshake_mgr, state_manager, gossip_mgr, intent_mgr, our_pubkey, bridge, vpn_transport, relay_mgr, phase6_optional_plugins

    plugin.log("cl-hive: Initializing Swarm Intelligence layer...")
    
    # Build configuration from options
    config = HiveConfig(
        db_path=options.get('hive-db-path', '~/.lightning/cl_hive.db'),
        governance_mode=options.get('hive-governance-mode', 'advisor'),
        membership_enabled=_parse_bool(options.get('hive-membership-enabled', 'true')),
        auto_join_enabled=_parse_bool(options.get('hive-auto-join', 'false')),
        auto_vouch_enabled=_parse_bool(options.get('hive-auto-vouch', 'true')),
        auto_promote_enabled=_parse_bool(options.get('hive-auto-promote', 'true')),
        ban_autotrigger_enabled=_parse_bool(options.get('hive-ban-autotrigger', 'false')),
        neophyte_fee_discount_pct=float(options.get('hive-neophyte-fee-discount', '0.5')),
        member_fee_ppm=int(options.get('hive-member-fee-ppm', '0')),
        probation_days=int(options.get('hive-probation-days', '90')),
        max_members=int(options.get('hive-max-members', '50')),
        market_share_cap_pct=float(options.get('hive-market-share-cap', '0.20')),
        intent_hold_seconds=int(options.get('hive-intent-hold-seconds', '60')),
        gossip_threshold_pct=float(options.get('hive-gossip-threshold', '0.10')),
        heartbeat_interval=int(options.get('hive-heartbeat-interval', '300')),
        planner_interval=int(options.get('hive-planner-interval', '3600')),
        planner_enable_expansions=_parse_bool(options.get('hive-planner-enable-expansions', 'false')),
        planner_min_channel_sats=int(options.get('hive-planner-min-channel-sats', '1000000')),
        planner_max_channel_sats=int(options.get('hive-planner-max-channel-sats', '50000000')),
        planner_default_channel_sats=int(options.get('hive-planner-default-channel-sats', '5000000')),
        planner_max_active_channels=int(options.get('hive-planner-max-active-channels', '50')),
        # Budget options (failsafe mode)
        failsafe_budget_per_day=int(options.get('hive-failsafe-budget-per-day', '10000000')),
        budget_reserve_pct=float(options.get('hive-budget-reserve-pct', '0.20')),
        budget_max_per_channel_pct=float(options.get('hive-budget-max-per-channel-pct', '0.50')),
        max_expansion_feerate_perkb=int(options.get('hive-max-expansion-feerate', '5000')),
        rpc_pool_size=int(options.get('hive-rpc-pool-size', '3')),
    )

    # Initialize RPC pool (Phase 3 — bounded execution via subprocess isolation)
    # Resolve the CLN RPC socket path for pool workers.
    # NOTE: We start the pool now but install the proxy at the END of init.
    # Reason: spawn-context workers take several seconds to start, but init
    # needs immediate RPC calls (getinfo, listpeerchannels, setchannel).
    # By the end of init, workers are ready for background thread use.
    global _rpc_pool, _msg_executor, _batched_log_writer
    _msg_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="hive_msg")

    # Install batched log writer to prevent IO thread starvation.
    # Must be BEFORE any background loops start logging.
    _batched_log_writer = BatchedLogWriter(plugin)

    _rpc_socket_path = getattr(plugin.rpc, "socket_path", None)
    if not _rpc_socket_path:
        ldir = configuration.get("lightning-dir") or configuration.get("lightning_dir")
        rpcfile = configuration.get("rpc-file") or configuration.get("rpc_file")
        if ldir and rpcfile:
            _rpc_socket_path = rpcfile if os.path.isabs(rpcfile) else os.path.join(ldir, rpcfile)
    if not _rpc_socket_path:
        ldir = configuration.get("lightning-dir") or "~/.lightning"
        _rpc_socket_path = os.path.expanduser(os.path.join(ldir, "lightning-rpc"))

    _rpc_pool = RpcPool(
        socket_path=str(_rpc_socket_path),
        log_fn=lambda msg, level="info": plugin.log(msg, level=level),
        pool_size=config.rpc_pool_size,
    )
    plugin.log(f"cl-hive: RPC pool started (workers={config.rpc_pool_size}, socket={_rpc_socket_path})")

    # Initialize database
    database = HiveDatabase(config.db_path, plugin)
    database.initialize()
    plugin.log(f"cl-hive: Database initialized at {config.db_path}")

    # Initialize handshake manager
    handshake_mgr = HandshakeManager(
        plugin.rpc, database, plugin
    )
    plugin.log("cl-hive: Handshake manager initialized")
    
    # Initialize state manager (Phase 2)
    state_manager = StateManager(database, plugin)
    state_manager.load_from_database()
    plugin.log(f"cl-hive: State manager initialized ({len(state_manager.get_all_peer_states())} peers cached)")
    
    # Initialize gossip manager (Phase 2)
    gossip_mgr = GossipManager(
        state_manager,
        plugin,
        heartbeat_interval=config.heartbeat_interval,
        get_membership_hash=database.get_membership_hash
    )
    plugin.log("cl-hive: Gossip manager initialized")
    
    # Initialize intent manager (Phase 3)
    # Get our pubkey for tie-breaker logic
    our_pubkey = plugin.rpc.getinfo().get('id', '')

    # Detect Phase 6 sibling plugins (used for runtime capability selection)
    phase6_optional_plugins = _detect_phase6_optional_plugins(plugin)
    comms = phase6_optional_plugins["cl_hive_comms"]
    archon = phase6_optional_plugins["cl_hive_archon"]
    plugin.log(
        "cl-hive: Sibling plugins - "
        f"cl-hive-comms(active={comms['active']}, installed={comms['installed']}), "
        f"cl-hive-archon(active={archon['active']}, installed={archon['installed']})"
    )
    for warning in phase6_optional_plugins.get("warnings", []):
        plugin.log(f"cl-hive: {warning}", level="warn")

    # Sync gossip version from persisted state to avoid version reset on restart
    gossip_mgr.sync_version_from_state_manager(our_pubkey)

    # Initialize relay manager for gossip propagation in non-mesh topologies
    def _relay_send_message(peer_id: str, message_bytes: bytes) -> bool:
        """Send message to peer for relay."""
        try:
            plugin.rpc.call("sendcustommsg", {
                "node_id": peer_id,
                "msg": message_bytes.hex()
            })
            return True
        except Exception:
            return False

    def _relay_get_members() -> list:
        """Get list of member pubkeys for relay (excludes banned)."""
        if not database:
            return []
        return [
            m["peer_id"] for m in database.get_all_members()
            if m.get("tier") == MembershipTier.MEMBER.value
            and not database.is_banned(m["peer_id"])
        ]

    relay_mgr = RelayManager(
        our_pubkey=our_pubkey,
        send_message=_relay_send_message,
        get_members=_relay_get_members,
        log=lambda msg, level: plugin.log(f"[Relay] {msg}", level=level)
    )
    plugin.log("cl-hive: Relay manager initialized (TTL-based gossip propagation)")

    intent_mgr = IntentManager(
        database,
        plugin,
        our_pubkey=our_pubkey,
        hold_seconds=config.intent_hold_seconds,
        expire_seconds=config.intent_expire_seconds
    )
    plugin.log("cl-hive: Intent manager initialized")
    
    # Collect background loop threads — started after init_background_loops() injects deps
    _deferred_threads = []

    # Background threads (Phase 3)
    _deferred_threads.append(threading.Thread(
        target=background_loops.intent_monitor_loop,
        name="cl-hive-intent-monitor",
        daemon=True
    ))
    
    # Initialize Integration Bridge (Phase 4)
    # Uses Circuit Breaker pattern for resilient cl-revenue-ops integration
    bridge = Bridge(plugin.rpc, plugin)
    bridge_status = bridge.initialize()
    
    if bridge_status == BridgeStatus.ENABLED:
        plugin.log(f"cl-hive: Bridge ENABLED - cl-revenue-ops {bridge._revenue_ops_version}")
    elif bridge_status == BridgeStatus.DEGRADED:
        plugin.log("cl-hive: Bridge DEGRADED - some features unavailable", level='warn')
    else:
        plugin.log(
            "cl-hive: Bridge DISABLED - cl-revenue-ops not detected or incompatible. "
            "Hive policy integration will be unavailable. Recommended: v1.4.0+",
            level='warn'
        )

    # Initialize contribution and membership managers (Phase 5)
    global contribution_mgr, membership_mgr
    contribution_mgr = ContributionManager(plugin.rpc, database, plugin, config)
    membership_mgr = MembershipManager(
        database,
        state_manager,
        contribution_mgr,
        bridge,
        config,
        plugin
    )
    plugin.log("cl-hive: Membership and contribution managers initialized")

    # Membership maintenance thread (Phase 5)
    _deferred_threads.append(threading.Thread(
        target=background_loops.membership_maintenance_loop,
        name="cl-hive-membership-maintenance",
        daemon=True
    ))

    # Sync bridge policies with database state on startup
    # This ensures members have correct 0 ppm policy even if previous set_tier failed
    try:
        synced = membership_mgr.sync_bridge_policies()
        if synced > 0:
            plugin.log(f"cl-hive: Synced bridge policies for {synced} members")
    except Exception as e:
        plugin.log(f"cl-hive: Failed to sync bridge policies: {e}", level="warn")

    # Initialize local node presence for settlement uptime tracking (Bug fix #1)
    # Without this, the local node shows 0% uptime in settlement calculations
    if our_pubkey:
        try:
            database.update_presence(our_pubkey, is_online=True, now_ts=int(time.time()), 
                                    window_seconds=30 * 86400)
            plugin.log(f"cl-hive: Initialized local node presence for settlement uptime")
        except Exception as e:
            plugin.log(f"cl-hive: Failed to initialize local presence: {e}", level="warn")
    
    # Sync uptime from presence data to hive_members on startup
    try:
        uptime_synced = database.sync_uptime_from_presence(window_seconds=30 * 86400)
        if uptime_synced > 0:
            plugin.log(f"cl-hive: Synced uptime for {uptime_synced} member(s)")
    except Exception as e:
        plugin.log(f"cl-hive: Failed to sync uptime: {e}", level="warn")

    # HIVE_SAFETY: Scan existing channels to hive members and enforce 0 fee
    # This catches cases where fees were set before joining the hive or by external tools
    try:
        hive_members = {m["peer_id"] for m in database.get_all_members()}
        if hive_members:
            channels = plugin.rpc.listpeerchannels()
            fixed_count = 0
            for peer in channels.get("channels", []):
                peer_id = peer.get("peer_id")
                if peer_id not in hive_members:
                    continue
                # Check if this is our channel (we set fees on our end)
                fee_base = peer.get("fee_base_msat", 0)
                fee_ppm = peer.get("fee_proportional_millionths", 0)
                channel_id = peer.get("short_channel_id")
                if channel_id and (fee_base > 0 or fee_ppm > 0):
                    try:
                        plugin.rpc.setchannel(
                            id=channel_id,
                            feebase=0,
                            feeppm=0
                        )
                        fixed_count += 1
                        plugin.log(
                            f"cl-hive: HIVE_SAFETY: Fixed non-zero fee on channel {channel_id} to member {peer_id[:16]}... "
                            f"(was {fee_base}msat base, {fee_ppm}ppm)",
                            level='info'
                        )
                    except Exception as e:
                        plugin.log(
                            f"cl-hive: HIVE_SAFETY: Failed to fix fee on {channel_id}: {e}",
                            level='warn'
                        )
            if fixed_count > 0:
                plugin.log(f"cl-hive: HIVE_SAFETY: Fixed fees on {fixed_count} hive member channel(s)")
    except Exception as e:
        plugin.log(f"cl-hive: HIVE_SAFETY startup scan failed: {e}", level="warn")

    # Initialize DecisionEngine (Phase 7)
    global decision_engine
    decision_engine = DecisionEngine(database=database, plugin=plugin)
    plugin.log("cl-hive: DecisionEngine initialized")

    # Initialize VPN Transport Manager
    vpn_transport = VPNTransportManager(plugin=plugin)
    vpn_result = vpn_transport.configure(
        mode=options.get('hive-transport-mode', 'any'),
        vpn_subnets=options.get('hive-vpn-subnets', ''),
        vpn_bind=options.get('hive-vpn-bind', ''),
        vpn_peers=options.get('hive-vpn-peers', ''),
        required_messages=options.get('hive-vpn-required-messages', 'all')
    )
    if vpn_transport.is_enabled():
        plugin.log(f"cl-hive: VPN transport ENABLED - mode={vpn_result['mode']}, subnets={len(vpn_result['subnets'])}")
    else:
        plugin.log("cl-hive: VPN transport configured (mode=any, not enforcing)")

    # Initialize Planner (Phase 6)
    global planner
    planner = Planner(
        state_manager=state_manager,
        database=database,
        bridge=bridge,
        plugin=plugin,
        intent_manager=intent_mgr,
        decision_engine=decision_engine
    )
    plugin.log("cl-hive: Planner initialized")

    # Planner loop thread (Phase 6)
    _deferred_threads.append(threading.Thread(
        target=background_loops.planner_loop,
        name="cl-hive-planner",
        daemon=True
    ))

    # Initialize Cooperative Expansion Manager (Phase 6.4)
    global coop_expansion, quality_scorer_mgr
    quality_scorer = PeerQualityScorer(database, plugin)
    quality_scorer_mgr = quality_scorer
    coop_expansion = CooperativeExpansionManager(
        database=database,
        quality_scorer=quality_scorer,
        plugin=plugin,
        our_id=our_pubkey,
        config_getter=lambda: config  # Provides access to budget settings
    )
    plugin.log("cl-hive: Cooperative expansion manager initialized")

    # Initialize Fee Intelligence Manager (Phase 7 - Cooperative Fee Coordination)
    global fee_intel_mgr
    fee_intel_mgr = FeeIntelligenceManager(
        database=database,
        plugin=plugin,
        our_pubkey=our_pubkey
    )
    plugin.log("cl-hive: Fee intelligence manager initialized")

    # Initialize Health Score Aggregator (Phase 7 - NNLB)
    global health_aggregator
    health_aggregator = HealthScoreAggregator(
        database=database,
        plugin=plugin
    )
    plugin.log("cl-hive: Health aggregator initialized")

    # Fee intelligence background thread (Phase 7)
    _deferred_threads.append(threading.Thread(
        target=background_loops.fee_intelligence_loop,
        name="cl-hive-fee-intelligence",
        daemon=True
    ))

    # Gossip loop thread (broadcasts capacity/state to hive members)
    _deferred_threads.append(threading.Thread(
        target=background_loops.gossip_loop,
        name="cl-hive-gossip",
        daemon=True
    ))

    # Distributed settlement loop thread (Phase 12)
    _deferred_threads.append(threading.Thread(
        target=background_loops.settlement_loop,
        name="cl-hive-settlement",
        daemon=True
    ))

    # Load persisted fee tracking state (Settlement Phase)
    _load_fee_tracking_state()
    plugin.log("cl-hive: Fee tracking state loaded")

    # Initialize Liquidity Coordinator (Phase 7.3 - Cooperative Rebalancing)
    global liquidity_coord
    liquidity_coord = LiquidityCoordinator(
        database=database,
        plugin=plugin,
        our_pubkey=our_pubkey,
        fee_intel_mgr=fee_intel_mgr,
        state_manager=state_manager
    )
    plugin.log("cl-hive: Liquidity coordinator initialized")

    # Initialize Splice Coordinator (Phase 3 - Splice Coordination)
    global splice_coord
    splice_coord = SpliceCoordinator(
        database=database,
        plugin=plugin,
        state_manager=state_manager
    )
    plugin.log("cl-hive: Splice coordinator initialized")

    # Link cooperation modules to Planner (Phase 7 - Cooperation Module Synergies)
    # These modules were initialized after the planner, so we set them via setter
    planner.set_cooperation_modules(
        liquidity_coordinator=liquidity_coord,
        splice_coordinator=splice_coord,
        health_aggregator=health_aggregator,
        cooperative_expansion=coop_expansion
    )
    plugin.log("cl-hive: Planner linked to cooperation modules")

    # Initialize Routing Map (Phase 7.4 - Routing Intelligence)
    global routing_map
    routing_map = HiveRoutingMap(
        database=database,
        plugin=plugin,
        our_pubkey=our_pubkey
    )
    # Load existing probes from database
    routing_map.aggregate_from_database()
    plugin.log("cl-hive: Routing map initialized")

    # Initialize Peer Reputation Manager (Phase 5 - Advanced Cooperation)
    global peer_reputation_mgr
    peer_reputation_mgr = PeerReputationManager(
        database=database,
        plugin=plugin,
        our_pubkey=our_pubkey
    )
    # Load existing reputation data from database
    peer_reputation_mgr.aggregate_from_database()
    plugin.log("cl-hive: Peer reputation manager initialized")

    # Initialize Routing Pool (Phase 0 - Collective Economics)
    global routing_pool
    routing_pool = RoutingPool(
        database=database,
        plugin=plugin,
        state_manager=state_manager
    )
    routing_pool.set_our_pubkey(our_pubkey)
    plugin.log("cl-hive: Routing pool initialized (collective economics)")

    # Initialize Network Metrics Calculator (shared module)
    network_metrics.init_calculator(
        state_manager=state_manager,
        database=database,
        plugin=plugin
    )
    plugin.log("cl-hive: Network metrics calculator initialized")

    # Initialize Settlement Manager (BOLT12 revenue distribution)
    global settlement_mgr
    settlement_mgr = SettlementManager(
        database=database,
        plugin=plugin,
        rpc=plugin.rpc
    )
    settlement_mgr.initialize_tables()
    plugin.log("cl-hive: Settlement manager initialized (BOLT12 payouts)")

    # Initialize Yield Metrics Manager (Phase 1 - Metrics & Measurement)
    global yield_metrics_mgr
    yield_metrics_mgr = YieldMetricsManager(
        database=database,
        plugin=plugin,
        state_manager=state_manager
    )
    yield_metrics_mgr.set_our_pubkey(our_pubkey)
    plugin.log("cl-hive: Yield metrics manager initialized")

    # Initialize Fee Coordination Manager (Phase 2 - Fee Coordination)
    global fee_coordination_mgr
    fee_coordination_mgr = FeeCoordinationManager(
        database=database,
        plugin=plugin,
        state_manager=state_manager,
        liquidity_coordinator=liquidity_coord,
        gossip_mgr=gossip_mgr
    )
    fee_coordination_mgr.set_our_pubkey(our_pubkey)
    fee_coordination_mgr.set_fee_intelligence_mgr(fee_intel_mgr)
    plugin.log("cl-hive: Fee coordination manager initialized")

    # Restore persisted routing intelligence
    try:
        restored = fee_coordination_mgr.restore_state_from_database()
        plugin.log(f"cl-hive: Restored routing intelligence "
                   f"(pheromones={restored['pheromones']}, markers={restored['markers']}, "
                   f"defense_reports={restored.get('defense_reports', 0)}, "
                   f"defense_fees={restored.get('defense_fees', 0)}, "
                   f"remote_pheromones={restored.get('remote_pheromones', 0)}, "
                   f"fee_observations={restored.get('fee_observations', 0)})")
    except Exception as e:
        plugin.log(f"cl-hive: Failed to restore routing intelligence: {e}", level='warn')

    # Initialize Cost Reduction Manager (Phase 3 - Cost Reduction)
    global cost_reduction_mgr
    cost_reduction_mgr = CostReductionManager(
        plugin=plugin,
        database=database,
        state_manager=state_manager,
        yield_metrics_mgr=yield_metrics_mgr,
        liquidity_coordinator=liquidity_coord
    )
    cost_reduction_mgr.set_our_pubkey(our_pubkey)
    plugin.log("cl-hive: Cost reduction manager initialized")

    # MCF optimization background thread (Phase 15)
    _deferred_threads.append(threading.Thread(
        target=background_loops.mcf_optimization_loop,
        name="cl-hive-mcf-optimization",
        daemon=True
    ))

    # Initialize Rationalization Manager (Channel Rationalization)
    global rationalization_mgr
    rationalization_mgr = RationalizationManager(
        plugin=plugin,
        database=database,
        state_manager=state_manager,
        fee_coordination_mgr=fee_coordination_mgr,
        governance=decision_engine
    )
    rationalization_mgr.set_our_pubkey(our_pubkey)
    plugin.log("cl-hive: Rationalization manager initialized")

    # Wire rationalization manager to cooperative expansion (slime mold coordination)
    if coop_expansion:
        coop_expansion.set_rationalization_manager(rationalization_mgr)
        plugin.log("cl-hive: Cooperative expansion linked to rationalization (redundancy checks enabled)")

    # Initialize Strategic Positioning Manager (Phase 5 - Strategic Positioning)
    global strategic_positioning_mgr
    strategic_positioning_mgr = StrategicPositioningManager(
        plugin=plugin,
        database=database,
        state_manager=state_manager,
        fee_coordination_mgr=fee_coordination_mgr,
        yield_metrics_mgr=yield_metrics_mgr,
        planner=planner
    )
    strategic_positioning_mgr.set_our_pubkey(our_pubkey)
    plugin.log("cl-hive: Strategic positioning manager initialized")

    # Initialize Anticipatory Liquidity Manager (Phase 7.1 - Anticipatory Liquidity)
    global anticipatory_liquidity_mgr
    anticipatory_liquidity_mgr = AnticipatoryLiquidityManager(
        database=database,
        plugin=plugin,
        state_manager=state_manager,
        our_id=our_pubkey
    )
    plugin.log("cl-hive: Anticipatory liquidity manager initialized")

    # Initialize Traffic Intelligence Manager (Phase 14 - Traffic Intelligence)
    global traffic_intel_mgr
    traffic_intel_mgr = TrafficIntelligenceManager(
        database=database,
        plugin=plugin,
        our_pubkey=our_pubkey,
        anticipatory_mgr=anticipatory_liquidity_mgr,
        liquidity_coordinator=liquidity_coord,
        membership_mgr=membership_mgr,
    )
    plugin.log("cl-hive: Traffic intelligence manager initialized")

    # Phase 3c: Wire traffic intelligence into fee coordination
    fee_coordination_mgr.set_traffic_intel_mgr(traffic_intel_mgr)

    # Initialize Task Manager (Phase 10 - Task Delegation Protocol)
    global task_mgr
    task_mgr = TaskManager(
        database=database,
        plugin=plugin,
        our_pubkey=our_pubkey
    )
    plugin.log("cl-hive: Task manager initialized")

    # Initialize Splice Manager (Phase 11 - Hive-Splice Coordination)
    global splice_mgr
    splice_mgr = SpliceManager(
        database=database,
        plugin=plugin,
        splice_coordinator=splice_coord,
        our_pubkey=our_pubkey
    )
    plugin.log("cl-hive: Splice manager initialized")

    # Initialize Outbox Manager (Phase D - Reliable Delivery)
    global outbox_mgr
    outbox_mgr = OutboxManager(
        database=database,
        send_fn=protocol_handlers._outbox_send_fn,
        get_members_fn=protocol_handlers._outbox_get_member_ids,
        our_pubkey=our_pubkey,
        log_fn=lambda msg, level='info': plugin.log(msg, level=level),
    )
    plugin.log("cl-hive: Outbox manager initialized")

    # Outbox retry background thread
    _deferred_threads.append(threading.Thread(
        target=background_loops.outbox_retry_loop,
        name="cl-hive-outbox-retry",
        daemon=True
    ))

    _phase6_plugins = phase6_optional_plugins if isinstance(phase6_optional_plugins, dict) else {}
    _comms_active = bool(_phase6_plugins.get("cl_hive_comms", {}).get("active"))
    _archon_active = bool(_phase6_plugins.get("cl_hive_archon", {}).get("active"))
    _companion_stack_active = _comms_active and _archon_active

    # Phase 16 / Phase 5 ecosystem features are optional and require the
    # companion plugin stack (comms + archon) to be active.
    global did_credential_mgr
    global management_schema_registry
    global cashu_escrow_mgr
    did_credential_mgr = None
    management_schema_registry = None
    cashu_escrow_mgr = None

    if _companion_stack_active:
        # Phase 16: DID Credential Manager
        did_credential_mgr = DIDCredentialManager(
            database=database,
            plugin=plugin,
            rpc=plugin.rpc,
            our_pubkey=our_pubkey,
        )
        plugin.log("cl-hive: DID credential manager initialized")

        # Phase 2: Management Schema Registry
        management_schema_registry = ManagementSchemaRegistry(
            database=database,
            plugin=plugin,
            rpc=plugin.rpc,
            our_pubkey=our_pubkey,
        )
        plugin.log("cl-hive: Management schema registry initialized")
    else:
        plugin.log(
            "cl-hive: DID/schema/cashu/marketplace features disabled "
            "(requires active cl-hive-comms and cl-hive-archon companion plugins)",
            level='info'
        )

    # Wire DID credential manager into planner for reputation-weighted expansion
    if planner and did_credential_mgr:
        planner.did_credential_mgr = did_credential_mgr

    # Wire DID credential manager into membership manager for promotion signals
    if membership_mgr and did_credential_mgr:
        membership_mgr.did_credential_mgr = did_credential_mgr

    # Wire DID credential manager into settlement manager for reputation metadata
    if settlement_mgr and did_credential_mgr:
        settlement_mgr.did_credential_mgr = did_credential_mgr

    if _companion_stack_active:
        # DID maintenance background thread
        _deferred_threads.append(threading.Thread(
            target=background_loops.did_maintenance_loop,
            name="cl-hive-did-maintenance",
            daemon=True
        ))

        # Phase 4A: Cashu Escrow Manager
        mint_urls_str = plugin.get_option('hive-cashu-mints')
        acceptable_mints = [u.strip() for u in mint_urls_str.split(',') if u.strip()] if mint_urls_str else []
        cashu_escrow_mgr = CashuEscrowManager(
            database=database,
            plugin=plugin,
            rpc=plugin.rpc,
            our_pubkey=our_pubkey,
            acceptable_mints=acceptable_mints,
        )
        plugin.log("cl-hive: Cashu escrow manager initialized")

        # Phase 4B: Wire extended settlement types into settlement manager
        if settlement_mgr and cashu_escrow_mgr:
            settlement_mgr.register_extended_types(cashu_escrow_mgr, did_credential_mgr)
            plugin.log("cl-hive: Extended settlement types registered")

        # Escrow maintenance background thread
        _deferred_threads.append(threading.Thread(
            target=background_loops.escrow_maintenance_loop,
            name="cl-hive-escrow-maintenance",
            daemon=True
        ))

    # Phase 5A/6: Nostr transport — external companion plugin only (cl-hive-comms)
    global nostr_transport
    try:
        comms_active = phase6_optional_plugins["cl_hive_comms"]["active"]

        if comms_active:
            # Delegate transport to cl-hive-comms
            nostr_transport = ExternalCommsTransport(plugin=plugin)
            nostr_transport.receive_dm(_handle_external_transport_dm)
            identity = nostr_transport.get_identity()
            plugin.log(
                f"cl-hive: Using External Transport (cl-hive-comms), "
                f"pubkey={identity.get('pubkey', '')[:16]}..."
            )
            # Start inbound pump thread to drain injected packets
            threading.Thread(
                target=_external_transport_pump,
                daemon=True,
                name="cl-hive-ext-pump",
            ).start()
        else:
            nostr_transport = None
            relays_opt = plugin.get_option('hive-nostr-relays')
            if relays_opt:
                plugin.log(
                    "cl-hive: hive-nostr-relays is ignored; internal Nostr transport has been removed",
                    level='warn'
                )
            plugin.log(
                "cl-hive: Nostr transport disabled (cl-hive-comms not active; "
                "companion plugin is optional, transport features unavailable)",
                level='warn'
            )
    except Exception as e:
        nostr_transport = None
        plugin.log(f"cl-hive: Nostr transport disabled (init error): {e}", level='warn')

    # Phase 6: Identity adapter — optional archon delegation, local signing remains supported
    global identity_adapter
    try:
        archon_active = phase6_optional_plugins["cl_hive_archon"]["active"]
        if archon_active:
            identity_adapter = RemoteArchonIdentity(plugin=plugin)
            _rpc_pool_mod.identity_adapter = identity_adapter
            plugin.log("cl-hive: Using Remote Identity (cl-hive-archon)")
        else:
            identity_adapter = None
            _rpc_pool_mod.identity_adapter = None
            plugin.log(
                "cl-hive: Identity adapter not available; "
                "install cl-hive-archon for delegated signing"
            )
    except Exception as e:
        identity_adapter = None
        _rpc_pool_mod.identity_adapter = None
        plugin.log(f"cl-hive: Identity adapter disabled (init error): {e}", level='warn')

    # Phase 5B/5C marketplace features (only with companion stack)
    global marketplace_mgr
    global liquidity_mgr
    marketplace_mgr = None
    liquidity_mgr = None
    if _companion_stack_active:
        try:
            marketplace_mgr = MarketplaceManager(
                database=database,
                plugin=plugin,
                nostr_transport=nostr_transport,
                did_credential_mgr=did_credential_mgr,
                management_schema_registry=management_schema_registry,
                cashu_escrow_mgr=cashu_escrow_mgr,
            )
            plugin.log("cl-hive: Marketplace manager initialized")
        except Exception as e:
            marketplace_mgr = None
            plugin.log(f"cl-hive: Marketplace manager disabled (init error): {e}", level='warn')

        try:
            liquidity_mgr = LiquidityMarketplaceManager(
                database=database,
                plugin=plugin,
                nostr_transport=nostr_transport,
                cashu_escrow_mgr=cashu_escrow_mgr,
                settlement_mgr=settlement_mgr,
                did_credential_mgr=did_credential_mgr,
            )
            plugin.log("cl-hive: Liquidity marketplace manager initialized")
        except Exception as e:
            liquidity_mgr = None
            plugin.log(f"cl-hive: Liquidity manager disabled (init error): {e}", level='warn')

        _deferred_threads.append(threading.Thread(
            target=background_loops.marketplace_maintenance_loop,
            name="cl-hive-marketplace-maintenance",
            daemon=True,
        ))
        _deferred_threads.append(threading.Thread(
            target=background_loops.liquidity_maintenance_loop,
            name="cl-hive-liquidity-maintenance",
            daemon=True,
        ))

    # Link anticipatory manager to fee coordination for time-based fees (Phase 7.4)
    if fee_coordination_mgr:
        fee_coordination_mgr.set_anticipatory_manager(anticipatory_liquidity_mgr)
        plugin.log("cl-hive: Time-based fee adjustment enabled")

    # Link defense system to peer reputation manager for collective warnings
    if fee_coordination_mgr and peer_reputation_mgr:
        fee_coordination_mgr.defense_system.set_peer_reputation_manager(peer_reputation_mgr)
        plugin.log("cl-hive: Defense system linked to peer reputation (collective warnings enabled)")

    # Link yield optimization modules to Planner (Slime mold coordination)
    # These enable the planner to avoid redundant opens and prioritize high-value corridors
    planner.set_cooperation_modules(
        rationalization_mgr=rationalization_mgr,
        strategic_positioning_mgr=strategic_positioning_mgr
    )
    plugin.log("cl-hive: Planner linked to yield optimization modules (slime mold mode)")

    # Initialize rate limiter for PEER_AVAILABLE messages (Security Enhancement)
    global peer_available_limiter
    peer_available_limiter = RateLimiter(max_per_minute=10, window_seconds=60)
    plugin.log("cl-hive: Rate limiter initialized (10 msg/min per peer)")

    # Inject all globals into the protocol_handlers module so that moved
    # handler functions can reference the same variable names they always did.
    protocol_handlers.init_protocol_handlers({
        'plugin': plugin,
        'database': database,
        'config': config,
        'shutdown_event': shutdown_event,
        'our_pubkey': our_pubkey,
        'handshake_mgr': handshake_mgr,
        'gossip_mgr': gossip_mgr,
        'state_manager': state_manager,
        'intent_mgr': intent_mgr,
        'membership_mgr': membership_mgr,
        'contribution_mgr': contribution_mgr,
        'bridge': bridge,
        'vpn_transport': vpn_transport,
        'relay_mgr': relay_mgr,
        'coop_expansion': coop_expansion,
        'fee_intel_mgr': fee_intel_mgr,
        'health_aggregator': health_aggregator,
        'liquidity_coord': liquidity_coord,
        'routing_map': routing_map,
        'peer_reputation_mgr': peer_reputation_mgr,
        'routing_pool': routing_pool,
        'settlement_mgr': settlement_mgr,
        'yield_metrics_mgr': yield_metrics_mgr,
        'fee_coordination_mgr': fee_coordination_mgr,
        'cost_reduction_mgr': cost_reduction_mgr,
        'rationalization_mgr': rationalization_mgr,
        'strategic_positioning_mgr': strategic_positioning_mgr,
        'anticipatory_liquidity_mgr': anticipatory_liquidity_mgr,
        'task_mgr': task_mgr,
        'splice_mgr': splice_mgr,
        'outbox_mgr': outbox_mgr,
        'did_credential_mgr': did_credential_mgr,
        'management_schema_registry': management_schema_registry,
        'cashu_escrow_mgr': cashu_escrow_mgr,
        'traffic_intel_mgr': traffic_intel_mgr,
        'peer_available_limiter': peer_available_limiter,
        'outbox': outbox_mgr,  # handlers reference 'outbox' for the outbox manager
        # Fee tracking state
        '_local_fees_lock': _local_fees_lock,
        '_local_fees_earned_sats': _local_fees_earned_sats,
        '_local_fees_forward_count': _local_fees_forward_count,
        '_local_fees_period_start': _local_fees_period_start,
        '_local_fees_last_broadcast': _local_fees_last_broadcast,
        '_local_fees_last_broadcast_amount': _local_fees_last_broadcast_amount,
        '_local_rebalance_costs_sats': _local_rebalance_costs_sats,
        'FEE_BROADCAST_MIN_SATS': FEE_BROADCAST_MIN_SATS,
        'FEE_BROADCAST_MIN_INTERVAL': FEE_BROADCAST_MIN_INTERVAL,
        'PHASE4B_RATE_LIMITS': PHASE4B_RATE_LIMITS,
        '_phase4b_rate_lock': _phase4b_rate_lock,
        '_phase4b_rate_windows': _phase4b_rate_windows,
        '_phase4b_netting_lock': _phase4b_netting_lock,
        '_phase4b_netting_proposals': _phase4b_netting_proposals,
    })
    plugin.log("cl-hive: Protocol handlers initialized")

    # Inject all globals into the background_loops module so that moved
    # loop functions can reference the same variable names they always did.
    background_loops.init_background_loops({
        'plugin': plugin,
        'database': database,
        'config': config,
        'shutdown_event': shutdown_event,
        'our_pubkey': our_pubkey,
        'state_manager': state_manager,
        'intent_mgr': intent_mgr,
        'membership_mgr': membership_mgr,
        'contribution_mgr': contribution_mgr,
        'did_credential_mgr': did_credential_mgr,
        'cashu_escrow_mgr': cashu_escrow_mgr,
        'marketplace_mgr': marketplace_mgr,
        'liquidity_mgr': liquidity_mgr,
        'outbox_mgr': outbox_mgr,
        'planner': planner,
        'coop_expansion': coop_expansion,
        'fee_intel_mgr': fee_intel_mgr,
        'gossip_mgr': gossip_mgr,
        'bridge': bridge,
        'routing_map': routing_map,
        'peer_reputation_mgr': peer_reputation_mgr,
        'fee_coordination_mgr': fee_coordination_mgr,
        'yield_metrics_mgr': yield_metrics_mgr,
        'anticipatory_liquidity_mgr': anticipatory_liquidity_mgr,
        'strategic_positioning_mgr': strategic_positioning_mgr,
        'rationalization_mgr': rationalization_mgr,
        'cost_reduction_mgr': cost_reduction_mgr,
        'traffic_intel_mgr': traffic_intel_mgr,
        'settlement_mgr': settlement_mgr,
        'liquidity_coord': liquidity_coord,
        'routing_pool': routing_pool,
        'splice_mgr': splice_mgr,
        'BAN_PROPOSAL_TTL_SECONDS': protocol_handlers.BAN_PROPOSAL_TTL_SECONDS,
    })
    plugin.log("cl-hive: Background loops initialized")

    # Start all deferred background loop threads now that deps are injected
    for t in _deferred_threads:
        t.start()
    plugin.log(f"cl-hive: Started {len(_deferred_threads)} background threads")

    # Remove ghost members (gone from gossip graph) BEFORE syncing policies,
    # so stale members don't get hive strategy re-applied on startup.
    ghost_removed = protocol_handlers._cleanup_ghost_members()
    if ghost_removed > 0:
        plugin.log(f"cl-hive: Removed {ghost_removed} ghost member(s) on startup")

    # Sync fee policies for existing members (Phase 4 integration)
    if bridge and bridge.status == BridgeStatus.ENABLED:
        protocol_handlers._sync_member_policies(plugin)

    # Broadcast membership to peers for consistency (Phase 5 enhancement)
    protocol_handlers._sync_membership_on_startup(plugin)

    # Auto-backfill routing intelligence on first-ever startup (empty DB)
    if fee_coordination_mgr and fee_coordination_mgr.should_auto_backfill():
        plugin.log("cl-hive: Empty routing intelligence, auto-backfilling from forwards...")
        try:
            result = hive_backfill_routing_intelligence(plugin, days=7)
            plugin.log(f"cl-hive: Auto-backfill complete: {result.get('processed', 0)} forwards")
        except Exception as e:
            plugin.log(f"cl-hive: Auto-backfill failed: {e}", level='warn')

    # Set up graceful shutdown handler
    def handle_shutdown_signal(signum, frame):
        plugin.log("cl-hive: Received shutdown signal, cleaning up...")
        # Signal background threads to stop FIRST so they don't try to
        # use resources we're about to tear down.
        shutdown_event.set()
        try:
            if fee_coordination_mgr:
                fee_coordination_mgr.save_state_to_database()
        except Exception:
            pass  # Best-effort on shutdown
        # Cancel queued message tasks BEFORE tearing down the RPC pool
        # they depend on — prevents queued tasks from starting with dead RPC.
        try:
            if _msg_executor:
                _msg_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass  # Best-effort on shutdown
        try:
            if _rpc_pool:
                _rpc_pool.stop()
        except Exception:
            pass  # Best-effort on shutdown
        try:
            if nostr_transport:
                nostr_transport.stop()
        except Exception:
            pass  # Best-effort on shutdown
        try:
            if cashu_escrow_mgr:
                cashu_escrow_mgr.shutdown()
        except Exception:
            pass  # Best-effort on shutdown
        try:
            if _batched_log_writer:
                _batched_log_writer.stop()
        except Exception:
            pass  # Best-effort on shutdown
    
    try:
        signal.signal(signal.SIGTERM, handle_shutdown_signal)
        signal.signal(signal.SIGINT, handle_shutdown_signal)
    except Exception as e:
        plugin.log(f"cl-hive: Could not set signal handlers: {e}", level='debug')
    
    # Install RPC pool proxy now that init is complete and workers are ready.
    # Background threads that access plugin.rpc will get bounded execution.
    plugin.rpc = RpcPoolProxy(_rpc_pool, timeout=30)
    plugin.log("cl-hive: RPC pool proxy installed")

    # Re-assign thread-safe RPC proxy to managers that cached the raw
    # plugin.rpc reference during init (before proxy was installed).
    if handshake_mgr:
        handshake_mgr.rpc = plugin.rpc
    if bridge:
        bridge.rpc = plugin.rpc
    if contribution_mgr:
        contribution_mgr.rpc = plugin.rpc
    if settlement_mgr:
        settlement_mgr.rpc = plugin.rpc
    if did_credential_mgr:
        did_credential_mgr.rpc = plugin.rpc
    if management_schema_registry:
        management_schema_registry.rpc = plugin.rpc
    if cashu_escrow_mgr:
        cashu_escrow_mgr.rpc = plugin.rpc

    plugin.log("cl-hive: Initialization complete. Swarm Intelligence ready.")


# =============================================================================
# PEER CONNECTED HOOK (Autodiscovery)
# =============================================================================

@plugin.hook("peer_connected")
def on_peer_connected(peer: dict, plugin: Plugin, **kwargs):
    """
    Handle peer connection - trigger autodiscovery if enabled.

    When a peer connects and we're not a hive member yet, send HIVE_HELLO
    to discover if they're part of a hive we can join.
    """
    global config, database, handshake_mgr

    # Extract peer_id from the peer dict
    peer_id = peer.get("id") if isinstance(peer, dict) else None
    if not peer_id:
        return {"result": "continue"}

    # Check if auto-join is enabled
    if not config or not config.auto_join_enabled:
        return {"result": "continue"}

    # Check if we're already a member
    if not handshake_mgr or not database:
        return {"result": "continue"}

    local_pubkey = handshake_mgr.get_our_pubkey()
    our_member = database.get_member(local_pubkey)

    # If we're already a member, no need to autodiscover
    if our_member:
        return {"result": "continue"}

    # Check if this peer is already known to us as a member
    peer_member = database.get_member(peer_id)
    if peer_member:
        # Peer is known, but we're not a member - this shouldn't happen normally
        return {"result": "continue"}

    # Send HIVE_HELLO in a background thread to avoid blocking the I/O thread.
    # (pyln-client is thread-safe per-call, no deadlock risk anymore)
    def _send_autodiscovery_hello():
        try:
            from modules.protocol import create_hello
            hello_msg = create_hello(local_pubkey)
            if hello_msg is None:
                plugin.log("cl-hive: HELLO message too large, skipping autodiscovery", level='warning')
                return

            plugin.rpc.call("sendcustommsg", {
                "node_id": peer_id,
                "msg": hello_msg.hex()
            })
            plugin.log(f"cl-hive: Sent HELLO to {peer_id[:16]}... (autodiscovery)")
        except Exception as e:
            plugin.log(f"cl-hive: Failed to send autodiscovery HELLO: {e}", level='debug')

    threading.Thread(target=_send_autodiscovery_hello, daemon=True).start()
    return {"result": "continue"}


# =============================================================================
# CUSTOM MESSAGE HOOK (BOLT 8 Protocol Layer)
# =============================================================================

@plugin.hook("custommsg")
def on_custommsg(peer_id: str, payload: str, plugin: Plugin, **kwargs):
    """
    Handle incoming custom BOLT 8 messages.
    
    Security: Implements "Peek & Check" pattern.
    - Read first 4 bytes of payload
    - If != HIVE_MAGIC (0x48495645), return continue immediately
    - Only process messages with valid Hive magic prefix
    
    This ensures cl-hive coexists peacefully with other plugins
    using the experimental message range (32768+).
    """
    if not database or not handshake_mgr:
        return {"result": "continue"}
    
    # Reject oversized payloads before hex decode
    if len(payload) > MAX_MESSAGE_BYTES * 2:
        return {"result": "continue"}

    # Decode hex payload to bytes
    try:
        data = bytes.fromhex(payload)
    except ValueError:
        return {"result": "continue"}
    
    # SECURITY: Peek & Check - Fast rejection of non-Hive messages
    if not is_hive_message(data):
        # Not our message, let other plugins handle it
        return {"result": "continue"}
    
    # Deserialize the Hive message
    msg_type, msg_payload = deserialize(data)
    
    if msg_type is None:
        # Phase B: distinguish version rejection from parse errors
        if is_hive_message(data):
            plugin.log(f"cl-hive: Rejected Hive message from {peer_id[:16]}... (version/parse)", level='debug')
        else:
            plugin.log(f"cl-hive: Malformed message from {peer_id[:16]}...", level='warn')
        return {"result": "continue"}

    _submit_hive_message(peer_id, msg_type, msg_payload, plugin)
    return {"result": "continue"}


def _dispatch_hive_message(peer_id: str, msg_type, msg_payload: Dict, plugin: Plugin):
    """Process a validated Hive message on a background thread."""
    try:
        if msg_type == HiveMessageType.HELLO:
            protocol_handlers.handle_hello(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.CHALLENGE:
            protocol_handlers.handle_challenge(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.ATTEST:
            protocol_handlers.handle_attest(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.WELCOME:
            protocol_handlers.handle_welcome(peer_id, msg_payload, plugin)
        # Phase 2: State Management
        elif msg_type == HiveMessageType.GOSSIP:
            protocol_handlers.handle_gossip(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.STATE_HASH:
            protocol_handlers.handle_state_hash(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.FULL_SYNC:
            protocol_handlers.handle_full_sync(peer_id, msg_payload, plugin)
        # Phase 3: Intent Lock Protocol
        elif msg_type == HiveMessageType.INTENT:
            protocol_handlers.handle_intent(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.INTENT_ABORT:
            protocol_handlers.handle_intent_abort(peer_id, msg_payload, plugin)
        # Phase 5: Membership Promotion
        elif msg_type == HiveMessageType.PROMOTION_REQUEST:
            protocol_handlers.handle_promotion_request(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.VOUCH:
            protocol_handlers.handle_vouch(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.PROMOTION:
            protocol_handlers.handle_promotion(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.MEMBER_LEFT:
            protocol_handlers.handle_member_left(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.BAN_PROPOSAL:
            protocol_handlers.handle_ban_proposal(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.BAN_VOTE:
            protocol_handlers.handle_ban_vote(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.BAN:
            protocol_handlers.handle_ban(peer_id, msg_payload, plugin)
        # Phase 6: Channel Coordination
        elif msg_type == HiveMessageType.PEER_AVAILABLE:
            protocol_handlers.handle_peer_available(peer_id, msg_payload, plugin)
        # Phase 6.4: Cooperative Expansion
        elif msg_type == HiveMessageType.EXPANSION_NOMINATE:
            protocol_handlers.handle_expansion_nominate(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.EXPANSION_ELECT:
            protocol_handlers.handle_expansion_elect(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.EXPANSION_DECLINE:
            protocol_handlers.handle_expansion_decline(peer_id, msg_payload, plugin)
        # Phase 7: Cooperative Fee Coordination
        elif msg_type == HiveMessageType.FEE_INTELLIGENCE_SNAPSHOT:
            protocol_handlers.handle_fee_intelligence_snapshot(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.HEALTH_REPORT:
            protocol_handlers.handle_health_report(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.LIQUIDITY_NEED:
            protocol_handlers.handle_liquidity_need(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.LIQUIDITY_SNAPSHOT:
            protocol_handlers.handle_liquidity_snapshot(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.ROUTE_PROBE:
            protocol_handlers.handle_route_probe(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.ROUTE_PROBE_BATCH:
            protocol_handlers.handle_route_probe_batch(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.PEER_REPUTATION_SNAPSHOT:
            protocol_handlers.handle_peer_reputation_snapshot(peer_id, msg_payload, plugin)
        # Phase 13: Stigmergic Marker Sharing
        elif msg_type == HiveMessageType.STIGMERGIC_MARKER_BATCH:
            protocol_handlers.handle_stigmergic_marker_batch(peer_id, msg_payload, plugin)
        # Phase 13: Pheromone Sharing
        elif msg_type == HiveMessageType.PHEROMONE_BATCH:
            protocol_handlers.handle_pheromone_batch(peer_id, msg_payload, plugin)
        # Phase 14: Fleet-Wide Intelligence Sharing
        elif msg_type == HiveMessageType.YIELD_METRICS_BATCH:
            protocol_handlers.handle_yield_metrics_batch(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.CIRCULAR_FLOW_ALERT:
            protocol_handlers.handle_circular_flow_alert(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.TEMPORAL_PATTERN_BATCH:
            protocol_handlers.handle_temporal_pattern_batch(peer_id, msg_payload, plugin)
        # Phase 14.2: Strategic Positioning & Rationalization
        elif msg_type == HiveMessageType.CORRIDOR_VALUE_BATCH:
            protocol_handlers.handle_corridor_value_batch(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.POSITIONING_PROPOSAL:
            protocol_handlers.handle_positioning_proposal(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.PHYSARUM_RECOMMENDATION:
            protocol_handlers.handle_physarum_recommendation(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.COVERAGE_ANALYSIS_BATCH:
            protocol_handlers.handle_coverage_analysis_batch(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.CLOSE_PROPOSAL:
            protocol_handlers.handle_close_proposal(peer_id, msg_payload, plugin)
        # Phase 9: Settlement
        elif msg_type == HiveMessageType.SETTLEMENT_OFFER:
            protocol_handlers.handle_settlement_offer(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.FEE_REPORT:
            protocol_handlers.handle_fee_report(peer_id, msg_payload, plugin)
        # Phase 12: Distributed Settlement
        elif msg_type == HiveMessageType.SETTLEMENT_PROPOSE:
            protocol_handlers.handle_settlement_propose(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.SETTLEMENT_READY:
            protocol_handlers.handle_settlement_ready(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.SETTLEMENT_EXECUTED:
            protocol_handlers.handle_settlement_executed(peer_id, msg_payload, plugin)
        # Phase 10: Task Delegation
        elif msg_type == HiveMessageType.TASK_REQUEST:
            protocol_handlers.handle_task_request(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.TASK_RESPONSE:
            protocol_handlers.handle_task_response(peer_id, msg_payload, plugin)
        # Phase 11: Hive-Splice Coordination
        elif msg_type == HiveMessageType.SPLICE_INIT_REQUEST:
            protocol_handlers.handle_splice_init_request(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.SPLICE_INIT_RESPONSE:
            protocol_handlers.handle_splice_init_response(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.SPLICE_UPDATE:
            protocol_handlers.handle_splice_update(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.SPLICE_SIGNED:
            protocol_handlers.handle_splice_signed(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.SPLICE_ABORT:
            protocol_handlers.handle_splice_abort(peer_id, msg_payload, plugin)
        # Phase 15: MCF (Min-Cost Max-Flow) Optimization
        elif msg_type == HiveMessageType.MCF_NEEDS_BATCH:
            protocol_handlers.handle_mcf_needs_batch(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.MCF_SOLUTION_BROADCAST:
            protocol_handlers.handle_mcf_solution_broadcast(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.MCF_ASSIGNMENT_ACK:
            protocol_handlers.handle_mcf_assignment_ack(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.MCF_COMPLETION_REPORT:
            protocol_handlers.handle_mcf_completion_report(peer_id, msg_payload, plugin)
        # Phase D: Reliable Delivery
        elif msg_type == HiveMessageType.MSG_ACK:
            protocol_handlers.handle_msg_ack(peer_id, msg_payload, plugin)
        # Phase 16: DID Credentials
        elif msg_type == HiveMessageType.DID_CREDENTIAL_PRESENT:
            protocol_handlers.handle_did_credential_present(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.DID_CREDENTIAL_REVOKE:
            protocol_handlers.handle_did_credential_revoke(peer_id, msg_payload, plugin)
        # Phase 16: Management Credentials
        elif msg_type == HiveMessageType.MGMT_CREDENTIAL_PRESENT:
            protocol_handlers.handle_mgmt_credential_present(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.MGMT_CREDENTIAL_REVOKE:
            protocol_handlers.handle_mgmt_credential_revoke(peer_id, msg_payload, plugin)
        # Phase 4: Extended Settlements
        elif msg_type == HiveMessageType.SETTLEMENT_RECEIPT:
            protocol_handlers.handle_settlement_receipt(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.BOND_POSTING:
            protocol_handlers.handle_bond_posting(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.BOND_SLASH:
            protocol_handlers.handle_bond_slash(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.NETTING_PROPOSAL:
            protocol_handlers.handle_netting_proposal(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.NETTING_ACK:
            protocol_handlers.handle_netting_ack(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.VIOLATION_REPORT:
            protocol_handlers.handle_violation_report(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.ARBITRATION_VOTE:
            protocol_handlers.handle_arbitration_vote(peer_id, msg_payload, plugin)
        # Phase 16: Traffic Intelligence
        elif msg_type == HiveMessageType.TRAFFIC_INTELLIGENCE_BATCH:
            protocol_handlers.handle_traffic_intelligence_batch(peer_id, msg_payload, plugin)
        else:
            plugin.log(f"cl-hive: Unhandled message type {msg_type.name} from {peer_id[:16]}...", level='debug')

    except Exception as e:
        plugin.log(f"cl-hive: Error handling {msg_type.name}: {e}\n{traceback.format_exc()}", level='warn')


# =============================================================================
# PEER CONNECTION HOOK (State Hash Exchange)
# =============================================================================

@plugin.subscribe("connect")
def on_peer_connected(**kwargs):
    """Hook called when a peer connects — offloaded to background thread."""
    peer_id = kwargs.get('id')
    if not peer_id or not database or not gossip_mgr:
        return
    # Quick DB check is fine on IO thread; offload RPC-heavy work
    member = database.get_member(peer_id)
    if not member:
        return
    # SECURITY: Do not exchange state with banned peers
    if database.is_banned(peer_id):
        return
    if _msg_executor is not None:
        _msg_executor.submit(protocol_handlers._handle_peer_connected, peer_id, member)
    else:
        protocol_handlers._handle_peer_connected(peer_id, member)




@plugin.subscribe("disconnect")
def on_peer_disconnected(**kwargs):
    """Update presence for disconnected peers."""
    peer_id = kwargs.get('id')
    if not peer_id or not database:
        return

    # Update VPN transport tracking
    if vpn_transport:
        vpn_transport.on_peer_disconnected(peer_id)

    member = database.get_member(peer_id)
    if not member:
        return
    now = int(time.time())
    database.update_member(peer_id, last_seen=now)
    database.update_presence(peer_id, is_online=False, now_ts=now, window_seconds=30 * 86400)




@plugin.subscribe("forward_event")
def on_forward_event(forward_event: Dict, plugin: Plugin, **kwargs):
    """Track forwarding events — offloaded to background thread to avoid blocking IO."""
    if _msg_executor is not None:
        _msg_executor.submit(protocol_handlers._handle_forward_event, forward_event)
    else:
        protocol_handlers._handle_forward_event(forward_event)





# =============================================================================
# RPC COMMANDS
# =============================================================================


def _require_rpc(plugin_obj: Plugin):
    """Check that plugin RPC is available and return it.

    Note: pyln-client is inherently thread-safe (opens new socket per call),
    so no locking wrapper is needed.
    """
    if plugin_obj is None or plugin_obj.rpc is None:
        return None, {"error": "plugin not initialized"}
    return plugin_obj.rpc, None


def _is_missing_file_rpc_error(exc: Exception) -> bool:
    """Return True when an RpcError is a filesystem missing-file failure."""
    if not isinstance(exc, RpcError):
        return False
    error = getattr(exc, "error", None)
    if isinstance(error, dict):
        message = str(error.get("message", ""))
    else:
        message = str(error or exc)
    return "no such file or directory" in message.lower()


@plugin.method("hive-getinfo")
def hive_getinfo(plugin: Plugin):
    """Proxy to CLN getinfo via plugin (native RPC)."""
    rpc, err = _require_rpc(plugin)
    if err:
        return err
    return rpc.getinfo()


@plugin.method("hive-listpeers")
def hive_listpeers(plugin: Plugin, id: str = None, level: str = None):
    """Proxy to CLN listpeers via plugin (native RPC)."""
    rpc, err = _require_rpc(plugin)
    if err:
        return err
    params = {}
    if isinstance(id, str):
        id = id.strip()
    if isinstance(level, str):
        level = level.strip()
    if id:
        params["id"] = id
    if level:
        params["level"] = level
    # Use raw rpc.call() to avoid pyln wrapper defaults injecting empty id="".
    return rpc.call("listpeers", params if params else {})


@plugin.method("hive-listpeerchannels")
def hive_listpeerchannels(plugin: Plugin, id: str = None):
    """Proxy to CLN listpeerchannels via plugin (native RPC)."""
    rpc, err = _require_rpc(plugin)
    if err:
        return err
    if isinstance(id, str):
        id = id.strip()
    # Use raw rpc.call() to avoid pyln wrapper defaults injecting empty id="".
    return rpc.call("listpeerchannels", {"id": id} if id else {})


@plugin.method("hive-listforwards")
def hive_listforwards(plugin: Plugin, status: str = None):
    """Proxy to CLN listforwards via plugin (native RPC)."""
    rpc, err = _require_rpc(plugin)
    if err:
        return err
    return rpc.listforwards(status=status) if status else rpc.listforwards()


@plugin.method("hive-listchannels")
def hive_listchannels(plugin: Plugin, source: str = None):
    """Proxy to CLN listchannels via plugin (native RPC)."""
    rpc, err = _require_rpc(plugin)
    if err:
        return err
    return rpc.listchannels(source=source) if source else rpc.listchannels()


@plugin.method("hive-listfunds")
def hive_listfunds(plugin: Plugin):
    """Proxy to CLN listfunds via plugin (native RPC)."""
    rpc, err = _require_rpc(plugin)
    if err:
        return err
    return rpc.listfunds()


@plugin.method("hive-listnodes")
def hive_listnodes(plugin: Plugin, id: str = None):
    """Proxy to CLN listnodes via plugin (native RPC)."""
    rpc, err = _require_rpc(plugin)
    if err:
        return err
    return rpc.listnodes(id=id) if id else rpc.listnodes()


@plugin.method("hive-plugin-list")
def hive_plugin_list(plugin: Plugin):
    """Proxy to CLN plugin list via plugin (native RPC)."""
    rpc, err = _require_rpc(plugin)
    if err:
        return err
    try:
        return rpc.plugin("list")
    except Exception:
        return rpc.listplugins()


@plugin.method("hive-phase6-plugins")
def hive_phase6_plugins(plugin: Plugin):
    """Detect optional Phase 6 sibling plugin status."""
    global phase6_optional_plugins
    phase6_optional_plugins = _detect_phase6_optional_plugins(plugin)
    return phase6_optional_plugins


@plugin.method("hive-inject-packet")
def hive_inject_packet(plugin: Plugin, payload=None, source="nostr", **kwargs):
    """Inject an inbound packet from cl-hive-comms (Coordinated Mode only).

    Requires an authenticated transport sender in `pubkey`/`sender_pubkey`.
    The protocol payload's embedded `sender` is treated as untrusted and is
    checked against this transport identity before dispatch.
    """
    comms_active = bool(phase6_optional_plugins.get("cl_hive_comms", {}).get("active"))
    if not comms_active or not isinstance(nostr_transport, ExternalCommsTransport):
        return {"error": "inject-packet only available in coordinated mode"}
    if not isinstance(payload, dict):
        return {"error": "payload must be a dict"}
    transport_pubkey = kwargs.get("sender_pubkey") or kwargs.get("pubkey") or kwargs.get("sender")
    if not isinstance(transport_pubkey, str) or not transport_pubkey.strip():
        return {"error": "authenticated sender pubkey is required (use pubkey or sender_pubkey)"}
    if not nostr_transport.inject_packet(payload, transport_pubkey=transport_pubkey.strip()):
        return {"error": "queue full, packet dropped"}
    return {"result": "queued", "source": source}


@plugin.method("hive-connect")
def hive_connect(plugin: Plugin, peer_id: str):
    """Connect to a peer via plugin (native RPC)."""
    rpc, err = _require_rpc(plugin)
    if err:
        return err
    if not peer_id:
        return {"error": "peer_id is required"}
    return rpc.connect(peer_id)


@plugin.method("hive-open-channel")
def hive_open_channel(plugin: Plugin, peer_id: str, amount_sats: int, feerate: str = "normal", announce: bool = True, request_amt: int = 0):
    """Open a channel via plugin (native RPC).

    When *request_amt* > 0, dual-fund (v2) is attempted if the peer supports it.
    """
    rpc, err = _require_rpc(plugin)
    if err:
        return err
    if not peer_id:
        return {"error": "peer_id is required"}
    if not amount_sats or amount_sats < 20000:
        return {"error": "amount_sats must be at least 20,000"}
    try:
        rpc.connect(peer_id)
    except Exception:
        pass
    from modules.rpc_commands import _open_channel
    return _open_channel(
        rpc=rpc,
        target=peer_id,
        amount_sats=amount_sats,
        feerate=feerate,
        announce=announce,
        request_amt=request_amt,
        log_fn=lambda msg, lvl="info": plugin.log(msg, level=lvl),
    )


@plugin.method("hive-close-channel")
def hive_close_channel(plugin: Plugin, peer_id: str = None, channel_id: str = None, unilateraltimeout: int = None):
    """Close a channel via plugin (native RPC)."""
    rpc, err = _require_rpc(plugin)
    if err:
        return err
    if not peer_id and not channel_id:
        return {"error": "peer_id or channel_id is required"}
    params = {}
    if peer_id:
        params["id"] = peer_id
    if channel_id:
        params["id"] = channel_id
    if unilateraltimeout is not None:
        params["unilateraltimeout"] = unilateraltimeout
    return rpc.close(**params)


@plugin.method("hive-setchannel")
def hive_setchannel(plugin: Plugin, id: str = None, feebase: int = None, feeppm: int = None):
    """Proxy to CLN setchannel with fleet fee bound enforcement."""
    rpc, err = _require_rpc(plugin)
    if err:
        return err
    if not id:
        return {"error": "id is required"}

    # Enforce fleet fee bounds on feeppm
    from modules.fee_coordination import FLEET_FEE_FLOOR_PPM, FLEET_FEE_CEILING_PPM
    if feeppm is not None:
        if not isinstance(feeppm, int) or feeppm < 0:
            return {"error": f"feeppm must be a non-negative integer, got {feeppm}"}
        # Allow zero-fee for hive member channels, but clamp positive fees
        if feeppm > 0:
            feeppm = max(FLEET_FEE_FLOOR_PPM, min(FLEET_FEE_CEILING_PPM, feeppm))
    if feebase is not None:
        if not isinstance(feebase, int) or feebase < 0:
            return {"error": f"feebase must be a non-negative integer, got {feebase}"}

    params = {"id": id}
    if feebase is not None:
        params["feebase"] = feebase
    if feeppm is not None:
        params["feeppm"] = feeppm
    return rpc.setchannel(**params)


@plugin.method("hive-sling-stats")
def hive_sling_stats(plugin: Plugin, scid: str = None, json: bool = True):
    """Proxy to sling-stats via plugin (native RPC)."""
    rpc, err = _require_rpc(plugin)
    if err:
        return err
    params = {}
    if scid:
        params["scid"] = scid
    if json:
        params["json"] = json
    return rpc.call("sling-stats", params) if params else rpc.call("sling-stats")


@plugin.method("hive-sling-status")
def hive_sling_status(plugin: Plugin):
    """Proxy to sling-stats via plugin (native RPC). Bug fix: sling v4.2.0 renamed command."""
    rpc, err = _require_rpc(plugin)
    if err:
        return err
    return rpc.call("sling-stats")


@plugin.method("hive-sling-deletejob")
def hive_sling_deletejob(plugin: Plugin, job: str = None):
    """Proxy to sling-deletejob via plugin (native RPC)."""
    rpc, err = _require_rpc(plugin)
    if err:
        return err
    if not job:
        return {"error": "job is required"}
    try:
        return rpc.call("sling-deletejob", {"job": job})
    except RpcError as exc:
        if job == "all" and _is_missing_file_rpc_error(exc):
            plugin.log(
                "SLING_DELETEJOB_NOOP: jobs store not initialized; treating delete-all as empty state.",
                level="info",
            )
            return {
                "status": "noop",
                "job": job,
                "deleted": 0,
                "message": "sling jobs store not initialized",
            }
        raise


@plugin.method("hive-askrene-listlayers")
def hive_askrene_listlayers(plugin: Plugin, layer: str = None):
    """Proxy to askrene-listlayers via plugin (native RPC)."""
    rpc, err = _require_rpc(plugin)
    if err:
        return err
    params = {}
    if layer:
        params["layer"] = layer
    return rpc.call("askrene-listlayers", params) if params else rpc.call("askrene-listlayers")


@plugin.method("hive-askrene-listreservations")
def hive_askrene_listreservations(plugin: Plugin):
    """Proxy to askrene-listreservations via plugin (native RPC)."""
    rpc, err = _require_rpc(plugin)
    if err:
        return err
    return rpc.call("askrene-listreservations")


@plugin.method("hive-health")
def hive_health(plugin: Plugin):
    """Lightweight health check — no RPC, no lock, no DB."""
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - _start_time),
        "threads_alive": threading.active_count(),
    }


@plugin.method("hive-rpc-pool-status")
def hive_rpc_pool_status(plugin: Plugin):
    """Inspect cl-hive RPC pool health (workers, pending requests, dispatcher state)."""
    global _rpc_pool
    if _rpc_pool is None:
        return {"status": "not_initialized"}
    try:
        return {"status": "ok", "rpc_pool": _rpc_pool.status()}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@plugin.method("hive-status")
def hive_status(plugin: Plugin):
    """
    Get current Hive status and membership info.

    Returns:
        Dict with hive state, member count, governance mode, etc.
    """
    return rpc_status(_get_hive_context())


@plugin.method("hive-report-period-costs")
def hive_report_period_costs(plugin: Plugin, rebalance_costs_sats: int = 0, boltz_costs_sats: int = 0):
    """
    Report rebalancing costs for the current settlement period.

    Called by cl-revenue-ops to report accumulated rebalance costs for
    net profit settlement calculation (Issue #42). The costs are included
    in the next fee report broadcast to other hive members.

    Args:
        rebalance_costs_sats: Total rebalancing costs in sats for the current period
        boltz_costs_sats: Boltz swap costs in sats for the current period

    Returns:
        Dict with status and accepted costs value
    """
    global _local_rebalance_costs_sats

    if not isinstance(rebalance_costs_sats, int) or rebalance_costs_sats < 0:
        return {"error": "rebalance_costs_sats must be a non-negative integer"}

    total_costs = rebalance_costs_sats + max(0, int(boltz_costs_sats or 0))

    with _local_fees_lock:
        _local_rebalance_costs_sats = total_costs

    plugin.log(
        f"[Settlement] Updated period costs: {rebalance_costs_sats} sats rebalance + {boltz_costs_sats} sats boltz = {total_costs} sats total",
        level="info"
    )

    return {
        "status": "accepted",
        "rebalance_costs_sats": rebalance_costs_sats,
        "boltz_costs_sats": int(boltz_costs_sats or 0),
        "total_costs_sats": total_costs,
    }


@plugin.method("hive-config")
def hive_config(plugin: Plugin):
    """
    Get current Hive configuration values.

    Shows all config options and their current values. Useful for verifying
    hot-reload changes made via `lightning-cli setconfig`.

    Example:
        lightning-cli hive-config

    Returns:
        Dict with all current config values and metadata.
    """
    return rpc_get_config(_get_hive_context())


@plugin.method("hive-reload-config")
def hive_reload_config(plugin: Plugin):
    """
    Reload configuration from CLN after using setconfig.

    CLN's setconfig command updates option values, but there's no automatic
    notification to plugins. Call this after using setconfig to sync the
    internal config object with CLN's current option values.

    Example:
        lightning-cli setconfig hive-governance-mode failsafe
        lightning-cli hive-reload-config

    Returns:
        Dict with list of updated options and any errors.
    """
    result = _reload_config_from_cln(plugin)
    result["config_version"] = config._version if config else 0
    return result


@plugin.method("hive-reinit-bridge")
def hive_reinit_bridge(plugin: Plugin):
    """
    Re-attempt bridge initialization if it failed at startup.

    Returns:
        Dict with bridge status and details.

    Permission: Admin only
    """
    return rpc_reinit_bridge(_get_hive_context())


@plugin.method("hive-vpn-status")
def hive_vpn_status(plugin: Plugin, peer_id: str = None):
    """
    Get VPN transport status and configuration.

    Shows the current VPN transport mode, configured subnets, peer mappings,
    and which hive members are connected via VPN.

    Args:
        peer_id: Optional - Get VPN info for a specific peer

    Returns:
        Dict with VPN transport configuration and status.

    Permission: Member (read-only status)
    """
    return rpc_vpn_status(_get_hive_context(), peer_id)


@plugin.method("hive-vpn-add-peer")
def hive_vpn_add_peer(plugin: Plugin, pubkey: str, vpn_address: str):
    """
    Add or update a VPN peer mapping.

    Maps a node's pubkey to its VPN address for routing hive gossip.

    Args:
        pubkey: Node pubkey
        vpn_address: VPN address in format ip:port or just ip (default port 9735)

    Returns:
        Dict with result.

    Permission: Admin only
    """
    return rpc_vpn_add_peer(_get_hive_context(), pubkey, vpn_address)


@plugin.method("hive-vpn-remove-peer")
def hive_vpn_remove_peer(plugin: Plugin, pubkey: str):
    """
    Remove a VPN peer mapping.

    Args:
        pubkey: Node pubkey to remove

    Returns:
        Dict with result.

    Permission: Admin only
    """
    return rpc_vpn_remove_peer(_get_hive_context(), pubkey)


@plugin.method("hive-members")
def hive_members(plugin: Plugin):
    """
    List all Hive members with their tier and stats.

    Returns:
        List of member records with tier, contribution ratio, uptime, etc.
    """
    return rpc_members(_get_hive_context())


@plugin.method("hive-propose-promotion")
def hive_propose_promotion(plugin: Plugin, target_peer_id: str,
                           proposer_peer_id: str = None):
    """
    Propose a neophyte for early promotion to member status.

    Any member can propose a neophyte for promotion before the 90-day
    probation period completes. When a majority (51%) of active members
    approve, the neophyte is promoted.

    Args:
        target_peer_id: The neophyte to propose for promotion
        proposer_peer_id: Optional, defaults to our pubkey

    Permission: Member only
    """
    from modules.rpc_commands import propose_promotion
    result = propose_promotion(_get_hive_context(), target_peer_id, proposer_peer_id)

    # Broadcast vote as VOUCH for cross-node sync
    if result.get("success") and membership_mgr and our_pubkey:
        protocol_handlers._broadcast_promotion_vote(target_peer_id, proposer_peer_id or our_pubkey)

    return result


@plugin.method("hive-vote-promotion")
def hive_vote_promotion(plugin: Plugin, target_peer_id: str,
                        voter_peer_id: str = None):
    """
    Vote to approve a neophyte's promotion to member.

    Args:
        target_peer_id: The neophyte being voted on
        voter_peer_id: Optional, defaults to our pubkey

    Permission: Member only
    """
    from modules.rpc_commands import vote_promotion
    result = vote_promotion(_get_hive_context(), target_peer_id, voter_peer_id)

    # Broadcast vote as VOUCH for cross-node sync
    if result.get("success") and membership_mgr and our_pubkey:
        protocol_handlers._broadcast_promotion_vote(target_peer_id, voter_peer_id or our_pubkey)

    return result


@plugin.method("hive-pending-promotions")
def hive_pending_promotions(plugin: Plugin):
    """
    View pending manual promotion proposals.

    Returns:
        Dict with pending promotions and their approval status.
    """
    from modules.rpc_commands import pending_promotions
    return pending_promotions(_get_hive_context())


@plugin.method("hive-execute-promotion")
def hive_execute_promotion(plugin: Plugin, target_peer_id: str):
    """
    Execute a manual promotion if quorum has been reached.

    This bypasses the normal 90-day probation period when a majority
    of members have approved the promotion.

    Args:
        target_peer_id: The neophyte to promote

    Permission: Any member can execute once quorum is reached
    """
    from modules.rpc_commands import execute_promotion
    return execute_promotion(_get_hive_context(), target_peer_id)


@plugin.method("hive-sync-promotion")
def hive_sync_promotion(plugin: Plugin, target_peer_id: str):
    """
    Sync promotion votes for a neophyte to other nodes.

    Broadcasts all local votes for this neophyte as VOUCH messages,
    enabling nodes that missed earlier votes to catch up.

    Args:
        target_peer_id: The neophyte whose promotion to sync

    Returns:
        Dict with sync status and vote count.

    Permission: Member only
    """
    if not config or not config.membership_enabled:
        return {"error": "membership_disabled"}
    if not membership_mgr or not our_pubkey or not database:
        return {"error": "membership_unavailable"}

    # Check our tier
    our_tier = membership_mgr.get_tier(our_pubkey)
    if our_tier not in (MembershipTier.MEMBER.value,):
        return {"error": "permission_denied", "required_tier": "member"}

    # Check target exists
    target = database.get_member(target_peer_id)
    if not target:
        return {"error": "peer_not_found", "peer_id": target_peer_id}

    # Broadcast our vote for this target
    success = protocol_handlers._broadcast_promotion_vote(target_peer_id, our_pubkey)

    # Get current vouch count
    request_id = target_peer_id[2:34]  # First 32 hex chars after "03" prefix
    vouches = database.get_promotion_vouches(target_peer_id, request_id)
    active_members = membership_mgr.get_active_members()
    quorum = membership_mgr.calculate_quorum(len(active_members))

    return {
        "success": success,
        "target_peer_id": target_peer_id,
        "request_id": request_id,
        "vouches_broadcast": 1 if success else 0,
        "total_local_vouches": len(vouches),
        "quorum_required": quorum,
        "quorum_reached": len(vouches) >= quorum
    }


@plugin.method("hive-topology")
def hive_topology(plugin: Plugin):
    """
    Get current topology analysis from the Planner.

    Returns:
        Dict with saturated targets, planner stats, and config.
    """
    return rpc_topology(_get_hive_context())


@plugin.method("hive-expansion-recommendations")
def hive_expansion_recommendations(plugin: Plugin, limit: int = 10):
    """
    Get expansion recommendations with cooperation module intelligence.

    Returns detailed recommendations integrating:
    - Hive coverage diversity (% of members with channels)
    - Network competition (peer channel count)
    - Bottleneck detection (from liquidity_coordinator)
    - Splice recommendations (from splice_coordinator)

    Args:
        limit: Maximum number of recommendations to return (default: 10)

    Returns:
        Dict with expansion recommendations and coverage summary.
    """
    return rpc_expansion_recommendations(_get_hive_context(), limit=limit)


@plugin.method("hive-channel-closed")
def hive_channel_closed(plugin: Plugin, peer_id: str, channel_id: str,
                        closer: str, close_type: str,
                        capacity_sats: int = 0,
                        # Profitability data
                        duration_days: int = 0,
                        total_revenue_sats: int = 0,
                        total_rebalance_cost_sats: int = 0,
                        net_pnl_sats: int = 0,
                        forward_count: int = 0,
                        forward_volume_sats: int = 0,
                        our_fee_ppm: int = 0,
                        their_fee_ppm: int = 0,
                        routing_score: float = 0.0,
                        profitability_score: float = 0.0):
    """
    Notification from cl-revenue-ops that a channel has closed.

    ALL closures are broadcast to hive members for topology awareness.
    This helps the hive make informed decisions about channel openings.

    Args:
        peer_id: The peer whose channel closed
        channel_id: The closed channel ID
        closer: Who initiated: 'local', 'remote', 'mutual', or 'unknown'
        close_type: Type of closure
        capacity_sats: Channel capacity that was closed

        # Profitability data from cl-revenue-ops:
        duration_days: How long the channel was open
        total_revenue_sats: Total routing fees earned
        total_rebalance_cost_sats: Total rebalancing costs
        net_pnl_sats: Net profit/loss for the channel
        forward_count: Number of forwards routed
        forward_volume_sats: Total volume routed through channel
        our_fee_ppm: Fee rate we charged
        their_fee_ppm: Fee rate they charged us
        routing_score: Routing quality score (0-1)
        profitability_score: Overall profitability score (0-1)

    Returns:
        Dict with action taken
    """
    if not config or not database:
        return {"error": "Hive not initialized"}

    result = {
        "peer_id": peer_id,
        "channel_id": channel_id,
        "closer": closer,
        "close_type": close_type,
        "action": "none",
        "broadcast_count": 0
    }

    # Don't notify about banned peers
    if database.is_banned(peer_id):
        result["action"] = "ignored"
        result["reason"] = "Peer is banned"
        return result

    # Map closer to event_type
    if closer == 'remote':
        event_type = 'remote_close'
    elif closer == 'local':
        event_type = 'local_close'
    elif closer == 'mutual':
        event_type = 'mutual_close'
    else:
        event_type = 'channel_close'

    # Broadcast to all hive members for topology awareness
    broadcast_count = protocol_handlers.broadcast_peer_available(
        target_peer_id=peer_id,
        event_type=event_type,
        channel_id=channel_id,
        capacity_sats=capacity_sats,
        routing_score=routing_score,
        profitability_score=profitability_score,
        duration_days=duration_days,
        total_revenue_sats=total_revenue_sats,
        total_rebalance_cost_sats=total_rebalance_cost_sats,
        net_pnl_sats=net_pnl_sats,
        forward_count=forward_count,
        forward_volume_sats=forward_volume_sats,
        our_fee_ppm=our_fee_ppm,
        their_fee_ppm=their_fee_ppm,
        reason=f"Channel {channel_id} closed ({closer})"
    )

    result["action"] = "notified_hive"
    result["broadcast_count"] = broadcast_count
    result["event_type"] = event_type
    result["message"] = f"Notified {broadcast_count} hive members about channel closure"

    plugin.log(
        f"cl-hive: Channel {channel_id} closed by {closer}, "
        f"notified {broadcast_count} members (pnl={net_pnl_sats} sats)",
        level='info'
    )

    return result


@plugin.method("hive-channel-opened")
def hive_channel_opened(plugin: Plugin, peer_id: str, channel_id: str,
                        opener: str, capacity_sats: int = 0,
                        our_funding_sats: int = 0, their_funding_sats: int = 0):
    """
    Notification from cl-revenue-ops that a channel has opened.

    ALL opens are broadcast to hive members for topology awareness.
    This helps the hive track who has channels to which peers.

    Args:
        peer_id: The peer the channel was opened with
        channel_id: The new channel ID
        opener: Who initiated: 'local' or 'remote'
        capacity_sats: Total channel capacity
        our_funding_sats: Amount we funded
        their_funding_sats: Amount they funded

    Returns:
        Dict with action taken
    """
    if not config or not database:
        return {"error": "Hive not initialized"}

    result = {
        "peer_id": peer_id,
        "channel_id": channel_id,
        "opener": opener,
        "capacity_sats": capacity_sats,
        "action": "none",
        "broadcast_count": 0
    }

    # Check if peer is a hive member (internal channel)
    member = database.get_member(peer_id)
    is_hive_internal = member is not None and not database.is_banned(peer_id)

    # HIVE SAFETY: Immediately set 0 fee for hive member channels
    if is_hive_internal and plugin:
        try:
            # Set both base fee and ppm to 0 for hive internal channels
            plugin.rpc.setchannel(
                id=channel_id,
                feebase=0,
                feeppm=0
            )
            plugin.log(
                f"cl-hive: HIVE_SAFETY: Set 0 fee on channel {channel_id} to fleet member {peer_id[:16]}...",
                level='info'
            )
            result["fee_action"] = "set_zero_fee"
        except Exception as e:
            plugin.log(
                f"cl-hive: Warning: Failed to set 0 fee on hive channel {channel_id}: {e}",
                level='warn'
            )
            result["fee_action"] = f"failed: {e}"

    # Broadcast to all hive members
    broadcast_count = protocol_handlers.broadcast_peer_available(
        target_peer_id=peer_id,
        event_type='channel_open',
        channel_id=channel_id,
        capacity_sats=capacity_sats,
        our_funding_sats=our_funding_sats,
        their_funding_sats=their_funding_sats,
        opener=opener,
        reason=f"Channel {channel_id} opened ({opener})"
    )

    result["action"] = "notified_hive"
    result["broadcast_count"] = broadcast_count
    result["is_hive_internal"] = is_hive_internal
    result["message"] = f"Notified {broadcast_count} hive members about new channel"

    plugin.log(
        f"cl-hive: Channel {channel_id} opened with {peer_id[:16]}... ({opener}), "
        f"notified {broadcast_count} members",
        level='info'
    )

    return result


@plugin.method("hive-peer-events")
def hive_peer_events(plugin: Plugin, peer_id: str = None, event_type: str = None,
                     reporter_id: str = None, days: int = 90, limit: int = 100,
                     summary: bool = False):
    """
    Query peer events for topology intelligence (Phase 6.1).

    This RPC provides access to the peer_events table which stores all channel
    open/close events received from hive members. Use this data to understand
    peer quality and make informed channel decisions.

    Args:
        peer_id: Filter by external peer pubkey (optional)
        event_type: Filter by event type: channel_open, channel_close,
                    remote_close, local_close, mutual_close (optional)
        reporter_id: Filter by reporting hive member pubkey (optional)
        days: Only include events from last N days (default: 90)
        limit: Maximum number of events to return (default: 100, max: 500)
        summary: If True and peer_id is set, return aggregated summary instead

    Returns:
        If summary=False: Dict with events list and metadata
        If summary=True: Dict with aggregated statistics for the peer

    Examples:
        # Get all events from last 30 days
        hive-peer-events days=30

        # Get events for a specific peer
        hive-peer-events peer_id=02abc123...

        # Get summary statistics for a peer
        hive-peer-events peer_id=02abc123... summary=true

        # Get only remote close events
        hive-peer-events event_type=remote_close

        # Get events reported by a specific hive member
        hive-peer-events reporter_id=03def456...
    """
    if not database:
        return {"error": "Database not initialized"}

    # Bound limit
    limit = min(max(1, limit), 500)
    days = min(max(1, days), 365)

    # If summary requested with peer_id, return aggregated stats
    if summary and peer_id:
        stats = database.get_peer_event_summary(peer_id, days=days)
        return {
            "peer_id": peer_id,
            "days": days,
            "summary": stats,
        }

    # Otherwise return event list
    events = database.get_peer_events(
        peer_id=peer_id,
        event_type=event_type,
        reporter_id=reporter_id,
        days=days,
        limit=limit
    )

    # Get list of unique peers with events if no peer_id filter
    peers_with_events = []
    if not peer_id:
        peers_with_events = database.get_peers_with_events(days=days)

    return {
        "count": len(events),
        "limit": limit,
        "days": days,
        "filters": {
            "peer_id": peer_id,
            "event_type": event_type,
            "reporter_id": reporter_id,
        },
        "peers_with_events": len(peers_with_events),
        "events": events,
    }


@plugin.method("hive-peer-quality")
def hive_peer_quality(plugin: Plugin, peer_id: str = None, days: int = 90,
                      min_confidence: float = 0.0, limit: int = 50):
    """
    Calculate quality scores for external peers (Phase 6.2).

    Quality scores are based on historical channel event data from hive members.
    Use this to evaluate peer reliability, profitability, and routing potential
    before opening channels.

    Score Components:
        - Reliability (35%): Based on closure behavior and duration
        - Profitability (25%): Based on P&L and revenue data
        - Routing (25%): Based on forward activity
        - Consistency (15%): Based on agreement across reporters

    Args:
        peer_id: Specific peer to score (optional). If not provided,
                 returns scores for all peers with event data.
        days: Number of days of history to consider (default: 90)
        min_confidence: Minimum confidence threshold (0-1) to include (default: 0)
        limit: Maximum number of peers to return when peer_id not set (default: 50)

    Returns:
        Dict with quality scores and recommendations.

    Examples:
        # Get quality score for a specific peer
        hive-peer-quality peer_id=02abc123...

        # Get top 20 highest quality peers
        hive-peer-quality limit=20

        # Get only high-confidence scores
        hive-peer-quality min_confidence=0.5

        # Use 30 days of data instead of 90
        hive-peer-quality peer_id=02abc123... days=30
    """
    if not database:
        return {"error": "Database not initialized"}

    # Create scorer instance
    scorer = PeerQualityScorer(database, plugin)

    # Bound parameters
    days = min(max(1, days), 365)
    limit = min(max(1, limit), 200)
    min_confidence = max(0.0, min(1.0, min_confidence))

    if peer_id:
        # Single peer score
        result = scorer.calculate_score(peer_id, days=days)
        return {
            "peer_id": peer_id,
            "days": days,
            "score": result.to_dict(),
        }

    # All peers with event data
    results = scorer.get_scored_peers(days=days, min_confidence=min_confidence)

    # Limit results
    results = results[:limit]

    return {
        "count": len(results),
        "limit": limit,
        "days": days,
        "min_confidence": min_confidence,
        "peers": [r.to_dict() for r in results],
        "score_breakdown": {
            "excellent": len([r for r in results if r.recommendation == "excellent"]),
            "good": len([r for r in results if r.recommendation == "good"]),
            "neutral": len([r for r in results if r.recommendation == "neutral"]),
            "caution": len([r for r in results if r.recommendation == "caution"]),
            "avoid": len([r for r in results if r.recommendation == "avoid"]),
        }
    }


@plugin.method("hive-quality-check")
def hive_quality_check(plugin: Plugin, peer_id: str, days: int = 90,
                       min_score: float = 0.45):
    """
    Quick quality check for a peer - should we open a channel? (Phase 6.2)

    This is a convenience method for the planner and governance engine to
    quickly determine if a peer is suitable for channel opening.

    Args:
        peer_id: Peer to evaluate (required)
        days: Days of history to consider (default: 90)
        min_score: Minimum quality score required (default: 0.45)

    Returns:
        Dict with recommendation and reasoning.

    Examples:
        # Check if peer is suitable for channel
        hive-quality-check peer_id=02abc123...

        # Use stricter threshold
        hive-quality-check peer_id=02abc123... min_score=0.6
    """
    if not database:
        return {"error": "Database not initialized"}

    if not peer_id:
        return {"error": "peer_id is required"}

    # Create scorer and check
    scorer = PeerQualityScorer(database, plugin)
    should_open, reason = scorer.should_open_channel(
        peer_id, days=days, min_score=min_score
    )

    # Also get full score for context
    result = scorer.calculate_score(peer_id, days=days)

    return {
        "peer_id": peer_id,
        "should_open": should_open,
        "reason": reason,
        "overall_score": round(result.overall_score, 3),
        "confidence": round(result.confidence, 3),
        "recommendation": result.recommendation,
        "min_score_threshold": min_score,
    }


@plugin.method("hive-calculate-size")
def hive_calculate_size(plugin: Plugin, peer_id: str, capacity_sats: int = None,
                        channel_count: int = None, hive_share_pct: float = 0.0):
    """
    Calculate recommended channel size for a peer (Phase 6.3).

    This RPC previews what channel size would be recommended for a given peer,
    taking into account quality scores, network factors, and configuration.

    Args:
        peer_id: Target peer pubkey (required)
        capacity_sats: Target's public capacity in sats (optional, will lookup)
        channel_count: Target's channel count (optional, will lookup)
        hive_share_pct: Current hive share to target 0-1 (default: 0)

    Returns:
        Dict with recommended size, factors, and reasoning.

    Examples:
        # Calculate size for a peer (auto-lookup capacity)
        hive-calculate-size peer_id=02abc123...

        # Override capacity and channel count
        hive-calculate-size peer_id=02abc123... capacity_sats=100000000 channel_count=50

        # Simulate existing hive share
        hive-calculate-size peer_id=02abc123... hive_share_pct=0.05
    """
    if not database:
        return {"error": "Database not initialized"}

    if not config:
        return {"error": "Config not initialized"}

    if not peer_id:
        return {"error": "peer_id is required"}

    # Get config snapshot
    cfg = config.snapshot()

    # Lookup capacity and channel count if not provided
    if capacity_sats is None or channel_count is None:
        try:
            # Try to get from listchannels
            channels = plugin.rpc.listchannels(source=peer_id)
            peer_channels = channels.get('channels', [])

            if capacity_sats is None:
                capacity_sats = sum(c.get('amount_msat', 0) // 1000 for c in peer_channels)
                if capacity_sats == 0:
                    capacity_sats = 100_000_000  # Default 1 BTC if not found

            if channel_count is None:
                channel_count = len(peer_channels)
                if channel_count == 0:
                    channel_count = 20  # Default moderate connectivity
        except Exception as e:
            plugin.log(f"cl-hive: Error looking up peer info: {e}", level='debug')
            if capacity_sats is None:
                capacity_sats = 100_000_000  # Default 1 BTC
            if channel_count is None:
                channel_count = 20  # Default moderate

    # Get onchain balance
    try:
        funds = plugin.rpc.listfunds()
        outputs = funds.get('outputs', [])
        onchain_balance = sum(
            protocol_handlers._parse_amount_msat(o.get('amount_msat', 0)) // 1000
            for o in outputs if o.get('status') == 'confirmed'
        )
    except Exception:
        onchain_balance = cfg.planner_default_channel_sats * 10  # Assume adequate

    # Get available budget (considering all constraints)
    daily_remaining = database.get_available_budget(cfg.failsafe_budget_per_day)
    max_per_channel = int(cfg.failsafe_budget_per_day * cfg.budget_max_per_channel_pct)
    spendable_onchain = int(onchain_balance * (1.0 - cfg.budget_reserve_pct))
    available_budget = min(daily_remaining, max_per_channel, spendable_onchain)

    # Create quality scorer and channel sizer
    scorer = PeerQualityScorer(database, plugin)
    sizer = ChannelSizer(plugin=plugin, quality_scorer=scorer)

    # Calculate size with budget constraint
    result = sizer.calculate_size(
        target=peer_id,
        target_capacity_sats=capacity_sats,
        target_channel_count=channel_count,
        hive_share_pct=hive_share_pct,
        target_share_cap=cfg.market_share_cap_pct * 0.5,
        onchain_balance_sats=onchain_balance,
        min_channel_sats=cfg.planner_min_channel_sats,
        max_channel_sats=cfg.planner_max_channel_sats,
        default_channel_sats=cfg.planner_default_channel_sats,
        available_budget_sats=available_budget,
    )

    # Get budget summary
    budget_info = database.get_budget_summary(cfg.failsafe_budget_per_day, days=1)

    return {
        "peer_id": peer_id,
        "recommended_size_sats": result.recommended_size_sats,
        "recommended_size_btc": round(result.recommended_size_sats / 100_000_000, 4),
        "reasoning": result.reasoning,
        "factors": result.factors,
        "inputs": {
            "capacity_sats": capacity_sats,
            "channel_count": channel_count,
            "hive_share_pct": hive_share_pct,
            "onchain_balance_sats": onchain_balance,
        },
        "budget": {
            "daily_budget_sats": cfg.failsafe_budget_per_day,
            "spent_today_sats": budget_info['today']['spent_sats'],
            "daily_remaining_sats": daily_remaining,
            "max_per_channel_sats": max_per_channel,
            "reserve_pct": cfg.budget_reserve_pct,
            "spendable_onchain_sats": spendable_onchain,
            "effective_budget_sats": available_budget,
            "budget_limited": result.factors.get('budget_limited', False),
        },
        "config_bounds": {
            "min_channel_sats": cfg.planner_min_channel_sats,
            "max_channel_sats": cfg.planner_max_channel_sats,
            "default_channel_sats": cfg.planner_default_channel_sats,
        },
        "feerate": _get_feerate_info(cfg.max_expansion_feerate_perkb),
    }


def _get_feerate_info(max_feerate_perkb: int) -> dict:
    """Get current feerate information for expansion decisions."""
    allowed, current, reason = protocol_handlers._check_feerate_for_expansion(max_feerate_perkb)
    return {
        "current_perkb": current,
        "max_allowed_perkb": max_feerate_perkb,
        "expansion_allowed": allowed,
        "reason": reason,
    }


@plugin.method("hive-expansion-status")
def hive_expansion_status(plugin: Plugin, round_id: str = None,
                          target_peer_id: str = None):
    """
    Get status of cooperative expansion rounds.

    Args:
        round_id: Get status of a specific round (optional)
        target_peer_id: Get rounds for a specific target peer (optional)

    Returns:
        Dict with expansion round status and statistics.
    """
    return rpc_expansion_status(_get_hive_context(), round_id=round_id,
                                target_peer_id=target_peer_id)


@plugin.method("hive-expansion-nominate")
def hive_expansion_nominate(plugin: Plugin, target_peer_id: str, round_id: str = None):
    """
    Manually trigger a cooperative expansion round for a peer (Phase 6.4).

    This RPC allows manually starting a cooperative expansion round
    for a target peer, useful for testing or when automatic triggering
    is disabled.

    Args:
        target_peer_id: The external peer to consider for expansion
        round_id: Optional existing round ID to join (if omitted, starts new round)

    Returns:
        Dict with round information.

    Examples:
        # Start a new expansion round
        hive-expansion-nominate target_peer_id=02abc123...

        # Join an existing round
        hive-expansion-nominate target_peer_id=02abc123... round_id=abc12345
    """
    if not coop_expansion:
        return {"error": "Cooperative expansion not initialized"}

    if not target_peer_id:
        return {"error": "target_peer_id is required"}

    # Check feerate and warn if high (but don't block manual operation)
    cfg = config.snapshot() if config else None
    max_feerate = cfg.max_expansion_feerate_perkb if cfg else 5000
    feerate_allowed, current_feerate, feerate_reason = protocol_handlers._check_feerate_for_expansion(max_feerate)
    feerate_warning = None
    if not feerate_allowed:
        feerate_warning = f"Warning: on-chain fees are high ({feerate_reason}). Consider waiting for lower fees."

    if round_id:
        # Join existing round - create it locally if we don't have it
        round_obj = coop_expansion.get_round(round_id)
        if not round_obj:
            # Create the round locally to join it
            plugin.log(f"cl-hive: Creating local copy of remote round {round_id[:8]}...")
            coop_expansion.join_remote_round(
                round_id=round_id,
                target_peer_id=target_peer_id,
                trigger_reporter=our_pubkey or ""
            )

        # Broadcast our nomination
        protocol_handlers._broadcast_expansion_nomination(round_id, target_peer_id)

        result = {
            "action": "joined",
            "round_id": round_id,
            "target_peer_id": target_peer_id,
        }
        if feerate_warning:
            result["warning"] = feerate_warning
            result["current_feerate_perkb"] = current_feerate
        return result

    # Start new round
    new_round_id = coop_expansion.start_round(
        target_peer_id=target_peer_id,
        trigger_event="manual",
        trigger_reporter=our_pubkey or "",
        quality_score=0.5
    )

    # Broadcast our nomination
    protocol_handlers._broadcast_expansion_nomination(new_round_id, target_peer_id)

    result = {
        "action": "started",
        "round_id": new_round_id,
        "target_peer_id": target_peer_id,
    }
    if feerate_warning:
        result["warning"] = feerate_warning
        result["current_feerate_perkb"] = current_feerate
    return result


@plugin.method("hive-expansion-elect")
def hive_expansion_elect(plugin: Plugin, round_id: str):
    """
    Manually trigger election for an expansion round (Phase 6.4).

    Normally elections happen automatically after the nomination window.
    This RPC allows manually triggering an election early.

    Args:
        round_id: The round to elect for (required)

    Returns:
        Dict with election result.

    Examples:
        hive-expansion-elect round_id=abc12345
    """
    if not coop_expansion:
        return {"error": "Cooperative expansion not initialized"}

    if not round_id:
        return {"error": "round_id is required"}

    round_obj = coop_expansion.get_round(round_id)
    if not round_obj:
        return {"error": f"Round {round_id} not found"}

    # Run election
    elected_id = coop_expansion.elect_winner(round_id)

    if not elected_id:
        return {
            "round_id": round_id,
            "elected": False,
            "reason": round_obj.result if round_obj else "Unknown",
        }

    # Broadcast election result
    protocol_handlers._broadcast_expansion_elect(
        round_id=round_id,
        target_peer_id=round_obj.target_peer_id,
        elected_id=elected_id,
        channel_size_sats=round_obj.recommended_size_sats,
        quality_score=round_obj.quality_score,
        nomination_count=len(round_obj.nominations)
    )

    # If we were elected, queue the pending action locally
    # (we won't receive our own broadcast message)
    if elected_id == our_pubkey and database and config:
        cfg = config.snapshot()
        proposed_size = round_obj.recommended_size_sats or cfg.planner_default_channel_sats

        # Check affordability before queuing
        capped_size, insufficient, was_capped = protocol_handlers._cap_channel_size_to_budget(
            proposed_size, cfg, f"Local election for {round_obj.target_peer_id[:16]}..."
        )
        if insufficient:
            plugin.log(
                f"cl-hive: [ELECT] Cannot queue channel: insufficient funds "
                f"(proposed={proposed_size}, min={cfg.planner_min_channel_sats})",
                level='warn'
            )
            return {
                "round_id": round_id,
                "elected": True,
                "elected_id": elected_id,
                "error": "insufficient_funds",
                "reason": f"Cannot afford minimum channel size ({cfg.planner_min_channel_sats} sats)"
            }
        if was_capped:
            plugin.log(
                f"cl-hive: [ELECT] Capping local election channel size from {proposed_size} to {capped_size}",
                level='info'
            )

        action_id = database.add_pending_action(
            action_type="channel_open",
            payload={
                "target": round_obj.target_peer_id,
                "amount_sats": capped_size,
                "source": "cooperative_expansion",
                "round_id": round_id,
                "reason": "Elected by hive for cooperative expansion"
            },
            expires_hours=24
        )
        plugin.log(
            f"cl-hive: Queued channel open to {round_obj.target_peer_id[:16]}... "
            f"(action_id={action_id}, size={capped_size})",
            level='info'
        )

    return {
        "round_id": round_id,
        "elected": True,
        "elected_id": elected_id,
        "target_peer_id": round_obj.target_peer_id,
        "nomination_count": len(round_obj.nominations),
    }


@plugin.method("hive-planner-log")
def hive_planner_log(plugin: Plugin, limit: int = 50):
    """
    Get recent Planner decision logs.

    Args:
        limit: Maximum number of log entries to return (default: 50)

    Returns:
        Dict with log entries and count.
    """
    return rpc_planner_log(_get_hive_context(), limit=limit)


@plugin.method("hive-planner-ignore")
def hive_planner_ignore(plugin: Plugin, peer_id: str, reason: str = "manual",
                        duration_hours: int = 0):
    """
    Add a peer to the planner ignore list (prevents channel opens to this peer).

    Use this when a peer is unreachable, rejected connections, or should be
    skipped for any reason. The planner will not propose this peer as an
    expansion target until the ignore is released or expires.

    Args:
        peer_id: Pubkey of peer to ignore
        reason: Reason for ignoring (default: "manual")
        duration_hours: Hours until auto-expire (0 = permanent until released)

    Returns:
        Dict with result and current ignored peers count.

    Example:
        lightning-cli hive-planner-ignore 035e4ff418fc... "connection_failed" 24
    """
    if not database:
        return {"error": "Database not initialized"}

    if len(peer_id) != 66:
        return {"error": "Invalid peer_id format (expected 66 hex chars)"}

    duration = duration_hours if duration_hours > 0 else None
    success = database.add_ignored_peer(peer_id, reason=reason, duration_hours=duration)

    # Also add to planner's runtime ignore set if available
    if planner and hasattr(planner, '_ignored_peers'):
        planner._ignored_peers.add(peer_id)

    # Log the action
    database.log_planner_action(
        action_type='ignore',
        target=peer_id,
        result='success' if success else 'failed',
        details={
            'reason': reason,
            'type': 'manual',
            'duration_hours': duration_hours if duration_hours > 0 else 'permanent'
        }
    )

    ignored_peers = database.get_ignored_peers()

    return {
        "result": "success" if success else "already_ignored",
        "peer_id": peer_id,
        "reason": reason,
        "duration_hours": duration_hours if duration_hours > 0 else "permanent",
        "ignored_peers_count": len(ignored_peers)
    }


@plugin.method("hive-planner-unignore")
def hive_planner_unignore(plugin: Plugin, peer_id: str):
    """
    Remove a peer from the planner ignore list.

    Args:
        peer_id: Pubkey of peer to unignore

    Returns:
        Dict with result and current ignored peers count.

    Example:
        lightning-cli hive-planner-unignore 035e4ff418fc...
    """
    if not database:
        return {"error": "Database not initialized"}

    if len(peer_id) != 66:
        return {"error": "Invalid peer_id format (expected 66 hex chars)"}

    success = database.remove_ignored_peer(peer_id)

    # Also remove from planner's runtime ignore set if available
    if planner and hasattr(planner, '_ignored_peers'):
        planner._ignored_peers.discard(peer_id)

    # Log the action
    database.log_planner_action(
        action_type='unignore',
        target=peer_id,
        result='success' if success else 'not_found',
        details={'type': 'manual'}
    )

    ignored_peers = database.get_ignored_peers()

    return {
        "result": "success" if success else "not_found",
        "peer_id": peer_id,
        "ignored_peers_count": len(ignored_peers)
    }


@plugin.method("hive-planner-ignored-peers")
def hive_planner_ignored_peers(plugin: Plugin, include_expired: bool = False):
    """
    Get list of currently ignored peers.

    Args:
        include_expired: Include expired ignores (default: False)

    Returns:
        Dict with ignored peers list and counts.

    Example:
        lightning-cli hive-planner-ignored-peers
    """
    if not database:
        return {"error": "Database not initialized"}

    # Cleanup expired ignores first
    expired_count = database.cleanup_expired_ignores()

    ignored_peers = database.get_ignored_peers(include_expired=include_expired)

    # Also get runtime ignores from planner
    runtime_ignores = set()
    if planner and hasattr(planner, '_ignored_peers'):
        runtime_ignores = planner._ignored_peers

    return {
        "ignored_peers": ignored_peers,
        "count": len(ignored_peers),
        "runtime_ignores": list(runtime_ignores),
        "runtime_count": len(runtime_ignores),
        "expired_cleaned": expired_count
    }


@plugin.method("hive-test-intent")
def hive_test_intent(plugin: Plugin, target: str, intent_type: str = "channel_open",
                     broadcast: bool = True):
    """
    Create and optionally broadcast a test intent (for simulation/testing).

    This command is for testing the Intent Lock Protocol and conflict resolution.

    Args:
        target: Target peer pubkey for the intent
        intent_type: Type of intent (channel_open, rebalance, ban_peer)
        broadcast: Whether to broadcast to Hive members (default: True)

    Returns:
        Dict with intent details and broadcast result.

    Example:
        lightning-cli hive-test-intent 02abc123...
    """
    # Permission check: Admin only (test commands)
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not planner or not planner.intent_manager:
        return {"error": "Intent manager not initialized"}

    intent_mgr = planner.intent_manager

    try:
        # Create the intent
        intent = intent_mgr.create_intent(intent_type, target)

        result = {
            "intent_id": intent.intent_id,
            "intent_type": intent.intent_type,
            "target": target,
            "initiator": intent.initiator,
            "timestamp": intent.timestamp,
            "expires_at": intent.expires_at,
            "hold_seconds": intent.expires_at - intent.timestamp,
            "status": intent.status,
            "broadcast": False,
            "broadcast_count": 0
        }

        # Broadcast if requested
        if broadcast:
            success = planner._broadcast_intent(intent)
            result["broadcast"] = success
            if success:
                members = database.get_all_members()
                our_id = plugin.rpc.getinfo().get('id', '')
                result["broadcast_count"] = len([m for m in members if m.get('peer_id') != our_id])

        return result

    except Exception as e:
        return {"error": str(e)}


@plugin.method("hive-intent-status")
def hive_intent_status(plugin: Plugin):
    """
    Get current intent status (local and remote intents).

    Returns:
        Dict with pending intents and stats.
    """
    return rpc_intent_status(_get_hive_context())


@plugin.method("hive-test-pending-action")
def hive_test_pending_action(plugin: Plugin, action_type: str = "channel_open",
                              target: str = None, capacity_sats: int = 1000000,
                              reason: str = "test_action"):
    """
    Create a test pending action for AI advisor testing.

    This command creates an entry in the pending_actions table that the AI
    advisor can evaluate. Use this to test the advisor without triggering
    the actual planner.

    Args:
        action_type: Type of action (channel_open, ban, unban, expand)
        target: Target peer pubkey (default: uses first external node in graph)
        capacity_sats: Proposed capacity for channel_open (default: 1M sats)
        reason: Reason for the action (default: test_action)

    Returns:
        Dict with the created pending action details.

    Example:
        lightning-cli hive-test-pending-action
        lightning-cli hive-test-pending-action channel_open 02abc123... 500000 "underserved_target"
    """
    # Permission check: Admin only (test commands)
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not database:
        return {"error": "Database not initialized"}

    # Get a target if not specified
    if not target:
        # Try to find an external node from the network graph
        try:
            channels = plugin.rpc.listchannels()
            our_id = plugin.rpc.getinfo().get('id', '')
            members = database.get_all_members()
            member_ids = {m.get('peer_id', '') for m in members}

            # Find a node that's not in our hive
            for ch in channels.get('channels', []):
                candidate = ch.get('destination')
                if candidate and candidate not in member_ids and candidate != our_id:
                    target = candidate
                    break

            if not target:
                return {"error": "No external target found in graph. Specify target manually."}
        except Exception as e:
            return {"error": f"Failed to find target: {e}"}

    # Build payload based on action type
    if action_type == "channel_open":
        # Create an intent for channel_open actions (required for approval)
        intent_id = None
        if planner and planner.intent_manager:
            try:
                intent = planner.intent_manager.create_intent("channel_open", target)
                intent_id = intent.intent_id
            except Exception as e:
                return {"error": f"Failed to create intent: {e}"}
        else:
            return {"error": "Intent manager not initialized (required for channel_open)"}

        payload = {
            "target": target,
            "capacity_sats": capacity_sats,
            "reason": reason,
            "intent_id": intent_id,
            "scoring": {
                "connectivity_score": 0.8,
                "fee_score": 0.7,
                "capacity_score": 0.6
            }
        }
    elif action_type == "ban":
        payload = {
            "target": target,
            "reason": reason,
            "evidence": "test_evidence"
        }
    else:
        payload = {
            "target": target,
            "action_type": action_type,
            "reason": reason
        }

    try:
        action_id = database.add_pending_action(action_type, payload, expires_hours=24)
        return {
            "status": "created",
            "action_id": action_id,
            "action_type": action_type,
            "target": target,
            "payload": payload,
            "expires_in_hours": 24
        }
    except Exception as e:
        return {"error": f"Failed to create pending action: {e}"}


@plugin.method("hive-pending-actions")
def hive_pending_actions(plugin: Plugin):
    """
    Get all pending actions awaiting operator approval.

    Returns:
        Dict with list of pending actions.
    """
    return rpc_pending_actions(_get_hive_context())


@plugin.method("hive-approve-action")
def hive_approve_action(plugin: Plugin, action_id="all", amount_sats: int = None):
    """
    Approve and execute pending action(s).

    Args:
        action_id: ID of the action to approve, or "all" to approve all pending actions.
            Defaults to "all" if not specified.
        amount_sats: Optional override for channel size (member budget control).
            If provided, uses this amount instead of the proposed amount.
            Must be >= min_channel_sats and will still be subject to budget limits.
            Only applies when approving a single action.

    Returns:
        Dict with approval result including budget details.

    Permission: Member or Admin only
    """
    return rpc_approve_action(_get_hive_context(), action_id, amount_sats)


@plugin.method("hive-reject-action")
def hive_reject_action(plugin: Plugin, action_id="all", reason=None):
    """
    Reject pending action(s).

    Args:
        action_id: ID of the action to reject, or "all" to reject all pending actions.
            Defaults to "all" if not specified.
        reason: Optional reason for rejection (stored for learning).

    Returns:
        Dict with rejection result.

    Permission: Member or Admin only
    """
    return rpc_reject_action(_get_hive_context(), action_id, reason=reason)


@plugin.method("hive-budget-summary")
def hive_budget_summary(plugin: Plugin, days: int = 7):
    """
    Get budget usage summary for autonomous mode.

    Args:
        days: Number of days of history to include (default: 7)

    Returns:
        Dict with budget utilization and spending history.

    Permission: Member or Admin only
    """
    return rpc_budget_summary(_get_hive_context(), days)


# =============================================================================
# PHASE 7: FEE INTELLIGENCE RPC COMMANDS
# =============================================================================

@plugin.method("hive-fee-profiles")
def hive_fee_profiles(plugin: Plugin, peer_id: str = None):
    """
    Get aggregated fee profiles for external peers.

    Fee profiles are built from collective intelligence shared by hive members.
    Includes optimal fee recommendations based on elasticity and NNLB.

    Args:
        peer_id: Optional specific peer to query (otherwise returns all)

    Returns:
        Dict with fee profile(s) and aggregation stats.

    Permission: Member or Admin
    """
    # Permission check: Member or Admin
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not database or not fee_intel_mgr:
        return {"error": "Fee intelligence not initialized"}

    if peer_id:
        # Query specific peer
        profile = database.get_peer_fee_profile(peer_id)
        if not profile:
            return {
                "peer_id": peer_id,
                "error": "No fee profile found",
                "hint": "No hive members have reported on this peer yet"
            }
        return {
            "profile": profile
        }
    else:
        # Return all profiles
        profiles = database.get_all_peer_fee_profiles()
        return {
            "profile_count": len(profiles),
            "profiles": profiles
        }


@plugin.method("hive-fee-recommendation")
def hive_fee_recommendation(plugin: Plugin, peer_id: str, channel_size: int = 0):
    """
    Get fee recommendation for an external peer.

    Uses collective fee intelligence and NNLB health adjustments
    to recommend optimal fee for maximum revenue while supporting
    struggling hive members.

    Args:
        peer_id: External peer to get recommendation for
        channel_size: Our channel size to this peer (for context)

    Returns:
        Dict with recommended fee and reasoning.

    Permission: Member or Admin
    """
    # Permission check: Member or Admin
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not database or not fee_intel_mgr:
        return {"error": "Fee intelligence not initialized"}

    # Get our health for NNLB adjustment
    our_health = 50  # Default to healthy
    if our_pubkey:
        health_record = database.get_member_health(our_pubkey)
        if health_record:
            our_health = health_record.get("overall_health", 50)

    recommendation = fee_intel_mgr.get_fee_recommendation(
        target_peer_id=peer_id,
        our_channel_size=channel_size,
        our_health=our_health
    )

    return recommendation


@plugin.method("hive-fee-intelligence")
def hive_fee_intelligence(plugin: Plugin, max_age_hours: int = 24, peer_id: str = None):
    """
    Get raw fee intelligence reports.

    Returns individual fee observations from hive members before aggregation.

    Args:
        max_age_hours: Maximum age of reports to return (default 24)
        peer_id: Optional filter by target peer

    Returns:
        Dict with fee intelligence reports.

    Permission: Member or Admin
    """
    # Permission check: Member or Admin
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not database:
        return {"error": "Database not initialized"}

    if peer_id:
        reports = database.get_fee_intelligence_for_peer(peer_id, max_age_hours)
    else:
        reports = database.get_all_fee_intelligence(max_age_hours)

    return {
        "report_count": len(reports),
        "max_age_hours": max_age_hours,
        "reports": reports
    }


@plugin.method("hive-aggregate-fees")
def hive_aggregate_fees(plugin: Plugin):
    """
    Trigger fee profile aggregation.

    Aggregates all recent fee intelligence into peer fee profiles.
    Normally runs automatically, but can be triggered manually.

    Returns:
        Dict with aggregation results.

    Permission: Member or Admin
    """
    # Permission check: Member or Admin
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not fee_intel_mgr:
        return {"error": "Fee intelligence manager not initialized"}

    updated_count = fee_intel_mgr.aggregate_fee_profiles()

    return {
        "status": "ok",
        "profiles_updated": updated_count
    }


@plugin.method("hive-fee-intel-query")
def hive_fee_intel_query(plugin: Plugin, peer_id: str = None, action: str = "query"):
    """
    Query aggregated fee intelligence from the hive.

    This RPC is designed for cl-revenue-ops to query competitor fee data
    for informing Hill Climbing fee decisions.

    Args:
        peer_id: Specific peer to query (None for all). Can also use
                 action="list" with peer_id=None to get all known peers.
        action: "query" (default) or "list"
            - query: Get aggregated profile for a single peer
            - list: Get all known peer profiles

    Returns for single peer (action="query"):
    {
        "peer_id": "02abc...",
        "avg_fee_charged": 250,
        "min_fee": 100,
        "max_fee": 500,
        "fee_volatility": 0.15,
        "estimated_elasticity": -0.8,
        "optimal_fee_estimate": 180,
        "confidence": 0.75,
        "market_share": 0.0,  # Calculated by caller with their capacity data
        "hive_capacity_sats": 6000000,
        "hive_reporters": 3,
        "last_updated": 1705000000
    }

    Returns for "list" action:
    {
        "peers": [...],  # List of profiles in same format
        "count": 25
    }

    Permission: None (accessible without hive membership for local cl-revenue-ops)
    """
    # No permission check - this is for local cl-revenue-ops integration
    # cl-revenue-ops runs on the same node, so it's trusted

    if not fee_intel_mgr:
        return {"error": "Fee intelligence manager not initialized"}

    if action == "list":
        profiles = fee_intel_mgr.get_all_profiles(limit=100)
        return {
            "peers": profiles,
            "count": len(profiles)
        }

    if not peer_id:
        return {"error": "peer_id required for query action"}

    profile = fee_intel_mgr.get_aggregated_profile(peer_id)
    if not profile:
        return {
            "error": "no_data",
            "peer_id": peer_id,
            "message": "No fee intelligence data for this peer"
        }

    return profile


@plugin.method("hive-report-fee-observation")
def hive_report_fee_observation(
    plugin: Plugin,
    peer_id: str = "",
    our_fee_ppm: int = 0,
    their_fee_ppm: int = None,
    volume_sats: int = 0,
    forward_count: int = 0,
    period_hours: float = 1.0,
    revenue_rate: float = None
):
    """
    Receive fee observation from cl-revenue-ops.

    This RPC is designed for cl-revenue-ops to report its fee observations
    back to cl-hive for collective intelligence sharing.

    The observation is:
    1. Stored locally in fee_intelligence table
    2. (Optionally) Broadcast to hive via FEE_INTELLIGENCE message
    3. Used in fee profile aggregation

    Args:
        peer_id: External peer being observed
        our_fee_ppm: Our current fee toward this peer
        their_fee_ppm: Their fee toward us (if known)
        volume_sats: Volume routed in observation period
        forward_count: Number of forwards
        period_hours: Observation window length
        revenue_rate: Calculated revenue rate (sats/hour)

    Returns:
        {"status": "accepted", "observation_id": <id>}

    Permission: None (local cl-revenue-ops integration)
    """
    # No permission check - this is for local cl-revenue-ops integration

    if not database or not fee_intel_mgr:
        return {"error": "Fee intelligence not initialized"}

    if not peer_id:
        return {"error": "peer_id is required"}

    if our_fee_ppm < 0:
        return {"error": "our_fee_ppm must be non-negative"}

    # Store the observation
    try:
        timestamp = int(time.time())

        # Calculate revenue if not provided
        if revenue_rate is None and period_hours > 0:
            revenue_sats = (volume_sats * our_fee_ppm) // 1_000_000
            revenue_rate = revenue_sats / period_hours

        # Determine flow direction based on balance change (simplified)
        flow_direction = "balanced"

        # Calculate utilization (simplified - would need channel capacity)
        utilization_pct = 0.0

        # Store via fee_intel_mgr's observation handler
        observation_id = fee_intel_mgr.store_local_observation(
            target_peer_id=peer_id,
            our_fee_ppm=our_fee_ppm,
            their_fee_ppm=their_fee_ppm,
            forward_count=forward_count,
            forward_volume_sats=volume_sats,
            revenue_rate=revenue_rate or 0.0,
            flow_direction=flow_direction,
            utilization_pct=utilization_pct,
            timestamp=timestamp
        )

        return {
            "status": "accepted",
            "observation_id": observation_id,
            "peer_id": peer_id
        }

    except Exception as e:
        plugin.log(f"Error storing fee observation: {e}", level='warn')
        return {"error": f"Failed to store observation: {e}"}


@plugin.method("hive-trigger-fee-broadcast")
def hive_trigger_fee_broadcast(plugin: Plugin):
    """
    Manually trigger fee intelligence broadcast.

    Immediately collects fee observations from our channels and broadcasts
    to all hive members. Useful for testing or forcing an immediate update.

    Returns:
        Dict with broadcast results.

    Permission: Member or Admin
    """
    # Permission check: Member or Admin
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not fee_intel_mgr :
        return {"error": "Fee intelligence manager not initialized"}

    try:
        background_loops._broadcast_our_fee_intelligence()
        return {"status": "ok", "message": "Fee intelligence broadcast triggered"}
    except Exception as e:
        return {"error": f"Broadcast failed: {e}"}


@plugin.method("hive-trigger-health-report")
def hive_trigger_health_report(plugin: Plugin):
    """
    Manually trigger health report broadcast.

    Immediately calculates our health score and broadcasts to all hive members.
    Useful for testing NNLB or forcing an immediate health update.

    Returns:
        Dict with health report results.

    Permission: Member or Admin
    """
    # Permission check: Member or Admin
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not fee_intel_mgr :
        return {"error": "Fee intelligence manager not initialized"}

    try:
        background_loops._broadcast_health_report()
        # Return current health after broadcast
        if database and our_pubkey:
            health = database.get_member_health(our_pubkey)
            if health:
                return {
                    "status": "ok",
                    "message": "Health report broadcast triggered",
                    "our_health": health
                }
        return {"status": "ok", "message": "Health report broadcast triggered"}
    except Exception as e:
        return {"error": f"Health report broadcast failed: {e}"}


@plugin.method("hive-trigger-all")
def hive_trigger_all(plugin: Plugin):
    """
    Manually trigger all fee intelligence operations.

    Runs the complete fee intelligence cycle:
    1. Broadcast fee intelligence
    2. Aggregate fee profiles
    3. Broadcast health report

    Useful for testing or forcing immediate updates.

    Returns:
        Dict with all operation results.

    Permission: Member or Admin
    """
    # Permission check: Member or Admin
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not fee_intel_mgr :
        return {"error": "Fee intelligence manager not initialized"}

    results = {}

    try:
        background_loops._broadcast_our_fee_intelligence()
        results["fee_broadcast"] = "ok"
    except Exception as e:
        results["fee_broadcast"] = f"error: {e}"

    try:
        updated = fee_intel_mgr.aggregate_fee_profiles()
        results["profiles_aggregated"] = updated
    except Exception as e:
        results["profiles_aggregated"] = f"error: {e}"

    try:
        background_loops._broadcast_health_report()
        results["health_broadcast"] = "ok"
    except Exception as e:
        results["health_broadcast"] = f"error: {e}"

    # Get current state after operations
    if database and our_pubkey:
        health = database.get_member_health(our_pubkey)
        if health:
            results["our_health"] = health.get("overall_health")
            results["our_tier"] = health.get("tier")

    results["status"] = "ok"
    return results


@plugin.method("hive-nnlb-status")
def hive_nnlb_status(plugin: Plugin):
    """
    Get NNLB (No Node Left Behind) status.

    Shows health distribution across hive members and identifies
    struggling members who may need assistance.

    Returns:
        Dict with NNLB statistics and member health tiers.

    Permission: Member or Admin
    """
    # Permission check: Member or Admin
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not fee_intel_mgr:
        return {"error": "Fee intelligence manager not initialized"}

    return fee_intel_mgr.get_nnlb_status()


@plugin.method("hive-member-health")
def hive_member_health(plugin: Plugin, member_id: str = None, action: str = "query"):
    """
    Query NNLB health scores for fleet members.

    This is INFORMATION SHARING only - no fund movement.
    Used by cl-revenue-ops to adjust its own rebalancing priorities.

    Args:
        member_id: Specific member (None for self, "all" for fleet summary)
        action: "query" (default) or "aggregate" (fleet summary)

    Returns for single member:
    {
        "member_id": "02abc...",
        "health_score": 65,
        "health_tier": "stable",
        "budget_multiplier": 1.0,
        "capacity_score": 70,
        "revenue_score": 60,
        "connectivity_score": 72,
        ...
    }

    Returns for "aggregate" or member_id="all":
    {
        "fleet_health": 58,
        "member_count": 5,
        "struggling_count": 1,
        "vulnerable_count": 2,
        "stable_count": 2,
        "thriving_count": 0,
        "members": [...]
    }

    Permission: None (local cl-revenue-ops integration)
    """
    # No permission check - this is for local cl-revenue-ops integration

    if not database or not health_aggregator:
        return {"error": "Health tracking not initialized"}

    # Handle "all" member_id or "aggregate" action
    if member_id == "all" or action == "aggregate":
        summary = health_aggregator.get_fleet_health_summary()
        return summary

    # Query specific member or self
    target_id = member_id if member_id else our_pubkey
    if not target_id:
        return {"error": "No member specified and our_pubkey not set"}

    health = health_aggregator.get_our_health(target_id)
    if not health:
        return {
            "member_id": target_id,
            "error": "No health record found",
            # Return defaults for graceful degradation
            "health_score": 50,
            "health_tier": "stable",
            "budget_multiplier": 1.0
        }

    # Rename overall_health to health_score for API consistency
    health["health_score"] = health.pop("overall_health", 50)
    health["member_id"] = target_id

    return health


@plugin.method("hive-report-health")
def hive_report_health(
    plugin: Plugin,
    profitable_channels: int = 0,
    underwater_channels: int = 0,
    stagnant_channels: int = 0,
    total_channels: int = None,
    revenue_trend: str = "stable",
    liquidity_score: int = 50
):
    """
    Report health status from cl-revenue-ops.

    Called periodically by cl-revenue-ops profitability analyzer.
    This shares INFORMATION - no sats move between nodes.

    The health score is calculated from profitability metrics and used
    to determine the node's NNLB budget multiplier for its own operations.

    Args:
        profitable_channels: Number of channels classified as profitable
        underwater_channels: Number of channels classified as underwater
        stagnant_channels: Number of stagnant/zombie channels
        total_channels: Total channel count (defaults to sum of above)
        revenue_trend: "improving", "stable", or "declining"
        liquidity_score: Liquidity balance score 0-100 (default 50)

    Returns:
        {"status": "reported", "health_score": 65, "health_tier": "stable",
         "budget_multiplier": 1.0}

    Permission: None (local cl-revenue-ops integration)
    """
    # No permission check - this is for local cl-revenue-ops integration

    if not database or not health_aggregator or not our_pubkey:
        return {"error": "Health tracking not initialized"}

    # Guard against empty-param relay — don't overwrite real health data with zeros
    if profitable_channels == 0 and underwater_channels == 0 and stagnant_channels == 0:
        return {"error": "At least one channel category must have a non-zero count"}

    # Calculate total if not provided
    if total_channels is None:
        total_channels = profitable_channels + underwater_channels + stagnant_channels

    # Validate inputs
    if total_channels < 0:
        return {"error": "total_channels must be non-negative"}
    if revenue_trend not in ["improving", "stable", "declining"]:
        revenue_trend = "stable"
    liquidity_score = max(0, min(100, liquidity_score))

    try:
        # Update our health using the aggregator
        result = health_aggregator.update_our_health(
            profitable_channels=profitable_channels,
            underwater_channels=underwater_channels,
            stagnant_channels=stagnant_channels,
            total_channels=total_channels,
            revenue_trend=revenue_trend,
            liquidity_score=liquidity_score,
            our_pubkey=our_pubkey
        )

        return {
            "status": "reported",
            "health_score": result.get("health_score", 50),
            "health_tier": result.get("health_tier", "stable"),
            "budget_multiplier": result.get("budget_multiplier", 1.0)
        }

    except Exception as e:
        plugin.log(f"Error updating health: {e}", level='warn')
        return {"error": f"Failed to update health: {e}"}


@plugin.method("hive-calculate-health")
def hive_calculate_health(plugin: Plugin):
    """
    Calculate and return our node's health score.

    Uses local channel and revenue data to calculate health scores
    for NNLB purposes.

    Returns:
        Dict with our health assessment.

    Permission: Member or Admin
    """
    # Permission check: Member or Admin
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not fee_intel_mgr :
        return {"error": "Not initialized"}

    # Get our channel data
    try:
        funds = plugin.rpc.listfunds()
        channels = funds.get("channels", [])

        capacity_sats = sum(
            ch.get("our_amount_msat", 0) // 1000 + ch.get("amount_msat", 0) // 1000 - ch.get("our_amount_msat", 0) // 1000
            for ch in channels if ch.get("state") == "CHANNELD_NORMAL"
        )
        available_sats = sum(
            ch.get("our_amount_msat", 0) // 1000
            for ch in channels if ch.get("state") == "CHANNELD_NORMAL"
        )
        channel_count = len([ch for ch in channels if ch.get("state") == "CHANNELD_NORMAL"])

    except Exception as e:
        return {"error": f"Failed to get channel data: {e}"}

    # Get hive averages for comparison
    all_health = database.get_all_member_health() if database else []
    if all_health:
        hive_avg_capacity = sum(h.get("capacity_score", 50) for h in all_health) / len(all_health) * 200000
    else:
        hive_avg_capacity = 10_000_000  # 10M default

    # Calculate health (revenue estimation simplified)
    health = fee_intel_mgr.calculate_our_health(
        capacity_sats=capacity_sats,
        available_sats=available_sats,
        channel_count=channel_count,
        daily_revenue_sats=0,  # Would need forwarding stats
        hive_avg_capacity=int(hive_avg_capacity)
    )

    return {
        "our_pubkey": our_pubkey,
        "channel_count": channel_count,
        "capacity_sats": capacity_sats,
        "available_sats": available_sats,
        **health
    }


@plugin.method("hive-routing-stats")
def hive_routing_stats(plugin: Plugin):
    """
    Get routing intelligence statistics.

    Shows collective routing intelligence from all hive members including
    path success rates, probe counts, and route suggestions.

    Returns:
        Dict with routing intelligence statistics.

    Permission: Member or Admin
    """
    # Permission check: Member or Admin
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not routing_map:
        return {"error": "Routing intelligence not initialized"}

    stats = routing_map.get_routing_stats()
    return {
        "paths_tracked": stats.get("total_paths", 0),
        "total_probes": stats.get("total_probes", 0),
        "total_successes": stats.get("total_successes", 0),
        "unique_destinations": stats.get("unique_destinations", 0),
        "high_quality_paths": stats.get("high_quality_paths", 0),
        "overall_success_rate": round(stats.get("overall_success_rate", 0.0), 3),
    }


@plugin.method("hive-route-suggest")
def hive_route_suggest(plugin: Plugin, destination: str, amount_sats: int = 100000):
    """
    Get route suggestions for a destination using hive intelligence.

    Uses collective routing data to suggest optimal paths.

    Args:
        destination: Target node pubkey
        amount_sats: Amount to route (default 100000)

    Returns:
        Dict with route suggestions.

    Permission: Member or Admin
    """
    # Permission check: Member or Admin
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not routing_map:
        return {"error": "Routing intelligence not initialized"}

    routes = routing_map.get_routes_to(destination, amount_sats)

    return {
        "destination": destination,
        "amount_sats": amount_sats,
        "route_count": len(routes),
        "routes": [
            {
                "path": list(r.path),
                "success_rate": r.success_rate,
                "expected_latency_ms": r.expected_latency_ms,
                "confidence": r.confidence,
            }
            for r in routes[:5]  # Top 5 suggestions
        ]
    }


@plugin.method("hive-peer-reputations")
def hive_peer_reputations(plugin: Plugin, peer_id: str = None):
    """
    Get aggregated peer reputations from hive intelligence.

    Peer reputations are aggregated from reports by all hive members
    with outlier detection to prevent manipulation.

    Args:
        peer_id: Optional specific peer to query

    Returns:
        Dict with peer reputation data.

    Permission: Member or Admin
    """
    # Permission check: Member or Admin
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not peer_reputation_mgr:
        return {"error": "Peer reputation manager not initialized"}

    if peer_id:
        rep = peer_reputation_mgr.get_reputation(peer_id)
        if not rep:
            return {
                "peer_id": peer_id,
                "error": "No reputation data found"
            }
        return {
            "peer_id": rep.peer_id,
            "reputation_score": rep.reputation_score,
            "confidence": rep.confidence,
            "avg_uptime": rep.avg_uptime,
            "avg_htlc_success": rep.avg_htlc_success,
            "avg_fee_stability": rep.avg_fee_stability,
            "total_force_closes": rep.total_force_closes,
            "report_count": rep.report_count,
            "reporter_count": len(rep.reporters),
            "warnings": rep.warnings,
        }
    else:
        stats = peer_reputation_mgr.get_reputation_stats()
        all_reps = peer_reputation_mgr.get_all_reputations()
        return {
            **stats,
            "reputations": [
                {
                    "peer_id": rep.peer_id,
                    "reputation_score": rep.reputation_score,
                    "confidence": rep.confidence,
                    "warnings": list(rep.warnings.keys()),
                }
                for rep in all_reps.values()
            ]
        }


@plugin.method("hive-reputation-stats")
def hive_reputation_stats(plugin: Plugin):
    """
    Get overall reputation tracking statistics.

    Returns summary statistics about tracked peer reputations.

    Returns:
        Dict with reputation statistics.

    Permission: Member or Admin
    """
    # Permission check: Member or Admin
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not peer_reputation_mgr:
        return {"error": "Peer reputation manager not initialized"}

    return peer_reputation_mgr.get_reputation_stats()


@plugin.method("hive-liquidity-needs")
def hive_liquidity_needs(plugin: Plugin, peer_id: str = None):
    """
    Get current liquidity needs from hive members.

    Shows liquidity requests from members that may need assistance
    with rebalancing or capacity.

    Args:
        peer_id: Optional filter by specific member

    Returns:
        Dict with liquidity needs.

    Permission: Member or Admin
    """
    # Permission check: Member or Admin
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not database:
        return {"error": "Database not initialized"}

    if peer_id:
        needs = database.get_liquidity_needs_for_reporter(peer_id)
    else:
        needs = database.get_all_liquidity_needs(max_age_hours=24)

    return {
        "need_count": len(needs),
        "needs": needs
    }


@plugin.method("hive-liquidity-status")
def hive_liquidity_status(plugin: Plugin):
    """
    Get liquidity coordination status.

    Shows rebalance proposals, pending needs, and assistance statistics.

    Returns:
        Dict with liquidity coordination status.

    Permission: Member or Admin
    """
    # Permission check: Member or Admin
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not liquidity_coord:
        return {"error": "Liquidity coordinator not initialized"}

    return liquidity_coord.get_status()


@plugin.method("hive-liquidity-state")
def hive_liquidity_state(plugin: Plugin, action: str = "status"):
    """
    Query fleet liquidity state for coordination.

    INFORMATION ONLY - no sats move between nodes. This enables nodes
    to make better independent decisions about fees and rebalancing.

    Args:
        action: "status" (overview), "needs" (who needs what)

    Returns for "status":
        Fleet liquidity state overview including:
        - Members with depleted/saturated channels
        - Common bottleneck peers
        - Rebalancing activity

    Returns for "needs":
        List of fleet liquidity needs with relevance scores

    Permission: Member or Admin
    """
    # Permission check: Member or Admin
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not liquidity_coord:
        return {"error": "Liquidity coordinator not initialized"}

    if action == "status":
        return liquidity_coord.get_fleet_liquidity_state()
    elif action == "needs":
        return {"fleet_needs": liquidity_coord.get_fleet_liquidity_needs()}
    else:
        return {"error": f"Unknown action: {action}"}


@plugin.method("hive-report-liquidity-state")
def hive_report_liquidity_state(
    plugin: Plugin,
    depleted_channels: list = None,
    saturated_channels: list = None,
    rebalancing_active: bool = False,
    rebalancing_peers: list = None,
    liquidity_needs: list = None
):
    """
    Report liquidity state from cl-revenue-ops.

    INFORMATION SHARING - enables coordinated fee/rebalance decisions.
    No sats transfer between nodes.

    Called periodically by cl-revenue-ops profitability analyzer to share
    current channel states with the fleet.

    Args:
        depleted_channels: List of {peer_id, local_pct, capacity_sats}
        saturated_channels: List of {peer_id, local_pct, capacity_sats}
        rebalancing_active: Whether we're currently rebalancing
        rebalancing_peers: Which peers we're rebalancing through
        liquidity_needs: Flow-aware enriched needs from cl-revenue-ops

    Returns:
        {"status": "recorded", "depleted_count": N, "saturated_count": M}

    Permission: None (local cl-revenue-ops integration)
    """
    # No permission check - this is for local cl-revenue-ops integration

    if not liquidity_coord or not our_pubkey:
        return {"error": "Liquidity coordinator not initialized"}

    return liquidity_coord.record_member_liquidity_report(
        member_id=our_pubkey,
        depleted_channels=depleted_channels or [],
        saturated_channels=saturated_channels or [],
        rebalancing_active=rebalancing_active,
        rebalancing_peers=rebalancing_peers,
        enriched_needs=liquidity_needs
    )


@plugin.method("hive-update-rebalancing-activity")
def hive_update_rebalancing_activity(
    plugin: Plugin,
    rebalancing_active: bool = False,
    rebalancing_peers: list = None
):
    """
    Targeted update of rebalancing activity from cl-revenue-ops rebalancer.

    Unlike hive-report-liquidity-state which UPSERTs all fields, this only
    updates rebalancing_active and rebalancing_peers, preserving existing
    depleted/saturated channel data.

    Called by the rebalancer's JobManager when sling jobs start or stop.

    Args:
        rebalancing_active: Whether we're currently rebalancing
        rebalancing_peers: Which peers we're rebalancing through

    Returns:
        {"status": "updated", ...}

    Permission: None (local cl-revenue-ops integration)
    """
    if not liquidity_coord or not our_pubkey:
        return {"error": "Liquidity coordinator not initialized"}

    return liquidity_coord.update_rebalancing_activity(
        member_id=our_pubkey,
        rebalancing_active=rebalancing_active,
        rebalancing_peers=rebalancing_peers
    )


@plugin.method("hive-check-rebalance-conflict")
def hive_check_rebalance_conflict(
    plugin: Plugin,
    peer_id: str = "",
    direction: str = "outbound",
    amount_sats: int = 0,
):
    """Check rebalance conflict with fleet activity."""
    ctx = _get_hive_context()
    return rpc_check_rebalance_conflict(
        ctx, peer_id=peer_id, direction=direction, amount_sats=amount_sats,
    )


@plugin.method("hive-report-traffic-profile")
def hive_report_traffic_profile(
    plugin: Plugin,
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
    """Receive traffic profile from cl-revenue-ops."""
    ctx = _get_hive_context()
    return rpc_report_traffic_profile(
        ctx, peer_id=peer_id, profile_type=profile_type,
        peak_hours_utc=peak_hours_utc, quiet_hours_utc=quiet_hours_utc,
        avg_forward_size_sats=avg_forward_size_sats,
        daily_volume_sats=daily_volume_sats,
        drain_direction=drain_direction, confidence=confidence,
        observation_window_hours=observation_window_hours,
    )


@plugin.method("hive-traffic-intelligence")
def hive_traffic_intelligence(
    plugin: Plugin,
    peer_id: str = None,
    profile_type: str = None,
):
    """Query aggregated fleet traffic intelligence."""
    ctx = _get_hive_context()
    return rpc_get_traffic_intelligence(ctx, peer_id=peer_id, profile_type=profile_type)


@plugin.method("hive-fleet-demand-forecast")
def hive_fleet_demand_forecast(plugin: Plugin, hours_ahead: int = 6):
    """Get fleet-wide demand forecast."""
    ctx = _get_hive_context()
    return rpc_get_fleet_demand_forecast(ctx, hours_ahead=hours_ahead)


@plugin.method("hive-splice-check")
def hive_splice_check(
    plugin: Plugin,
    peer_id: str,
    splice_type: str,
    amount_sats: int,
    channel_id: str = None
):
    """
    Check if a splice operation is safe for fleet connectivity.

    SAFETY CHECK ONLY - no fund movement between nodes.
    Each node manages its own splices. This is advisory.

    Use this before performing splice-out to ensure fleet connectivity
    is maintained. Splice-in is always safe (increases capacity).

    Args:
        peer_id: External peer being spliced from/to
        splice_type: "splice_in" or "splice_out"
        amount_sats: Amount to splice in/out
        channel_id: Optional specific channel ID

    Returns for splice_out:
        {
            "safety": "safe" | "coordinate" | "blocked",
            "reason": str,
            "can_proceed": bool,
            "fleet_capacity": int,
            "new_fleet_capacity": int,
            "fleet_share": float,
            "new_share": float,
            "recommendation": str (if not safe)
        }

    Returns for splice_in:
        {"safety": "safe", "reason": "Splice-in always safe"}

    Permission: Member or Admin
    """
    # Permission check: Member or Admin
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not splice_coord:
        return {"error": "Splice coordinator not initialized"}

    if splice_type == "splice_in":
        return splice_coord.check_splice_in_safety(peer_id, amount_sats)
    elif splice_type == "splice_out":
        return splice_coord.check_splice_out_safety(peer_id, amount_sats, channel_id)
    else:
        return {"error": f"Unknown splice_type: {splice_type}, use 'splice_in' or 'splice_out'"}


@plugin.method("hive-splice-recommendations")
def hive_splice_recommendations(plugin: Plugin, peer_id: str):
    """
    Get splice recommendations for a specific peer.

    Returns info about fleet connectivity and safe splice amounts.
    INFORMATION ONLY - helps nodes make informed splice decisions.

    Args:
        peer_id: External peer to analyze

    Returns:
        {
            "peer_id": str,
            "fleet_capacity": int,
            "our_capacity": int,
            "other_member_capacity": int,
            "safe_splice_out_amount": int,
            "has_fleet_coverage": bool,
            "recommendations": [str]
        }

    Permission: Member or Admin
    """
    # Permission check: Member or Admin
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not splice_coord:
        return {"error": "Splice coordinator not initialized"}

    return splice_coord.get_splice_recommendations(peer_id)


@plugin.method("hive-set-mode")
def hive_set_mode(plugin: Plugin, mode: str):
    """
    Change the governance mode at runtime.

    Args:
        mode: New governance mode ('advisor' or 'autonomous')

    Returns:
        Dict with new mode and previous mode.

    Permission: Admin only
    """
    return rpc_set_mode(_get_hive_context(), mode)


@plugin.method("hive-enable-expansions")
def hive_enable_expansions(plugin: Plugin, enabled: bool = True):
    """
    Enable or disable expansion proposals at runtime.

    Args:
        enabled: True to enable expansions, False to disable (default: True)

    Returns:
        Dict with new setting.

    Permission: Admin only
    """
    return rpc_enable_expansions(_get_hive_context(), enabled)


@plugin.method("hive-bump-version")
def hive_bump_version(plugin: Plugin, version: int):
    """
    Manually set the gossip state version for restart recovery.

    Use this to fix version sync issues where the persisted version
    diverged from what peers remember.

    Args:
        version: New version number (must be higher than current)

    Returns:
        Dict with old and new version.
    """
    if not state_manager or not gossip_mgr or not our_pubkey:
        return {"error": "state_manager_unavailable"}

    # Get current versions
    our_state = state_manager.get_peer_state(our_pubkey)
    old_db_version = our_state.version if our_state else 0
    with gossip_mgr._lock:
        old_gossip_version = gossip_mgr._last_broadcast_state.version

    # Update in-memory state and database via proper locked API
    state_manager.update_local_state(
        capacity_sats=our_state.capacity_sats if our_state else 0,
        available_sats=our_state.available_sats if our_state else 0,
        fee_policy=our_state.fee_policy if our_state else {},
        topology=our_state.topology if our_state else [],
        our_pubkey=our_pubkey,
        force_version=version
    )

    # Update gossip manager version
    with gossip_mgr._lock:
        gossip_mgr._last_broadcast_state.version = version

    return {
        "old_db_version": old_db_version,
        "old_gossip_version": old_gossip_version,
        "new_version": version
    }


@plugin.method("hive-gossip-stats")
def hive_gossip_stats(plugin: Plugin):
    """
    Get gossip statistics and state versions for all peers.

    Shows version numbers for debugging state synchronization issues.
    Useful to verify that nodes have consistent views of each other's state.

    Returns:
        Dict with our state, gossip manager state, and all peer states.
    """
    if not state_manager or not gossip_mgr or not our_pubkey:
        return {"error": "state_manager_unavailable"}

    # Get gossip manager internal state
    gossip_state = gossip_mgr.get_gossip_stats()

    # Get our own state from state manager
    our_state = state_manager.get_peer_state(our_pubkey)

    # Get all peer states
    all_states = state_manager.get_all_peer_states()
    peer_versions = {}
    for state in all_states:
        peer_versions[state.peer_id[:16] + "..."] = {
            "version": state.version,
            "last_update": state.last_update,
            "capacity_sats": state.capacity_sats,
            "available_sats": state.available_sats,
            "is_self": state.peer_id == our_pubkey
        }

    return {
        "our_pubkey": our_pubkey[:16] + "...",
        "gossip_manager": {
            "broadcast_version": gossip_state["version"],
            "last_broadcast_ago": gossip_state["last_broadcast_ago"],
            "heartbeat_interval": gossip_state["heartbeat_interval"],
            "active_peers": gossip_state["active_peers"]
        },
        "our_state": {
            "version": our_state.version if our_state else None,
            "capacity_sats": our_state.capacity_sats if our_state else 0,
            "available_sats": our_state.available_sats if our_state else 0
        },
        "peer_states": peer_versions
    }


@plugin.method("hive-vouch")
def hive_vouch(plugin: Plugin, peer_id: str):
    """
    Manually vouch for a neophyte to support their promotion.

    Args:
        peer_id: Public key of the neophyte to vouch for

    Returns:
        Dict with vouch status.
    """
    if not config or not config.membership_enabled:
        return {"error": "membership_disabled"}
    if not membership_mgr or not our_pubkey or not database:
        return {"error": "membership_unavailable"}

    # Check our tier - must be member or admin to vouch
    our_tier = membership_mgr.get_tier(our_pubkey)
    if our_tier not in (MembershipTier.MEMBER.value,):
        return {"error": "permission_denied", "required_tier": "member"}

    # Check target is a neophyte
    target = database.get_member(peer_id)
    if not target:
        return {"error": "peer_not_found", "peer_id": peer_id}
    if target.get("tier") != MembershipTier.NEOPHYTE.value:
        return {"error": "peer_not_neophyte", "current_tier": target.get("tier")}

    # Check if target has a pending promotion request
    requests = database.get_promotion_requests(peer_id)
    pending_request = None
    for req in requests:
        if req.get("status") == "pending":
            pending_request = req
            break

    if not pending_request:
        # Auto-create promotion request if member is vouching
        # This allows members to initiate promotion without neophyte requesting
        request_id = f"member_initiated_{int(time.time())}"
        database.add_promotion_request(peer_id, request_id, status="pending")
        plugin.log(f"cl-hive: Auto-created promotion request for {peer_id[:16]}... (member-initiated vouch)")
    else:
        request_id = pending_request["request_id"]

    # Check if we already vouched
    existing_vouches = database.get_promotion_vouches(peer_id, request_id)
    for vouch in existing_vouches:
        if vouch.get("voucher_peer_id") == our_pubkey:
            return {"error": "already_vouched", "peer_id": peer_id}

    # Create and sign vouch
    vouch_ts = int(time.time())
    canonical = membership_mgr.build_vouch_message(peer_id, request_id, vouch_ts)

    try:
        sig = plugin.rpc.signmessage(canonical)["zbase"]
    except Exception as e:
        return {"error": f"Failed to sign vouch: {e}"}

    # Store locally
    database.add_promotion_vouch(peer_id, request_id, our_pubkey, sig, vouch_ts)

    # Broadcast to members
    vouch_payload = {
        "target_pubkey": peer_id,
        "request_id": request_id,
        "timestamp": vouch_ts,
        "voucher_pubkey": our_pubkey,
        "sig": sig
    }
    vouch_msg = serialize(HiveMessageType.VOUCH, vouch_payload)
    protocol_handlers._broadcast_to_members(vouch_msg)

    # Check if quorum reached
    all_vouches = database.get_promotion_vouches(peer_id, request_id)
    active_members = membership_mgr.get_active_members()
    quorum = membership_mgr.calculate_quorum(len(active_members))
    quorum_reached = len(all_vouches) >= quorum

    # Auto-promote if quorum reached
    if quorum_reached and config.auto_promote_enabled:
        # Update member tier via membership manager (triggers set_hive_policy)
        membership_mgr.set_tier(peer_id, MembershipTier.MEMBER.value)
        database.update_promotion_request_status(peer_id, request_id, "accepted")
        plugin.log(f"cl-hive: Promoted {peer_id[:16]}... to member (quorum reached)")

        # Broadcast PROMOTION message
        promotion_payload = {
            "target_pubkey": peer_id,
            "request_id": request_id,
            "vouches": [
                {
                    "target_pubkey": v["target_peer_id"],
                    "request_id": v["request_id"],
                    "timestamp": v["timestamp"],
                    "voucher_pubkey": v["voucher_peer_id"],
                    "sig": v["sig"]
                } for v in all_vouches[:MAX_VOUCHES_IN_PROMOTION]
            ]
        }
        promo_msg = serialize(HiveMessageType.PROMOTION, promotion_payload)
        protocol_handlers._broadcast_to_members(promo_msg)

    return {
        "status": "vouched",
        "peer_id": peer_id,
        "request_id": request_id,
        "vouch_count": len(all_vouches),
        "quorum_needed": quorum,
        "quorum_reached": quorum_reached,
    }


@plugin.method("hive-force-promote")
def hive_force_promote(plugin: Plugin, peer_id: str):
    """
    Admin command to force-promote a neophyte to member during bootstrap.

    This bypasses the normal quorum requirement when the hive is too small
    to reach quorum naturally. Only works when total member count < min_vouch_count.

    Args:
        peer_id: Public key of the neophyte to promote

    Returns:
        Dict with promotion status.

    Permission: Admin only, bootstrap phase only
    """
    # Permission check: Admin only
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not database or not our_pubkey or not membership_mgr:
        return {"error": "Database not initialized"}

    # Check we're in bootstrap phase (member count < 3)
    # Note: This function is deprecated as admin tier was removed
    members = database.get_all_members()
    member_count = len(members)
    min_for_quorum = 3  # Hardcoded - vouch system removed

    if member_count >= min_for_quorum:
        return {
            "error": "bootstrap_complete",
            "message": f"Hive has {member_count} members, use normal promotion process",
            "member_count": member_count
        }

    # Check target is a neophyte
    target = database.get_member(peer_id)
    if not target:
        return {"error": "peer_not_found", "peer_id": peer_id}
    if target.get("tier") != MembershipTier.NEOPHYTE.value:
        return {"error": "peer_not_neophyte", "current_tier": target.get("tier")}

    # Force promote via membership manager (triggers set_hive_policy)
    success = membership_mgr.set_tier(peer_id, MembershipTier.MEMBER.value)
    if not success:
        return {"error": "promotion_failed", "peer_id": peer_id}

    plugin.log(f"cl-hive: Force-promoted {peer_id[:16]}... to member (bootstrap)")

    # Broadcast PROMOTION message to sync state
    now_ts = int(time.time())
    request_id = f"bootstrap_{now_ts}"
    # Sign the vouch with our node key for authenticity
    vouch_msg = f"VOUCH:{peer_id}:{request_id}:{now_ts}"
    vouch_sig = ""
    if identity_adapter:
        vouch_sig = identity_adapter.sign_message(vouch_msg)
    if not vouch_sig:
        vouch_sig = "unsigned_bootstrap"
        plugin.log("cl-hive: WARNING - could not sign bootstrap promotion vouch", level='warn')
    promotion_payload = {
        "target_pubkey": peer_id,
        "request_id": request_id,
        "vouches": [{
            "target_pubkey": peer_id,
            "request_id": request_id,
            "timestamp": now_ts,
            "voucher_pubkey": our_pubkey,
            "sig": vouch_sig
        }]
    }
    promo_msg = serialize(HiveMessageType.PROMOTION, promotion_payload)
    protocol_handlers._broadcast_to_members(promo_msg)

    return {
        "status": "promoted",
        "peer_id": peer_id,
        "new_tier": MembershipTier.MEMBER.value,
        "method": "admin_bootstrap",
        "remaining_bootstrap_slots": min_for_quorum - member_count - 1
    }


@plugin.method("hive-ban")
def hive_ban(plugin: Plugin, peer_id: str, reason: str):
    """
    Propose a ban for a peer.

    Args:
        peer_id: Public key of the peer to ban
        reason: Reason for the ban

    Returns:
        Dict with ban status.

    Permission: Admin only
    """
    # Permission check: Admin only
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not database or not our_pubkey:
        return {"error": "Database not initialized"}

    # Check if already banned
    if database.is_banned(peer_id):
        return {"error": "peer_already_banned", "peer_id": peer_id}

    # Check if peer is a member
    member = database.get_member(peer_id)
    if not member:
        return {"error": "peer_not_member", "peer_id": peer_id}

    # Cannot direct-ban full members; use hive-propose-ban + vote instead
    if member.get("tier") == MembershipTier.MEMBER.value:
        return {"error": "cannot_ban_member", "message": "Full members require proposal/vote via hive-propose-ban", "peer_id": peer_id}

    # Sign the ban reason
    now = int(time.time())
    ban_message = f"BAN:{peer_id}:{reason}:{now}"

    try:
        sig = plugin.rpc.signmessage(ban_message)["zbase"]
    except Exception as e:
        return {"error": f"Failed to sign ban: {e}"}

    # R5-M-8 fix: add_ban accepts expires_days (int), not expires_at (timestamp)
    expires_days = 365  # 1 year default
    success = database.add_ban(
        peer_id=peer_id,
        reason=reason,
        reporter=our_pubkey,
        signature=sig,
        expires_days=expires_days
    )

    if not success:
        return {"error": "Failed to add ban", "peer_id": peer_id}

    # R5-M-9 fix: Remove member from roster after successful ban
    database.remove_member(peer_id)

    plugin.log(f"cl-hive: Banned peer {peer_id[:16]}... reason: {reason}")

    return {
        "status": "banned",
        "peer_id": peer_id,
        "reason": reason,
        "reporter": our_pubkey,
        "expires_days": expires_days,
    }


@plugin.method("hive-promote-admin")
def hive_promote_admin(plugin: Plugin, peer_id: str):
    """
    DEPRECATED: Admin tier has been removed from the 2-tier membership system.

    The current system uses only NEOPHYTE and MEMBER tiers.
    Use hive-propose-promotion to promote neophytes to member.
    """
    return {
        "error": "deprecated",
        "message": "Admin tier removed. Use hive-propose-promotion for neophyte->member promotions."
    }


@plugin.method("hive-leave")
def hive_leave(plugin: Plugin, reason: str = "voluntary"):
    """
    Voluntarily leave the hive.

    This removes you from the hive member list and notifies other members.
    Your fee policies will be reverted to dynamic.

    Restrictions:
    - The last full member cannot leave (would make hive headless)
    - Promote a neophyte to member before leaving if you're the last one

    Args:
        reason: Optional reason for leaving (default: "voluntary")

    Returns:
        Dict with leave status.

    Permission: Any member
    """
    if not database or not our_pubkey :
        return {"error": "Hive not initialized"}

    # Check we're a member of the hive
    member = database.get_member(our_pubkey)
    if not member:
        return {"error": "not_a_member", "message": "You are not a member of any hive"}

    our_tier = member.get("tier")

    # Check if we're the last full member
    if our_tier == MembershipTier.MEMBER.value:
        all_members = database.get_all_members()
        member_count = sum(1 for m in all_members if m.get("tier") == MembershipTier.MEMBER.value)
        if member_count <= 1:
            return {
                "error": "cannot_leave",
                "message": "Cannot leave: you are the only full member. Promote a neophyte first, or the hive will become headless."
            }

    # Create signed leave message
    timestamp = int(time.time())
    canonical = f"hive:leave:{our_pubkey}:{timestamp}:{reason}"

    try:
        sig = plugin.rpc.signmessage(canonical)["zbase"]
    except Exception as e:
        return {"error": f"Failed to sign leave message: {e}"}

    # Broadcast to members before removing ourselves (reliable delivery)
    leave_payload = {
        "peer_id": our_pubkey,
        "timestamp": timestamp,
        "reason": reason,
        "signature": sig
    }
    protocol_handlers._reliable_broadcast(HiveMessageType.MEMBER_LEFT, leave_payload)

    # Revert our fee policy to dynamic
    if bridge and bridge.status == BridgeStatus.ENABLED:
        try:
            bridge.set_hive_policy(our_pubkey, is_member=False)
        except Exception:
            pass  # Best effort

    # Remove ourselves from the member list
    database.remove_member(our_pubkey)
    plugin.log(f"cl-hive: Left the hive ({our_tier}): {reason}")

    return {
        "status": "left",
        "peer_id": our_pubkey,
        "former_tier": our_tier,
        "reason": reason,
        "message": "You have left the hive. Fee policies reverted to dynamic."
    }


@plugin.method("hive-remove-member")
def hive_remove_member(plugin: Plugin, peer_id: str, reason: str = "maintenance", force: bool = False):
    """
    Remove a member from the hive (admin maintenance).

    Use this to clean up stale/orphaned member entries, such as when a node's
    database was reset and needs to rejoin fresh.

    Args:
        peer_id: Public key of the member to remove
        reason: Reason for removal (default: "maintenance")
        force: Allow removal even if the peer still has active/open channels

    Returns:
        Dict with removal status.

    Permission: Member only (cannot remove yourself - use hive-leave)
    """
    if not database or not our_pubkey:
        return {"error": "Hive not initialized"}

    # Permission check: must be a member
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    # Cannot remove yourself - use hive-leave
    if peer_id == our_pubkey:
        return {"error": "cannot_remove_self", "message": "Use hive-leave to remove yourself"}

    # Check if target is a member
    member = database.get_member(peer_id)
    if not member:
        return {"error": "peer_not_found", "peer_id": peer_id}

    target_tier = member.get("tier")

    # Safety check: refuse removal when the peer still has active/open channels
    # unless the caller explicitly forces it. This prevents accidentally removing
    # active external peers (e.g. cyber-hornet) from Hive membership.
    try:
        lpc = plugin.rpc.listpeerchannels(id=peer_id)
        peer_channels = lpc.get("channels", []) if isinstance(lpc, dict) else []
    except Exception as e:
        return {
            "error": "channel_check_failed",
            "peer_id": peer_id,
            "message": f"Failed to verify channel state before removal: {e}"
        }

    active_channel_states = []
    for ch in peer_channels:
        state = ch.get("state") or ""
        owner = ch.get("owner") or ""
        # ONCHAIN/onchaind channels are already closed from routing perspective.
        if state.startswith("ONCHAIN") or owner == "onchaind":
            continue
        active_channel_states.append({
            "channel_id": ch.get("short_channel_id"),
            "state": state,
            "owner": owner
        })

    if active_channel_states and not force:
        try:
            peer_alias = (plugin.rpc.listnodes(peer_id).get("nodes") or [{}])[0].get("alias")
        except Exception:
            peer_alias = None
        return {
            "error": "active_channels_present",
            "peer_id": peer_id,
            "peer_alias": peer_alias,
            "active_channel_count": len(active_channel_states),
            "active_channels": active_channel_states[:10],
            "message": (
                "Refusing to remove hive member with active/open channels. "
                "Use force=true only if you intend to remove Hive membership while keeping LN channels."
            )
        }

    # Full removal: DB, state manager, bridge policy, and broadcast
    protocol_handlers._execute_member_removal(peer_id, reason)

    plugin.log(
        f"cl-hive: Removed member {peer_id[:16]}... ({target_tier})"
        f"{' [FORCED]' if force and active_channel_states else ''}: {reason}"
    )

    return {
        "status": "removed",
        "peer_id": peer_id,
        "former_tier": target_tier,
        "reason": reason,
        "forced": bool(force and active_channel_states),
        "message": f"Member removed. They can rejoin with a new invite ticket."
    }


@plugin.method("hive-propose-ban")
def hive_propose_ban(plugin: Plugin, peer_id: str, reason: str = "no reason given"):
    """
    Propose banning a member from the hive.

    Requires quorum vote (51% of members) to execute.
    The proposal is valid for 7 days.

    Args:
        peer_id: Public key of the member to ban
        reason: Reason for the ban proposal (max 500 chars)

    Returns:
        Dict with proposal status.

    Permission: Member or Admin
    """
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not database or not our_pubkey :
        return {"error": "Hive not initialized"}

    # Validate reason length
    if len(reason) > 500:
        return {"error": "reason_too_long", "max_length": 500}

    # Check target exists and is a member
    target = database.get_member(peer_id)
    if not target:
        return {"error": "peer_not_found", "peer_id": peer_id}

    # Cannot ban yourself
    if peer_id == our_pubkey:
        return {"error": "cannot_ban_self"}

    # Check for existing pending proposal
    existing = database.get_ban_proposal_for_target(peer_id)
    if existing and existing.get("status") == "pending":
        return {
            "error": "proposal_exists",
            "proposal_id": existing["proposal_id"],
            "message": "A ban proposal already exists for this peer"
        }

    # Generate proposal ID
    proposal_id = secrets.token_hex(16)
    timestamp = int(time.time())

    # Sign the proposal
    canonical = f"hive:ban_proposal:{proposal_id}:{peer_id}:{timestamp}:{reason}"
    try:
        sig = plugin.rpc.signmessage(canonical)["zbase"]
    except Exception as e:
        return {"error": f"Failed to sign proposal: {e}"}

    # Store locally
    expires_at = timestamp + BAN_PROPOSAL_TTL_SECONDS
    database.create_ban_proposal(proposal_id, peer_id, our_pubkey,
                                 reason, timestamp, expires_at)

    # Add our vote (proposer auto-votes approve)
    vote_canonical = f"hive:ban_vote:{proposal_id}:approve:{timestamp}"
    try:
        vote_sig = plugin.rpc.signmessage(vote_canonical).get("zbase", "")
    except Exception as e:
        return {"error": f"Failed to sign proposal vote: {e}"}
    database.add_ban_vote(proposal_id, our_pubkey, "approve", timestamp, vote_sig)

    # Broadcast proposal
    proposal_payload = {
        "proposal_id": proposal_id,
        "target_peer_id": peer_id,
        "proposer_peer_id": our_pubkey,
        "reason": reason,
        "timestamp": timestamp,
        "signature": sig
    }
    protocol_handlers._reliable_broadcast(HiveMessageType.BAN_PROPOSAL, proposal_payload,
                        msg_id=proposal_id)

    # Also broadcast our vote
    vote_payload = {
        "proposal_id": proposal_id,
        "voter_peer_id": our_pubkey,
        "vote": "approve",
        "timestamp": timestamp,
        "signature": vote_sig
    }
    protocol_handlers._reliable_broadcast(HiveMessageType.BAN_VOTE, vote_payload)

    # Calculate quorum info
    all_members = database.get_all_members()
    eligible = [m for m in all_members
                if m.get("tier") in (MembershipTier.MEMBER.value,)
                and m["peer_id"] != peer_id]
    quorum_needed = int(len(eligible) * BAN_QUORUM_THRESHOLD) + 1

    plugin.log(f"cl-hive: Ban proposal created for {peer_id[:16]}...: {reason}")

    return {
        "status": "proposed",
        "proposal_id": proposal_id,
        "target_peer_id": peer_id,
        "reason": reason,
        "expires_at": expires_at,
        "votes_needed": quorum_needed,
        "votes_received": 1,
        "message": f"Ban proposal created. Need {quorum_needed} votes to execute."
    }


@plugin.method("hive-vote-ban")
def hive_vote_ban(plugin: Plugin, proposal_id: str, vote: str):
    """
    Vote on a pending ban proposal.

    Args:
        proposal_id: ID of the ban proposal
        vote: "approve" or "reject"

    Returns:
        Dict with vote status.

    Permission: Member or Admin
    """
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not database or not our_pubkey :
        return {"error": "Hive not initialized"}

    # Validate vote
    if vote not in ("approve", "reject"):
        return {"error": "invalid_vote", "valid_options": ["approve", "reject"]}

    # Get proposal
    proposal = database.get_ban_proposal(proposal_id)
    if not proposal:
        return {"error": "proposal_not_found", "proposal_id": proposal_id}

    if proposal.get("status") != "pending":
        return {
            "error": "proposal_not_pending",
            "status": proposal.get("status"),
            "message": f"Proposal is {proposal.get('status')}, cannot vote"
        }

    # Check if expired
    now = int(time.time())
    if now > proposal.get("expires_at", 0):
        database.update_ban_proposal_status(proposal_id, "expired")
        return {"error": "proposal_expired"}

    # Cannot vote on proposal targeting self
    if proposal["target_peer_id"] == our_pubkey:
        return {"error": "cannot_vote_on_own_ban"}

    # Check if already voted
    existing_vote = database.get_ban_vote(proposal_id, our_pubkey)
    if existing_vote:
        if existing_vote["vote"] == vote:
            return {"error": "already_voted", "vote": vote}
        # Allow changing vote

    # Sign vote
    timestamp = int(time.time())
    canonical = f"hive:ban_vote:{proposal_id}:{vote}:{timestamp}"
    try:
        sig = plugin.rpc.signmessage(canonical)["zbase"]
    except Exception as e:
        return {"error": f"Failed to sign vote: {e}"}

    # Store vote
    database.add_ban_vote(proposal_id, our_pubkey, vote, timestamp, sig)

    # Broadcast vote
    vote_payload = {
        "proposal_id": proposal_id,
        "voter_peer_id": our_pubkey,
        "vote": vote,
        "timestamp": timestamp,
        "signature": sig
    }
    vote_msg = serialize(HiveMessageType.BAN_VOTE, vote_payload)
    protocol_handlers._broadcast_to_members(vote_msg)

    # Check if quorum reached
    was_executed = protocol_handlers._check_ban_quorum(proposal_id, proposal, plugin)

    # Get current vote counts
    all_votes = database.get_ban_votes(proposal_id)
    all_members = database.get_all_members()
    eligible = [m for m in all_members
                if m.get("tier") in (MembershipTier.MEMBER.value,)
                and m["peer_id"] != proposal["target_peer_id"]]
    eligible_ids = set(m["peer_id"] for m in eligible)

    approve_count = sum(1 for v in all_votes if v["vote"] == "approve" and v["voter_peer_id"] in eligible_ids)
    reject_count = sum(1 for v in all_votes if v["vote"] == "reject" and v["voter_peer_id"] in eligible_ids)
    quorum_needed = int(len(eligible) * BAN_QUORUM_THRESHOLD) + 1

    result = {
        "status": "voted",
        "proposal_id": proposal_id,
        "vote": vote,
        "approve_count": approve_count,
        "reject_count": reject_count,
        "quorum_needed": quorum_needed,
    }

    if was_executed:
        result["status"] = "ban_executed"
        result["message"] = f"Ban executed! Target {proposal['target_peer_id'][:16]}... removed from hive."
    else:
        result["message"] = f"Vote recorded. {approve_count}/{quorum_needed} approvals."

    return result


@plugin.method("hive-pending-bans")
def hive_pending_bans(plugin: Plugin):
    """
    View pending ban proposals.

    Returns:
        Dict with pending ban proposals and their vote counts.

    Permission: Any member
    """
    return rpc_pending_bans(_get_hive_context())


@plugin.method("hive-contribution")
def hive_contribution(plugin: Plugin, peer_id: str = None):
    """
    View contribution stats for a peer or self.

    Args:
        peer_id: Optional peer to view (defaults to self)

    Returns:
        Dict with contribution statistics.
    """
    return rpc_contribution(_get_hive_context(), peer_id=peer_id)


# =============================================================================
# ROUTING POOL COMMANDS (Phase 0 - Collective Economics)
# =============================================================================

@plugin.method("hive-pool-status")
def hive_pool_status(plugin: Plugin, period: str = None):
    """
    Get current routing pool status and statistics.

    Args:
        period: Optional period to query (format: YYYY-Www, defaults to current week)

    Returns:
        Dict with pool status including revenue, contributions, and distributions.
    """
    return rpc_pool_status(_get_hive_context(), period=period)


@plugin.method("hive-pool-member-status")
def hive_pool_member_status(plugin: Plugin, peer_id: str = None):
    """
    Get routing pool status for a specific member.

    Args:
        peer_id: Member pubkey (defaults to self)

    Returns:
        Dict with member's pool status and history.
    """
    return rpc_pool_member_status(_get_hive_context(), peer_id=peer_id)


@plugin.method("hive-pool-snapshot")
def hive_pool_snapshot(plugin: Plugin, period: str = None):
    """
    Trigger a contribution snapshot for all hive members.

    Permission: Admin only

    Args:
        period: Optional period (format: YYYY-Www, defaults to current week)

    Returns:
        Dict with snapshot results.
    """
    return rpc_pool_snapshot(_get_hive_context(), period=period)


@plugin.method("hive-pool-distribution")
def hive_pool_distribution(plugin: Plugin, period: str = None):
    """
    Calculate distribution amounts for a period (dry run).

    Args:
        period: Optional period (format: YYYY-Www, defaults to current week)

    Returns:
        Dict with calculated distribution amounts.
    """
    return rpc_pool_distribution(_get_hive_context(), period=period)


@plugin.method("hive-pool-settle")
def hive_pool_settle(plugin: Plugin, period: str = None, dry_run: bool = True):
    """
    Settle a routing pool period and record distributions.

    Permission: Admin only

    Args:
        period: Period to settle (format: YYYY-Www, defaults to PREVIOUS week)
        dry_run: If True, calculate but don't record (default: True)

    Returns:
        Dict with settlement results.
    """
    return rpc_pool_settle(_get_hive_context(), period=period, dry_run=dry_run)


@plugin.method("hive-pool-record-revenue")
def hive_pool_record_revenue(plugin: Plugin, amount_sats: int,
                              channel_id: str = None, payment_hash: str = None):
    """
    Manually record routing revenue to the pool.

    Permission: Admin only

    Args:
        amount_sats: Revenue amount in satoshis
        channel_id: Optional channel ID
        payment_hash: Optional payment hash

    Returns:
        Dict with recording result.
    """
    return rpc_pool_record_revenue(
        _get_hive_context(),
        amount_sats=amount_sats,
        channel_id=channel_id,
        payment_hash=payment_hash
    )


# =============================================================================
# NETWORK METRICS COMMANDS
# =============================================================================

@plugin.method("hive-network-metrics")
def hive_network_metrics(plugin: Plugin, member_id: str = None):
    """
    Get network position metrics for hive members.

    Returns centrality, unique peers, bridge scores, hive centrality, and
    rebalance hub scores. These metrics are used for fair share calculations
    and routing optimization.

    Args:
        member_id: Specific member pubkey (omit for all members)

    Returns:
        Dict with network metrics for the specified member(s).
    """
    return rpc_network_metrics(_get_hive_context(), member_id=member_id)


@plugin.method("hive-rebalance-hubs")
def hive_rebalance_hubs(plugin: Plugin, top_n: int = 3, exclude_members: str = None):
    """
    Get the best zero-fee rebalance intermediaries in the hive.

    Nodes with high hive centrality make good rebalance hubs because they
    have channels to many other hive members. Routing rebalances through
    these nodes is free (0 ppm fees within hive).

    Args:
        top_n: Number of top hubs to return (default: 3)
        exclude_members: Comma-separated member IDs to exclude

    Returns:
        Dict with ranked list of best rebalance hubs.
    """
    exclude_list = exclude_members.split(",") if exclude_members else None
    return rpc_rebalance_hubs(
        _get_hive_context(),
        top_n=top_n,
        exclude_members=exclude_list
    )


@plugin.method("hive-rebalance-path")
def hive_rebalance_path(plugin: Plugin, source_member: str, dest_member: str,
                        max_hops: int = 2):
    """
    Find the optimal zero-fee path for internal hive rebalancing.

    Finds a path through the hive's internal network from source to destination.
    All channels between hive members have 0 ppm fees, so internal rebalancing
    through these paths is free.

    Args:
        source_member: Source member pubkey
        dest_member: Destination member pubkey
        max_hops: Maximum number of hops (default: 2)

    Returns:
        Dict with path information including intermediaries.
    """
    return rpc_rebalance_path(
        _get_hive_context(),
        source_member=source_member,
        dest_member=dest_member,
        max_hops=max_hops
    )


# =============================================================================
# FLEET HEALTH MONITORING COMMANDS
# =============================================================================

@plugin.method("hive-fleet-health")
def hive_fleet_health(plugin: Plugin):
    """
    Get overall fleet connectivity health metrics.

    Returns aggregated metrics showing how well-connected the fleet is
    internally, including health score (0-100) and letter grade.

    Returns:
        Dict with fleet health metrics including avg centrality,
        reachability, hub count, and health grade.
    """
    return rpc_fleet_health(_get_hive_context())


@plugin.method("hive-connectivity-alerts")
def hive_connectivity_alerts(plugin: Plugin):
    """
    Check for fleet connectivity issues that need attention.

    Returns alerts for:
    - Disconnected members (no hive channels)
    - Isolated members (low reachability)
    - Low hub availability
    - Low centrality members

    Returns:
        Dict with alerts sorted by severity (critical, warning, info).
    """
    return rpc_connectivity_alerts(_get_hive_context())


@plugin.method("hive-member-connectivity")
def hive_member_connectivity(plugin: Plugin, member_id: str):
    """
    Get detailed connectivity report for a specific member.

    Shows how well-connected the member is within the fleet,
    comparison to fleet average, and recommendations for improvement.

    Args:
        member_id: Member's public key

    Returns:
        Dict with connectivity details and recommended connections.
    """
    return rpc_member_connectivity(_get_hive_context(), member_id=member_id)


@plugin.method("hive-neophyte-rankings")
def hive_neophyte_rankings(plugin: Plugin):
    """
    Get all neophytes ranked by their promotion readiness.

    Returns neophytes sorted by a readiness score (0-100) based on:
    - Probation progress (40%)
    - Uptime (20%)
    - Contribution ratio (20%)
    - Hive centrality (20%) - higher centrality = stronger commitment

    Neophytes with high hive centrality (>=0.5) may be eligible for
    fast-track promotion after 30 days instead of the full 90-day period.

    Returns:
        Dict with ranked neophytes and their metrics.
    """
    return rpc_neophyte_rankings(_get_hive_context())


# =============================================================================
# SETTLEMENT RPC METHODS (BOLT12 Revenue Distribution)
# =============================================================================

@plugin.method("hive-settlement-register-offer")
def hive_settlement_register_offer(plugin: Plugin, peer_id: str, bolt12_offer: str):
    """
    Register a BOLT12 offer for receiving settlement payments.

    Each hive member must register their offer to participate in revenue distribution.
    If registering your own offer, it will be broadcast to other hive members.

    Args:
        peer_id: Member's node public key
        bolt12_offer: BOLT12 offer string (starts with lno1...)

    Returns:
        Dict with registration result.
    """
    if not settlement_mgr:
        return {"error": "Settlement manager not initialized"}

    result = settlement_mgr.register_offer(peer_id, bolt12_offer)

    # Broadcast if this is our own offer and registration succeeded
    if "error" not in result and handshake_mgr:
        if peer_id == handshake_mgr.get_our_pubkey():
            broadcast_count = protocol_handlers._broadcast_settlement_offer(peer_id, bolt12_offer)
            result["broadcast_count"] = broadcast_count

    return result


@plugin.method("hive-settlement-generate-offer")
def hive_settlement_generate_offer(plugin: Plugin):
    """
    Auto-generate and register a BOLT12 offer for this node.

    This creates a new BOLT12 offer for receiving settlement payments
    and registers it automatically. The offer is broadcast to all hive members.

    Returns:
        Dict with offer generation result.
    """
    if not settlement_mgr:
        return {"error": "Settlement manager not initialized"}
    if not handshake_mgr:
        return {"error": "Handshake manager not initialized"}

    our_pubkey = handshake_mgr.get_our_pubkey()
    result = settlement_mgr.generate_and_register_offer(our_pubkey)

    # Broadcast to hive members if generation succeeded
    if "error" not in result:
        # Get the full offer from the database
        bolt12_offer = settlement_mgr.get_offer(our_pubkey)
        if bolt12_offer:
            broadcast_count = protocol_handlers._broadcast_settlement_offer(our_pubkey, bolt12_offer)
            result["broadcast_count"] = broadcast_count

    return result


@plugin.method("hive-settlement-list-offers")
def hive_settlement_list_offers(plugin: Plugin):
    """
    List all registered BOLT12 offers for settlement.

    Returns:
        Dict with list of registered offers.
    """
    if not settlement_mgr:
        return {"error": "Settlement manager not initialized"}
    return settlement_mgr.list_offers()


@plugin.method("hive-settlement-calculate")
def hive_settlement_calculate(plugin: Plugin):
    """
    Calculate fair shares for the current period without executing.

    Shows what each member would receive/pay based on:
    - 30% capacity weight
    - 60% routing activity weight
    - 10% uptime weight

    Returns:
        Dict with calculated fair shares.
    """
    from modules.settlement import MemberContribution

    if not settlement_mgr:
        return {"error": "Settlement manager not initialized"}
    if not routing_pool:
        return {"error": "Routing pool not initialized"}
    if not database:
        return {"error": "Database not initialized"}

    # Get our pubkey upfront to avoid scoping issues
    node_pubkey = our_pubkey
    if not node_pubkey:
        try:
            info = plugin.rpc.getinfo()
            node_pubkey = info.get("id")
        except Exception:
            return {"error": "Could not determine our node pubkey"}

    # CRITICAL: Validate cl-revenue-ops is available for fee data
    warnings = []
    if not bridge or bridge.status != BridgeStatus.ENABLED:
        warnings.append(
            "cl-revenue-ops not available - fees_earned will be 0. "
            "Settlement requires cl-revenue-ops for accurate fee distribution."
        )

    # Canonical settlement period and fee-report-driven contribution view.
    current_period = settlement_mgr.get_period_string()
    pool_status = routing_pool.get_pool_status(period=current_period)
    gathered = settlement_mgr.gather_contributions_from_gossip(state_manager, current_period)

    member_contributions = []
    for contrib in gathered:
        peer_id = str(contrib.get("peer_id", ""))
        if not peer_id:
            continue

        uptime = int(contrib.get("uptime", 100) or 100)
        offer = settlement_mgr.get_offer(peer_id)
        member_contributions.append(MemberContribution(
            peer_id=peer_id,
            capacity_sats=int(contrib.get("capacity", 0) or 0),
            forwards_sats=int(contrib.get("forward_count", 0) or 0),
            fees_earned_sats=int(contrib.get("fees_earned", 0) or 0),
            rebalance_costs_sats=int(contrib.get("rebalance_costs", 0) or 0),
            uptime_pct=max(0.0, min(1.0, float(uptime) / 100.0)),
            bolt12_offer=offer
        ))

    if not member_contributions:
        warnings.append(
            "No settlement contributions found for current period. "
            "Fee reports may not have been received yet."
        )

    # Validate state data quality
    zero_capacity = sum(1 for c in member_contributions if c.capacity_sats == 0)
    zero_uptime = sum(1 for c in member_contributions if c.uptime_pct == 0)
    zero_fees = sum(1 for c in member_contributions if c.fees_earned_sats == 0)

    if zero_capacity > 0:
        warnings.append(
            f"{zero_capacity} member(s) have 0 capacity. "
            "Ensure gossip is running and state_manager has current data."
        )
    if zero_uptime > 0:
        warnings.append(
            f"{zero_uptime} member(s) have 0% uptime. "
            "Check state_manager or run hive-pool-snapshot to update."
        )
    if zero_fees == len(member_contributions) and len(member_contributions) > 0:
        warnings.append(
            "All members have 0 fees_earned. cl-revenue-ops is required for fee data."
        )

    # Calculate fair shares
    results = settlement_mgr.calculate_fair_shares(member_contributions)
    total_fees = sum(r.fees_earned for r in results)

    # Generate payments that would be required
    payments = settlement_mgr.generate_payments(results, total_fees=total_fees)

    # Format for JSON response
    response = {
        "period": pool_status.get("period", "unknown"),
        "total_members": len(results),
        "total_fees_sats": total_fees,
        "fair_shares": [
            {
                "peer_id": r.peer_id[:16] + "...",
                "peer_id_full": r.peer_id,
                "fees_earned": r.fees_earned,
                "fair_share": r.fair_share,
                "balance": r.balance,
                "has_offer": r.bolt12_offer is not None,
                "status": "pays" if r.balance < 0 else ("receives" if r.balance > 0 else "even")
            }
            for r in results
        ],
        "payments_required": [
            {
                "from_peer": p.from_peer[:16] + "...",
                "from_peer_full": p.from_peer,
                "to_peer": p.to_peer[:16] + "...",
                "to_peer_full": p.to_peer,
                "amount_sats": p.amount_sats,
                "bolt12_offer": p.bolt12_offer[:40] + "..." if p.bolt12_offer else None
            }
            for p in payments
        ]
    }

    if warnings:
        response["warnings"] = warnings

    return response


@plugin.method("hive-settlement-execute")
def hive_settlement_execute(plugin: Plugin, dry_run: bool = True):
    """
    Execute settlement for the current period.

    Calculates fair shares and generates BOLT12 payments from members
    with surplus to members with deficit.

    Args:
        dry_run: If True, calculate but don't execute payments (default: True)

    Returns:
        Dict with settlement execution result.
    """
    from modules.settlement import MemberContribution, SettlementResult

    if not settlement_mgr:
        return {"error": "Settlement manager not initialized"}
    if not routing_pool:
        return {"error": "Routing pool not initialized"}
    if not database:
        return {"error": "Database not initialized"}

    # Get our pubkey upfront to avoid scoping issues
    node_pubkey = our_pubkey
    if not node_pubkey:
        try:
            info = plugin.rpc.getinfo()
            node_pubkey = info.get("id")
        except Exception:
            return {"error": "Could not determine our node pubkey"}

    # CRITICAL: Validate cl-revenue-ops is available for fee data
    if not bridge or bridge.status != BridgeStatus.ENABLED:
        return {
            "error": "cl-revenue-ops is required for settlement",
            "detail": "Settlement uses fees_earned data from cl-revenue-ops. "
                      "Ensure cl-revenue-ops plugin is running and bridge is ENABLED."
        }

    period = settlement_mgr.get_period_string()
    gathered = settlement_mgr.gather_contributions_from_gossip(state_manager, period)

    member_contributions = []
    for contrib in gathered:
        peer_id = str(contrib.get("peer_id", ""))
        if not peer_id:
            continue
        uptime = int(contrib.get("uptime", 100) or 100)
        offer = settlement_mgr.get_offer(peer_id)
        member_contributions.append(MemberContribution(
            peer_id=peer_id,
            capacity_sats=int(contrib.get("capacity", 0) or 0),
            forwards_sats=int(contrib.get("forward_count", 0) or 0),
            fees_earned_sats=int(contrib.get("fees_earned", 0) or 0),
            rebalance_costs_sats=int(contrib.get("rebalance_costs", 0) or 0),
            uptime_pct=max(0.0, min(1.0, float(uptime) / 100.0)),
            bolt12_offer=offer
        ))

    if not member_contributions:
        return {"error": "No member contributions found"}

    # Calculate fair shares
    results = settlement_mgr.calculate_fair_shares(member_contributions)
    total_fees = sum(r.fees_earned for r in results)

    # Generate payments from results
    payments = settlement_mgr.generate_payments(results, total_fees=total_fees)

    # Build response
    response = {
        "period": period,
        "total_members": len(results),
        "total_fees_sats": total_fees,
        "fair_shares": [
            {
                "peer_id": r.peer_id[:16] + "...",
                "peer_id_full": r.peer_id,
                "fees_earned": r.fees_earned,
                "fair_share": r.fair_share,
                "balance": r.balance,
                "has_offer": r.bolt12_offer is not None,
                "status": "pays" if r.balance < 0 else ("receives" if r.balance > 0 else "even")
            }
            for r in results
        ],
        "payments_required": [
            {
                "from_peer": p.from_peer[:16] + "...",
                "from_peer_full": p.from_peer,
                "to_peer": p.to_peer[:16] + "...",
                "to_peer_full": p.to_peer,
                "amount_sats": p.amount_sats,
                "bolt12_offer": p.bolt12_offer[:40] + "..." if p.bolt12_offer else None
            }
            for p in payments
        ]
    }

    # For dry run, return calculation without executing
    if dry_run:
        response["execution_status"] = "dry_run"
        response["message"] = f"Dry run - {len(payments)} payments would be executed"
        return response

    # CRITICAL: Check if previous week was already settled to prevent duplicates
    # Use start_time to determine which period was settled (Issue #44)
    from datetime import datetime, timedelta
    now = datetime.now()
    prev_date = now - timedelta(days=7)
    previous_week = f"{prev_date.year}-{prev_date.isocalendar()[1]:02d}"

    existing_periods = settlement_mgr.get_settlement_history(limit=10)
    for p in existing_periods:
        if p.get("status") == "completed" and p.get("start_time"):
            start_dt = datetime.fromtimestamp(p["start_time"])
            settled_week = f"{start_dt.year}-{start_dt.isocalendar()[1]:02d}"
            if settled_week == previous_week:
                return {
                    "error": "duplicate_settlement",
                    "message": f"Week {previous_week} was already settled (period_id={p['period_id']})",
                    "existing_period_id": p["period_id"],
                    "settled_at": p.get("settled_at")
                }

    # Check if we have any payments to execute
    if not payments:
        response["execution_status"] = "no_payments"
        response["message"] = "No payments required (all members at fair share or below minimum threshold)"
        return response

    # Execute payments - we can only pay from our own node
    executed = []
    skipped = []
    errors = []

    for payment in payments:
        # We can only execute payments FROM our own node
        if payment.from_peer != node_pubkey:
            skipped.append({
                "from_peer": payment.from_peer[:16] + "...",
                "to_peer": payment.to_peer[:16] + "...",
                "amount_sats": payment.amount_sats,
                "reason": "not_our_payment"
            })
            continue

        if not payment.bolt12_offer:
            errors.append({
                "to_peer": payment.to_peer[:16] + "...",
                "amount_sats": payment.amount_sats,
                "error": "recipient has no BOLT12 offer registered"
            })
            continue

        try:
            # Fetch invoice from BOLT12 offer
            invoice_result = plugin.rpc.fetchinvoice(
                offer=payment.bolt12_offer,
                amount_msat=f"{payment.amount_sats * 1000}msat"
            )

            if "invoice" not in invoice_result:
                errors.append({
                    "to_peer": payment.to_peer[:16] + "...",
                    "amount_sats": payment.amount_sats,
                    "error": "Failed to fetch invoice from offer"
                })
                continue

            bolt12_invoice = invoice_result["invoice"]

            # Pay the invoice
            # NOTE: Allow a tiny fee budget. Without this, CLN xpay may report max==amount-1msat
            # even when channels are 0ppm, due to rounding/overhead in the pay layers.
            # 1 sat (1000 msat) is ample for these small settlement payments and prevents
            # deterministic failures like: "xpay says max is 293999msat" for a 294000msat pay.
            pay_result = plugin.rpc.pay(
                bolt12_invoice,
                maxfee="1sat",
                # CLN constraint: cannot specify exemptfee when maxfee is set.
                retry_for=30,
            )

            if pay_result.get("status") == "complete":
                executed.append({
                    "to_peer": payment.to_peer[:16] + "...",
                    "amount_sats": payment.amount_sats,
                    "payment_hash": pay_result.get("payment_hash"),
                    "status": "completed"
                })
            else:
                errors.append({
                    "to_peer": payment.to_peer[:16] + "...",
                    "amount_sats": payment.amount_sats,
                    "error": pay_result.get("message", "Payment failed")
                })

        except Exception as e:
            errors.append({
                "to_peer": payment.to_peer[:16] + "...",
                "amount_sats": payment.amount_sats,
                "error": str(e)
            })

    # Create settlement period record
    period_id = settlement_mgr.create_settlement_period()
    settlement_mgr.record_contributions(period_id, results, member_contributions)
    settlement_mgr.record_payments(period_id, payments)

    # Update payment statuses in database
    for exec_payment in executed:
        # Find original payment to get full peer IDs
        for p in payments:
            if p.to_peer[:16] == exec_payment["to_peer"][:16]:
                settlement_mgr.update_payment_status(
                    period_id=period_id,
                    from_peer=p.from_peer,
                    to_peer=p.to_peer,
                    status="completed",
                    payment_hash=exec_payment.get("payment_hash")
                )
                break

    for err_payment in errors:
        for p in payments:
            if p.to_peer[:16] == err_payment["to_peer"][:16]:
                settlement_mgr.update_payment_status(
                    period_id=period_id,
                    from_peer=p.from_peer,
                    to_peer=p.to_peer,
                    status="error",
                    error=err_payment.get("error")
                )
                break

    # Complete period if all our payments are done
    if not errors:
        settlement_mgr.complete_settlement_period(period_id)

    response["execution_status"] = "executed"
    response["period_id"] = period_id
    response["payments_executed"] = executed
    response["payments_skipped"] = skipped
    response["payments_errors"] = errors
    response["message"] = (
        f"Settlement executed: {len(executed)} payments completed, "
        f"{len(skipped)} skipped (other nodes), {len(errors)} errors"
    )

    return response


@plugin.method("hive-settlement-history")
def hive_settlement_history(plugin: Plugin, limit: int = 10):
    """
    Get settlement history showing past periods and distributions.

    Args:
        limit: Number of periods to return (default: 10)

    Returns:
        Dict with settlement history.
    """
    if not settlement_mgr:
        return {"error": "Settlement manager not initialized"}
    return {"settlement_periods": settlement_mgr.get_settlement_history(limit=limit)}


@plugin.method("hive-settlement-period-details")
def hive_settlement_period_details(plugin: Plugin, period_id: int):
    """
    Get detailed information about a specific settlement period.

    Args:
        period_id: Settlement period ID

    Returns:
        Dict with period details including contributions, fair shares, and payments.
    """
    if not settlement_mgr:
        return {"error": "Settlement manager not initialized"}
    return settlement_mgr.get_period_details(period_id)


# =============================================================================
# DISTRIBUTED SETTLEMENT RPC METHODS (Phase 12)
# =============================================================================

@plugin.method("hive-distributed-settlement-status")
def hive_distributed_settlement_status(plugin: Plugin):
    """
    Get distributed settlement status.

    Shows pending proposals, ready settlements, and recent completions
    for the decentralized settlement system.

    Returns:
        Dict with distributed settlement status.
    """
    if not settlement_mgr:
        return {"error": "Settlement manager not initialized"}
    return settlement_mgr.get_distributed_settlement_status()


@plugin.method("hive-distributed-settlement-proposals")
def hive_distributed_settlement_proposals(plugin: Plugin, status: str = None):
    """
    Get settlement proposals with voting status.

    Args:
        status: Filter by status (pending, ready, completed, expired). Default: all.

    Returns:
        Dict with proposals and their voting progress.
    """
    if not database:
        return {"error": "Database not initialized"}

    if status == 'pending':
        proposals = database.get_pending_settlement_proposals()
    elif status == 'ready':
        proposals = database.get_ready_settlement_proposals()
    else:
        # Get all proposals
        proposals = (
            database.get_pending_settlement_proposals() +
            database.get_ready_settlement_proposals()
        )

    # Enrich with vote counts
    for prop in proposals:
        proposal_id = prop.get('proposal_id')
        prop['vote_count'] = database.count_settlement_ready_votes(proposal_id)
        votes = database.get_settlement_ready_votes(proposal_id)
        prop['voters'] = [v.get('voter_peer_id')[:16] + '...' for v in votes]
        if prop.get('last_broadcast_at') is None and prop.get('proposed_at') is not None:
            prop['effective_last_broadcast_at'] = prop.get('proposed_at')
            prop['last_broadcast_at_inferred_from_proposed_at'] = True
        else:
            prop['effective_last_broadcast_at'] = prop.get('last_broadcast_at')
            prop['last_broadcast_at_inferred_from_proposed_at'] = False

    return {
        "proposals": proposals,
        "total": len(proposals)
    }


@plugin.method("hive-distributed-settlement-participation")
def hive_distributed_settlement_participation(plugin: Plugin, periods: int = 10):
    """
    Get settlement participation rates for all members.

    Identifies nodes that skip votes or fail to execute payments,
    which may indicate gaming behavior to avoid paying out.

    Args:
        periods: Number of recent periods to analyze (default: 10)

    Returns:
        Dict with participation rates per member.
    """
    if not database:
        return {"error": "Database not initialized"}

    # Get recent settled periods
    settled = database.get_settled_periods(limit=periods)
    period_count = len(settled)

    if period_count == 0:
        return {
            "members": [],
            "periods_analyzed": 0,
            "note": "No settlement history available"
        }

    # Get all members
    all_members = database.get_all_members()

    member_stats = []
    for member in all_members:
        peer_id = member['peer_id']

        # Count how many times they voted
        vote_count = 0
        exec_count = 0
        total_owed = 0

        for period in settled:
            proposal_id = period.get('proposal_id')

            # Check if they voted
            if database.has_voted_settlement(proposal_id, peer_id):
                vote_count += 1

            # Check if they executed
            if database.has_executed_settlement(proposal_id, peer_id):
                exec_count += 1

                # Get their execution to see amount
                executions = database.get_settlement_executions(proposal_id)
                for ex in executions:
                    if ex.get('executor_peer_id') == peer_id:
                        amount = ex.get('amount_paid_sats', 0)
                        if amount > 0:
                            total_owed -= amount  # They paid

        vote_rate = round((vote_count / period_count) * 100, 1) if period_count > 0 else 0
        exec_rate = round((exec_count / period_count) * 100, 1) if period_count > 0 else 0

        member_stats.append({
            "peer_id": peer_id,
            "tier": member.get('tier', 'unknown'),
            "periods_analyzed": period_count,
            "votes_cast": vote_count,
            "vote_rate": vote_rate,
            "executions": exec_count,
            "execution_rate": exec_rate,
            "total_paid": abs(total_owed) if total_owed < 0 else 0,
            "participation_score": round((vote_rate + exec_rate) / 2, 1)
        })

    # Sort by participation score (lowest first to highlight suspects)
    member_stats.sort(key=lambda x: x.get('participation_score', 100))

    return {
        "members": member_stats,
        "periods_analyzed": period_count,
        "total_members": len(member_stats)
    }


@plugin.method("hive-backfill-fees")
def hive_backfill_fees(plugin: Plugin, period: str = None, source: str = "revenue-ops"):
    """
    Backfill fee reports from historical data.

    This populates the fee_reports table with historical fee data from
    cl-revenue-ops or local tracking, enabling accurate settlement
    calculations even after node restarts.

    Args:
        period: Optional specific period to backfill (YYYY-Www format).
                If not provided, backfills current period.
        source: Data source - "revenue-ops" (default) or "local"

    Returns:
        Dict with backfill status and amounts
    """
    if not database or not our_pubkey:
        return {"error": "Plugin not initialized"}

    from modules.settlement import SettlementManager
    import datetime

    # Determine period
    if period is None:
        period = SettlementManager.get_period_string()

    results = {
        "period": period,
        "source": source,
        "backfilled": []
    }

    if source == "revenue-ops":
        # Try to get fee data from cl-revenue-ops
        try:
            # Get dashboard data which includes fee totals
            dashboard = plugin.rpc.call("revenue-dashboard", {
                "window_days": 7
            })

            # Fee data is in the 'period' sub-object
            period_data = dashboard.get("period", {})
            fees_earned = period_data.get("gross_revenue_sats", 0)
            forwards = period_data.get("total_forwards", 0)
            # Include rebalance costs for net profit settlement (Issue #42)
            rebalance_costs = period_data.get("rebalance_cost_sats", 0)

            # Calculate period timestamps using ISO week (proper handling)
            year, week = map(int, period.split('-'))
            # Use fromisocalendar for correct ISO week handling
            week_start = datetime.date.fromisocalendar(year, week, 1)  # Monday
            dt = datetime.datetime.combine(week_start, datetime.time.min, tzinfo=datetime.timezone.utc)
            period_start = int(dt.timestamp())
            period_end = int(datetime.datetime.now(tz=datetime.timezone.utc).timestamp())
            # Ensure period_end >= period_start (in case of edge cases)
            period_end = max(period_end, period_start)

            # Save our fee report to database
            database.save_fee_report(
                peer_id=our_pubkey,
                period=period,
                fees_earned_sats=fees_earned,
                forward_count=forwards,
                period_start=period_start,
                period_end=period_end,
                rebalance_costs_sats=rebalance_costs
            )

            # Also update local_fee_tracking so gossip loop broadcasts correct fees
            now = int(time.time())
            database._get_connection().execute("""
                INSERT INTO local_fee_tracking (id, earned_sats, forward_count,
                                                period_start_ts, last_broadcast_ts,
                                                last_broadcast_amount, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    earned_sats = excluded.earned_sats,
                    forward_count = excluded.forward_count,
                    period_start_ts = excluded.period_start_ts,
                    last_broadcast_ts = excluded.last_broadcast_ts,
                    last_broadcast_amount = excluded.last_broadcast_amount,
                    updated_at = excluded.updated_at
            """, (fees_earned, forwards, period_start, now, fees_earned, now))

            # Trigger immediate fee report broadcast
            protocol_handlers._broadcast_fee_report(fees_earned, forwards, period_start, period_end,
                                  rebalance_costs)

            results["backfilled"].append({
                "peer_id": our_pubkey[:16] + "...",
                "fees_earned_sats": fees_earned,
                "rebalance_costs_sats": rebalance_costs,
                "forward_count": forwards,
                "broadcast": True
            })

            plugin.log(f"Backfilled fees for {period}: {fees_earned} sats, costs={rebalance_costs} (broadcast triggered)", level='info')

        except Exception as e:
            results["error"] = f"Failed to get data from cl-revenue-ops: {e}"

    elif source == "local":
        # Use local fee tracking state
        try:
            row = database._get_connection().execute(
                "SELECT * FROM local_fee_tracking WHERE id = 1"
            ).fetchone()

            if row:
                fees_earned = row["earned_sats"] or 0
                forwards = row["forward_count"] or 0
                period_start = row["period_start_ts"] or int(time.time())
                period_end = int(time.time())

                database.save_fee_report(
                    peer_id=our_pubkey,
                    period=period,
                    fees_earned_sats=fees_earned,
                    forward_count=forwards,
                    period_start=period_start,
                    period_end=period_end
                )

                results["backfilled"].append({
                    "peer_id": our_pubkey[:16] + "...",
                    "fees_earned_sats": fees_earned,
                    "forward_count": forwards
                })

                plugin.log(f"Backfilled local fees for {period}: {fees_earned} sats", level='info')
            else:
                results["error"] = "No local fee tracking data found"

        except Exception as e:
            results["error"] = f"Failed to read local fee data: {e}"

    else:
        results["error"] = f"Unknown source: {source}. Use 'revenue-ops' or 'local'"

    return results


@plugin.method("hive-fee-reports")
def hive_fee_reports(plugin: Plugin, period: str = None):
    """
    Get all fee reports stored in the database.

    Args:
        period: Optional specific period (YYYY-Www format). If not provided,
                returns the latest report for each peer.

    Returns:
        Dict with fee reports and totals
    """
    if not database:
        return {"error": "Plugin not initialized"}

    from modules.settlement import SettlementManager

    # Handle "latest" as a special case to get most recent per peer
    if period and period.lower() != "latest":
        reports = database.get_fee_reports_for_period(period)
    else:
        reports = database.get_latest_fee_reports()

    total_fees = sum(r.get('fees_earned_sats', 0) for r in reports)
    total_forwards = sum(r.get('forward_count', 0) for r in reports)

    return {
        "period": period or "latest",
        "reports": [
            {
                "peer_id": r.get('peer_id', '')[:16] + "...",
                "fees_earned_sats": r.get('fees_earned_sats', 0),
                "forward_count": r.get('forward_count', 0),
                "period": r.get('period', ''),
                "received_at": r.get('received_at', 0)
            }
            for r in reports
        ],
        "total_fees_sats": total_fees,
        "total_forwards": total_forwards,
        "report_count": len(reports)
    }


# =============================================================================
# YIELD METRICS RPC METHODS (Phase 1 - Metrics & Measurement)
# =============================================================================

@plugin.method("hive-yield-metrics")
def hive_yield_metrics(plugin: Plugin, channel_id: str = None, period_days: int = 30):
    """
    Get yield metrics for channels.

    Args:
        channel_id: Optional specific channel ID (defaults to all channels)
        period_days: Analysis period in days (default: 30)

    Returns:
        Dict with channel yield metrics including ROI, capital efficiency, turn rate.
    """
    return rpc_yield_metrics(_get_hive_context(), channel_id=channel_id, period_days=period_days)


@plugin.method("hive-yield-summary")
def hive_yield_summary(plugin: Plugin, period_days: int = 30):
    """
    Get fleet-wide yield summary.

    Args:
        period_days: Analysis period in days (default: 30)

    Returns:
        Dict with fleet yield summary including total revenue, avg ROI, efficiency.
    """
    return rpc_yield_summary(_get_hive_context(), period_days=period_days)


@plugin.method("hive-velocity-prediction")
def hive_velocity_prediction(plugin: Plugin, channel_id: str, hours: int = 24):
    """
    Predict channel state based on flow velocity.

    Args:
        channel_id: Channel ID to predict
        hours: Prediction horizon in hours (default: 24)

    Returns:
        Dict with velocity prediction including depletion/saturation risk.
    """
    return rpc_velocity_prediction(_get_hive_context(), channel_id=channel_id, hours=hours)


@plugin.method("hive-critical-velocity")
def hive_critical_velocity(plugin: Plugin, threshold_hours: int = 24):
    """
    Get channels with critical velocity (depleting/filling rapidly).

    Args:
        threshold_hours: Alert threshold in hours (default: 24)

    Returns:
        Dict with channels predicted to deplete or saturate within threshold.
    """
    return rpc_critical_velocity_channels(_get_hive_context(), threshold_hours=threshold_hours)


@plugin.method("hive-internal-competition")
def hive_internal_competition(plugin: Plugin):
    """
    Detect internal competition between hive members.

    Returns:
        Dict with competition instances where multiple hive members
        compete for the same source/destination routes.
    """
    return rpc_internal_competition(_get_hive_context())


@plugin.method("hive-report-kalman-velocity")
def hive_report_kalman_velocity(
    plugin: Plugin,
    channel_id: str = "",
    peer_id: str = "",
    velocity_pct_per_hour: float = 0.0,
    uncertainty: float = 0.0,
    flow_ratio: float = 0.0,
    confidence: float = 0.0,
    is_regime_change: bool = False
):
    """
    Report Kalman-estimated velocity from cl-revenue-ops.

    Fleet members share their Kalman filter velocity estimates for
    coordinated anticipatory liquidity predictions.

    Args:
        channel_id: Channel SCID
        peer_id: Peer pubkey
        velocity_pct_per_hour: Kalman velocity estimate (% change per hour)
        uncertainty: Standard deviation of velocity estimate
        flow_ratio: Current flow ratio estimate (-1 to 1)
        confidence: Observation confidence (0.0-1.0)
        is_regime_change: True if regime change detected

    Returns:
        Dict with status and acknowledgement
    """
    ctx = _get_hive_context()
    if not ctx.anticipatory_manager:
        return {"error": "Anticipatory liquidity manager not initialized"}

    try:
        # Get reporter ID from our own node
        reporter_id = ctx.our_id or ""

        success = ctx.anticipatory_manager.receive_kalman_velocity(
            reporter_id=reporter_id,
            channel_id=channel_id,
            peer_id=peer_id,
            velocity_pct_per_hour=velocity_pct_per_hour,
            uncertainty=uncertainty,
            flow_ratio=flow_ratio,
            confidence=confidence,
            is_regime_change=is_regime_change
        )

        return {
            "status": "ok" if success else "failed",
            "channel_id": channel_id,
            "velocity_pct_per_hour": velocity_pct_per_hour,
            "acknowledged": success
        }
    except Exception as e:
        return {"error": f"Failed to receive Kalman velocity: {e}"}


@plugin.method("hive-query-kalman-velocity")
def hive_query_kalman_velocity(plugin: Plugin, channel_id: str):
    """
    Query aggregated Kalman velocity for a channel.

    Returns consensus velocity from all fleet members who have
    reported Kalman estimates for this channel.

    Args:
        channel_id: Channel SCID to query

    Returns:
        Dict with consensus Kalman velocity data
    """
    ctx = _get_hive_context()
    if not ctx.anticipatory_manager:
        return {"error": "Anticipatory liquidity manager not initialized"}

    try:
        result = ctx.anticipatory_manager.query_kalman_velocity(channel_id)
        if not result:
            return {
                "status": "no_data",
                "channel_id": channel_id,
                "message": "No Kalman velocity data available for this channel"
            }
        return result
    except Exception as e:
        return {"error": f"Failed to query Kalman velocity: {e}"}


@plugin.method("hive-detect-patterns")
def hive_detect_patterns(plugin: Plugin, channel_id: str):
    """
    Detect Kalman-enhanced intra-day flow patterns for a channel.

    Analyzes historical flow data to find recurring patterns within each day
    (morning surge, lunch lull, evening peak, overnight recovery), using
    Kalman velocity estimates for improved confidence.

    Args:
        channel_id: Channel SCID to analyze

    Returns:
        Dict with detected intra-day patterns and statistics
    """
    ctx = _get_hive_context()
    if not ctx.anticipatory_manager:
        return {"error": "Anticipatory liquidity manager not initialized"}

    try:
        patterns = ctx.anticipatory_manager.detect_intraday_patterns(channel_id)
        return {
            "status": "ok",
            "channel_id": channel_id,
            "pattern_count": len(patterns),
            "actionable_count": sum(1 for p in patterns if p.is_actionable),
            "patterns": [p.to_dict() for p in patterns]
        }
    except Exception as e:
        return {"error": f"Failed to detect patterns: {e}"}


@plugin.method("hive-predict-liquidity")
def hive_predict_liquidity_intraday(
    plugin: Plugin,
    channel_id: str,
    current_local_pct: float = 0.5,
    hours_ahead: int = 12
):
    """
    Get intra-day liquidity forecast for a channel.

    Predicts what will happen in the next few hours based on detected
    patterns and current Kalman velocity, with recommended actions.

    Args:
        channel_id: Channel SCID
        current_local_pct: Current local balance percentage (0.0-1.0)
        hours_ahead: Hours to predict ahead (default: 12)

    Returns:
        Dict with forecast and recommended actions
    """
    ctx = _get_hive_context()
    if not ctx.anticipatory_manager:
        return {"error": "Anticipatory liquidity manager not initialized"}

    try:
        current_local_pct = float(current_local_pct)
        hours_ahead = int(hours_ahead)
        forecast = ctx.anticipatory_manager.get_intraday_forecast(
            channel_id, current_local_pct
        )
        if not forecast:
            return {
                "status": "no_forecast",
                "channel_id": channel_id,
                "message": "Insufficient data for forecast"
            }
        return {
            "status": "ok",
            **forecast.to_dict()
        }
    except Exception as e:
        return {"error": f"Failed to get forecast: {e}"}


@plugin.method("hive-anticipatory-predictions")
def hive_anticipatory_predictions(
    plugin: Plugin,
    channel_id: str = None,
    hours_ahead: int = 12,
    min_risk: float = 0.3
):
    """
    Get intra-day pattern summary for one or all channels.

    Shows detected patterns, forecasts, and urgent actions needed.

    Args:
        channel_id: Optional specific channel, None for all
        hours_ahead: Prediction horizon in hours (default: 12)
        min_risk: Minimum risk threshold to include (default: 0.3)

    Returns:
        Dict with pattern summary and forecasts
    """
    ctx = _get_hive_context()
    if not ctx.anticipatory_manager:
        return {"error": "Anticipatory liquidity manager not initialized"}

    try:
        # Note: hours_ahead and min_risk are accepted for API compatibility
        # but get_intraday_summary uses its own defaults internally
        summary = ctx.anticipatory_manager.get_intraday_summary(channel_id)
        return {
            "status": "ok",
            **summary
        }
    except Exception as e:
        return {"error": f"Failed to get predictions: {e}"}


# =============================================================================
# PHASE 2 FEE COORDINATION RPC METHODS
# =============================================================================

@plugin.method("hive-coord-fee-recommendation")
def hive_coord_fee_recommendation(
    plugin: Plugin,
    channel_id: str,
    current_fee: int = 500,
    local_balance_pct: float = 0.5,
    source: str = None,
    destination: str = None
):
    """
    Get coordinated fee recommendation for a channel (Phase 2 Fee Coordination).

    Uses corridor ownership, pheromone levels, stigmergic markers, and defense
    signals to recommend optimal fees while avoiding internal fleet competition.

    Args:
        channel_id: Channel ID to get recommendation for
        current_fee: Current fee in ppm (default: 500)
        local_balance_pct: Current local balance percentage (default: 0.5)
        source: Source peer hint for corridor lookup
        destination: Destination peer hint for corridor lookup

    Returns:
        Dict with fee recommendation, reasoning, and coordination factors.
    """
    return rpc_fee_recommendation(
        _get_hive_context(),
        channel_id=channel_id,
        current_fee=current_fee,
        local_balance_pct=local_balance_pct,
        source=source,
        destination=destination
    )


@plugin.method("hive-corridor-assignments")
def hive_corridor_assignments(plugin: Plugin, force_refresh: bool = False):
    """
    Get flow corridor assignments for the fleet.

    Shows which member is primary for each (source, destination) pair.

    Args:
        force_refresh: Force refresh of cached assignments

    Returns:
        Dict with corridor assignments and statistics.
    """
    return rpc_corridor_assignments(_get_hive_context(), force_refresh=force_refresh)


@plugin.method("hive-stigmergic-markers")
def hive_stigmergic_markers(plugin: Plugin, source: str = None, destination: str = None):
    """
    Get stigmergic route markers from the fleet.

    Shows fee signals left by members after routing attempts.

    Args:
        source: Filter by source peer
        destination: Filter by destination peer

    Returns:
        Dict with route markers and analysis.
    """
    return rpc_stigmergic_markers(_get_hive_context(), source=source, destination=destination)


@plugin.method("hive-deposit-marker")
def hive_deposit_marker(
    plugin: Plugin,
    source: str,
    destination: str,
    fee_ppm: int,
    success: bool,
    volume_sats: int = 0,
    channel_id: str = None,
    peer_id: str = None,
    amount_sats: int = 0
):
    """
    Deposit a stigmergic route marker.

    Args:
        source: Source peer ID
        destination: Destination peer ID
        fee_ppm: Fee charged in ppm
        success: Whether routing succeeded
        volume_sats: Volume routed in sats
        channel_id: Optional channel ID (for compatibility)
        peer_id: Optional peer ID (for compatibility)
        amount_sats: Optional amount (alias for volume_sats)

    Returns:
        Dict with deposited marker info.
    """
    # Use amount_sats as fallback for volume_sats
    actual_volume = volume_sats if volume_sats else amount_sats
    return rpc_deposit_marker(
        _get_hive_context(),
        source=source,
        destination=destination,
        fee_ppm=fee_ppm,
        success=success,
        volume_sats=actual_volume
    )


@plugin.method("hive-record-routing-outcome")
def hive_record_routing_outcome(
    plugin: Plugin,
    channel_id: str,
    peer_id: str,
    fee_ppm: int,
    success: bool,
    amount_sats: int = 0,
    source: str = None,
    destination: str = None
):
    """
    Record a routing outcome for pheromone and stigmergic learning.

    Updates pheromone levels for the channel and optionally deposits
    a stigmergic marker if source/destination are provided.

    Args:
        channel_id: Channel that routed the payment
        peer_id: Peer on this channel
        fee_ppm: Fee charged in ppm
        success: Whether routing succeeded
        amount_sats: Forwarded amount in satoshis
        source: Source peer (optional, for stigmergic marker)
        destination: Destination peer (optional, for stigmergic marker)

    Returns:
        Dict with status.
    """
    ctx = _get_hive_context()
    if not ctx.fee_coordination_mgr:
        return {"error": "Fee coordination not initialized"}

    try:
        revenue_sats = int((amount_sats * fee_ppm) / 1_000_000) if success and amount_sats > 0 else 0
        ctx.fee_coordination_mgr.record_routing_outcome(
            channel_id=channel_id,
            peer_id=peer_id,
            fee_ppm=fee_ppm,
            success=success,
            revenue_sats=revenue_sats,
            volume_sats=amount_sats if success else 0,
            source=source,
            destination=destination
        )
        return {"status": "recorded", "channel_id": channel_id}
    except Exception as e:
        return {"error": f"Failed to record routing outcome: {e}"}


@plugin.method("hive-defense-status")
def hive_defense_status(plugin: Plugin, peer_id: str = None):
    """
    Get mycelium defense system status.

    Args:
        peer_id: Optional peer to check for threats (returns peer_threat info)

    Returns:
        Dict with active warnings and defensive fee adjustments.
        If peer_id specified, includes peer_threat with is_threat, threat_type, etc.
    """
    return rpc_defense_status(_get_hive_context(), peer_id=peer_id)


@plugin.method("hive-broadcast-warning")
def hive_broadcast_warning(
    plugin: Plugin,
    peer_id: str = "",
    threat_type: str = "drain",
    severity: float = 0.5
):
    """
    Broadcast a peer warning to the fleet.

    Permission: Member only

    Args:
        peer_id: Peer to warn about
        threat_type: Type of threat ('drain', 'unreliable', 'force_close')
        severity: Severity from 0.0 to 1.0

    Returns:
        Dict with broadcast result.
    """
    if not peer_id:
        return {"error": "peer_id is required"}
    return rpc_broadcast_warning(
        _get_hive_context(),
        peer_id=peer_id,
        threat_type=threat_type,
        severity=severity
    )


@plugin.method("hive-ban-candidates")
def hive_ban_candidates(plugin: Plugin, auto_propose: bool = False):
    """
    Get peers that should be considered for ban proposals.

    Uses accumulated warnings from local threat detection and peer reputation
    reports from other hive members to identify malicious actors.

    Permission: Member only

    Args:
        auto_propose: If True, automatically create ban proposals for severe cases

    Returns:
        Dict with ban candidates and their severity scores.
    """
    if not fee_coordination_mgr:
        return {"error": "Fee coordination manager not initialized"}

    # Get candidates from defense system
    candidates = fee_coordination_mgr.defense_system.get_ban_candidates()

    result = {
        "ban_candidates": candidates,
        "count": len(candidates),
        "auto_propose_enabled": auto_propose
    }

    if auto_propose and candidates:
        # Check each candidate for auto-ban threshold
        proposed = []
        for candidate in candidates:
            peer_id = candidate.get("peer_id")
            reason = fee_coordination_mgr.defense_system.should_auto_propose_ban(peer_id)
            if reason:
                # Create ban proposal
                try:
                    ban_result = hive_ban(plugin, peer_id, reason)
                    if "error" not in ban_result:
                        proposed.append({
                            "peer_id": peer_id,
                            "reason": reason,
                            "proposal_id": ban_result.get("proposal_id")
                        })
                except Exception as e:
                    plugin.log(f"cl-hive: Failed to auto-propose ban for {peer_id[:16]}: {e}", level='warn')

        result["auto_proposed"] = proposed
        result["auto_proposed_count"] = len(proposed)

    return result


@plugin.method("hive-accumulated-warnings")
def hive_accumulated_warnings(plugin: Plugin, peer_id: str):
    """
    Get accumulated warning information for a specific peer.

    Combines local threat detection with aggregated peer reputation data
    from other hive members.

    Args:
        peer_id: Peer to check

    Returns:
        Dict with warning summary including all reporters' data.
    """
    if not fee_coordination_mgr:
        return {"error": "Fee coordination manager not initialized"}

    warnings = fee_coordination_mgr.defense_system.get_accumulated_warnings(peer_id)

    # Add auto-ban check
    auto_ban_reason = fee_coordination_mgr.defense_system.should_auto_propose_ban(peer_id)
    warnings["should_auto_ban"] = auto_ban_reason is not None
    warnings["auto_ban_reason"] = auto_ban_reason

    return warnings


@plugin.method("hive-pheromone-levels")
def hive_pheromone_levels(plugin: Plugin, channel_id: str = None):
    """
    Get pheromone levels for adaptive fee control.

    Args:
        channel_id: Optional specific channel

    Returns:
        Dict with pheromone levels.
    """
    return rpc_pheromone_levels(_get_hive_context(), channel_id=channel_id)


@plugin.method("hive-get-routing-intelligence")
def hive_get_routing_intelligence(plugin: Plugin, scid: str = None):
    """
    Get routing intelligence for channel(s).

    Exports pheromone levels, trends, and corridor membership for use by
    external fee optimization systems (e.g., cl-revenue-ops Thompson sampling).

    Args:
        scid: Optional specific channel short_channel_id. If None, returns all.

    Returns:
        Dict with routing intelligence including pheromone levels, trends,
        last forward age, marker count, and active corridor status.
    """
    return rpc_get_routing_intelligence(_get_hive_context(), scid=scid)


@plugin.method("hive-fee-coordination-status")
def hive_fee_coordination_status(plugin: Plugin):
    """
    Get overall fee coordination status.

    Returns:
        Dict with comprehensive fee coordination status.
    """
    return rpc_fee_coordination_status(_get_hive_context())


# =============================================================================
# YIELD OPTIMIZATION PHASE 3: COST REDUCTION
# =============================================================================

@plugin.method("hive-rebalance-recommendations")
def hive_rebalance_recommendations(
    plugin: Plugin,
    prediction_hours: int = 24
):
    """
    Get predictive rebalance recommendations.

    Analyzes channels to find those predicted to deplete or saturate,
    with recommendations for preemptive rebalancing at lower fees.

    Args:
        prediction_hours: How far ahead to predict (default: 24)

    Returns:
        Dict with rebalance recommendations sorted by urgency.
    """
    return rpc_rebalance_recommendations(
        _get_hive_context(),
        prediction_hours=prediction_hours
    )


@plugin.method("hive-fleet-rebalance-path")
def hive_fleet_rebalance_path(
    plugin: Plugin,
    from_channel: str,
    to_channel: str,
    amount_sats: int
):
    """
    Get fleet rebalance path recommendation.

    Checks if rebalancing through fleet members is cheaper than
    external routing.

    Args:
        from_channel: Source channel SCID
        to_channel: Destination channel SCID
        amount_sats: Amount to rebalance

    Returns:
        Dict with path recommendation and savings estimate.
    """
    return rpc_fleet_rebalance_path(
        _get_hive_context(),
        from_channel=from_channel,
        to_channel=to_channel,
        amount_sats=amount_sats
    )


@plugin.method("hive-fleet-boltz-status")
def hive_fleet_boltz_status(plugin: Plugin):
    """
    Aggregate Boltz swap activity across all fleet members from gossip state.

    Returns per-member breakdown of pending swaps, daily spend, and
    fleet totals for coordination.
    """
    ctx = _get_hive_context()
    if ctx.state_manager is None:
        return {"error": "state_manager not initialized"}

    members = {}
    fleet_pending = 0
    fleet_daily_spend = 0

    for state in ctx.state_manager.get_all_peer_states():
        peer_id = state.peer_id
        boltz = getattr(state, 'boltz_activity', None) or {}
        if not isinstance(boltz, dict):
            boltz = {}
        pending = int(boltz.get("pending_swaps", 0) or 0)
        spend = int(boltz.get("daily_spend_sats", 0) or 0)
        last_ts = int(boltz.get("last_swap_ts", 0) or 0)
        members[peer_id] = {
            "pending_swaps": pending,
            "daily_spend_sats": spend,
            "last_swap_ts": last_ts,
        }
        fleet_pending += pending
        fleet_daily_spend += spend

    return {
        "fleet_pending_swaps": fleet_pending,
        "fleet_daily_spend_sats": fleet_daily_spend,
        "member_count": len(members),
        "members": members,
    }


@plugin.method("hive-report-rebalance-outcome")
def hive_report_rebalance_outcome(
    plugin: Plugin,
    from_channel: str = "",
    to_channel: str = "",
    amount_sats: int = 0,
    cost_sats: int = 0,
    success: bool = False,
    via_fleet: bool = False,
    failure_reason: str = ""
):
    """
    Record a rebalance outcome for tracking and circular flow detection.

    Args:
        from_channel: Source channel SCID
        to_channel: Destination channel SCID
        amount_sats: Amount rebalanced
        cost_sats: Cost paid
        success: Whether rebalance succeeded
        via_fleet: Whether routed through fleet members
        failure_reason: Error description if failed

    Returns:
        Dict with recording result and any circular flow warnings.
    """
    return rpc_record_rebalance_outcome(
        _get_hive_context(),
        from_channel=from_channel,
        to_channel=to_channel,
        amount_sats=amount_sats,
        cost_sats=cost_sats,
        success=success,
        via_fleet=via_fleet,
        failure_reason=failure_reason
    )


@plugin.method("hive-circular-flow-status")
def hive_circular_flow_status(plugin: Plugin):
    """
    Get circular flow detection status.

    Shows any detected circular flows (e.g., A→B→C→A) that waste
    fees moving liquidity in circles.

    Returns:
        Dict with circular flow status and detected patterns.
    """
    return rpc_circular_flow_status(_get_hive_context())


@plugin.method("hive-cost-reduction-status")
def hive_cost_reduction_status(plugin: Plugin):
    """
    Get overall cost reduction status.

    Comprehensive view of all Phase 3 cost reduction systems.

    Returns:
        Dict with cost reduction status.
    """
    return rpc_cost_reduction_status(_get_hive_context())


@plugin.method("hive-execute-circular-rebalance")
def hive_execute_circular_rebalance(
    plugin: Plugin,
    from_channel: str,
    to_channel: str,
    amount_sats: int,
    via_members: list = None,
    dry_run: bool = True
):
    """
    Execute a circular rebalance through the hive using explicit sendpay route.

    This bypasses sling's automatic route finding and uses an explicit route
    through hive members, ensuring zero-fee internal routing. The route goes:
    us -> from_channel_peer -> to_channel_peer -> us

    Args:
        from_channel: Source channel SCID (where we have outbound liquidity)
        to_channel: Destination channel SCID (where we want more local balance)
        amount_sats: Amount to rebalance in satoshis
        via_members: Optional list of intermediate member pubkeys
        dry_run: If True, just show the route without executing (default: True)

    Returns:
        Dict with route details and execution result (or preview if dry_run)

    Example:
        # Preview the route:
        lightning-cli hive-execute-circular-rebalance 933128x1345x0 933882x99x0 50000

        # Execute the rebalance:
        lightning-cli hive-execute-circular-rebalance 933128x1345x0 933882x99x0 50000 null false
    """
    return rpc_execute_hive_circular_rebalance(
        _get_hive_context(),
        from_channel=from_channel,
        to_channel=to_channel,
        amount_sats=amount_sats,
        via_members=via_members,
        dry_run=dry_run
    )


# =============================================================================
# MCF (MIN-COST MAX-FLOW) OPTIMIZATION RPC METHODS
# =============================================================================

@plugin.method("hive-mcf-status")
def hive_mcf_status(plugin: Plugin):
    """
    Get MCF (Min-Cost Max-Flow) optimizer status.

    The MCF optimizer computes globally optimal rebalance assignments for
    the entire fleet, minimizing total routing costs while satisfying
    liquidity needs.

    Returns:
        Dict with MCF status including:
        - is_coordinator: Whether we are the elected coordinator
        - coordinator_id: Pubkey of current coordinator
        - last_solution: Details of last computed solution
        - solution_valid: Whether solution is still within validity window
        - our_assignments: Pending assignments for our node
    """
    return rpc_mcf_status(_get_hive_context())


@plugin.method("hive-mcf-solve")
def hive_mcf_solve(plugin: Plugin):
    """
    Trigger MCF optimization cycle.

    Only succeeds if we are the elected coordinator. Collects liquidity
    needs from all fleet members and computes globally optimal rebalance
    assignments using the Successive Shortest Paths algorithm.

    The solution prefers zero-fee hive internal channels and prevents
    circular flows at the planning stage.

    Returns:
        Dict with MCF solution including:
        - assignments: List of rebalance assignments for fleet members
        - total_flow_sats: Total liquidity moved
        - total_cost_sats: Total routing cost
        - unmet_demand_sats: Demand that couldn't be satisfied
        - computation_time_ms: Time to solve
        - iterations: Number of solver iterations

    Example:
        lightning-cli hive-mcf-solve
    """
    return rpc_mcf_solve(_get_hive_context())


@plugin.method("hive-mcf-assignments")
def hive_mcf_assignments(plugin: Plugin):
    """
    Get pending MCF assignments for our node.

    These are the rebalance operations we should execute as part of
    the fleet-wide optimization computed by the MCF solver.

    Returns:
        Dict with:
        - assignments: List of pending assignments with from_channel,
          to_channel, amount_sats, expected_cost_sats, priority
        - count: Number of pending assignments
    """
    return rpc_mcf_assignments(_get_hive_context())


@plugin.method("hive-mcf-optimized-path")
def hive_mcf_optimized_path(
    plugin: Plugin,
    from_channel: str,
    to_channel: str,
    amount_sats: int
):
    """
    Get MCF-optimized rebalance path between channels.

    Uses the latest MCF solution if available and valid,
    otherwise falls back to BFS-based fleet routing.

    Args:
        from_channel: Source channel SCID
        to_channel: Destination channel SCID
        amount_sats: Amount to rebalance

    Returns:
        Dict with path recommendation including:
        - source: "mcf" or "bfs" indicating which algorithm found the path
        - fleet_path_available: Whether a fleet path exists
        - fleet_path: List of pubkeys in the path
        - estimated_fleet_cost_sats: Expected cost
        - recommendation: Recommended action

    Example:
        lightning-cli hive-mcf-optimized-path 933128x1345x0 933882x99x0 100000
    """
    return rpc_mcf_optimized_path(
        _get_hive_context(),
        from_channel=from_channel,
        to_channel=to_channel,
        amount_sats=amount_sats
    )


@plugin.method("hive-report-mcf-completion")
def hive_report_mcf_completion(
    plugin: Plugin,
    assignment_id: str = "",
    success: bool = False,
    actual_amount_sats: int = 0,
    actual_cost_sats: int = 0,
    failure_reason: str = ""
):
    """
    Report completion of an MCF assignment.

    After executing (or failing) an MCF-assigned rebalance, report
    the outcome so the coordinator can track fleet-wide progress.

    Args:
        assignment_id: ID of the completed assignment
        success: Whether rebalance succeeded
        actual_amount_sats: Actual amount rebalanced
        actual_cost_sats: Actual routing cost
        failure_reason: Reason for failure if not successful

    Returns:
        Dict with success status
    """
    if not liquidity_coord:
        return {"success": False, "error": "Liquidity coordinator not initialized"}

    try:
        # Update local assignment status
        updated = liquidity_coord.update_mcf_assignment_status(
            assignment_id=assignment_id,
            status="completed" if success else "failed",
            actual_amount_sats=actual_amount_sats,
            actual_cost_sats=actual_cost_sats,
            error_message=failure_reason
        )

        if not updated:
            return {
                "success": False,
                "error": f"Assignment {assignment_id} not found"
            }

        # Broadcast completion to fleet
        broadcast_count = protocol_handlers._broadcast_mcf_completion(
            assignment_id=assignment_id,
            success=success,
            actual_amount_sats=actual_amount_sats,
            actual_cost_sats=actual_cost_sats,
            failure_reason=failure_reason
        )

        return {
            "success": True,
            "assignment_id": assignment_id,
            "status": "completed" if success else "failed",
            "broadcast_count": broadcast_count
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


@plugin.method("hive-claim-mcf-assignment")
def hive_claim_mcf_assignment(plugin: Plugin, assignment_id: str = None):
    """
    Claim an MCF assignment for execution.

    Marks an assignment as "executing" to prevent double execution.
    If no assignment_id provided, claims the highest priority pending.

    Args:
        assignment_id: Specific assignment to claim, or None for next pending

    Returns:
        Dict with claimed assignment details
    """
    if not liquidity_coord:
        return {"success": False, "error": "Liquidity coordinator not initialized"}

    try:
        # Atomically find and claim assignment (prevents TOCTOU race)
        claimed = liquidity_coord.claim_pending_assignment(assignment_id)

        if not claimed:
            error_msg = f"Assignment {assignment_id} not found or not pending" if assignment_id else "No pending assignments"
            return {"success": False, "error": error_msg}

        return {
            "success": True,
            "assignment": {
                "assignment_id": claimed.assignment_id,
                "from_channel": claimed.from_channel,
                "to_channel": claimed.to_channel,
                "amount_sats": claimed.amount_sats,
                "expected_cost_sats": claimed.expected_cost_sats,
                "priority": claimed.priority,
                "path": claimed.path,
                "via_fleet": claimed.via_fleet,
            }
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


# =============================================================================
# CHANNEL RATIONALIZATION RPC METHODS
# =============================================================================

@plugin.method("hive-coverage-analysis")
def hive_coverage_analysis(plugin: Plugin, peer_id: str = None):
    """
    Analyze fleet coverage for redundant channels.

    Shows which fleet members have channels to the same peers
    and determines ownership based on routing activity (stigmergic markers).

    Args:
        peer_id: Specific peer to analyze, or omit for all redundant peers

    Returns:
        Dict with coverage analysis showing ownership and redundancy.
    """
    return rpc_coverage_analysis(_get_hive_context(), peer_id=peer_id)


@plugin.method("hive-close-recommendations")
def hive_close_recommendations(plugin: Plugin, our_node_only: bool = False):
    """
    Get channel close recommendations for underperforming redundant channels.

    Uses stigmergic markers (routing success) to determine which member
    "owns" each peer relationship. Recommends closes for members with
    <10% of the owner's routing activity.

    Part of the Hive covenant: members follow swarm intelligence.

    Args:
        our_node_only: If True, only return recommendations for our node

    Returns:
        Dict with close recommendations sorted by urgency.
    """
    return rpc_close_recommendations(_get_hive_context(), our_node_only=our_node_only)


@plugin.method("hive-create-close-actions")
def hive_create_close_actions(plugin: Plugin):
    """
    Create pending_actions for close recommendations.

    Puts high-confidence close recommendations into the pending_actions
    queue for AI/human approval.

    Returns:
        Dict with number of actions created.
    """
    return rpc_create_close_actions(_get_hive_context())


@plugin.method("hive-rationalization-summary")
def hive_rationalization_summary(plugin: Plugin):
    """
    Get summary of channel rationalization analysis.

    Shows fleet coverage health: well-owned peers, contested peers,
    orphan peers (channels with no routing activity), and close recommendations.

    Returns:
        Dict with rationalization summary.
    """
    return rpc_rationalization_summary(_get_hive_context())


@plugin.method("hive-rationalization-status")
def hive_rationalization_status(plugin: Plugin):
    """
    Get channel rationalization status.

    Shows overall coverage health metrics and configuration thresholds.

    Returns:
        Dict with rationalization status.
    """
    return rpc_rationalization_status(_get_hive_context())


# =============================================================================
# PHASE 5: STRATEGIC POSITIONING COMMANDS
# =============================================================================

@plugin.method("hive-valuable-corridors")
def hive_valuable_corridors(plugin: Plugin, min_score: float = 0.05):
    """
    Get high-value routing corridors for strategic positioning.

    Corridors are scored by: Volume × Margin × (1/Competition)
    Higher scores indicate better positioning opportunities.

    Args:
        min_score: Minimum value score to include (default: 0.05)

    Returns:
        Dict with valuable corridors sorted by score.
    """
    return rpc_valuable_corridors(_get_hive_context(), min_score=min_score)


@plugin.method("hive-exchange-coverage")
def hive_exchange_coverage(plugin: Plugin):
    """
    Get priority exchange connectivity status.

    Shows which major Lightning exchanges the fleet is connected to
    (ACINQ, Kraken, Bitfinex, etc.) and which still need channels.

    Returns:
        Dict with exchange coverage analysis.
    """
    return rpc_exchange_coverage(_get_hive_context())


@plugin.method("hive-positioning-recommendations")
def hive_positioning_recommendations(plugin: Plugin, count: int = 5):
    """
    Get channel open recommendations for strategic positioning.

    Recommends where to open channels for maximum routing value,
    considering existing fleet coverage and competition.

    Args:
        count: Number of recommendations to return (default: 5)

    Returns:
        Dict with positioning recommendations sorted by priority.
    """
    return rpc_positioning_recommendations(_get_hive_context(), count=count)


@plugin.method("hive-flow-recommendations")
def hive_flow_recommendations(plugin: Plugin, channel_id: str = None):
    """
    Get Physarum-inspired flow recommendations for channel lifecycle.

    Channels evolve based on flow like slime mold tubes:
    - High flow (>2% daily) → strengthen (splice in capacity)
    - Low flow (<0.1% daily) → atrophy (recommend close)
    - Young + low flow → stimulate (fee reduction)

    Args:
        channel_id: Specific channel, or None for all non-hold recommendations

    Returns:
        Dict with flow recommendations.
    """
    return rpc_flow_recommendations(_get_hive_context(), channel_id=channel_id)


@plugin.method("hive-report-flow-intensity")
def hive_report_flow_intensity(plugin: Plugin, channel_id: str = "", peer_id: str = "", intensity: float = 0.0):
    """
    Report flow intensity for a channel to the Physarum model.

    Flow intensity = Daily volume / Capacity
    This updates the slime-mold model that drives channel lifecycle decisions.

    Args:
        channel_id: Channel ID (SCID format)
        peer_id: Peer public key
        intensity: Observed flow intensity (0.0 to 1.0+)

    Returns:
        Dict with acknowledgment.
    """
    if not channel_id or not peer_id:
        return {"error": "channel_id and peer_id are required"}
    return rpc_report_flow_intensity(
        _get_hive_context(),
        channel_id=channel_id,
        peer_id=peer_id,
        intensity=intensity
    )


@plugin.method("hive-positioning-summary")
def hive_positioning_summary(plugin: Plugin):
    """
    Get summary of strategic positioning analysis.

    Shows high-value corridors, exchange coverage, and recommended actions.

    Returns:
        Dict with positioning summary.
    """
    return rpc_positioning_summary(_get_hive_context())


@plugin.method("hive-positioning-status")
def hive_positioning_status(plugin: Plugin):
    """
    Get strategic positioning status.

    Shows overall status, thresholds, and priority exchanges.

    Returns:
        Dict with positioning status.
    """
    return rpc_positioning_status(_get_hive_context())


# =============================================================================
# PHYSARUM AUTO-TRIGGER RPC METHODS (Phase 7.2)
# =============================================================================

@plugin.method("hive-physarum-cycle")
def hive_physarum_cycle(plugin: Plugin):
    """
    Execute one Physarum optimization cycle.

    Evaluates all channels and creates pending_actions for:
    - High-flow channels that should be strengthened (splice-in)
    - Old low-flow channels that should atrophy (close recommendation)
    - Young low-flow channels that need stimulation (fee reduction)

    All actions go through governance approval - nothing executes directly.

    Returns:
        Dict with cycle results including proposals created.
    """
    if not strategic_positioning_mgr:
        return {"error": "Strategic positioning manager not initialized"}

    result = strategic_positioning_mgr.physarum_mgr.execute_physarum_cycle()
    return result


@plugin.method("hive-physarum-status")
def hive_physarum_status(plugin: Plugin):
    """
    Get Physarum auto-trigger status.

    Shows configuration, thresholds, rate limits, and current usage.

    Returns:
        Dict with auto-trigger status.
    """
    if not strategic_positioning_mgr:
        return {"error": "Strategic positioning manager not initialized"}

    return strategic_positioning_mgr.physarum_mgr.get_auto_trigger_status()


@plugin.method("hive-request-promotion")
def hive_request_promotion(plugin: Plugin):
    """
    Request promotion from neophyte to member.
    """
    if not config or not config.membership_enabled:
        return {"error": "membership_disabled"}
    if not membership_mgr or not our_pubkey:
        return {"error": "membership_unavailable"}

    tier = membership_mgr.get_tier(our_pubkey)
    if tier != MembershipTier.NEOPHYTE.value:
        return {"error": "permission_denied", "required_tier": "neophyte"}

    request_id = secrets.token_hex(16)
    now = int(time.time())
    database.add_promotion_request(our_pubkey, request_id, status="pending")

    payload = {
        "target_pubkey": our_pubkey,
        "request_id": request_id,
        "timestamp": now
    }
    msg = serialize(HiveMessageType.PROMOTION_REQUEST, payload)
    protocol_handlers._broadcast_to_members(msg)

    active_members = membership_mgr.get_active_members()
    quorum = membership_mgr.calculate_quorum(len(active_members))
    return {
        "status": "requested",
        "request_id": request_id,
        "vouches_needed": quorum
    }


@plugin.method("hive-genesis")
def hive_genesis(plugin: Plugin, hive_id: str = None):
    """
    Initialize this node as the Genesis (Admin) node of a new Hive.

    This creates the first member record with member privileges and
    generates a self-signed genesis ticket.

    Args:
        hive_id: Optional custom Hive identifier (auto-generated if not provided)

    Returns:
        Dict with genesis status and member ticket
    """
    if not database or not plugin or not handshake_mgr:
        return {"error": "Hive not initialized"}

    existing_members = database.get_all_members()
    if existing_members:
        return {"error": "Genesis already performed. Use hive-reset to reinitialize."}

    try:
        result = handshake_mgr.genesis(hive_id)

        # Auto-generate and register BOLT12 offer for settlement
        if settlement_mgr:
            our_pubkey = handshake_mgr.get_our_pubkey()
            offer_result = settlement_mgr.generate_and_register_offer(our_pubkey)
            if "error" in offer_result:
                plugin.log(f"cl-hive: Failed to auto-register settlement offer: {offer_result['error']}", level='warn')
            else:
                result["settlement_offer"] = offer_result.get("status")
                plugin.log(f"cl-hive: Settlement offer auto-registered for genesis member")

        return result
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Genesis failed: {e}"}


@plugin.method("hive-invite")
def hive_invite(plugin: Plugin, valid_hours: int = 24, requirements: int = 0,
                tier: str = 'neophyte'):
    """
    Generate an invitation ticket for a new member.

    Only full members can generate invite tickets. New members join as neophytes
    and can be promoted to member after meeting the promotion criteria.

    Args:
        valid_hours: Hours until ticket expires (default: 24)
        requirements: Bitmask of required features (default: 0 = none)
        tier: Starting tier - 'neophyte' (default) or 'member' (bootstrap only)

    Returns:
        Dict with base64-encoded ticket

    Permission: Member only
    """
    # Permission check: Member only
    perm_error = _check_permission('member')
    if perm_error:
        return perm_error

    if not handshake_mgr:
        return {"error": "Hive not initialized"}

    # Validate tier (2-tier system: member or neophyte)
    if tier not in ('neophyte', 'member'):
        return {"error": f"Invalid tier: {tier}. Use 'neophyte' (default) or 'member' (bootstrap)"}

    try:
        ticket = handshake_mgr.generate_invite_ticket(valid_hours, requirements, tier)
        bootstrap_note = " (BOOTSTRAP - grants full member tier)" if tier == 'member' else ""
        return {
            "status": "ticket_generated",
            "ticket": ticket,
            "valid_hours": valid_hours,
            "initial_tier": tier,
            "instructions": f"Share this ticket with the candidate.{bootstrap_note} They should use 'hive-join <ticket>' to request membership."
        }
    except PermissionError as e:
        return {"error": str(e)}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to generate ticket: {e}"}


@plugin.method("hive-join")
def hive_join(plugin: Plugin, ticket: str, peer_id: str = None):
    """
    Request to join a Hive using an invitation ticket.
    
    This initiates the handshake protocol by sending a HELLO message
    to a known Hive member.
    
    Args:
        ticket: Base64-encoded invitation ticket
        peer_id: Node ID of a known Hive member (optional, extracted from ticket if not provided)
    
    Returns:
        Dict with join request status
    """
    if not handshake_mgr :
        return {"error": "Hive not initialized"}
    
    # Decode ticket to get admin pubkey if peer_id not provided
    try:
        ticket_obj = Ticket.from_base64(ticket)
        if not peer_id:
            peer_id = ticket_obj.admin_pubkey
    except Exception as e:
        return {"error": f"Invalid ticket format: {e}"}
    
    # Check if ticket is expired
    if ticket_obj.is_expired():
        return {"error": "Ticket has expired"}
    
    # Send HELLO message with our pubkey (for identity binding)
    from modules.protocol import create_hello
    our_pubkey = handshake_mgr.get_our_pubkey()
    hello_msg = create_hello(our_pubkey)
    if hello_msg is None:
        return {"error": "HELLO message too large to serialize"}

    try:
        plugin.rpc.call("sendcustommsg", {
            "node_id": peer_id,
            "msg": hello_msg.hex()
        })
        
        return {
            "status": "join_requested",
            "target_peer": peer_id[:16] + "...",
            "hive_id": ticket_obj.hive_id,
            "message": "HELLO sent. Awaiting CHALLENGE from Hive member."
        }
    except Exception as e:
        return {"error": f"Failed to send HELLO: {e}"}


# =============================================================================
# ANTICIPATORY LIQUIDITY RPC METHODS (Phase 7.1)
# =============================================================================

@plugin.method("hive-record-flow")
def hive_record_flow(
    plugin: Plugin,
    channel_id: str,
    inbound_sats: int,
    outbound_sats: int,
    timestamp: int = None
):
    """
    Record a flow observation for pattern detection.

    Called periodically (e.g., hourly) to build flow history for
    temporal pattern detection and predictive rebalancing.

    Args:
        channel_id: Channel SCID
        inbound_sats: Satoshis received in this period
        outbound_sats: Satoshis sent in this period
        timestamp: Unix timestamp (defaults to now)

    Returns:
        Dict with recording result.
    """
    if not anticipatory_liquidity_mgr:
        return {"error": "Anticipatory liquidity manager not initialized"}

    anticipatory_liquidity_mgr.record_flow_sample(
        channel_id=channel_id,
        inbound_sats=inbound_sats,
        outbound_sats=outbound_sats,
        timestamp=timestamp
    )

    return {
        "status": "ok",
        "channel_id": channel_id,
        "net_flow": inbound_sats - outbound_sats
    }


@plugin.method("hive-fleet-anticipation")
def hive_fleet_anticipation(plugin: Plugin):
    """
    Get fleet-wide anticipatory positioning recommendations.

    Coordinates predictions across hive members to avoid competing
    for the same rebalance routes.

    Returns:
        Dict with fleet coordination recommendations.
    """
    if not anticipatory_liquidity_mgr:
        return {"error": "Anticipatory liquidity manager not initialized"}

    recommendations = anticipatory_liquidity_mgr.get_fleet_recommendations()

    return {
        "recommendation_count": len(recommendations),
        "recommendations": [r.to_dict() for r in recommendations]
    }


@plugin.method("hive-anticipatory-status")
def hive_anticipatory_status(plugin: Plugin):
    """
    Get anticipatory liquidity manager status.

    Returns operational status and configuration for diagnostics.

    Returns:
        Dict with manager status.
    """
    if not anticipatory_liquidity_mgr:
        return {"error": "Anticipatory liquidity manager not initialized"}

    return anticipatory_liquidity_mgr.get_status()


# =============================================================================
# TIME-BASED FEE RPC METHODS (Phase 7.4)
# =============================================================================

@plugin.method("hive-time-fee-status")
def hive_time_fee_status(plugin: Plugin):
    """
    Get time-based fee adjustment status.

    Returns current time context, active adjustments, and configuration.

    Returns:
        Dict with time-based fee status.
    """
    if not fee_coordination_mgr:
        return {"error": "Fee coordination manager not initialized"}

    return fee_coordination_mgr.get_time_fee_status()


@plugin.method("hive-time-fee-adjustment")
def hive_time_fee_adjustment(plugin: Plugin, channel_id: str, base_fee: int = 250):
    """
    Get time-based fee adjustment for a specific channel.

    Analyzes temporal patterns to determine optimal fee for current time.

    Args:
        channel_id: Channel short ID (e.g., "123x456x0")
        base_fee: Current/base fee in ppm (default: 250)

    Returns:
        Dict with adjustment details including recommended fee.
    """
    if not fee_coordination_mgr:
        return {"error": "Fee coordination manager not initialized"}

    return fee_coordination_mgr.get_time_fee_adjustment(channel_id, base_fee)


@plugin.method("hive-time-peak-hours")
def hive_time_peak_hours(plugin: Plugin, channel_id: str):
    """
    Get detected peak routing hours for a channel.

    Returns hours with above-average routing volume based on historical patterns.

    Args:
        channel_id: Channel short ID

    Returns:
        List of peak hour details with intensity and confidence.
    """
    if not fee_coordination_mgr:
        return {"error": "Fee coordination manager not initialized"}

    peak_hours = fee_coordination_mgr.get_channel_peak_hours(channel_id)
    return {
        "channel_id": channel_id,
        "peak_hours": peak_hours,
        "count": len(peak_hours)
    }


@plugin.method("hive-time-low-hours")
def hive_time_low_hours(plugin: Plugin, channel_id: str):
    """
    Get detected low-activity hours for a channel.

    Returns hours with below-average routing volume where fee reduction may help.

    Args:
        channel_id: Channel short ID

    Returns:
        List of low-activity hour details with intensity and confidence.
    """
    if not fee_coordination_mgr:
        return {"error": "Fee coordination manager not initialized"}

    low_hours = fee_coordination_mgr.get_channel_low_hours(channel_id)
    return {
        "channel_id": channel_id,
        "low_hours": low_hours,
        "count": len(low_hours)
    }


@plugin.method("hive-backfill-routing-intelligence")
def hive_backfill_routing_intelligence(
    plugin: Plugin,
    days: int = 30,
    status_filter: str = "settled"
):
    """
    Backfill pheromone levels and stigmergic markers from historical forwards.

    Reads historical forward data and populates the fee coordination systems
    (pheromones + stigmergic markers) to bootstrap swarm intelligence.

    Args:
        days: Number of days of history to process (default: 30)
        status_filter: Forward status to include: "settled", "failed", or "all" (default: settled)

    Returns:
        Dict with backfill statistics.
    """
    if not fee_coordination_mgr:
        return {"error": "Fee coordination manager not initialized"}

    if not plugin:
        return {"error": "Plugin not initialized"}

    try:
        # Get historical forwards
        forwards_result = plugin.rpc.listforwards(status=status_filter if status_filter != "all" else None)
        forwards = forwards_result.get("forwards", [])

        if not forwards:
            return {
                "status": "no_data",
                "message": "No forwards found to backfill",
                "processed": 0
            }

        # Get channel info for peer mapping
        funds = plugin.rpc.listfunds()
        channels = {ch.get("short_channel_id"): ch for ch in funds.get("channels", [])}

        # Calculate cutoff time
        cutoff_time = int(time.time()) - (days * 86400)

        # Process forwards
        processed = 0
        skipped = 0
        errors = 0
        pheromone_deposits = 0
        marker_deposits = 0

        for fwd in forwards:
            try:
                # Check timestamp if available
                received_time = fwd.get("received_time", 0)
                if received_time and received_time < cutoff_time:
                    skipped += 1
                    continue

                out_channel = fwd.get("out_channel", "")
                in_channel = fwd.get("in_channel", "")
                fee_msat = protocol_handlers._parse_msat_value(
                    fwd.get("fee_msat", fwd.get("fee_msatoshi", 0))
                )
                out_msat = protocol_handlers._parse_msat_value(
                    fwd.get("out_msat", fwd.get("out_msatoshi", 0))
                )
                status = fwd.get("status", "unknown")

                if not out_channel:
                    skipped += 1
                    continue

                # Get peer IDs
                out_peer = channels.get(out_channel, {}).get("peer_id", "")
                in_peer = channels.get(in_channel, {}).get("peer_id", "") if in_channel else ""

                if not out_peer:
                    skipped += 1
                    continue

                # Calculate metrics
                fee_ppm = int((fee_msat * 1_000_000) / out_msat) if out_msat > 0 else 0
                fee_sats = fee_msat // 1000
                volume_sats = out_msat // 1000 if out_msat else 0
                success = status == "settled"

                # Record to fee coordination manager
                fee_coordination_mgr.record_routing_outcome(
                    channel_id=out_channel,
                    peer_id=out_peer,
                    fee_ppm=fee_ppm,
                    success=success,
                    revenue_sats=fee_sats if success else 0,
                    volume_sats=volume_sats if success else 0,
                    source=in_peer if in_peer else None,
                    destination=out_peer
                )

                processed += 1

                # Track what was deposited
                if success and fee_sats > 0:
                    pheromone_deposits += 1
                if in_peer and out_peer:
                    marker_deposits += 1

            except Exception as e:
                errors += 1
                continue

        # Get current levels after backfill
        pheromone_levels = fee_coordination_mgr.adaptive_controller.get_all_pheromone_levels()
        markers = fee_coordination_mgr.stigmergic_coord.get_all_markers()

        return {
            "status": "success",
            "days_processed": days,
            "status_filter": status_filter,
            "forwards_found": len(forwards),
            "processed": processed,
            "skipped": skipped,
            "errors": errors,
            "pheromone_deposits": pheromone_deposits,
            "marker_deposits": marker_deposits,
            "current_pheromone_channels": len(pheromone_levels),
            "current_active_markers": len(markers),
            "pheromone_summary": {
                ch: round(level, 2)
                for ch, level in sorted(
                    pheromone_levels.items(),
                    key=lambda x: x[1],
                    reverse=True
                )[:10]  # Top 10 channels
            }
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }


@plugin.method("hive-routing-intelligence-status")
def hive_routing_intelligence_status(plugin: Plugin):
    """
    Get current status of routing intelligence systems (pheromones + markers).

    Returns current pheromone levels and stigmergic markers.

    Returns:
        Dict with routing intelligence status.
    """
    if not fee_coordination_mgr:
        return {"error": "Fee coordination manager not initialized"}

    pheromone_levels = fee_coordination_mgr.adaptive_controller.get_all_pheromone_levels()
    markers = fee_coordination_mgr.stigmergic_coord.get_all_markers()

    # Build marker summary
    marker_summary = []
    for m in markers[:20]:  # Limit to 20 most recent
        marker_summary.append({
            "source": m.source_peer_id[:12] + "..." if m.source_peer_id else "",
            "destination": m.destination_peer_id[:12] + "..." if m.destination_peer_id else "",
            "fee_ppm": m.fee_ppm,
            "success": m.success,
            "strength": round(m.strength, 3),
            "age_hours": round((time.time() - m.timestamp) / 3600, 1)
        })

    # Build pheromone summary
    pheromone_summary = []
    for ch, level in sorted(pheromone_levels.items(), key=lambda x: x[1], reverse=True):
        pheromone_summary.append({
            "channel_id": ch,
            "level": round(level, 3),
            "above_threshold": level > 10.0  # PHEROMONE_EXPLOIT_THRESHOLD
        })

    return {
        "pheromone_channels": len(pheromone_levels),
        "active_markers": len(markers),
        "successful_markers": sum(1 for m in markers if m.success),
        "failed_markers": sum(1 for m in markers if not m.success),
        "pheromone_levels": pheromone_summary,
        "stigmergic_markers": marker_summary,
        "config": {
            "pheromone_exploit_threshold": 2.0,
            "marker_half_life_hours": 168,
            "marker_min_strength": 0.1
        }
    }


# =============================================================================
# PHASE 11: HIVE-SPLICE COORDINATION
# =============================================================================

@plugin.method("hive-splice")
def hive_splice(
    plugin: Plugin,
    channel_id: str,
    relative_amount: int,
    feerate_per_kw: int = None,
    dry_run: bool = False,
    force: bool = False
):
    """
    Execute a coordinated splice operation with a hive member.

    Splices must be with channels to other hive members. This command handles
    the full splice coordination workflow between nodes.

    Args:
        channel_id: Channel ID to splice (must be with a hive member)
        relative_amount: Positive = splice-in, Negative = splice-out (satoshis)
        feerate_per_kw: Optional feerate (default: use urgent rate)
        dry_run: If true, preview the operation without executing
        force: If true, skip safety warnings for splice-out

    Returns:
        Dict with splice result including session_id, status, and txid when complete.

    Examples:
        # Splice in 1M sats (add to channel)
        lightning-cli hive-splice 123x456x0 1000000

        # Splice out 500k sats (remove from channel)
        lightning-cli hive-splice 123x456x0 -500000

        # Preview a splice without executing
        lightning-cli hive-splice 123x456x0 1000000 dry_run=true
    """
    if not splice_mgr:
        return {"error": "Splice manager not initialized"}

    if not database:
        return {"error": "Database not initialized"}

    # Find the peer for this channel
    try:
        peer_id = None
        result = plugin.rpc.listpeerchannels()
        for ch in result.get("channels", []):
            scid = ch.get("short_channel_id", ch.get("channel_id"))
            if scid == channel_id:
                peer_id = ch.get("peer_id")
                break

        if not peer_id:
            return {"error": "channel_not_found", "message": f"Channel {channel_id} not found"}

    except Exception as e:
        return {"error": "rpc_error", "message": str(e)}

    # Verify peer is a hive member
    member = database.get_member(peer_id)
    if not member:
        return {
            "error": "not_hive_member",
            "message": f"Channel peer {peer_id[:16]}... is not a hive member. "
                      "Splices are only supported with hive members."
        }

    # Initiate the splice
    return splice_mgr.initiate_splice(
        peer_id=peer_id,
        channel_id=channel_id,
        relative_amount=relative_amount,
        rpc=plugin.rpc,
        feerate_perkw=feerate_per_kw,
        dry_run=dry_run,
        force=force
    )


@plugin.method("hive-splice-status")
def hive_splice_status(plugin: Plugin, session_id: str = None):
    """
    Get status of splice sessions.

    Args:
        session_id: Optional specific session ID. If not provided, returns all active sessions.

    Returns:
        Session details or list of active sessions.
    """
    if not splice_mgr:
        return {"error": "Splice manager not initialized"}

    if session_id:
        session = splice_mgr.get_session_status(session_id)
        if not session:
            return {"error": "unknown_session", "message": f"Session {session_id} not found"}
        return session

    sessions = splice_mgr.get_active_sessions()
    return {
        "active_sessions": sessions,
        "count": len(sessions)
    }


@plugin.method("hive-splice-abort")
def hive_splice_abort(plugin: Plugin, session_id: str):
    """
    Abort an active splice session.

    Args:
        session_id: Session ID to abort.

    Returns:
        Abort result.
    """
    if not splice_mgr:
        return {"error": "Splice manager not initialized"}

    return splice_mgr.abort_session(session_id, plugin.rpc)


# =============================================================================
# REVENUE OPS INTEGRATION RPCs
# =============================================================================
# These methods provide data to cl-revenue-ops for improved fee optimization
# and rebalancing decisions. They expose cl-hive's intelligence layer.


@plugin.method("hive-get-defense-status")
def hive_get_defense_status(plugin: Plugin, scid: str = None):
    """
    Get defense status for channel(s).

    Returns whether channels are under defensive fee protection due to
    drain attacks, spam, or fee wars. Used by cl-revenue-ops to avoid
    overriding defensive fees during optimization.

    Args:
        scid: Optional specific channel SCID. If None, returns all channels.

    Returns:
        Dict with defense status for each channel.

    Example:
        lightning-cli hive-get-defense-status
        lightning-cli hive-get-defense-status 932263x1883x0
    """
    ctx = _get_hive_context()
    return rpc_get_defense_status(ctx, scid)


@plugin.method("hive-get-peer-quality")
def hive_get_peer_quality(plugin: Plugin, peer_id: str = None):
    """
    Get peer quality assessments from the hive's collective intelligence.

    Returns quality ratings based on uptime, routing success, fee stability,
    and fleet-wide reputation. Used by cl-revenue-ops to adjust optimization
    intensity.

    Args:
        peer_id: Optional specific peer ID. If None, returns all peers.

    Returns:
        Dict with peer quality assessments.

    Example:
        lightning-cli hive-get-peer-quality
        lightning-cli hive-get-peer-quality 03abc...
    """
    ctx = _get_hive_context()
    return rpc_get_peer_quality(ctx, peer_id)


@plugin.method("hive-get-fee-change-outcomes")
def hive_get_fee_change_outcomes(plugin: Plugin, scid: str = None, days: int = 30):
    """
    Get outcomes of past fee changes for learning.

    Returns historical fee changes with before/after metrics to help
    cl-revenue-ops learn from past decisions.

    Args:
        scid: Optional specific channel SCID. If None, returns all.
        days: Number of days of history (default: 30, max: 90)

    Returns:
        Dict with fee change outcomes.

    Example:
        lightning-cli hive-get-fee-change-outcomes
        lightning-cli hive-get-fee-change-outcomes scid=932263x1883x0 days=14
    """
    ctx = _get_hive_context()
    return rpc_get_fee_change_outcomes(ctx, scid, days)


@plugin.method("hive-get-channel-flags")
def hive_get_channel_flags(plugin: Plugin, scid: str = None):
    """
    Get special flags for channels.

    Returns flags identifying hive-internal channels that should be excluded
    from optimization (always 0 fee) or have other special treatment.

    Args:
        scid: Optional specific channel SCID. If None, returns all.

    Returns:
        Dict with channel flags.

    Example:
        lightning-cli hive-get-channel-flags
        lightning-cli hive-get-channel-flags 932263x1883x0
    """
    ctx = _get_hive_context()
    return rpc_get_channel_flags(ctx, scid)


@plugin.method("hive-get-mcf-targets")
def hive_get_mcf_targets(plugin: Plugin):
    """
    Get MCF-computed optimal balance targets.

    Returns the Multi-Commodity Flow computed optimal local balance
    percentages for each channel. Used by cl-revenue-ops to guide
    rebalancing toward globally optimal distribution.

    Returns:
        Dict with MCF targets for each channel.

    Example:
        lightning-cli hive-get-mcf-targets
    """
    ctx = _get_hive_context()
    return rpc_get_mcf_targets(ctx)


@plugin.method("hive-get-nnlb-opportunities")
def hive_get_nnlb_opportunities(plugin: Plugin, min_amount: int = 50000):
    """
    Get Nearest-Neighbor Load Balancing opportunities.

    Returns low-cost rebalance opportunities between fleet members where
    the rebalance can be done at zero or minimal fee.

    Args:
        min_amount: Minimum amount in sats to consider (default: 50000)

    Returns:
        Dict with NNLB opportunities.

    Example:
        lightning-cli hive-get-nnlb-opportunities
        lightning-cli hive-get-nnlb-opportunities 100000
    """
    ctx = _get_hive_context()
    return rpc_get_nnlb_opportunities(ctx, min_amount)


@plugin.method("hive-get-channel-ages")
def hive_get_channel_ages(plugin: Plugin, scid: str = None):
    """
    Get channel age information.

    Returns age and maturity classification for channels. Used by
    cl-revenue-ops to adjust exploration vs exploitation in Thompson
    sampling.

    Args:
        scid: Optional specific channel SCID. If None, returns all.

    Returns:
        Dict with channel ages and maturity classifications.

    Example:
        lightning-cli hive-get-channel-ages
        lightning-cli hive-get-channel-ages 932263x1883x0
    """
    ctx = _get_hive_context()
    return rpc_get_channel_ages(ctx, scid)


# =============================================================================
# DID CREDENTIAL RPC COMMANDS (Phase 16)
# =============================================================================

@plugin.method("hive-did-issue")
def hive_did_issue(plugin: Plugin, subject_id: str, domain: str,
                   metrics_json: str, outcome: str = "neutral",
                   evidence_json: str = "[]"):
    """
    Issue a DID reputation credential for a subject.

    Args:
        subject_id: Pubkey of the credential subject
        domain: Credential domain (hive:advisor, hive:node, hive:client, agent:general)
        metrics_json: JSON string of domain-specific metrics
        outcome: 'renew', 'revoke', or 'neutral'
        evidence_json: JSON array of evidence references

    Example:
        lightning-cli hive-did-issue 03abc... hive:node '{"routing_reliability":0.95,"uptime":0.99,"htlc_success_rate":0.98,"avg_fee_ppm":50}'
    """
    ctx = _get_hive_context()
    return rpc_did_issue_credential(ctx, subject_id, domain, metrics_json, outcome, evidence_json)


@plugin.method("hive-did-list")
def hive_did_list(plugin: Plugin, subject_id: str = "", domain: str = "",
                  issuer_id: str = ""):
    """
    List DID credentials with optional filters.

    Args:
        subject_id: Filter by subject pubkey
        domain: Filter by domain
        issuer_id: Filter by issuer pubkey

    Example:
        lightning-cli hive-did-list 03abc...
        lightning-cli hive-did-list subject_id=03abc... domain=hive:node
    """
    ctx = _get_hive_context()
    return rpc_did_list_credentials(ctx, subject_id, domain, issuer_id)


@plugin.method("hive-did-revoke")
def hive_did_revoke(plugin: Plugin, credential_id: str, reason: str):
    """
    Revoke a DID credential we issued.

    Args:
        credential_id: UUID of the credential to revoke
        reason: Revocation reason

    Example:
        lightning-cli hive-did-revoke "a1b2c3d4-..." "peer went offline permanently"
    """
    ctx = _get_hive_context()
    return rpc_did_revoke_credential(ctx, credential_id, reason)


@plugin.method("hive-did-reputation")
def hive_did_reputation(plugin: Plugin, subject_id: str, domain: str = ""):
    """
    Get aggregated reputation score for a subject.

    Args:
        subject_id: Pubkey of the subject
        domain: Optional domain filter (empty = cross-domain)

    Example:
        lightning-cli hive-did-reputation 03abc...
        lightning-cli hive-did-reputation 03abc... hive:node
    """
    ctx = _get_hive_context()
    return rpc_did_get_reputation(ctx, subject_id, domain)


@plugin.method("hive-did-profiles")
def hive_did_profiles(plugin: Plugin):
    """
    List supported DID credential profiles.

    Returns all 4 credential domains with their required metrics,
    optional metrics, and valid ranges.

    Example:
        lightning-cli hive-did-profiles
    """
    ctx = _get_hive_context()
    return rpc_did_list_profiles(ctx)


# =============================================================================
# MANAGEMENT SCHEMA RPC (Phase 2)
# =============================================================================

@plugin.method("hive-schema-list")
def hive_schema_list(plugin: Plugin):
    """
    List all management schemas with their actions and danger scores.

    Returns the 15 management schema categories, each with its actions,
    danger scores (5 dimensions), and required permission tiers.

    Example:
        lightning-cli hive-schema-list
    """
    ctx = _get_hive_context()
    return rpc_schema_list(ctx)


@plugin.method("hive-schema-validate")
def hive_schema_validate(plugin: Plugin, schema_id: str, action: str,
                         params_json: str = None):
    """
    Validate a command against its schema definition (dry run).

    Checks that schema_id and action exist, validates parameter types,
    and returns the danger score and required tier.

    Example:
        lightning-cli hive-schema-validate hive:fee-policy/v1 set_single
    """
    ctx = _get_hive_context()
    return rpc_schema_validate(ctx, schema_id, action, params_json)


@plugin.method("hive-mgmt-credential-issue")
def hive_mgmt_credential_issue(plugin: Plugin, agent_id: str, tier: str,
                                allowed_schemas_json: str,
                                constraints_json: str = None,
                                valid_days: int = 90):
    """
    Issue a management credential granting an agent permission to manage our node.

    The credential is signed with our HSM and can be presented by the agent
    to prove authorization for specific management actions.

    Example:
        lightning-cli hive-mgmt-credential-issue 03abc... standard '["hive:fee-policy/*","hive:monitor/*"]'
    """
    ctx = _get_hive_context()
    return rpc_mgmt_credential_issue(ctx, agent_id, tier,
                                      allowed_schemas_json,
                                      constraints_json, valid_days)


@plugin.method("hive-mgmt-credential-list")
def hive_mgmt_credential_list(plugin: Plugin, agent_id: str = None,
                               node_id: str = None):
    """
    List management credentials with optional filters.

    Example:
        lightning-cli hive-mgmt-credential-list
        lightning-cli hive-mgmt-credential-list agent_id=03abc...
    """
    ctx = _get_hive_context()
    return rpc_mgmt_credential_list(ctx, agent_id, node_id)


@plugin.method("hive-mgmt-credential-revoke")
def hive_mgmt_credential_revoke(plugin: Plugin, credential_id: str):
    """
    Revoke a management credential we issued.

    Once revoked, the credential can no longer be used to authorize
    management actions.

    Example:
        lightning-cli hive-mgmt-credential-revoke <credential-id>
    """
    ctx = _get_hive_context()
    return rpc_mgmt_credential_revoke(ctx, credential_id)


# =============================================================================
# PHASE 4A: CASHU ESCROW RPC METHODS
# =============================================================================

@plugin.method("hive-escrow-create")
def hive_escrow_create(plugin: Plugin, agent_id: str, schema_id: str = "",
                       action: str = "", danger_score: int = 1,
                       amount_sats: int = 0, mint_url: str = "",
                       ticket_type: str = "single"):
    """
    Create a Cashu escrow ticket for agent task payment.

    Example:
        lightning-cli hive-escrow-create agent_id=03abc... danger_score=5 amount_sats=100 mint_url=https://mint.example.com
    """
    ctx = _get_hive_context()
    return rpc_escrow_create(ctx, agent_id, schema_id, action,
                             danger_score, amount_sats, mint_url, ticket_type)


@plugin.method("hive-escrow-list")
def hive_escrow_list(plugin: Plugin, agent_id: str = None,
                     status: str = None):
    """
    List escrow tickets with optional filters.

    Example:
        lightning-cli hive-escrow-list
        lightning-cli hive-escrow-list status=active
    """
    ctx = _get_hive_context()
    return rpc_escrow_list(ctx, agent_id, status)


@plugin.method("hive-escrow-redeem")
def hive_escrow_redeem(plugin: Plugin, ticket_id: str, preimage: str):
    """
    Redeem an escrow ticket with HTLC preimage.

    Example:
        lightning-cli hive-escrow-redeem ticket_id=abc123 preimage=deadbeef...
    """
    ctx = _get_hive_context()
    return rpc_escrow_redeem(ctx, ticket_id, preimage)


@plugin.method("hive-escrow-refund")
def hive_escrow_refund(plugin: Plugin, ticket_id: str):
    """
    Refund an escrow ticket after timelock expiry.

    Example:
        lightning-cli hive-escrow-refund ticket_id=abc123
    """
    ctx = _get_hive_context()
    return rpc_escrow_refund(ctx, ticket_id)


@plugin.method("hive-escrow-receipt")
def hive_escrow_receipt(plugin: Plugin, ticket_id: str):
    """
    Get escrow receipts for a ticket.

    Example:
        lightning-cli hive-escrow-receipt ticket_id=abc123
    """
    ctx = _get_hive_context()
    return rpc_escrow_get_receipt(ctx, ticket_id)


@plugin.method("hive-escrow-complete")
def hive_escrow_complete(plugin: Plugin, ticket_id: str, schema_id: str = "",
                         action: str = "", params_json: str = "{}",
                         result_json: str = "{}", success: bool = True,
                         reveal_preimage: bool = True):
    """
    Complete an escrow task: create receipt and optionally reveal preimage.

    Example:
        lightning-cli hive-escrow-complete ticket_id=abc123 success=true
    """
    ctx = _get_hive_context()
    return rpc_escrow_complete(
        ctx, ticket_id, schema_id, action, params_json,
        result_json, success, reveal_preimage
    )


# =============================================================================
# PHASE 4B: EXTENDED SETTLEMENT RPC METHODS
# =============================================================================

@plugin.method("hive-bond-post")
def hive_bond_post(plugin: Plugin, amount_sats: int = 0,
                   tier: str = ""):
    """
    Post a settlement bond.

    Example:
        lightning-cli hive-bond-post amount_sats=50000
    """
    ctx = _get_hive_context()
    return rpc_bond_post(ctx, amount_sats, tier)


@plugin.method("hive-bond-status")
def hive_bond_status(plugin: Plugin, peer_id: str = None):
    """
    Get bond status for a peer.

    Example:
        lightning-cli hive-bond-status
        lightning-cli hive-bond-status peer_id=03abc...
    """
    ctx = _get_hive_context()
    return rpc_bond_status(ctx, peer_id)


@plugin.method("hive-settlement-list")
def hive_settlement_list(plugin: Plugin, window_id: str = None,
                         peer_id: str = None):
    """
    List settlement obligations.

    Example:
        lightning-cli hive-settlement-list window_id=2024-W01
    """
    ctx = _get_hive_context()
    return rpc_settlement_obligations_list(ctx, window_id, peer_id)


@plugin.method("hive-settlement-net")
def hive_settlement_net(plugin: Plugin, window_id: str = "",
                        peer_id: str = None):
    """
    Compute netting for a settlement window.

    Example:
        lightning-cli hive-settlement-net window_id=2024-W01
        lightning-cli hive-settlement-net window_id=2024-W01 peer_id=03abc...
    """
    ctx = _get_hive_context()
    return rpc_settlement_net(ctx, window_id, peer_id)


@plugin.method("hive-dispute-file")
def hive_dispute_file(plugin: Plugin, obligation_id: str = "",
                      evidence_json: str = "{}"):
    """
    File a settlement dispute.

    Example:
        lightning-cli hive-dispute-file obligation_id=abc123 evidence_json='{"reason":"underpayment"}'
    """
    ctx = _get_hive_context()
    return rpc_dispute_file(ctx, obligation_id, evidence_json)


@plugin.method("hive-dispute-vote")
def hive_dispute_vote(plugin: Plugin, dispute_id: str = "",
                      vote: str = "", reason: str = ""):
    """
    Cast an arbitration panel vote.

    Example:
        lightning-cli hive-dispute-vote dispute_id=abc123 vote=upheld reason="clear evidence"
    """
    ctx = _get_hive_context()
    return rpc_dispute_vote(ctx, dispute_id, vote, reason)


@plugin.method("hive-dispute-status")
def hive_dispute_status(plugin: Plugin, dispute_id: str = ""):
    """
    Get dispute status.

    Example:
        lightning-cli hive-dispute-status dispute_id=abc123
    """
    ctx = _get_hive_context()
    return rpc_dispute_status(ctx, dispute_id)


@plugin.method("hive-credit-tier")
def hive_credit_tier(plugin: Plugin, peer_id: str = None):
    """
    Get credit tier information for a peer.

    Example:
        lightning-cli hive-credit-tier
        lightning-cli hive-credit-tier peer_id=03abc...
    """
    ctx = _get_hive_context()
    return rpc_credit_tier_info(ctx, peer_id)


# =============================================================================
# PHASE 5B: ADVISOR MARKETPLACE RPC METHODS
# =============================================================================

@plugin.method("hive-marketplace-discover")
def hive_marketplace_discover(plugin: Plugin, criteria_json: str = "{}"):
    """Discover advisor profiles from marketplace cache."""
    ctx = _get_hive_context()
    return rpc_marketplace_discover(ctx, criteria_json)


@plugin.method("hive-marketplace-profile")
def hive_marketplace_profile(plugin: Plugin, profile_json: str = ""):
    """View cached advisor profiles or publish local advisor profile."""
    ctx = _get_hive_context()
    return rpc_marketplace_profile(ctx, profile_json)


@plugin.method("hive-marketplace-propose")
def hive_marketplace_propose(plugin: Plugin, advisor_did: str, node_id: str,
                             scope_json: str = "{}", tier: str = "standard",
                             pricing_json: str = "{}"):
    """Propose a contract to an advisor."""
    ctx = _get_hive_context()
    return rpc_marketplace_propose(ctx, advisor_did, node_id, scope_json, tier, pricing_json)


@plugin.method("hive-marketplace-accept")
def hive_marketplace_accept(plugin: Plugin, contract_id: str):
    """Accept an advisor contract proposal."""
    ctx = _get_hive_context()
    return rpc_marketplace_accept(ctx, contract_id)


@plugin.method("hive-marketplace-trial")
def hive_marketplace_trial(plugin: Plugin, contract_id: str, action: str = "start",
                           duration_days: int = 14, flat_fee_sats: int = 0,
                           evaluation_json: str = "{}"):
    """Start or evaluate a trial for an advisor contract."""
    ctx = _get_hive_context()
    return rpc_marketplace_trial(
        ctx, contract_id, action, duration_days, flat_fee_sats, evaluation_json
    )


@plugin.method("hive-marketplace-terminate")
def hive_marketplace_terminate(plugin: Plugin, contract_id: str, reason: str = ""):
    """Terminate an advisor contract."""
    ctx = _get_hive_context()
    return rpc_marketplace_terminate(ctx, contract_id, reason)


@plugin.method("hive-marketplace-status")
def hive_marketplace_status(plugin: Plugin):
    """Get advisor marketplace status."""
    ctx = _get_hive_context()
    return rpc_marketplace_status(ctx)


# =============================================================================
# PHASE 5C: LIQUIDITY MARKETPLACE RPC METHODS
# =============================================================================

@plugin.method("hive-liquidity-discover")
def hive_liquidity_discover(plugin: Plugin, service_type: int = None,
                            min_capacity: int = 0, max_rate: int = None):
    """Discover liquidity offers."""
    ctx = _get_hive_context()
    return rpc_liquidity_discover(ctx, service_type, min_capacity, max_rate)


@plugin.method("hive-liquidity-offer")
def hive_liquidity_offer(plugin: Plugin, provider_id: str, service_type: int,
                         capacity_sats: int, duration_hours: int = 24,
                         pricing_model: str = "sat-hours",
                         rate_json: str = "{}",
                         min_reputation: int = 0,
                         expires_at: int = None):
    """Publish a liquidity offer."""
    ctx = _get_hive_context()
    return rpc_liquidity_offer(
        ctx, provider_id, service_type, capacity_sats, duration_hours,
        pricing_model, rate_json, min_reputation, expires_at
    )


@plugin.method("hive-liquidity-request")
def hive_liquidity_request(plugin: Plugin, requester_id: str, service_type: int,
                           capacity_sats: int, details_json: str = "{}"):
    """Publish a liquidity request (RFP)."""
    ctx = _get_hive_context()
    return rpc_liquidity_request(ctx, requester_id, service_type, capacity_sats, details_json)


@plugin.method("hive-liquidity-lease")
def hive_liquidity_lease(plugin: Plugin, offer_id: str, client_id: str,
                         heartbeat_interval: int = 3600):
    """Accept a liquidity offer and create a lease."""
    ctx = _get_hive_context()
    return rpc_liquidity_lease(ctx, offer_id, client_id, heartbeat_interval)


@plugin.method("hive-liquidity-heartbeat")
def hive_liquidity_heartbeat(plugin: Plugin, lease_id: str, action: str = "send",
                             heartbeat_id: str = "", channel_id: str = "",
                             remote_balance_sats: int = 0,
                             capacity_sats: int = None):
    """Send or verify a lease heartbeat."""
    ctx = _get_hive_context()
    return rpc_liquidity_heartbeat(
        ctx, lease_id, action, heartbeat_id, channel_id, remote_balance_sats, capacity_sats
    )


@plugin.method("hive-liquidity-lease-status")
def hive_liquidity_lease_status(plugin: Plugin, lease_id: str):
    """Get liquidity lease status."""
    ctx = _get_hive_context()
    return rpc_liquidity_lease_status(ctx, lease_id)


@plugin.method("hive-liquidity-terminate")
def hive_liquidity_terminate(plugin: Plugin, lease_id: str, reason: str = ""):
    """Terminate a liquidity lease."""
    ctx = _get_hive_context()
    return rpc_liquidity_terminate(ctx, lease_id, reason)


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    plugin.run()
