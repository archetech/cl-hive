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
    validate_member_left,
    create_challenge, create_welcome,
    # Signed message validation (security hardening)
    validate_gossip, validate_state_hash, validate_full_sync, validate_intent_abort,
    get_gossip_signing_payload, get_state_hash_signing_payload,
    get_full_sync_signing_payload, get_intent_signing_payload, get_intent_abort_signing_payload,
    compute_states_hash,
    # Reliable delivery
    IMPLICIT_ACK_MAP, IMPLICIT_ACK_MATCH_FIELD,
    RELIABLE_MESSAGE_TYPES,
)
from modules.handshake import HandshakeManager, CHALLENGE_TTL_SECONDS
from modules.state_manager import StateManager, HivePeerState
from modules.gossip import GossipManager
from modules.intent_manager import IntentManager, Intent, IntentType
from modules.bridge import Bridge, BridgeStatus, CircuitOpenError
from modules.contribution import ContributionManager
from modules.membership import MembershipManager, MEMBER_TIER
from modules.planner import Planner, ChannelSizer
from modules.quality_scorer import PeerQualityScorer
from modules.governance import RecommendationLogger
from modules.fee_intelligence import FeeIntelligenceManager
from modules.traffic_intelligence import TrafficIntelligenceManager
from modules.liquidity_coordinator import LiquidityCoordinator
from modules.health_aggregator import HealthScoreAggregator, HealthTier
from modules.peer_reputation import PeerReputationManager
from modules.yield_metrics import YieldMetricsManager
from modules.fee_coordination import FeeCoordinationManager
from modules.channel_rationalization import RationalizationManager
from modules.strategic_positioning import StrategicPositioningManager
from modules.relay import RelayManager
from modules.idempotency import check_and_record, generate_event_id
from modules.outbox import OutboxManager
from modules import network_metrics
from modules.plugin_options import (
    RateLimiter, _parse_bool, _parse_setconfig_value,
    OPTION_TO_CONFIG_MAP, register_options,
)
from modules.log_writer import BatchedLogWriter
from modules import protocol_handlers
from modules import background_loops
from modules.rpc_commands import (
    HiveContext,
    status as rpc_status,
    get_config as rpc_get_config,
    members as rpc_members,
    expansion_recommendations as rpc_expansion_recommendations,
    # Phase 4: Topology, Planner, and Query Commands
    reinit_bridge as rpc_reinit_bridge,
    topology as rpc_topology,
    planner_log as rpc_planner_log,
    intent_status as rpc_intent_status,
    contribution as rpc_contribution,
    # Phase 1: Yield Metrics & Measurement
    yield_metrics as rpc_yield_metrics,
    yield_summary as rpc_yield_summary,
    velocity_prediction as rpc_velocity_prediction,
    critical_velocity_channels as rpc_critical_velocity_channels,
    # Phase 2: Fee Coordination
    fee_recommendation as rpc_fee_recommendation,
    egress_desaturation_bias as rpc_egress_desaturation_bias,
    corridor_assignments as rpc_corridor_assignments,
    fee_coordination_status as rpc_fee_coordination_status,
    rebalance_hubs as rpc_rebalance_hubs,
    check_rebalance_conflict as rpc_check_rebalance_conflict,
    # Channel Rationalization
    coverage_analysis as rpc_coverage_analysis,
    close_recommendations as rpc_close_recommendations,
    rationalization_summary as rpc_rationalization_summary,
    rationalization_status as rpc_rationalization_status,
    # Phase 5 - Strategic Positioning
    valuable_corridors as rpc_valuable_corridors,
    exchange_coverage as rpc_exchange_coverage,
    positioning_recommendations as rpc_positioning_recommendations,
    positioning_summary as rpc_positioning_summary,
    positioning_status as rpc_positioning_status,
    # Network Metrics
    network_metrics as rpc_network_metrics,
    # Fleet Health Monitoring
    fleet_health as rpc_fleet_health,
    connectivity_alerts as rpc_connectivity_alerts,
    member_connectivity as rpc_member_connectivity,
    # Revenue Ops Integration
    get_peer_quality as rpc_get_peer_quality,
    get_channel_flags as rpc_get_channel_flags,
    get_nnlb_opportunities as rpc_get_nnlb_opportunities,
    get_channel_ages as rpc_get_channel_ages,
    # Phase 14: Traffic Intelligence
    report_traffic_profile as rpc_report_traffic_profile,
    get_traffic_intelligence as rpc_get_traffic_intelligence,
    get_fleet_demand_forecast as rpc_get_fleet_demand_forecast,
    # Export hints (local trusted integration for cl-revenue-ops)
    export_hints as rpc_export_hints,
    # Routing intelligence
    get_routing_intelligence as rpc_get_routing_intelligence,
)

# Initialize the plugin
plugin = Plugin()

# =============================================================================
# GRACEFUL SHUTDOWN SUPPORT
# =============================================================================
# This event signals all background threads to exit cleanly.
# When `lightning-cli plugin stop cl-hive` is called, CLN sends SIGTERM.

shutdown_event = threading.Event()

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
recommendation_logger: Optional[RecommendationLogger] = None
fee_intel_mgr: Optional[FeeIntelligenceManager] = None
traffic_intel_mgr: Optional[TrafficIntelligenceManager] = None
health_aggregator: Optional[HealthScoreAggregator] = None
liquidity_coord: Optional[LiquidityCoordinator] = None
peer_reputation_mgr: Optional[PeerReputationManager] = None
askrene_layer_mgr = None
yield_metrics_mgr: Optional[YieldMetricsManager] = None
fee_coordination_mgr: Optional[FeeCoordinationManager] = None
rationalization_mgr: Optional[RationalizationManager] = None
strategic_positioning_mgr: Optional[StrategicPositioningManager] = None
quality_scorer_mgr: Optional[PeerQualityScorer] = None
relay_mgr: Optional[RelayManager] = None
outbox_mgr: Optional[OutboxManager] = None
our_pubkey: Optional[str] = None
# Startup timestamp for lightweight health endpoint (Phase 4)
_start_time: float = time.time()

# Fee tracking for real-time gossip
_local_fees_earned_sats: int = 0
_local_fees_forward_count: int = 0
_local_fees_period_start: int = 0
_local_fees_last_broadcast: int = 0
_local_fees_last_broadcast_amount: int = 0  # Tracks fees at last broadcast
_local_rebalance_costs_sats: int = 0
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

    # Check if saved state is from the current weekly period
    # (Aligned to Monday 00:00 UTC)
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    days_since_monday = dt.weekday()
    current_week_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    current_week_start = int(current_week_start.timestamp() - (days_since_monday * 86400))

    saved_period_start = saved.get("period_start_ts", 0)

    with _local_fees_lock:
        if saved_period_start >= current_week_start:
            # Same weekly period - restore the state
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
            # New weekly period - start fresh but log the old data
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




def _check_permission() -> Optional[Dict[str, Any]]:
    """
    Check if the local node is a hive member.

    Returns:
        None if permission granted, or error dict if denied
    """
    if not our_pubkey or not database:
        return {"error": "Not initialized"}

    member = database.get_member(our_pubkey)
    if not member:
        return {"error": "Not a Hive member"}

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
    _planner = planner if planner is not None else None
    _bridge = bridge if bridge is not None else None
    _intent_mgr = intent_mgr if intent_mgr is not None else None
    _membership_mgr = membership_mgr if membership_mgr is not None else None
    _contribution_mgr = contribution_mgr if contribution_mgr is not None else None
    _yield_metrics_mgr = yield_metrics_mgr if yield_metrics_mgr is not None else None
    _liquidity_coord = liquidity_coord if liquidity_coord is not None else None
    _fee_coordination_mgr = fee_coordination_mgr if fee_coordination_mgr is not None else None
    _rationalization_mgr = rationalization_mgr if rationalization_mgr is not None else None
    _strategic_positioning_mgr = strategic_positioning_mgr if strategic_positioning_mgr is not None else None

    # Create a log wrapper that calls plugin.log
    def _log(msg: str, level: str = 'info'):
        plugin.log(msg, level=level)

    return HiveContext(
        database=_database,
        config=_config,
        safe_plugin=plugin,  # Direct plugin access - pyln-client is thread-safe per-call
        our_pubkey=_our_pubkey,
        planner=_planner,
        quality_scorer=quality_scorer_mgr if quality_scorer_mgr is not None else None,
        bridge=_bridge,
        intent_mgr=_intent_mgr,
        membership_mgr=_membership_mgr,
        contribution_mgr=_contribution_mgr,
        yield_metrics_mgr=_yield_metrics_mgr,
        liquidity_coordinator=_liquidity_coord,
        fee_coordination_mgr=_fee_coordination_mgr,
        rationalization_mgr=_rationalization_mgr,
        strategic_positioning_mgr=_strategic_positioning_mgr,
        traffic_intel_mgr=traffic_intel_mgr,
        signing_backend="none",
        our_id=_our_pubkey or "",
        log=_log,
    )


# =============================================================================
# PLUGIN OPTIONS
# =============================================================================
# Options, config maps, and parsers moved to modules/plugin_options.py
register_options(plugin)


def _reload_config_from_cln(plugin_obj: Plugin) -> Dict[str, Any]:
    """
    Reload all hive config options from CLN's current values.

    Call this after using `lightning-cli setconfig` to sync the internal
    config object with CLN's option values.

    Returns dict with list of updated options and any errors.
    """
    global config

    results = {"updated": [], "errors": []}

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

    return results


# =============================================================================
# EXTERNAL TRANSPORT PUMP (Coordinated Mode)
# =============================================================================


def _submit_hive_message(peer_id: str, msg_type: HiveMessageType, msg_payload: Dict[str, Any], plugin_obj: Plugin) -> bool:
    """Apply common policy checks and dispatch a validated Hive message."""
    if not peer_id or msg_type is None or not isinstance(msg_payload, dict):
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
    global database, config, handshake_mgr, state_manager, gossip_mgr, intent_mgr, our_pubkey, bridge, relay_mgr

    plugin.log("cl-hive: Initializing Swarm Intelligence layer...")

    # Build configuration from options
    # Options removed in fleet simplification use dataclass defaults in HiveConfig.
    config = HiveConfig(
        db_path=options.get('hive-db-path', '~/.lightning/cl_hive.db'),
        max_members=int(options.get('hive-max-members', '50')),
        market_share_cap_pct=float(options.get('hive-market-share-cap', '0.20')),
        auto_join_enabled=_parse_bool(options.get('hive-auto-join', 'false')),
        intent_hold_seconds=int(options.get('hive-intent-hold-seconds', '60')),
        gossip_threshold_pct=float(options.get('hive-gossip-threshold', '0.10')),
        heartbeat_interval=int(options.get('hive-heartbeat-interval', '300')),
        planner_interval=int(options.get('hive-planner-interval', '3600')),
    )

    # Thread pool for message dispatch
    global _msg_executor, _batched_log_writer
    _msg_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="hive_msg")

    # Install batched log writer to prevent IO thread starvation.
    # Must be BEFORE any background loops start logging.
    _batched_log_writer = BatchedLogWriter(plugin)

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
        except Exception as e:
            plugin.log(f"cl-hive: relay send failed to {peer_id[:16]}...: {e}", level='debug')
            return False

    def _relay_get_members() -> list:
        """Get list of member pubkeys for relay (excludes banned)."""
        if not database:
            return []
        return [
            m["peer_id"] for m in database.get_all_members()
            if m.get("tier") == MEMBER_TIER
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
    
    if bridge_status == BridgeStatus.DEGRADED:
        plugin.log("cl-hive: Bridge DEGRADED - some features unavailable", level='warn')
    elif bridge_status == BridgeStatus.DISABLED:
        plugin.log(
            "cl-hive: Bridge DISABLED - cl-revenue-ops not detected or incompatible. "
            "Recommended: v1.4.0+",
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

    # Initialize local node presence for uptime tracking
    if our_pubkey:
        try:
            database.update_presence(our_pubkey, is_online=True, now_ts=int(time.time()), 
                                    window_seconds=30 * 86400)
            plugin.log(f"cl-hive: Initialized local node presence for uptime tracking")
        except Exception as e:
            plugin.log(f"cl-hive: Failed to initialize local presence: {e}", level="warn")
    
    # Sync uptime from presence data to hive_members on startup
    try:
        uptime_synced = database.sync_uptime_from_presence(window_seconds=30 * 86400)
        if uptime_synced > 0:
            plugin.log(f"cl-hive: Synced uptime for {uptime_synced} member(s)")
    except Exception as e:
        plugin.log(f"cl-hive: Failed to sync uptime: {e}", level="warn")

    # Initialize RecommendationLogger (simplified governance)
    global recommendation_logger
    recommendation_logger = RecommendationLogger(database=database, plugin=plugin)
    plugin.log("cl-hive: RecommendationLogger initialized")

    # Initialize Planner (Phase 6)
    global planner
    planner = Planner(
        state_manager=state_manager,
        database=database,
        bridge=bridge,
        plugin=plugin,
        intent_manager=intent_mgr,
        recommendation_logger=recommendation_logger
    )
    plugin.log("cl-hive: Planner initialized")

    # Planner loop thread (Phase 6)
    _deferred_threads.append(threading.Thread(
        target=background_loops.planner_loop,
        name="cl-hive-planner",
        daemon=True
    ))

    # Initialize Quality Scorer (kept for planner use)
    global quality_scorer_mgr
    quality_scorer = PeerQualityScorer(database, plugin)
    quality_scorer_mgr = quality_scorer

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

    # Load persisted fee tracking state
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

    # Link cooperation modules to Planner
    planner.set_cooperation_modules(
        liquidity_coordinator=liquidity_coord,
        health_aggregator=health_aggregator,
    )
    plugin.log("cl-hive: Planner linked to cooperation modules")

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

    # Initialize askrene layer manager (manages hive-fleet + hive-reputation layers)
    from modules.askrene_layers import AskreneLayerManager
    global askrene_layer_mgr
    askrene_layer_mgr = AskreneLayerManager(
        plugin=plugin,
        database=database,
        peer_reputation_mgr=peer_reputation_mgr,
    )
    plugin.log("cl-hive: askrene layer manager initialized")

    # Initialize Network Metrics Calculator (shared module)
    network_metrics.init_calculator(
        state_manager=state_manager,
        database=database,
        plugin=plugin
    )
    plugin.log("cl-hive: Network metrics calculator initialized")

    # Initialize Yield Metrics Manager (Phase 1 - Metrics & Measurement)
    global yield_metrics_mgr
    yield_metrics_mgr = YieldMetricsManager(
        database=database,
        plugin=plugin,
        state_manager=state_manager,
        bridge=bridge
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

    # Restore persisted routing intelligence (no-op after simplification)
    try:
        fee_coordination_mgr.restore_state_from_database()
    except Exception as e:
        plugin.log(f"cl-hive: Failed to restore routing intelligence: {e}", level='warn')

    # Initialize Rationalization Manager (Channel Rationalization)
    global rationalization_mgr
    rationalization_mgr = RationalizationManager(
        plugin=plugin,
        database=database,
        state_manager=state_manager,
        fee_coordination_mgr=fee_coordination_mgr,
        recommendation_logger=recommendation_logger
    )
    rationalization_mgr.set_our_pubkey(our_pubkey)
    plugin.log("cl-hive: Rationalization manager initialized")

    # Initialize Strategic Positioning Manager (Phase 5 - Strategic Positioning)
    global strategic_positioning_mgr
    strategic_positioning_mgr = StrategicPositioningManager(
        plugin=plugin,
        database=database,
        state_manager=state_manager,
        fee_coordination_mgr=fee_coordination_mgr,
        planner=planner
    )
    strategic_positioning_mgr.set_our_pubkey(our_pubkey)
    plugin.log("cl-hive: Strategic positioning manager initialized")

    # Initialize Traffic Intelligence Manager (Phase 14 - Traffic Intelligence)
    global traffic_intel_mgr
    traffic_intel_mgr = TrafficIntelligenceManager(
        database=database,
        plugin=plugin,
        our_pubkey=our_pubkey,
        liquidity_coordinator=liquidity_coord,
        membership_mgr=membership_mgr,
    )
    plugin.log("cl-hive: Traffic intelligence manager initialized")

    # Phase 3c: Wire traffic intelligence into fee coordination
    fee_coordination_mgr.set_traffic_intel_mgr(traffic_intel_mgr)

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

    # (Defense system removed during simplification)

    # Link yield optimization modules to Planner (Slime mold coordination)
    # These enable the planner to avoid redundant opens and prioritize high-value corridors
    planner.set_cooperation_modules(
        rationalization_mgr=rationalization_mgr,
        strategic_positioning_mgr=strategic_positioning_mgr
    )
    plugin.log("cl-hive: Planner linked to cooperation modules")

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
        'contribution_mgr': contribution_mgr,
        'bridge': bridge,
        'relay_mgr': relay_mgr,
        'fee_intel_mgr': fee_intel_mgr,
        'liquidity_coord': liquidity_coord,
        'peer_reputation_mgr': peer_reputation_mgr,
        'yield_metrics_mgr': yield_metrics_mgr,
        'rationalization_mgr': rationalization_mgr,
        'strategic_positioning_mgr': strategic_positioning_mgr,
        'outbox_mgr': outbox_mgr,
        'traffic_intel_mgr': traffic_intel_mgr,
        'outbox': outbox_mgr,
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
        'outbox_mgr': outbox_mgr,
        'handshake_mgr': handshake_mgr,
        'planner': planner,
        'fee_intel_mgr': fee_intel_mgr,
        'gossip_mgr': gossip_mgr,
        'peer_reputation_mgr': peer_reputation_mgr,
        'askrene_layer_mgr': askrene_layer_mgr,
        'yield_metrics_mgr': yield_metrics_mgr,
        'strategic_positioning_mgr': strategic_positioning_mgr,
        'rationalization_mgr': rationalization_mgr,
        'traffic_intel_mgr': traffic_intel_mgr,
        'liquidity_coord': liquidity_coord,
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

    # Broadcast membership to peers for consistency (Phase 5 enhancement)
    protocol_handlers._sync_membership_on_startup(plugin)

    # Set up graceful shutdown handler
    def handle_shutdown_signal(signum, frame):
        plugin.log("cl-hive: Received shutdown signal, cleaning up...")
        shutdown_event.set()
        try:
            if fee_coordination_mgr:
                fee_coordination_mgr.save_state_to_database()
        except Exception as e:
            plugin.log(f"cl-hive: shutdown fee_coordination save error: {e}", level='debug')
        try:
            if _msg_executor:
                _msg_executor.shutdown(wait=False, cancel_futures=True)
        except Exception as e:
            plugin.log(f"cl-hive: shutdown executor cleanup error: {e}", level='debug')
        try:
            if _batched_log_writer:
                _batched_log_writer.stop()
        except Exception as e:
            plugin.log(f"cl-hive: shutdown log writer cleanup error: {e}", level='debug')

    try:
        signal.signal(signal.SIGTERM, handle_shutdown_signal)
        signal.signal(signal.SIGINT, handle_shutdown_signal)
    except Exception as e:
        plugin.log(f"cl-hive: Could not set signal handlers: {e}", level='debug')

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
                plugin.log("cl-hive: HELLO message too large, skipping autodiscovery", level='warn')
                return

            plugin.rpc.call("sendcustommsg", {
                "node_id": peer_id,
                "msg": hello_msg.hex()
            })
            if handshake_mgr:
                handshake_mgr.record_hello_sent(peer_id)
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
        # Phase 1: Handshake
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
        # Membership
        elif msg_type == HiveMessageType.MEMBER_LEFT:
            protocol_handlers.handle_member_left(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.MEMBER_REMOVED:
            protocol_handlers.handle_member_removed(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.BAN:
            protocol_handlers.handle_ban(peer_id, msg_payload, plugin)
        # Fee Intelligence
        elif msg_type == HiveMessageType.FEE_INTELLIGENCE_SNAPSHOT:
            protocol_handlers.handle_fee_intelligence_snapshot(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.TRAFFIC_INTELLIGENCE_BATCH:
            protocol_handlers.handle_traffic_intelligence_batch(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.HEALTH_REPORT:
            protocol_handlers.handle_health_report(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.LIQUIDITY_NEED:
            protocol_handlers.handle_liquidity_need(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.LIQUIDITY_SNAPSHOT:
            protocol_handlers.handle_liquidity_snapshot(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.PEER_REPUTATION_SNAPSHOT:
            protocol_handlers.handle_peer_reputation_snapshot(peer_id, msg_payload, plugin)
        # Fleet-Wide Intelligence
        elif msg_type == HiveMessageType.YIELD_METRICS_BATCH:
            protocol_handlers.handle_yield_metrics_batch(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.CORRIDOR_VALUE_BATCH:
            protocol_handlers.handle_corridor_value_batch(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.POSITIONING_PROPOSAL:
            protocol_handlers.handle_positioning_proposal(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.COVERAGE_ANALYSIS_BATCH:
            protocol_handlers.handle_coverage_analysis_batch(peer_id, msg_payload, plugin)
        elif msg_type == HiveMessageType.CLOSE_PROPOSAL:
            protocol_handlers.handle_close_proposal(peer_id, msg_payload, plugin)
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


@plugin.method("hive-connect")
def hive_connect(plugin: Plugin, peer_id: str):
    """Connect to a peer (used during membership flow)."""
    rpc, err = _require_rpc(plugin)
    if err:
        return err
    if not peer_id:
        return {"error": "peer_id is required"}
    return rpc.connect(peer_id)


@plugin.method("hive-health")
def hive_health(plugin: Plugin):
    """Lightweight health check — no RPC, no lock, no DB."""
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - _start_time),
        "threads_alive": threading.active_count(),
    }
@plugin.method("hive-status")
def hive_status(plugin: Plugin):
    """
    Get current Hive status and membership info.

    Returns:
        Dict with hive state and member count.
    """
    return rpc_status(_get_hive_context())
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
        lightning-cli setconfig hive-planner-interval 1800
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

    Permission: Any member
    """
    return rpc_reinit_bridge(_get_hive_context())
@plugin.method("hive-members")
def hive_members(plugin: Plugin):
    """
    List all Hive members with their stats.

    Returns:
        List of member records with tier, contribution ratio, uptime, etc.
    """
    return rpc_members(_get_hive_context())
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

    result["action"] = "channel_closed"
    result["event_type"] = event_type
    result["message"] = f"Channel {channel_id} closed ({closer})"

    plugin.log(
        f"cl-hive: Channel {channel_id} closed by {closer} (pnl={net_pnl_sats} sats)",
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

    result["action"] = "channel_opened"
    result["is_hive_internal"] = is_hive_internal
    result["message"] = f"Channel {channel_id} opened ({opener})"

    plugin.log(
        f"cl-hive: Channel {channel_id} opened with {peer_id[:16]}... ({opener})",
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
    except Exception as e:
        plugin.log(f"cl-hive: listfunds failed in calculate-size: {e}", level='debug')
        onchain_balance = cfg.planner_default_channel_sats * 10  # Assume adequate

    # Get available budget (considering all constraints)
    daily_remaining = database.get_available_budget(cfg.daily_expansion_budget_sats)
    max_per_channel = int(cfg.daily_expansion_budget_sats * cfg.budget_max_per_channel_pct)
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
    budget_info = database.get_budget_summary(cfg.daily_expansion_budget_sats, days=1)

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
            "daily_budget_sats": cfg.daily_expansion_budget_sats,
            "spent_today_sats": budget_info.get('spent_sats', 0),
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
    # Permission check: member
    perm_error = _check_permission()
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

    Permission: Any member
    """
    # Permission check: member
    perm_error = _check_permission()
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

    Permission: Any member
    """
    # Permission check: member
    perm_error = _check_permission()
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

    Permission: Any member
    """
    # Permission check: member
    perm_error = _check_permission()
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

    Permission: Any member
    """
    # Permission check: member
    perm_error = _check_permission()
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

    Permission: Any member
    """
    # Permission check: member
    perm_error = _check_permission()
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

    Permission: Any member
    """
    # Permission check: member
    perm_error = _check_permission()
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

    Permission: Any member
    """
    # Permission check: member
    perm_error = _check_permission()
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

    Permission: Any member
    """
    # Permission check: member
    perm_error = _check_permission()
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

    Permission: Any member
    """
    # Permission check: member
    perm_error = _check_permission()
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

    Permission: Any member
    """
    # Permission check: member
    perm_error = _check_permission()
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

    Permission: Any member
    """
    # Permission check: member
    perm_error = _check_permission()
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

    Permission: Any member
    """
    # Permission check: member
    perm_error = _check_permission()
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

    Permission: Any member
    """
    # Permission check: member
    perm_error = _check_permission()
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

    Permission: Any member
    """
    # Permission check: member
    perm_error = _check_permission()
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


@plugin.method("hive-export-hints")
def hive_export_hints(plugin: Plugin, ttl_seconds: int = 900):
    """
    Export compact short-lived per-peer hints for trusted local consumers.

    cl-revenue-ops polls this locally and uses the hints as bounded soft
    biases in its own fee/rebalance logic. Read-only, no side effects.

    Args:
        ttl_seconds: Hint validity window (default: 900 = 15 minutes)

    Returns:
        Dict with generated_at, ttl_seconds, peer_count, and per-peer hints.

    Permission: Any member
    """
    # Permission check: member
    perm_error = _check_permission()
    if perm_error:
        return perm_error

    return rpc_export_hints(_get_hive_context(), ttl_seconds=ttl_seconds)


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
            "broadcast_version": gossip_state.get("version"),
            "last_broadcast_ago": gossip_state.get("last_broadcast_ago"),
            "heartbeat_interval": gossip_state.get("heartbeat_interval"),
        },
        "our_state": {
            "version": our_state.version if our_state else None,
            "capacity_sats": our_state.capacity_sats if our_state else 0,
            "available_sats": our_state.available_sats if our_state else 0
        },
        "peer_states": peer_versions
    }
@plugin.method("hive-ban")
def hive_ban(plugin: Plugin, peer_id: str, reason: str):
    """
    Ban a peer from the hive.

    Args:
        peer_id: Public key of the peer to ban
        reason: Reason for the ban

    Returns:
        Dict with ban status.

    Permission: Any member
    """
    perm_error = _check_permission()
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

    # Sign the ban reason
    now = int(time.time())
    ban_message = f"BAN:{peer_id}:{reason}:{now}"

    try:
        sig = plugin.rpc.signmessage(ban_message).get("zbase", "")
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

    joined_at_cutoff = int(member.get("joined_at") or 0)
    ban_payload = {
        "peer_id": peer_id,
        "reason": reason,
        "reporter": our_pubkey,
        "timestamp": now,
    }
    ban_payload["event_id"] = generate_event_id("BAN", ban_payload) or secrets.token_hex(16)
    ban_payload["signature"] = sig
    database.record_membership_tombstone(
        event_id=ban_payload["event_id"],
        peer_id=peer_id,
        event="banned",
        actor_peer_id=our_pubkey,
        reason=reason,
        timestamp=now,
        joined_at_cutoff=joined_at_cutoff,
    )

    # Remove member from roster after successful ban
    protocol_handlers.database = database
    protocol_handlers._execute_member_removal(peer_id, reason="banned")
    database.log_membership_event("banned", peer_id, actor_peer_id=our_pubkey, reason=reason)
    protocol_handlers._reliable_broadcast(HiveMessageType.BAN, ban_payload)

    plugin.log(f"cl-hive: Banned peer {peer_id[:16]}... reason: {reason}")

    return {
        "status": "banned",
        "peer_id": peer_id,
        "reason": reason,
        "reporter": our_pubkey,
        "expires_days": expires_days,
    }



@plugin.method("hive-leave")
def hive_leave(plugin: Plugin, reason: str = "voluntary"):
    """
    Voluntarily leave the hive.

    This removes you from the hive member list and notifies other members.
    Your fee policies will be reverted to dynamic.

    Restrictions:
    - The last member cannot leave (would make hive headless)

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

    # Check if we're the last member
    all_members = database.get_all_members()
    member_count = len(all_members)
    if member_count <= 1:
        return {
            "error": "cannot_leave",
            "message": "Cannot leave: you are the only member. The hive would become headless."
        }

    # Create signed leave message
    timestamp = int(time.time())
    canonical = f"hive:leave:{our_pubkey}:{timestamp}:{reason}"

    try:
        sig = plugin.rpc.signmessage(canonical).get("zbase", "")
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

    # Remove ourselves from the member list
    database.remove_member(our_pubkey)
    database.log_membership_event("left", our_pubkey, reason=reason)
    plugin.log(f"cl-hive: Left the hive: {reason}")

    return {
        "status": "left",
        "peer_id": our_pubkey,
        "reason": reason,
        "message": "You have left the hive."
    }


@plugin.method("hive-remove-member")
def hive_remove_member(plugin: Plugin, peer_id: str, reason: str = "maintenance", force: bool = False):
    """
    Remove a member from the hive (fleet maintenance).

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
    perm_error = _check_permission()
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
    joined_at_cutoff = int(member.get("joined_at") or 0)

    # Safety check: refuse removal when the peer still has active/open channels
    # unless the caller explicitly forces it. This prevents accidentally removing
    # active external peers (e.g. cyber-hornet) from Hive membership.
    try:
        lpc = plugin.rpc.call("listpeerchannels", {"id": peer_id})
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
        except Exception as e:
            plugin.log(f"cl-hive: alias lookup failed for {peer_id[:16]}...: {e}", level='debug')
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
    timestamp = int(time.time())
    removed_payload = {
        "peer_id": peer_id,
        "actor_peer_id": our_pubkey,
        "reason": reason,
        "timestamp": timestamp,
        "joined_at_cutoff": joined_at_cutoff,
    }
    removed_payload["event_id"] = generate_event_id("MEMBER_REMOVED", removed_payload) or secrets.token_hex(16)
    try:
        removed_payload["signature"] = plugin.rpc.signmessage(
            f"hive:remove:{our_pubkey}:{peer_id}:{timestamp}:{reason}"
        ).get("zbase", "")
    except Exception as e:
        return {"error": f"Failed to sign removal: {e}"}
    database.record_membership_tombstone(
        event_id=removed_payload["event_id"],
        peer_id=peer_id,
        event="removed",
        actor_peer_id=our_pubkey,
        reason=reason,
        timestamp=timestamp,
        joined_at_cutoff=joined_at_cutoff,
    )

    protocol_handlers.database = database
    protocol_handlers._execute_member_removal(peer_id, reason)
    protocol_handlers._reliable_broadcast(HiveMessageType.MEMBER_REMOVED, removed_payload)
    database.log_membership_event("removed", peer_id, actor_peer_id=our_pubkey, reason=reason)

    plugin.log(
        f"cl-hive: Removed member {peer_id[:16]}..."
        f"{' [FORCED]' if force and active_channel_states else ''}: {reason}"
    )

    return {
        "status": "removed",
        "peer_id": peer_id,
        "reason": reason,
        "forced": bool(force and active_channel_states),
        "message": f"Member removed. They can rejoin by sending HELLO and awaiting approval."
    }
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
    Get coordinated fee recommendation for a channel.

    Uses corridor ownership and centrality signals to recommend optimal fees
    while avoiding internal fleet competition.

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


@plugin.method("hive-egress-desaturation-bias")
def hive_egress_desaturation_bias(
    plugin: Plugin,
    channel_id: str = None,
    peer_id: str = None
):
    """
    Report whether a local non-hive exit competes with a saturated local
    hive-member egress and recommend a bounded surcharge.

    Args:
        channel_id: Optional channel ID to inspect
        peer_id: Optional peer ID to inspect

    Returns:
        Structured bias payload with match status and surcharge recommendation.
    """
    return rpc_egress_desaturation_bias(
        _get_hive_context(),
        channel_id=channel_id,
        peer_id=peer_id
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
    Record a routing outcome (no-op after simplification, kept for compatibility).

    Args:
        channel_id: Channel that routed the payment
        peer_id: Peer on this channel
        fee_ppm: Fee charged in ppm
        success: Whether routing succeeded
        amount_sats: Forwarded amount in satoshis
        source: Source peer (optional)
        destination: Destination peer (optional)

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
@plugin.method("hive-ban-candidates")
def hive_ban_candidates(plugin: Plugin, auto_propose: bool = False):
    """
    Get peers that should be considered for banning.

    Uses accumulated warnings from local threat detection and peer reputation
    reports from other hive members to identify problematic peers.

    Permission: Any member

    Args:
        auto_propose: Reserved for future use

    Returns:
        Dict with ban candidates and their severity scores.
    """
    # Defense system removed during simplification
    return {
        "ban_candidates": [],
        "count": 0,
        "auto_propose_enabled": auto_propose
    }
@plugin.method("hive-get-routing-intelligence")
def hive_get_routing_intelligence(plugin: Plugin, scid: str = None):
    """
    Get routing intelligence based on corridor assignments.

    Args:
        scid: Optional specific channel short_channel_id (unused, kept for compat).

    Returns:
        Dict with corridor assignment data.
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

# =============================================================================
# CHANNEL RATIONALIZATION RPC METHODS
# =============================================================================

@plugin.method("hive-coverage-analysis")
def hive_coverage_analysis(plugin: Plugin, peer_id: str = None):
    """
    Analyze fleet coverage for redundant channels.

    Shows which fleet members have channels to the same peers
    and determines ownership based on corridor assignments.

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

    Uses corridor assignments to determine which member "owns" each peer
    relationship. Recommends closes for members with redundant channels.

    Args:
        our_node_only: If True, only return recommendations for our node

    Returns:
        Dict with close recommendations sorted by urgency.
    """
    return rpc_close_recommendations(_get_hive_context(), our_node_only=our_node_only)
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
@plugin.method("hive-genesis")
def hive_genesis(plugin: Plugin, hive_id: str = None):
    """
    Initialize this node as the founding Member of a new Hive.

    This creates the first member record. Other nodes join by opening
    a channel and awaiting approval via hive-approve.

    Args:
        hive_id: Optional custom Hive identifier (auto-generated if not provided)

    Returns:
        Dict with genesis status
    """
    if not database or not plugin or not handshake_mgr:
        return {"error": "Hive not initialized"}

    existing_members = database.get_all_members()
    if existing_members:
        return {"error": "Genesis already performed. Use hive-reset to reinitialize."}

    try:
        result = handshake_mgr.genesis(hive_id)
        return result
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Genesis failed: {e}"}


@plugin.method("hive-repair-member")
def hive_repair_member(plugin: Plugin, peer_id: str):
    """
    Repair a member's integration state.

    Re-runs post-join setup for an existing member: populates metadata,
    captures addresses, updates presence, and triggers a state sync.
    Use this for members added during development or who didn't complete
    the full handshake flow.

    Args:
        peer_id: Public key of the member to repair

    Returns:
        Dict with repair actions taken.
    """
    if not database or not our_pubkey:
        return {"error": "Hive not initialized"}

    perm_error = _check_permission()
    if perm_error:
        return perm_error

    member = database.get_member(peer_id)
    if not member:
        return {"error": "peer_not_member", "peer_id": peer_id}

    actions = []

    # 1. Fix metadata (hive_id) if missing
    if not member.get("metadata"):
        # Get hive_id from our own or any member's metadata
        hive_id = "hive"
        for m in database.get_all_members():
            if m.get("metadata"):
                try:
                    import json
                    md = json.loads(m["metadata"])
                    hive_id = md.get("hive_id", "hive")
                    break
                except Exception:
                    pass
        try:
            import json
            database.update_member(peer_id, metadata=json.dumps({"hive_id": hive_id}))
            actions.append(f"metadata: set hive_id={hive_id}")
        except Exception as e:
            actions.append(f"metadata: failed ({e})")

    # 2. Capture addresses
    is_self = (peer_id == our_pubkey)
    if not member.get("addresses"):
        try:
            import json
            if is_self:
                # Get our own addresses from getinfo
                info = plugin.rpc.getinfo()
                addrs = [b.get("address", "") for b in info.get("binding", []) if b.get("address")]
                if not addrs:
                    addrs = info.get("address", [])
                    if isinstance(addrs, list):
                        addrs = [a.get("address", "") for a in addrs if isinstance(a, dict)]
            else:
                addrs = []
                peers_info = plugin.rpc.call("listpeers", {"id": peer_id})
                if peers_info and peers_info.get("peers"):
                    addrs = peers_info["peers"][0].get("netaddr", [])

            if addrs:
                database.update_member(peer_id, addresses=json.dumps(addrs))
                actions.append(f"addresses: captured {len(addrs)} addresses")
            else:
                actions.append("addresses: no addresses found")
        except Exception as e:
            actions.append(f"addresses: failed ({e})")

    # 3. Update presence tracking
    try:
        import time as _time
        if is_self:
            is_connected = True  # We're always connected to ourselves
        else:
            peers_info = plugin.rpc.call("listpeers", {"id": peer_id})
            is_connected = bool(
                peers_info and peers_info.get("peers")
                and peers_info["peers"][0].get("connected", False)
            )
        database.update_presence(peer_id, is_online=is_connected, now_ts=int(_time.time()), window_seconds=30 * 86400)
        database.update_member(peer_id, last_seen=int(_time.time()))
        actions.append(f"presence: updated (connected={is_connected})")
    except Exception as e:
        actions.append(f"presence: failed ({e})")

    # 4. Trigger state sync (skip for self — can't sendcustommsg to ourselves)
    if not is_self:
        try:
            state_hash_msg = protocol_handlers._create_signed_state_hash_msg()
            if state_hash_msg:
                plugin.rpc.call("sendcustommsg", {
                    "node_id": peer_id,
                    "msg": state_hash_msg.hex()
                })
                actions.append("sync: STATE_HASH sent")
            else:
                actions.append("sync: could not create STATE_HASH")
        except Exception as e:
            actions.append(f"sync: failed ({e})")
    else:
        actions.append("sync: skipped (self)")

    # 5. Broadcast full sync to all members
    try:
        protocol_handlers._broadcast_full_sync_to_members(plugin)
        actions.append("broadcast: FULL_SYNC sent to fleet")
    except Exception as e:
        actions.append(f"broadcast: failed ({e})")

    plugin.log(f"cl-hive: Repaired member {peer_id[:16]}...: {actions}")

    return {
        "status": "repaired",
        "peer_id": peer_id,
        "actions": actions,
    }


@plugin.method("hive-join")
def hive_join(plugin: Plugin, peer_id: str):
    """
    Request to join a hive by sending HELLO to an existing member.

    The target peer must be an existing hive member that you have a
    channel with. After sending HELLO, wait for the member to approve
    you via hive-approve.

    Args:
        peer_id: Public key of an existing hive member

    Returns:
        Dict with join request status.
    """
    if not peer_id:
        return {"error": "peer_id is required"}

    if not our_pubkey:
        return {"error": "Plugin not initialized"}

    # Check if we're already a member
    if database and database.get_member(our_pubkey):
        return {"error": "Already a hive member"}

    try:
        from modules.protocol import create_hello
        hello_msg = create_hello(our_pubkey)
        if hello_msg is None:
            return {"error": "Failed to create HELLO message"}

        plugin.rpc.call("sendcustommsg", {
            "node_id": peer_id,
            "msg": hello_msg.hex()
        })

        if handshake_mgr:
            handshake_mgr.record_hello_sent(peer_id)

        plugin.log(f"cl-hive: Sent HELLO to {peer_id[:16]}... (manual join request)")

        return {
            "status": "hello_sent",
            "peer_id": peer_id,
            "message": "HELLO sent. Wait for the member to run hive-approve."
        }
    except Exception as e:
        return {"error": f"Failed to send HELLO: {e}"}


@plugin.method("hive-approve")
def hive_approve(plugin: Plugin, peer_id: str):
    """
    Approve a pending join request.

    After a peer sends HELLO and is stored as pending, any member can
    approve them. This generates a challenge and sends it to the peer
    to complete the handshake.

    Args:
        peer_id: Public key of the peer to approve

    Returns:
        Dict with approval status
    """
    perm_error = _check_permission()
    if perm_error:
        return perm_error

    if not handshake_mgr or not database:
        return {"error": "Hive not initialized"}

    # Read pending request without consuming it until the challenge is delivered.
    request = handshake_mgr.get_pending_request(peer_id)
    if not request:
        return {"error": "no_pending_request", "peer_id": peer_id,
                "message": "No pending join request from this peer"}

    # Check if already a member
    if database.get_member(peer_id):
        return {"error": "already_member", "peer_id": peer_id}

    # Check if banned
    if database.is_banned(peer_id):
        return {"error": "peer_banned", "peer_id": peer_id}

    # Generate challenge and send it
    nonce = handshake_mgr.generate_challenge(peer_id, requirements=0, initial_tier='member')

    # Get Hive ID from metadata
    members = database.get_all_members()
    hive_id = "hive"
    for m in members:
        if m.get('metadata'):
            try:
                metadata = json.loads(m['metadata'])
                hive_id = metadata.get('hive_id', 'hive')
                break
            except (json.JSONDecodeError, TypeError):
                continue

    from modules.protocol import create_challenge
    challenge_msg = create_challenge(nonce, hive_id)

    try:
        plugin.rpc.call("sendcustommsg", {
            "node_id": peer_id,
            "msg": challenge_msg.hex()
        })
    except Exception as e:
        handshake_mgr.clear_challenge(peer_id)
        return {"error": f"Failed to send CHALLENGE: {e}"}

    handshake_mgr.pop_pending_request(peer_id)
    database.log_membership_event("approved", peer_id, actor_peer_id=our_pubkey)
    plugin.log(f"cl-hive: Approved {peer_id[:16]}..., CHALLENGE sent")

    return {
        "status": "challenge_sent",
        "peer_id": peer_id,
        "message": "CHALLENGE sent. Peer will complete attestation automatically."
    }


@plugin.method("hive-pending")
def hive_pending(plugin: Plugin):
    """
    List pending join requests awaiting approval.

    Returns:
        Dict with list of pending requests
    """
    perm_error = _check_permission()
    if perm_error:
        return perm_error

    if not handshake_mgr:
        return {"error": "Hive not initialized"}

    requests = handshake_mgr.get_pending_requests()
    return {
        "status": "ok",
        "pending_count": len(requests),
        "pending": requests,
    }


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


@plugin.method("hive-routing-intelligence-status")
def hive_routing_intelligence_status(plugin: Plugin):
    """
    Get current routing intelligence status (corridor assignments).
    """
    if not fee_coordination_mgr:
        return {"error": "Fee coordination manager not initialized"}

    return fee_coordination_mgr.get_coordination_status()
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
# MAIN
# =============================================================================

if __name__ == "__main__":
    plugin.run()
