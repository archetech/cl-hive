"""
background_loops - Background daemon loop functions for cl-hive.

This module contains all *_loop functions and their private helper functions
that run as background daemon threads.  These were extracted verbatim from
cl-hive.py during the monolith decomposition.

Dependencies are injected at startup via init_background_loops() to avoid
rewriting every function body during the extraction.
"""

import asyncio
import json
import secrets
import time
from typing import Dict, Optional, Any, List

from modules import protocol_handlers
from modules.protocol import (
    HiveMessageType, serialize,
    VOUCH_TTL_SECONDS,
    create_mcf_needs_batch,
)

# Phase 3b: MCF assignment defer tracking
_mcf_defer_counts: Dict[str, int] = {}
_MCF_MAX_DEFER_CYCLES = 3


def init_background_loops(deps: dict):
    """Inject dependency references into this module's namespace.

    Called once from cl-hive.py init() after all managers are created.
    Every key in *deps* becomes a module-level name so that the moved
    loop functions can reference the exact same variable names they
    always did.
    """
    globals().update(deps)


def did_maintenance_loop():
    """Background thread for DID credential maintenance."""
    # Wait for initialization
    shutdown_event.wait(60)

    last_rebroadcast = 0

    while not shutdown_event.is_set():
        try:
            if not did_credential_mgr or not database:
                shutdown_event.wait(60)
                continue

            now = int(time.time())

            # 1. Cleanup expired credentials
            did_credential_mgr.cleanup_expired()

            # 2. Refresh stale aggregation cache entries
            did_credential_mgr.refresh_stale_aggregations()

            # 3. Auto-issue hive:node credentials for peers we have data on
            did_credential_mgr.auto_issue_node_credentials(
                state_manager=state_manager,
                contribution_tracker=contribution_mgr,
                broadcast_fn=protocol_handlers._broadcast_to_members,
            )

            # 4. Rebroadcast our credentials periodically (every 4h)
            if now - last_rebroadcast >= did_credential_mgr.REBROADCAST_INTERVAL:
                did_credential_mgr.rebroadcast_own_credentials(
                    broadcast_fn=protocol_handlers._broadcast_to_members,
                )
                last_rebroadcast = now

        except Exception as e:
            plugin.log(f"cl-hive: did_maintenance_loop error: {e}", level='warn')

        shutdown_event.wait(1800)  # 30 min cycle


# =============================================================================
# PHASE 4: EXTENDED SETTLEMENT MESSAGE HANDLERS
# =============================================================================



# =============================================================================
# PHASE 4: ESCROW MAINTENANCE LOOP
# =============================================================================

def escrow_maintenance_loop():
    """
    Background thread for escrow maintenance.

    15-minute cycle: expire tickets, retry mint ops, prune secrets.
    """
    shutdown_event.wait(30)

    while not shutdown_event.is_set():
        try:
            if not cashu_escrow_mgr or not database:
                shutdown_event.wait(60)
                continue

            # 1. Cleanup expired tickets
            cashu_escrow_mgr.cleanup_expired_tickets()

            # 2. Retry pending mint operations
            cashu_escrow_mgr.retry_pending_operations()

            # 3. Prune old revealed secrets
            cashu_escrow_mgr.prune_old_secrets()

        except Exception as e:
            plugin.log(f"cl-hive: escrow_maintenance_loop error: {e}", level='warn')

        shutdown_event.wait(900)  # 15 min cycle


def marketplace_maintenance_loop():
    """Background maintenance for advisor marketplace state."""
    shutdown_event.wait(30)

    while not shutdown_event.is_set():
        try:
            if not marketplace_mgr or not database:
                shutdown_event.wait(60)
                continue

            marketplace_mgr.cleanup_stale_profiles()
            marketplace_mgr.evaluate_expired_trials()
            marketplace_mgr.check_contract_renewals()
            marketplace_mgr.republish_profile()
        except Exception as e:
            plugin.log(f"cl-hive: marketplace_maintenance_loop error: {e}", level='warn')

        shutdown_event.wait(3600)  # 1h cycle


def liquidity_maintenance_loop():
    """Background maintenance for liquidity leases/offers."""
    shutdown_event.wait(30)

    while not shutdown_event.is_set():
        try:
            if not liquidity_mgr or not database:
                shutdown_event.wait(60)
                continue

            liquidity_mgr.check_heartbeat_deadlines()
            liquidity_mgr.terminate_dead_leases()
            liquidity_mgr.expire_stale_offers()
            liquidity_mgr.republish_offers()
        except Exception as e:
            plugin.log(f"cl-hive: liquidity_maintenance_loop error: {e}", level='warn')

        shutdown_event.wait(600)  # 10 min cycle


def outbox_retry_loop():
    """
    Background thread for outbox message retry.

    Runs every 30 seconds to retry pending messages.
    Runs hourly cleanup of expired/terminal entries.
    """
    RETRY_INTERVAL = 30
    CLEANUP_INTERVAL = 3600
    last_cleanup = 0

    # Startup delay
    shutdown_event.wait(15)

    while not shutdown_event.is_set():
        try:
            if outbox_mgr:
                outbox_mgr.retry_pending()
                # Hourly cleanup
                now = time.time()
                if now - last_cleanup > CLEANUP_INTERVAL:
                    outbox_mgr.expire_and_cleanup()
                    last_cleanup = now
        except Exception as e:
            if plugin:
                plugin.log(f"Outbox retry error: {e}", level='warn')
        shutdown_event.wait(RETRY_INTERVAL)


def intent_monitor_loop():
    """
    Background thread that monitors pending intents and commits them.
    
    Runs every 5 seconds and:
    1. Checks for intents where hold period has elapsed
    2. Commits them if no abort signal was received
    3. Cleans up expired/stale intents
    """
    MONITOR_INTERVAL = 5  # seconds
    
    while not shutdown_event.is_set():
        try:
            if intent_mgr and database and config:
                process_ready_intents()
                intent_mgr.cleanup_expired_intents()
                intent_mgr.recover_stuck_intents(max_age_seconds=300)
        except Exception as e:
            if plugin:
                plugin.log(f"Intent monitor error: {e}", level='warn')
        
        # Wait for next iteration or shutdown
        shutdown_event.wait(MONITOR_INTERVAL)


def process_ready_intents():
    """
    Process intents that are ready to commit.

    An intent is ready if:
    - Status is 'pending'
    - Current time > timestamp + hold_seconds
    """
    if not intent_mgr or not database or not config:
        return

    # Use config snapshot to avoid reading mutable config mid-cycle
    cfg = config.snapshot()

    ready_intents = database.get_pending_intents_ready(cfg.intent_hold_seconds)

    for intent_row in ready_intents:
        intent_id = intent_row.get('id')
        intent_type = intent_row.get('intent_type')
        target = intent_row.get('target')

        # SECURITY (Issue #12): Check governance mode BEFORE committing
        # to prevent state inconsistency where intents are COMMITTED but never executed
        # In advisor mode, intents wait for AI/human approval
        # In failsafe mode, only emergency actions auto-execute (not intents)
        if cfg.governance_mode != "failsafe":
            if plugin:
                plugin.log(
                    f"cl-hive: Intent {intent_id} ready but not committing "
                    f"(mode={cfg.governance_mode})",
                    level='debug'
                )
            continue

        # Commit the intent (only in failsafe mode for backwards compatibility)
        if intent_mgr.commit_intent(intent_id):
            if plugin:
                plugin.log(f"cl-hive: Committed intent {intent_id}: {intent_type} -> {target[:16]}...")

            # Execute the action (callback registry)
            intent_mgr.execute_committed_intent(intent_row)


# =============================================================================
# PHASE 5: MEMBERSHIP MAINTENANCE LOOP
# =============================================================================

def _auto_connect_to_all_members() -> int:
    """
    Ensure we're connected to all hive members (Issue #38).

    Called periodically to maintain full mesh connectivity.

    Returns:
        Number of new connections established
    """
    if not database :
        return 0

    members = database.get_all_members()
    connected = 0

    for member in members:
        member_peer_id = member.get("peer_id")
        if not member_peer_id or member_peer_id == our_pubkey:
            continue
        # SECURITY: Do not auto-connect to banned peers
        if database.is_banned(member_peer_id):
            continue

        # Skip if already connected
        if protocol_handlers._is_peer_connected(member_peer_id):
            continue

        # Get addresses from database
        addresses = []
        addresses_json = member.get("addresses")
        if addresses_json:
            try:
                import json
                addresses = json.loads(addresses_json)
            except (json.JSONDecodeError, TypeError):
                pass

        if not addresses:
            continue

        # Try to connect
        if protocol_handlers._try_auto_connect(member_peer_id, addresses):
            connected += 1

    return connected


def membership_maintenance_loop():
    """
    Periodic pruning of membership-related data.

    Runs hourly to clean up:
    - Old contribution records (> 45 days)
    - Old vouches (> VOUCH_TTL)
    - Stale presence data
    - Old planner logs (> 30 days)
    - Expired/completed pending actions (> 7 days)
    - Auto-connect to disconnected hive members (Issue #38)
    """
    MAINTENANCE_INTERVAL = 3600  # seconds
    PRESENCE_WINDOW_SECONDS = 30 * 86400

    # X-01 FIX: Delay first run to let init() complete (avoid RPC lock contention)
    # The _auto_connect_to_all_members() call uses rpc.connect() which can block
    # for extended periods, causing RPC lock timeout for startup sync.
    STARTUP_DELAY_SECONDS = 30
    if not shutdown_event.wait(STARTUP_DELAY_SECONDS):
        if plugin:
            plugin.log("cl-hive: Membership maintenance starting after init delay", level='debug')

    while not shutdown_event.is_set():
        try:
            if database:
                # Phase 5: Membership data pruning
                database.prune_old_contributions(older_than_days=45)
                database.prune_old_vouches(older_than_seconds=VOUCH_TTL_SECONDS)
                database.prune_presence(window_seconds=PRESENCE_WINDOW_SECONDS)

                # Sync uptime from presence data to hive_members
                updated = database.sync_uptime_from_presence(window_seconds=PRESENCE_WINDOW_SECONDS)
                if updated > 0 and plugin:
                    plugin.log(f"Synced uptime for {updated} member(s)", level='debug')

                # Sync contribution ratios from ledger to hive_members (Issue #59)
                if membership_mgr:
                    members_list = database.get_all_members()
                    for m in members_list:
                        pid = m.get("peer_id")
                        if pid:
                            ratio = membership_mgr.calculate_contribution_ratio(pid)
                            database.update_member(pid, contribution_ratio=ratio)

                # Phase 9: Planner and governance data pruning
                database.cleanup_expired_actions()  # Mark expired as 'expired'
                database.prune_planner_logs(older_than_days=30)
                database.prune_old_actions(older_than_days=7)

                # Phase C: Proto events cleanup (30-day retention)
                database.cleanup_proto_events(max_age_seconds=30 * 86400)

                # Prune old peer events (180-day retention)
                database.prune_peer_events(older_than_days=180)

                # Prune old budget tracking (90-day retention)
                database.prune_budget_tracking(older_than_days=90)

                # Prune old flow samples (30-day retention)
                database.prune_old_flow_samples(days_to_keep=30)

                # Prune old pool revenue (90-day retention)
                database.cleanup_old_pool_revenue(days_to_keep=90)

                # Prune old pool contributions (keep 12 most recent periods)
                database.cleanup_old_pool_contributions(periods_to_keep=12)

                # Prune old pool distributions (365-day retention)
                database.cleanup_old_pool_distributions(days_to_keep=365)

                # Prune old settlement periods (fee_reports, pool data > 365 days)
                database.prune_old_settlement_periods(older_than_days=365)

                # Cleanup expired splice sessions (audit fix #21)
                if splice_mgr:
                    try:
                        splice_mgr.cleanup_expired_sessions()
                    except Exception as e:
                        if plugin:
                            plugin.log(f"cl-hive: splice session cleanup error: {e}", level='debug')

                # Prune old ban proposals and votes (180-day retention)
                database.prune_old_ban_data(older_than_days=180)

                # Issue #38: Auto-connect to hive members we're not connected to
                reconnected = _auto_connect_to_all_members()
                if reconnected > 0 and plugin:
                    plugin.log(f"Auto-connected to {reconnected} hive member(s)", level='info')

                # Auto-remove members whose node is no longer in the gossip
                # graph.  The gossip graph retains node announcements for ~2
                # weeks, so absence from the graph is a strong signal the node
                # is permanently gone.  This prevents ghost members from
                # polluting settlement calculations and hive policies.
                protocol_handlers._cleanup_ghost_members()

                # Sweep expired settlement_gaming ban proposals that may need quorum check.
                # These use reversed voting (non-participation = approve) so bans only
                # execute after the voting window expires, but nothing re-checks quorum
                # post-window unless we sweep here. Run this BEFORE generic expiry.
                try:
                    pending_proposals = database.get_pending_ban_proposals()
                    now_ts = int(time.time())
                    for prop in pending_proposals:
                        if prop.get("proposal_type") != "settlement_gaming":
                            continue
                        expires_at = prop.get("expires_at", 0)
                        if expires_at > 0 and expires_at < now_ts:
                            protocol_handlers._check_ban_quorum(prop["proposal_id"], prop, plugin)
                except Exception as sweep_err:
                    if plugin:
                        plugin.log(f"cl-hive: Settlement gaming ban sweep error: {sweep_err}", level='warn')

                # R5-M-7 fix: Expire all still-pending ban proposals past expires_at.
                # This runs after settlement_gaming sweep so those proposals can still
                # execute via reversed voting at the expiry boundary.
                try:
                    expired_count = database.cleanup_expired_ban_proposals(now=int(time.time()))
                    if expired_count > 0 and plugin:
                        plugin.log(f"cl-hive: Expired {expired_count} ban proposal(s)", level='info')
                except Exception as expire_err:
                    if plugin:
                        plugin.log(f"cl-hive: Ban proposal expiry sweep error: {expire_err}", level='warn')

        except Exception as e:
            if plugin:
                plugin.log(f"Membership maintenance error: {e}", level='warn')

        shutdown_event.wait(MAINTENANCE_INTERVAL)


# =============================================================================
# PHASE 6: PLANNER BACKGROUND LOOP
# =============================================================================

# Security: Hard minimum interval to prevent Intent Storms
PLANNER_MIN_INTERVAL_SECONDS = 300  # 5 minutes minimum

# Jitter range to prevent all Hive nodes waking simultaneously
PLANNER_JITTER_SECONDS = 300  # ±5 minutes


def planner_loop():
    """
    Background thread that runs Planner cycles for topology optimization.

    Runs periodically to:
    1. Detect saturated targets and record for native expansion control
    2. Release saturation flags when share drops below threshold
    3. (If enabled) Propose channel expansions to underserved targets

    Security:
    - Enforces hard minimum interval (300s) to prevent Intent Storms
    - Adds random jitter to prevent simultaneous wake-up across swarm
    - Respects shutdown_event for graceful termination
    """
    # X-01 FIX: Delay first cycle to let init() complete (avoid RPC lock contention)
    # The listchannels() call in _refresh_network_cache can hold the lock for seconds,
    # blocking startup sync's signmessage() call.
    PLANNER_STARTUP_DELAY_SECONDS = 45
    if not shutdown_event.wait(PLANNER_STARTUP_DELAY_SECONDS):
        if plugin:
            plugin.log("cl-hive: Planner starting after init delay", level='debug')

    first_run = True

    while not shutdown_event.is_set():
        try:
            if planner and config:
                # Take config snapshot at cycle start (determinism)
                cfg_snapshot = config.snapshot()
                run_id = secrets.token_hex(8)

                if plugin:
                    plugin.log(f"cl-hive: Planner cycle starting (run_id={run_id})")

                # Run the planner cycle
                decisions = planner.run_cycle(
                    cfg_snapshot,
                    shutdown_event=shutdown_event,
                    run_id=run_id
                )

                if plugin:
                    plugin.log(
                        f"cl-hive: Planner cycle complete: {len(decisions)} decisions"
                    )

                # Clean up expired expansion rounds
                if coop_expansion:
                    cleaned = coop_expansion.cleanup_expired_rounds()
                    if cleaned > 0 and plugin:
                        plugin.log(
                            f"cl-hive: Cleaned up {cleaned} expired expansion rounds"
                        )
        except Exception as e:
            if plugin:
                plugin.log(f"Planner loop error: {e}", level='warn')

        # Calculate next sleep interval
        if first_run:
            first_run = False

        if config:
            # SECURITY: Enforce hard minimum interval
            interval = max(config.planner_interval, PLANNER_MIN_INTERVAL_SECONDS)

            # Add random jitter (±5 minutes) to prevent synchronization
            jitter = secrets.randbelow(PLANNER_JITTER_SECONDS * 2) - PLANNER_JITTER_SECONDS
            sleep_time = interval + jitter
        else:
            sleep_time = 3600  # Default 1 hour if config unavailable

        # Wait for next cycle or shutdown
        shutdown_event.wait(sleep_time)


# =============================================================================
# PHASE 7: FEE INTELLIGENCE BACKGROUND LOOP
# =============================================================================

# Fee intelligence loop interval (1 hour default)
FEE_INTELLIGENCE_INTERVAL = 3600

# Health report broadcast interval (1 hour)
HEALTH_REPORT_INTERVAL = 3600

# Fee intelligence cleanup interval (keep 7 days)
FEE_INTELLIGENCE_MAX_AGE_HOURS = 168


def fee_intelligence_loop():
    """
    Background thread for cooperative fee coordination.

    Runs periodically to:
    1. Collect and broadcast our fee observations to hive members
    2. Aggregate received fee intelligence into peer profiles
    3. Broadcast our health report for NNLB coordination
    4. Clean up old fee intelligence records
    """
    # Wait for initialization
    shutdown_event.wait(60)

    while not shutdown_event.is_set():
        try:
            if not fee_intel_mgr or not database or not plugin or not our_pubkey:
                shutdown_event.wait(60)
                continue

            # Step 1: Collect and broadcast our fee intelligence
            _broadcast_our_fee_intelligence()

            # Step 2: Aggregate all received fee intelligence
            try:
                updated = fee_intel_mgr.aggregate_fee_profiles()
                if updated > 0:
                    plugin.log(
                        f"cl-hive: Aggregated {updated} peer fee profiles",
                        level='debug'
                    )
            except Exception as e:
                plugin.log(f"cl-hive: Fee aggregation error: {e}", level='warn')

            # Step 3: Broadcast our health report
            _broadcast_health_report()

            # Step 4: Cleanup old records
            try:
                deleted = database.cleanup_old_fee_intelligence(FEE_INTELLIGENCE_MAX_AGE_HOURS)
                if deleted > 0:
                    plugin.log(
                        f"cl-hive: Cleaned up {deleted} old fee intelligence records",
                        level='debug'
                    )
            except Exception as e:
                plugin.log(f"cl-hive: Fee intelligence cleanup error: {e}", level='warn')

            # Step 5: Broadcast liquidity needs
            # NOTE: Small delays (50ms) between broadcasts reduce RPC lock contention
            # and allow incoming RPC requests (e.g., hive-deposit-marker) to be processed
            _broadcast_liquidity_needs()
            shutdown_event.wait(0.05)  # Yield to allow other RPC processing

            # Step 5a: Broadcast stigmergic markers (Phase 13 - Fleet Learning)
            _broadcast_our_stigmergic_markers()
            shutdown_event.wait(0.05)

            # Step 5b: Broadcast pheromones (Phase 13 - Fleet Learning)
            _broadcast_our_pheromones()
            shutdown_event.wait(0.05)

            # Step 5c: Broadcast yield metrics (Phase 14 - Daily, only once per day)
            # Check if we've already broadcast today
            try:
                from datetime import datetime, timezone
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                last_yield_broadcast = getattr(_broadcast_our_yield_metrics, '_last_broadcast', None)
                if last_yield_broadcast != today:
                    _broadcast_our_yield_metrics()
                    _broadcast_our_yield_metrics._last_broadcast = today
                    shutdown_event.wait(0.05)
            except Exception as e:
                plugin.log(f"cl-hive: Yield metrics broadcast check error: {e}", level='debug')

            # Step 5d: Broadcast circular flow alerts (Phase 14 - Event-driven)
            _broadcast_circular_flow_alerts()
            shutdown_event.wait(0.05)

            # Step 5e: Broadcast temporal patterns (Phase 14 - Weekly)
            try:
                from datetime import datetime, timezone
                current_week = datetime.now(timezone.utc).strftime("%Y-W%W")
                last_temporal_broadcast = getattr(_broadcast_our_temporal_patterns, '_last_broadcast', None)
                if last_temporal_broadcast != current_week:
                    _broadcast_our_temporal_patterns()
                    _broadcast_our_temporal_patterns._last_broadcast = current_week
                    shutdown_event.wait(0.05)
            except Exception as e:
                plugin.log(f"cl-hive: Temporal patterns broadcast check error: {e}", level='debug')

            # Step 5f: Broadcast corridor values (Phase 14.2 - Weekly)
            try:
                from datetime import datetime, timezone
                current_week = datetime.now(timezone.utc).strftime("%Y-W%W")
                last_corridor_broadcast = getattr(_broadcast_our_corridor_values, '_last_broadcast', None)
                if last_corridor_broadcast != current_week:
                    _broadcast_our_corridor_values()
                    _broadcast_our_corridor_values._last_broadcast = current_week
                    shutdown_event.wait(0.05)
            except Exception as e:
                plugin.log(f"cl-hive: Corridor values broadcast check error: {e}", level='debug')

            # Step 5g: Broadcast positioning proposals (Phase 14.2 - Event-driven)
            _broadcast_our_positioning_proposals()
            shutdown_event.wait(0.05)

            # Step 5h: Broadcast Physarum recommendations (Phase 14.2 - Event-driven)
            _broadcast_our_physarum_recommendations()
            shutdown_event.wait(0.05)

            # Step 5i: Broadcast coverage analysis (Phase 14.2 - Weekly)
            try:
                from datetime import datetime, timezone
                current_week = datetime.now(timezone.utc).strftime("%Y-W%W")
                last_coverage_broadcast = getattr(_broadcast_our_coverage_analysis, '_last_broadcast', None)
                if last_coverage_broadcast != current_week:
                    _broadcast_our_coverage_analysis()
                    _broadcast_our_coverage_analysis._last_broadcast = current_week
                    shutdown_event.wait(0.05)
            except Exception as e:
                plugin.log(f"cl-hive: Coverage analysis broadcast check error: {e}", level='debug')

            # Step 5j: Broadcast close proposals (Phase 14.2 - Event-driven)
            _broadcast_our_close_proposals()
            shutdown_event.wait(0.05)

            # Step 5k: Broadcast traffic intelligence (Phase 14 - every 6 hours)
            try:
                from datetime import datetime, timezone
                now_ts = int(datetime.now(timezone.utc).timestamp())
                last_traffic_broadcast = getattr(_broadcast_our_traffic_intelligence, '_last_ts', 0)
                if now_ts - last_traffic_broadcast >= 6 * 3600:
                    _broadcast_our_traffic_intelligence()
                    _broadcast_our_traffic_intelligence._last_ts = now_ts
                    shutdown_event.wait(0.05)
            except Exception as e:
                plugin.log(f"cl-hive: Traffic intelligence broadcast check error: {e}", level='debug')

            # Step 6: Cleanup old liquidity needs
            try:
                deleted_needs = database.cleanup_old_liquidity_needs(max_age_hours=24)
                if deleted_needs > 0:
                    plugin.log(
                        f"cl-hive: Cleaned up {deleted_needs} old liquidity needs",
                        level='debug'
                    )
            except Exception as e:
                plugin.log(f"cl-hive: Liquidity needs cleanup error: {e}", level='warn')

            # Step 7: Cleanup old route probes
            try:
                if routing_map:
                    # Clean database
                    deleted_probes = database.cleanup_old_route_probes(max_age_hours=24)
                    if deleted_probes > 0:
                        plugin.log(
                            f"cl-hive: Cleaned up {deleted_probes} old route probes from database",
                            level='debug'
                        )
                    # Clean in-memory stats
                    cleaned_paths = routing_map.cleanup_stale_data()
                    if cleaned_paths > 0:
                        plugin.log(
                            f"cl-hive: Cleaned up {cleaned_paths} stale paths from routing map",
                            level='debug'
                        )
            except Exception as e:
                plugin.log(f"cl-hive: Route probe cleanup error: {e}", level='warn')

            # Step 8: Cleanup stale peer states (memory management)
            try:
                if state_manager:
                    cleaned_states = state_manager.cleanup_stale_states()
                    if cleaned_states > 0:
                        plugin.log(
                            f"cl-hive: Cleaned up {cleaned_states} stale peer states",
                            level='debug'
                        )
            except Exception as e:
                plugin.log(f"cl-hive: State cleanup error: {e}", level='warn')

            # Step 8a: Verify hive channel zero-fee policy (security check)
            try:
                if bridge and membership_mgr:
                    # Get all current hive members
                    members = membership_mgr.get_all_members()
                    violations = []
                    for member in members:
                        peer_id = member.get('peer_id')
                        if peer_id and peer_id != our_pubkey and not database.is_banned(peer_id):
                            is_valid, reason = bridge.verify_hive_channel_zero_fees(peer_id)
                            if not is_valid and reason not in ('no_channel', 'our_direction_not_found'):
                                violations.append((peer_id[:16], reason))
                    if violations:
                        plugin.log(
                            f"cl-hive: SECURITY WARNING - Hive channels with non-zero fees: {violations}",
                            level='warn'
                        )
            except Exception as e:
                plugin.log(f"cl-hive: Zero-fee verification error: {e}", level='debug')

            # Step 9: Cleanup old peer reputation (Phase 5 - Advanced Cooperation)
            try:
                if peer_reputation_mgr:
                    # Clean database
                    deleted_reps = database.cleanup_old_peer_reputation(max_age_hours=168)
                    if deleted_reps > 0:
                        plugin.log(
                            f"cl-hive: Cleaned up {deleted_reps} old peer reputation records",
                            level='debug'
                        )
                    # Clean in-memory aggregations
                    cleaned_reps = peer_reputation_mgr.cleanup_stale_data()
                    if cleaned_reps > 0:
                        plugin.log(
                            f"cl-hive: Cleaned up {cleaned_reps} stale peer reputations",
                            level='debug'
                        )
            except Exception as e:
                plugin.log(f"cl-hive: Peer reputation cleanup error: {e}", level='warn')

            # Step 10: Cleanup old remote pheromones (Phase 13 - Fleet Learning)
            try:
                if fee_coordination_mgr:
                    cleaned_pheromones = fee_coordination_mgr.adaptive_controller.cleanup_old_remote_pheromones(
                        max_age_hours=48
                    )
                    if cleaned_pheromones > 0:
                        plugin.log(
                            f"cl-hive: Cleaned up {cleaned_pheromones} old remote pheromones",
                            level='debug'
                        )
            except Exception as e:
                plugin.log(f"cl-hive: Remote pheromone cleanup error: {e}", level='warn')

            # Step 10a: Evaporate local pheromones (time-based decay for idle channels)
            try:
                if fee_coordination_mgr:
                    evaporated = fee_coordination_mgr.adaptive_controller.evaporate_all_pheromones()
                    if evaporated > 0:
                        plugin.log(
                            f"cl-hive: Applied time-based decay to {evaporated} channel pheromones",
                            level='debug'
                        )
            except Exception as e:
                plugin.log(f"cl-hive: Local pheromone evaporation error: {e}", level='warn')

            # Step 10b: Update velocity cache for adaptive evaporation
            try:
                if fee_coordination_mgr:
                    funds = plugin.rpc.listfunds()
                    for ch in funds.get("channels", []):
                        scid = ch.get("short_channel_id")
                        if not scid or ch.get("state") != "CHANNELD_NORMAL":
                            continue
                        amount_msat = ch.get("amount_msat", 0)
                        our_msat = ch.get("our_amount_msat", 0)
                        capacity = amount_msat if amount_msat > 0 else 1
                        balance_pct = our_msat / capacity
                        # Use balance deviation from 50% as proxy for velocity
                        # Channels far from 50% are experiencing directional flow
                        velocity = (balance_pct - 0.5) * 2  # -1 to +1 range
                        fee_coordination_mgr.adaptive_controller.update_velocity(scid, velocity)
            except Exception as e:
                plugin.log(f"cl-hive: Velocity cache update error: {e}", level='debug')

            # Step 10c: Save routing intelligence to database (every cycle, ~5 min)
            try:
                if fee_coordination_mgr:
                    saved = fee_coordination_mgr.save_state_to_database()
                    if any(saved.get(k, 0) > 0 for k in saved):
                        plugin.log(
                            f"cl-hive: Saved routing intelligence "
                            f"(pheromones={saved['pheromones']}, markers={saved['markers']}, "
                            f"defense_reports={saved.get('defense_reports', 0)}, "
                            f"defense_fees={saved.get('defense_fees', 0)}, "
                            f"remote_pheromones={saved.get('remote_pheromones', 0)}, "
                            f"fee_observations={saved.get('fee_observations', 0)})",
                            level='debug'
                        )
            except Exception as e:
                plugin.log(f"cl-hive: Failed to save routing intelligence: {e}", level='warn')

            # Step 11: Cleanup old remote yield metrics (Phase 14)
            try:
                if yield_metrics_mgr:
                    cleaned_yields = yield_metrics_mgr.cleanup_old_remote_yield_metrics(max_age_days=30)
                    if cleaned_yields > 0:
                        plugin.log(
                            f"cl-hive: Cleaned up {cleaned_yields} old remote yield metrics",
                            level='debug'
                        )
            except Exception as e:
                plugin.log(f"cl-hive: Remote yield metrics cleanup error: {e}", level='warn')

            # Step 12: Cleanup old remote temporal patterns (Phase 14)
            try:
                if anticipatory_liquidity_mgr:
                    cleaned_patterns = anticipatory_liquidity_mgr.cleanup_old_remote_patterns(max_age_days=14)
                    if cleaned_patterns > 0:
                        plugin.log(
                            f"cl-hive: Cleaned up {cleaned_patterns} old remote temporal patterns",
                            level='debug'
                        )
            except Exception as e:
                plugin.log(f"cl-hive: Remote temporal patterns cleanup error: {e}", level='warn')

            # Step 13: Cleanup old remote strategic positioning data (Phase 14.2)
            try:
                if strategic_positioning_mgr:
                    cleaned_positioning = strategic_positioning_mgr.cleanup_old_remote_data(max_age_days=7)
                    if cleaned_positioning > 0:
                        plugin.log(
                            f"cl-hive: Cleaned up {cleaned_positioning} old remote positioning data",
                            level='debug'
                        )
            except Exception as e:
                plugin.log(f"cl-hive: Remote positioning cleanup error: {e}", level='warn')

            # Step 14: Cleanup old remote rationalization data (Phase 14.2)
            try:
                if rationalization_mgr:
                    cleaned_rationalization = rationalization_mgr.cleanup_old_remote_data(max_age_days=7)
                    if cleaned_rationalization > 0:
                        plugin.log(
                            f"cl-hive: Cleaned up {cleaned_rationalization} old remote rationalization data",
                            level='debug'
                        )
            except Exception as e:
                plugin.log(f"cl-hive: Remote rationalization cleanup error: {e}", level='warn')

        except Exception as e:
            if plugin:
                plugin.log(f"cl-hive: Fee intelligence loop error: {e}", level='warn')

        # Wait for next cycle
        shutdown_event.wait(FEE_INTELLIGENCE_INTERVAL)


# =============================================================================
# PHASE 12: DISTRIBUTED SETTLEMENT BACKGROUND LOOP
# =============================================================================

# Settlement check interval (1 hour)
SETTLEMENT_CHECK_INTERVAL = 3600

# Settlement rebroadcast interval (4 hours) - Issue #49
# Pending proposals are rebroadcast to ensure members who missed the initial
# broadcast can still vote. Only the proposer rebroadcasts their own proposals.
SETTLEMENT_REBROADCAST_INTERVAL = 4 * 3600


def _auto_finalize_pool_backlog(routing_pool, settlement_mgr, database, plugin):
    """
    Process at most one unsettled routing-pool backlog period for this cycle.

    Returns the period handled, or None if nothing was eligible.
    """
    previous_period = settlement_mgr.get_previous_period()
    for period in database.get_pool_candidate_periods_up_to(previous_period):
        if database.get_pool_distributions(period):
            continue

        contributions = database.get_pool_contributions(period)
        total_revenue = database.get_pool_revenue(period=period).get("total_sats", 0)
        marker = database.get_pool_settlement_marker(period)
        if marker:
            marker_reason = marker.get("reason")
            should_reopen = total_revenue > 0
            if not should_reopen and marker_reason != "zero_total_revenue" and contributions:
                should_reopen = True

            if should_reopen:
                if not database.remove_pool_settlement_marker(period):
                    continue
            else:
                continue

        # Historical backlog periods must not fabricate shares from current state.
        if not contributions:
            if total_revenue == 0:
                database.mark_pool_period_cleared(period, "zero_total_revenue")
                return period
            return None

        if total_revenue == 0:
            database.mark_pool_period_cleared(period, "zero_total_revenue")
            return period

        routing_pool.settle_period(period)
        return period

    return None


def _process_pending_settlement_proposals_once(settlement_mgr, database, state_manager, plugin, our_pubkey):
    """Process pending settlement proposals once, logging verify failures with context."""
    pending = database.get_pending_settlement_proposals()
    for proposal in pending:
        proposal_id = proposal.get('proposal_id')
        member_count = proposal.get('member_count', 0)

        if not database.has_voted_settlement(proposal_id, our_pubkey):
            vote = settlement_mgr.verify_and_vote(
                proposal=proposal,
                our_peer_id=our_pubkey,
                state_manager=state_manager,
                rpc=plugin.rpc
            )
            if vote:
                from modules.protocol import create_settlement_ready
                vote_msg = create_settlement_ready(
                    proposal_id=vote['proposal_id'],
                    voter_peer_id=vote['voter_peer_id'],
                    data_hash=vote['data_hash'],
                    timestamp=vote['timestamp'],
                    signature=vote['signature']
                )
                protocol_handlers._broadcast_to_members(vote_msg)
            else:
                protocol_handlers._log_settlement_vote_skip_reason(
                    plugin,
                    proposal_id,
                    proposal.get("period"),
                    settlement_mgr,
                )

        settlement_mgr.check_quorum_and_mark_ready(proposal_id, member_count)


def settlement_loop():
    """
    Background thread for distributed settlement coordination.

    Runs hourly to:
    1. Check if we should propose settlement for previous week
    2. Rebroadcast pending proposals that haven't reached quorum (Issue #49)
    3. Process any pending proposals (auto-vote if hash matches)
    4. Execute any ready settlements we haven't paid yet
    5. Cleanup expired proposals
    """
    from modules.protocol import (
        create_settlement_propose,
        create_settlement_executed,
        get_settlement_propose_signing_payload,
        get_settlement_executed_signing_payload
    )

    # Wait for initialization (2 minutes)
    shutdown_event.wait(120)

    while not shutdown_event.is_set():
        try:
            if not settlement_mgr or not database or not state_manager or not plugin or not our_pubkey:
                shutdown_event.wait(60)
                continue

            # Step 0: Ensure routing-pool contribution snapshots exist for current
            # and previous settlement periods. This keeps hive-pool-status usable
            # without requiring manual hive-pool-snapshot calls.
            try:
                if routing_pool:
                    current_period = settlement_mgr.get_period_string()
                    previous_period = settlement_mgr.get_previous_period()
                    for period_to_snapshot in (current_period, previous_period):
                        existing = database.get_pool_contributions(period_to_snapshot)
                        if existing:
                            continue
                        snap = routing_pool.snapshot_contributions(period_to_snapshot)
                        if snap:
                            plugin.log(
                                f"SETTLEMENT: Auto-snapshotted routing pool for {period_to_snapshot} "
                                f"({len(snap)} members)",
                                level='info'
                            )
            except Exception as e:
                plugin.log(f"SETTLEMENT: Pool snapshot ensure error: {e}", level='warn')

            # Step 0.5: Auto-finalize at most one historical routing-pool period
            # before creating any new distributed settlement proposals.
            try:
                if routing_pool:
                    _auto_finalize_pool_backlog(
                        routing_pool=routing_pool,
                        settlement_mgr=settlement_mgr,
                        database=database,
                        plugin=plugin,
                    )
            except Exception as e:
                plugin.log(f"SETTLEMENT: Pool backlog finalize error: {e}", level='warn')

            # Step 1: Check if we should propose settlement for previous week
            try:
                # Need at least 2 members for distributed settlement proposals.
                # With a single-member hive (e.g. after decommissioning peers),
                # proposal generation should pause quietly instead of scanning
                # backlog periods every cycle.
                try:
                    member_count = len(database.get_all_members() or [])
                except Exception:
                    member_count = 0
                if member_count < 2:
                    plugin.log(
                        f"SETTLEMENT: Skipping proposal generation (member_count={member_count}, requires >=2)",
                        level='debug'
                    )
                    previous_period = None
                else:
                    previous_period = settlement_mgr.get_previous_period()

                # Backlog-first: propose the oldest eligible unsettled period up to
                # the previous week, not just the immediately previous week.
                target_period = None
                blocked_by_active_period = None
                candidate_periods = []
                if previous_period:
                    try:
                        candidate_periods = database.get_fee_report_periods_up_to(previous_period)
                    except Exception:
                        candidate_periods = [previous_period]
                    if previous_period not in candidate_periods:
                        candidate_periods = sorted(set(candidate_periods + [previous_period]))

                for period_candidate in candidate_periods:
                    if database.is_period_settled(period_candidate):
                        continue

                    existing = database.get_settlement_proposal_by_period(period_candidate)
                    if not existing:
                        target_period = period_candidate
                        break

                    status = (existing.get("status") or "").lower()
                    if status == "expired":
                        target_period = period_candidate
                        break

                    if status in ("pending", "ready"):
                        blocked_by_active_period = period_candidate
                        break

                    # Unknown/legacy statuses (including completed without a settled_period row)
                    # are treated as blocking to avoid duplicate settlement risk.
                    blocked_by_active_period = period_candidate
                    break

                if blocked_by_active_period:
                    plugin.log(
                        f"SETTLEMENT: Backlog-first proposal blocked by active {blocked_by_active_period} proposal",
                        level='debug'
                    )

                if target_period:
                    proposal = None
                    attempted_periods = [
                        p for p in candidate_periods
                        if p >= target_period and not database.is_period_settled(p)
                    ]
                    for attempt_idx, attempt_period in enumerate(attempted_periods):
                        existing_attempt = database.get_settlement_proposal_by_period(attempt_period)
                        if existing_attempt and (existing_attempt.get("status") or "").lower() not in ("expired",):
                            # Active/unknown proposal appeared since selection pass; stop backlog attempts.
                            if attempt_idx == 0:
                                plugin.log(
                                    f"SETTLEMENT: Backlog-first proposal blocked by active {attempt_period} proposal",
                                    level='debug'
                                )
                            break

                        if attempt_period != previous_period:
                            plugin.log(
                                f"SETTLEMENT: Backlog-first selecting oldest unsettled period "
                                f"{attempt_period} (latest eligible={previous_period})",
                                level='info'
                            )

                        proposal = settlement_mgr.create_proposal(
                            period=attempt_period,
                            our_peer_id=our_pubkey,
                            state_manager=state_manager,
                            rpc=plugin.rpc
                        )
                        if proposal:
                            break

                        skip_reason = getattr(settlement_mgr, "last_create_proposal_skip_reason", None)

                        # Periods with nothing to settle (zero fees or no contributions)
                        # should be marked as settled so the backlog scan doesn't retry
                        # them every cycle.
                        if skip_reason in ("zero_total_fees", "no_contributions"):
                            try:
                                database.mark_period_settled(
                                    attempt_period,
                                    proposal_id=f"auto-cleared-{skip_reason}-{attempt_period}",
                                    total_distributed_sats=0,
                                )
                                plugin.log(
                                    f"SETTLEMENT: Marked {attempt_period} as settled ({skip_reason}, nothing to distribute)",
                                    level='info'
                                )
                            except Exception as e:
                                plugin.log(
                                    f"SETTLEMENT: Failed to auto-settle {skip_reason} period {attempt_period}: {e}",
                                    level='warn'
                                )
                            continue

                        if attempt_idx + 1 < len(attempted_periods):
                            plugin.log(
                                f"SETTLEMENT: Could not create proposal for {attempt_period}; "
                                f"reason={skip_reason or 'unknown'}; trying next eligible unsettled period",
                                level='debug'
                            )

                    if proposal:
                            # Sign the outgoing proposal payload (binds to timestamp).
                            outgoing = {
                                "proposal_id": proposal["proposal_id"],
                                "period": proposal["period"],
                                "proposer_peer_id": proposal["proposer_peer_id"],
                                "data_hash": proposal["data_hash"],
                                "plan_hash": proposal["plan_hash"],
                                "total_fees_sats": proposal["total_fees_sats"],
                                "member_count": proposal["member_count"],
                                "timestamp": proposal["timestamp"],
                            }
                            signing_payload = get_settlement_propose_signing_payload(outgoing)
                            try:
                                sig_result = plugin.rpc.signmessage(signing_payload)
                                signature = sig_result.get('zbase', '')
                            except Exception as e:
                                plugin.log(f"SETTLEMENT: Failed to sign proposal: {e}", level='warn')
                                signature = ''

                            if signature:
                                # Create payload and broadcast via outbox for reliable delivery
                                propose_payload = {
                                    "proposal_id": proposal['proposal_id'],
                                    "period": proposal['period'],
                                    "proposer_peer_id": proposal['proposer_peer_id'],
                                    "data_hash": proposal['data_hash'],
                                    "plan_hash": proposal['plan_hash'],
                                    "total_fees_sats": proposal['total_fees_sats'],
                                    "member_count": proposal['member_count'],
                                    "contributions": proposal['contributions'],
                                    "timestamp": proposal['timestamp'],
                                    "signature": signature
                                }
                                protocol_handlers._reliable_broadcast(
                                    HiveMessageType.SETTLEMENT_PROPOSE,
                                    propose_payload,
                                    msg_id=proposal['proposal_id']
                                )
                                plugin.log(
                                    f"SETTLEMENT: Proposed settlement for {proposal['period']}"
                                )

                                # Vote on our own proposal (skip hash re-verification
                                # since we just computed the plan moments ago)
                                vote = settlement_mgr.verify_and_vote(
                                    proposal=proposal,
                                    our_peer_id=our_pubkey,
                                    state_manager=state_manager,
                                    rpc=plugin.rpc,
                                    skip_hash_verify=True,
                                )
                                if vote:
                                    from modules.protocol import create_settlement_ready
                                    vote_msg = create_settlement_ready(
                                        proposal_id=vote['proposal_id'],
                                        voter_peer_id=vote['voter_peer_id'],
                                        data_hash=vote['data_hash'],
                                        timestamp=vote['timestamp'],
                                        signature=vote['signature']
                                    )
                                    protocol_handlers._broadcast_to_members(vote_msg)
            except Exception as e:
                plugin.log(f"SETTLEMENT: Error proposing settlement: {e}", level='warn')

            # Step 2: Settlement rebroadcast is now handled by the outbox retry loop
            # (Phase D). The outbox entries created by _reliable_broadcast() in Step 1
            # are retried with exponential backoff (30s -> 1h cap, 24h expiry).
            # The old 4-hour rebroadcast block has been removed.

            # Step 3: Process pending proposals (vote if hash matches)
            try:
                _process_pending_settlement_proposals_once(
                    settlement_mgr=settlement_mgr,
                    database=database,
                    state_manager=state_manager,
                    plugin=plugin,
                    our_pubkey=our_pubkey,
                )
            except Exception as e:
                plugin.log(f"SETTLEMENT: Error processing pending: {e}", level='warn')

            # Step 4: Execute ready settlements
            try:
                # Governance gate: only auto-execute in failsafe mode.
                # In advisor mode, queue for human/AI approval.
                cfg = config.snapshot() if config else None
                governance_mode = getattr(cfg, 'governance_mode', 'advisor') if cfg else 'advisor'

                ready = database.get_ready_settlement_proposals()
                for proposal in ready:
                    proposal_id = proposal.get('proposal_id')

                    # Check if we've already executed
                    if database.has_executed_settlement(proposal_id, our_pubkey):
                        continue

                    # Use the proposal's canonical contributions snapshot for execution.
                    contributions_json = proposal.get("contributions_json")
                    if not contributions_json:
                        continue
                    try:
                        contributions = json.loads(contributions_json)
                    except Exception:
                        continue

                    if governance_mode != "failsafe":
                        # Queue settlement execution as a pending action for approval
                        database.add_pending_action(
                            action_type="settlement_execute",
                            target=proposal_id,
                            payload=json.dumps({
                                "proposal_id": proposal_id,
                                "period": proposal.get("period", ""),
                                "total_fees_sats": proposal.get("total_fees_sats", 0),
                                "member_count": proposal.get("member_count", 0),
                            }),
                            source="settlement_loop",
                        )
                        plugin.log(
                            f"SETTLEMENT: Queued execution of {proposal_id[:16]}... for approval (governance={governance_mode})",
                            level='info'
                        )
                        continue

                    # Execute our settlement (this is async but we run it sync here)
                    import asyncio
                    try:
                        loop = asyncio.new_event_loop()
                        try:
                            asyncio.set_event_loop(loop)
                            exec_result = loop.run_until_complete(
                                settlement_mgr.execute_our_settlement(
                                    proposal=proposal,
                                    contributions=contributions,
                                    our_peer_id=our_pubkey,
                                    rpc=plugin.rpc
                                )
                            )
                        finally:
                            loop.close()

                        if exec_result:
                            # Broadcast execution confirmation via reliable delivery
                            exec_payload = {
                                'proposal_id': exec_result['proposal_id'],
                                'executor_peer_id': exec_result['executor_peer_id'],
                                'timestamp': exec_result['timestamp'],
                                'signature': exec_result['signature'],
                                'plan_hash': exec_result.get('plan_hash', ''),
                                'total_sent_sats': exec_result.get('total_sent_sats', 0),
                                'payment_hash': exec_result.get('payment_hash', ''),
                                'amount_paid_sats': exec_result.get('amount_paid_sats', 0),
                            }
                            protocol_handlers._reliable_broadcast(
                                HiveMessageType.SETTLEMENT_EXECUTED,
                                exec_payload
                            )

                            # Check if settlement is complete
                            settlement_mgr.check_and_complete_settlement(proposal_id)

                    except Exception as e:
                        plugin.log(f"SETTLEMENT: Execution error: {e}", level='warn')
            except Exception as e:
                plugin.log(f"SETTLEMENT: Error executing ready: {e}", level='warn')

            # Step 5: Cleanup expired proposals
            try:
                expired_pending = database.cleanup_expired_settlement_proposals()
                expired_ready = database.cleanup_stale_ready_settlement_proposals(
                    stale_after_seconds=SETTLEMENT_READY_STALE_EXPIRY_GRACE_SECONDS
                )
                if expired_pending > 0 or expired_ready > 0:
                    plugin.log(
                        "SETTLEMENT: Cleaned up expired proposals "
                        f"(pending={expired_pending}, ready_stale={expired_ready})"
                    )
            except Exception as e:
                plugin.log(f"SETTLEMENT: Cleanup error: {e}", level='warn')

            # Step 6: Check for gaming behavior and auto-propose bans
            try:
                _check_settlement_gaming_and_propose_bans()
            except Exception as e:
                plugin.log(f"SETTLEMENT: Gaming check error: {e}", level='warn')

        except Exception as e:
            if plugin:
                plugin.log(f"SETTLEMENT: Loop error: {e}", level='warn')

        # Wait for next cycle
        shutdown_event.wait(SETTLEMENT_CHECK_INTERVAL)


# Settlement gaming detection thresholds
SETTLEMENT_GAMING_MIN_PERIODS = 3  # Minimum periods to analyze
SETTLEMENT_GAMING_LOW_VOTE_THRESHOLD = 30  # Below 30% vote rate = suspicious
SETTLEMENT_GAMING_LOW_EXEC_THRESHOLD = 30  # Below 30% execution rate = suspicious
SETTLEMENT_READY_STALE_EXPIRY_GRACE_SECONDS = 72 * 3600  # 72h grace for stuck ready proposals


def _check_settlement_gaming_and_propose_bans():
    """
    Check for settlement gaming behavior and propose bans for high-risk members.

    A member is considered high-risk if they:
    1. Have vote rate < 30% over at least 3 settlement periods
    2. Have execution rate < 30% over at least 3 settlement periods
    3. Consistently owe money (negative balance in settlements)

    This protects the hive from members who intentionally skip votes/payments
    to avoid paying their fair share.
    """
    if not database or not our_pubkey :
        return

    # Get recent settled periods
    settled = database.get_settled_periods(limit=10)
    period_count = len(settled)

    if period_count < SETTLEMENT_GAMING_MIN_PERIODS:
        # Not enough history to detect gaming
        return

    # Get all members
    all_members = database.get_all_members()

    for member in all_members:
        peer_id = member['peer_id']

        # Skip ourselves
        if peer_id == our_pubkey:
            continue

        # Skip ourselves is handled above; no tier is exempt from gaming detection

        # Calculate participation rates
        vote_count = 0
        exec_count = 0
        total_owed = 0

        for period in settled:
            proposal_id = period.get('proposal_id')

            if database.has_voted_settlement(proposal_id, peer_id):
                vote_count += 1

            if database.has_executed_settlement(proposal_id, peer_id):
                exec_count += 1
                # Check execution amount
                executions = database.get_settlement_executions(proposal_id)
                for ex in executions:
                    if ex.get('executor_peer_id') == peer_id:
                        amount = ex.get('amount_paid_sats', 0)
                        if amount > 0:
                            total_owed -= amount

        vote_rate = (vote_count / period_count) * 100 if period_count > 0 else 100

        # Gaming detection uses vote_rate only. Execution compliance is
        # enforced structurally: settlement won't complete without payer
        # execution.  Receivers submit 0-sat confirmations which would
        # inflate exec_rate, making it an unreliable gaming signal.
        is_low_vote = vote_rate < SETTLEMENT_GAMING_LOW_VOTE_THRESHOLD
        owes_money = total_owed < 0

        # HIGH RISK: Low vote participation AND owes money
        if is_low_vote and owes_money:
            # Check if there's already a pending ban proposal for this member
            existing = database.get_ban_proposal_for_target(peer_id)
            if existing and existing.get("status") == "pending":
                continue  # Already proposed

            # Propose ban
            reason = (
                f"Settlement gaming detected: vote_rate={vote_rate:.1f}% "
                f"over {period_count} periods "
                f"while owing {abs(total_owed)} sats. "
                f"Automatic proposal for repeated settlement evasion."
            )

            plugin.log(
                f"SETTLEMENT GAMING: Proposing ban for {peer_id[:16]}... "
                f"(vote={vote_rate:.1f}%, owed={total_owed})",
                level='warn'
            )

            # Create ban proposal
            _propose_settlement_gaming_ban(peer_id, reason)


def _propose_settlement_gaming_ban(target_peer_id: str, reason: str):
    """
    Propose a ban for settlement gaming behavior.

    This is called automatically when a member is detected gaming
    the settlement system. Uses the standard ban proposal flow.
    """
    if not database or not our_pubkey :
        return

    # Verify target is still a member
    target = database.get_member(target_peer_id)
    if not target:
        return

    # Generate proposal ID
    proposal_id = secrets.token_hex(16)
    timestamp = int(time.time())

    # Sign the proposal
    canonical = f"hive:ban_proposal:{proposal_id}:{target_peer_id}:{timestamp}:{reason[:500]}"
    try:
        sig = plugin.rpc.signmessage(canonical)["zbase"]
    except Exception as e:
        plugin.log(f"SETTLEMENT: Failed to sign gaming ban proposal: {e}", level='warn')
        return

    # Store locally - use 'settlement_gaming' proposal_type for reversed voting
    expires_at = timestamp + BAN_PROPOSAL_TTL_SECONDS
    database.create_ban_proposal(proposal_id, target_peer_id, our_pubkey,
                                 reason[:500], timestamp, expires_at,
                                 proposal_type='settlement_gaming')

    # Add our vote (proposer auto-votes approve)
    vote_canonical = f"hive:ban_vote:{proposal_id}:approve:{timestamp}"
    try:
        vote_sig = plugin.rpc.signmessage(vote_canonical).get("zbase", "")
    except Exception as e:
        plugin.log(f"SETTLEMENT: Failed to sign gaming ban vote: {e}", level='warn')
        return
    database.add_ban_vote(proposal_id, our_pubkey, "approve", timestamp, vote_sig)

    # Broadcast proposal
    # R5-H-3 fix: Include proposal_type so receivers can apply reversed voting logic
    proposal_payload = {
        "proposal_id": proposal_id,
        "target_peer_id": target_peer_id,
        "proposer_peer_id": our_pubkey,
        "reason": reason[:500],
        "timestamp": timestamp,
        "signature": sig,
        "proposal_type": "settlement_gaming",
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

    plugin.log(
        f"SETTLEMENT: Proposed ban for gaming member {target_peer_id[:16]}... "
        f"(proposal_id={proposal_id[:16]}...)",
        level='warn'
    )


def gossip_loop():
    """
    Background thread for gossiping node state to hive members.

    Runs periodically to:
    1. Calculate our hive channel capacity and available liquidity
    2. Gather our external peer topology
    3. Broadcast GOSSIP message to all hive members (threshold-based)

    This populates state_manager with capacity data needed for fair
    routing pool distribution (capacity-weighted shares).

    Heartbeat: Every 5 minutes (DEFAULT_HEARTBEAT_INTERVAL)
    """
    from modules.gossip import DEFAULT_HEARTBEAT_INTERVAL

    # Wait for initialization
    shutdown_event.wait(30)

    while not shutdown_event.is_set():
        try:
            if not gossip_mgr or not plugin or not database or not our_pubkey:
                shutdown_event.wait(60)
                continue

            # Step 1: Get our channel data
            try:
                funds = plugin.rpc.listfunds()
                channels = funds.get("channels", [])
            except Exception as e:
                plugin.log(f"cl-hive: gossip_loop listfunds error: {e}", level='warn')
                shutdown_event.wait(DEFAULT_HEARTBEAT_INTERVAL)
                continue

            # Get list of hive members
            members = database.get_all_members()
            member_ids = {m.get("peer_id") for m in members}

            # Step 2: Calculate hive capacity (channels with hive members)
            hive_capacity_sats = 0
            hive_available_sats = 0
            external_peers = []

            for ch in channels:
                if ch.get("state") != "CHANNELD_NORMAL":
                    continue

                peer_id = ch.get("peer_id")
                amount_msat = ch.get("amount_msat", 0)
                our_amount_msat = ch.get("our_amount_msat", 0)

                if peer_id in member_ids:
                    # Channel with hive member
                    hive_capacity_sats += amount_msat // 1000
                    hive_available_sats += our_amount_msat // 1000
                else:
                    # External peer - add to topology
                    if peer_id and peer_id not in external_peers:
                        external_peers.append(peer_id)

            # Step 3: Get current fee policy (simplified)
            fee_policy = {
                "base_fee": 0,
                "fee_rate": 0,
                "min_htlc": 0,
                "max_htlc": 0,
                "cltv_delta": 40
            }

            # Step 4: Check if we should broadcast (threshold-based)
            should_broadcast = gossip_mgr.should_broadcast(
                new_capacity=hive_capacity_sats,
                new_available=hive_available_sats,
                new_fee_policy=fee_policy,
                new_topology=external_peers,
                force_status=False
            )

            if should_broadcast:
                # Step 5: Create signed GOSSIP message (with addresses for auto-connect)
                our_addresses = protocol_handlers._get_our_addresses()
                boltz_activity = bridge.get_boltz_activity() if bridge else None
                gossip_msg = protocol_handlers._create_signed_gossip_msg(
                    capacity_sats=hive_capacity_sats,
                    available_sats=hive_available_sats,
                    fee_policy=fee_policy,
                    topology=external_peers,
                    addresses=our_addresses,
                    boltz_activity=boltz_activity
                )

                if gossip_msg:
                    result = protocol_handlers._broadcast_member_message(
                        message_bytes=gossip_msg,
                        reliability="direct",
                        failure_policy="best_effort",
                        log_label="gossip",
                    )
                    broadcast_count = result["sent"]

                    if broadcast_count > 0:
                        plugin.log(
                            f"cl-hive: Gossip broadcast (capacity={hive_capacity_sats}sats, "
                            f"available={hive_available_sats}sats, external_peers={len(external_peers)}, "
                            f"sent to {broadcast_count} members)",
                            level='debug'
                        )

        except Exception as e:
            if plugin:
                plugin.log(f"cl-hive: Gossip loop error: {e}", level='warn')

        # Wait for next cycle (5 minutes default)
        shutdown_event.wait(DEFAULT_HEARTBEAT_INTERVAL)


# =============================================================================
# PHASE 15: MCF OPTIMIZATION BACKGROUND LOOP
# =============================================================================

def mcf_optimization_loop():
    """Removed — MCF solver deleted."""
    return  # Module deleted

    # Wait for initialization
    shutdown_event.wait(60)

    while not shutdown_event.is_set():
        try:
            if not cost_reduction_mgr or not plugin or not database or not our_pubkey:
                shutdown_event.wait(60)
                continue

            if not cost_reduction_mgr._mcf_enabled:
                # MCF disabled, just wait
                shutdown_event.wait(MCF_CYCLE_INTERVAL)
                continue

            mcf_coord = cost_reduction_mgr._mcf_coordinator
            if not mcf_coord:
                shutdown_event.wait(MCF_CYCLE_INTERVAL)
                continue

            # Step 1: Check if we're coordinator
            if mcf_coord.is_coordinator():
                # Step 2: Run optimization cycle
                solution = mcf_coord.run_optimization_cycle()

                if solution and solution.assignments:
                    # Step 3: Broadcast solution to fleet
                    _broadcast_mcf_solution(solution)
            else:
                # Not coordinator - broadcast our needs to the coordinator
                _broadcast_mcf_needs()

            # Step 4: Check for assignments from received solution
            _process_mcf_assignments()

        except Exception as e:
            if plugin:
                plugin.log(f"cl-hive: MCF optimization loop error: {e}", level='warn')

        # Wait for next cycle (10 minutes)
        shutdown_event.wait(MCF_CYCLE_INTERVAL)


def _broadcast_mcf_solution(solution):
    """
    Broadcast MCF solution to all fleet members.

    Args:
        solution: MCFSolution to broadcast
    """
    from modules.protocol import create_mcf_solution_broadcast

    if not plugin or not database or not our_pubkey:
        return

    try:
        # Create signed solution broadcast message
        assignments_data = [a.to_dict() for a in solution.assignments]

        msg = create_mcf_solution_broadcast(
            assignments=assignments_data,
            total_flow_sats=solution.total_flow_sats,
            total_cost_sats=solution.total_cost_sats,
            unmet_demand_sats=solution.unmet_demand_sats,
            iterations=solution.iterations,
            rpc=plugin.rpc,
            our_pubkey=our_pubkey
        )

        if not msg:
            plugin.log("cl-hive: Failed to create MCF solution message", level='warn')
            return

        result = protocol_handlers._broadcast_member_message(
            message_bytes=msg,
            reliability="reliable",
            failure_policy="fail_closed",
            log_label="mcf_solution",
        )
        broadcast_count = result["queued"] or result["sent"]

        if not result["ok"]:
            plugin.log(
                f"cl-hive: MCF solution broadcast incomplete: {broadcast_count}/{result['attempted']} delivered",
                level='warn'
            )
            return

        if broadcast_count > 0:
            plugin.log(
                f"cl-hive: MCF solution broadcast to {broadcast_count} members "
                f"(flow={solution.total_flow_sats}sats, assignments={len(solution.assignments)})",
                level='info'
            )

    except Exception as e:
        plugin.log(f"cl-hive: MCF solution broadcast error: {e}", level='warn')


def _broadcast_mcf_needs():
    """
    Broadcast our liquidity needs to the MCF coordinator.

    Non-coordinator members call this to share their needs
    with the coordinator for inclusion in MCF optimization.
    """
    if not plugin or not liquidity_coord or not cost_reduction_mgr or not our_pubkey:
        return

    try:
        # Get coordinator
        coordinator_id = cost_reduction_mgr.get_current_mcf_coordinator()
        if not coordinator_id or coordinator_id == our_pubkey:
            # We are coordinator or no coordinator
            return

        # Get our needs
        needs = liquidity_coord.get_all_liquidity_needs_for_mcf()

        # Filter to just our own needs
        our_needs = [n for n in needs if n.get("member_id") == our_pubkey]

        if not our_needs:
            # No needs to broadcast
            return

        # Format needs for protocol
        needs_for_batch = []
        for need in our_needs:
            needs_for_batch.append({
                "need_type": need.get("need_type", "inbound"),
                "target_peer": need.get("target_peer", ""),
                "amount_sats": need.get("amount_sats", 0),
                "urgency": need.get("urgency", "medium"),
                "max_fee_ppm": need.get("max_fee_ppm", 1000),
            })

        # Create signed needs batch message
        msg = create_mcf_needs_batch(
            needs=needs_for_batch,
            rpc=plugin.rpc,
            our_pubkey=our_pubkey
        )

        if not msg:
            plugin.log("cl-hive: Failed to create MCF needs batch", level='debug')
            return

        # Send to coordinator
        try:
            plugin.rpc.sendcustommsg(
                node_id=coordinator_id,
                msg=msg.hex()
            )
            plugin.log(
                f"cl-hive: Sent {len(needs_for_batch)} MCF need(s) to coordinator",
                level='debug'
            )
        except Exception as e:
            plugin.log(
                f"cl-hive: Failed to send MCF needs to coordinator: {e}",
                level='debug'
            )

    except Exception as e:
        plugin.log(f"cl-hive: MCF needs broadcast error: {e}", level='debug')


def _process_mcf_assignments():
    """
    Process pending MCF assignments for our node.

    Phase 3b: Before ACK, checks traffic intelligence for peak-hour
    conflicts and active fleet rebalancing. Defers up to 3 cycles
    (~90 minutes), then executes regardless.
    """
    global _mcf_defer_counts

    if not liquidity_coord or not cost_reduction_mgr:
        return

    try:
        status = liquidity_coord.get_mcf_status()
        counts = status.get("assignment_counts", {})

        pending_count = counts.get("pending", 0)
        executing_count = counts.get("executing", 0)
        completed_count = counts.get("completed", 0)
        failed_count = counts.get("failed", 0)

        # Fetch pending assignments once (reused by traffic check and ACK)
        pending = None
        if pending_count > 0:
            pending = liquidity_coord.get_pending_mcf_assignments()

        # Phase 3b: Check traffic intelligence before ACK
        if pending and traffic_intel_mgr:
            active_ids = set()
            for assignment in pending:
                peer_id = getattr(assignment, 'to_channel', '')
                assign_id = getattr(assignment, 'assignment_id', str(id(assignment)))
                active_ids.add(assign_id)

                # Check fleet rebalancing conflict and peak hours
                try:
                    conflict_info = traffic_intel_mgr.check_rebalance_conflict(
                        peer_id=peer_id,
                        direction="outbound",
                        amount_sats=getattr(assignment, 'amount_sats', 0),
                    )
                except Exception:
                    conflict_info = {}

                # Active conflict — skip entirely (another member rebalancing)
                if conflict_info.get("conflict"):
                    member = conflict_info.get("conflicting_member", "unknown")
                    plugin.log(
                        f"cl-hive: MCF assignment {assign_id[:12]}... skipped — "
                        f"conflict with {str(member)[:12]}...",
                        level='info'
                    )
                    continue

                # Peak hours — defer up to max_defer_cycles
                defer_count = _mcf_defer_counts.get(assign_id, 0)
                if conflict_info.get("peer_in_peak_hours") and defer_count < _MCF_MAX_DEFER_CYCLES:
                    _mcf_defer_counts[assign_id] = defer_count + 1
                    window = conflict_info.get("suggested_window_utc")
                    plugin.log(
                        f"cl-hive: MCF assignment {assign_id[:12]}... deferred "
                        f"(peer in peak hours, defer {defer_count + 1}/{_MCF_MAX_DEFER_CYCLES})"
                        f"{f', suggested window: {window}' if window else ''}",
                        level='info'
                    )
                    continue

                # Clear defer count on execution
                _mcf_defer_counts.pop(assign_id, None)

            # Prune stale defer entries for assignments no longer pending
            stale_ids = [k for k in _mcf_defer_counts if k not in active_ids]
            for k in stale_ids:
                _mcf_defer_counts.pop(k, None)

        # Send ACK if we have pending assignments and haven't ACKed yet
        if pending and not status.get("ack_sent", False):
            if pending:
                solution_timestamp = pending[0].solution_timestamp
                ack_msg = liquidity_coord.create_mcf_ack_message()
                if ack_msg:
                    _broadcast_mcf_ack(ack_msg)

        # Log status periodically
        if pending_count > 0 or executing_count > 0:
            plugin.log(
                f"cl-hive: MCF assignments - pending={pending_count}, "
                f"executing={executing_count}, completed={completed_count}, "
                f"failed={failed_count}",
                level='debug'
            )

        _check_stuck_mcf_assignments()

    except Exception as e:
        plugin.log(f"cl-hive: MCF assignment processing error: {e}", level='debug')


def _check_stuck_mcf_assignments():
    """Check for and handle assignments stuck in 'executing' state."""
    if not liquidity_coord:
        return

    timed_out = liquidity_coord.timeout_stuck_assignments(max_execution_time=1800)
    if timed_out:
        plugin.log(
            f"cl-hive: Timed out {len(timed_out)} stuck MCF assignments",
            level='warn'
        )


def _broadcast_mcf_ack(ack_msg: bytes):
    """Broadcast MCF assignment ACK to coordinator."""
    if not cost_reduction_mgr or not cost_reduction_mgr._mcf_coordinator:
        return

    coordinator_id = cost_reduction_mgr._mcf_coordinator.elect_coordinator()

    if coordinator_id == our_pubkey:
        return  # We're coordinator, no need to ACK ourselves

    try:
        plugin.rpc.sendcustommsg(
            node_id=coordinator_id,
            msg=ack_msg.hex()
        )
        plugin.log(
            f"cl-hive: MCF ACK sent to coordinator {coordinator_id[:16]}...",
            level='debug'
        )
    except Exception as e:
        plugin.log(f"cl-hive: Failed to send MCF ACK: {e}", level='debug')


def _broadcast_our_fee_intelligence():
    """
    Collect fee observations from our channels and broadcast to hive.

    Gathers fee and performance data for each external peer we have
    channels with and broadcasts a single FEE_INTELLIGENCE_SNAPSHOT message
    containing all peer observations.
    """
    if not fee_intel_mgr or not plugin or not database or not our_pubkey:
        return

    try:
        # Get our channels
        funds = plugin.rpc.listfunds()
        channels = funds.get("channels", [])

        # Get list of hive members (to exclude from external peer reporting)
        members = database.get_all_members()
        member_ids = {m.get("peer_id") for m in members}

        # Build fee map from listpeerchannels for actual fee rates
        try:
            peer_channels = plugin.rpc.listpeerchannels()
            fee_map = {}
            for pc in peer_channels.get("channels", []):
                scid = pc.get("short_channel_id")
                updates = pc.get("updates", {})
                local = updates.get("local", {})
                if scid and local:
                    fee_map[scid] = local.get("fee_proportional_millionths", 100)
        except Exception:
            fee_map = {}

        # Get forwarding stats if available
        try:
            forwards = plugin.rpc.listforwards(status="settled")
            forwards_list = forwards.get("forwards", [])
        except Exception:
            forwards_list = []

        # Build forward stats by peer
        peer_forwards = {}
        seven_days_ago = int(time.time()) - (7 * 24 * 3600)
        for fwd in forwards_list:
            # Filter to last 7 days
            received_time = fwd.get("received_time", 0)
            if received_time < seven_days_ago:
                continue

            out_channel = fwd.get("out_channel")
            if out_channel:
                if out_channel not in peer_forwards:
                    peer_forwards[out_channel] = {
                        "count": 0,
                        "volume_msat": 0,
                        "fee_msat": 0
                    }
                peer_forwards[out_channel]["count"] += 1
                peer_forwards[out_channel]["volume_msat"] += fwd.get("out_msat", 0)
                peer_forwards[out_channel]["fee_msat"] += fwd.get("fee_msat", 0)

        # Collect fee intelligence for each external peer into a list
        peers_data = []
        for channel in channels:
            if channel.get("state") != "CHANNELD_NORMAL":
                continue

            peer_id = channel.get("peer_id")
            if not peer_id or peer_id in member_ids:
                # Skip hive members - only report on external peers
                continue

            short_channel_id = channel.get("short_channel_id")
            if not short_channel_id:
                continue

            # Get channel capacity and balance
            amount_msat = channel.get("amount_msat", 0)
            our_amount_msat = channel.get("our_amount_msat", 0)
            capacity_sats = amount_msat // 1000
            available_sats = our_amount_msat // 1000

            if capacity_sats == 0:
                continue

            utilization_pct = available_sats / capacity_sats if capacity_sats > 0 else 0

            # Determine flow direction based on balance
            if utilization_pct > 0.7:
                flow_direction = "source"  # We have excess, liquidity flows out
            elif utilization_pct < 0.3:
                flow_direction = "sink"  # We need liquidity, flows in
            else:
                flow_direction = "balanced"

            # Get forward stats for this channel
            stats = peer_forwards.get(short_channel_id, {})
            forward_count = stats.get("count", 0)
            forward_volume_sats = stats.get("volume_msat", 0) // 1000
            revenue_sats = stats.get("fee_msat", 0) // 1000

            # Get actual fee rate for this channel from listpeerchannels data
            our_fee_ppm = fee_map.get(short_channel_id, 100)

            # Add peer data to snapshot list
            peers_data.append({
                "peer_id": peer_id,
                "our_fee_ppm": our_fee_ppm,
                "their_fee_ppm": 0,  # Would need to look up
                "forward_count": forward_count,
                "forward_volume_sats": forward_volume_sats,
                "revenue_sats": revenue_sats,
                "flow_direction": flow_direction,
                "utilization_pct": round(utilization_pct, 4),
                "days_observed": 7
            })

        if not peers_data:
            return

        # Create single snapshot message with all peer data
        try:
            msg = fee_intel_mgr.create_fee_intelligence_snapshot_message(
                peers=peers_data,
                rpc=plugin.rpc
            )

            if msg:
                result = protocol_handlers._broadcast_member_message(
                    message_bytes=msg,
                    reliability="direct",
                    failure_policy="best_effort",
                    log_label="fee_intelligence",
                )
                broadcast_count = result["sent"]

                if broadcast_count > 0:
                    plugin.log(
                        f"cl-hive: Broadcast fee intelligence snapshot "
                        f"({len(peers_data)} peers to {broadcast_count} members)",
                        level='debug'
                    )

        except Exception as e:
            plugin.log(
                f"cl-hive: Failed to create fee intelligence snapshot: {e}",
                level='debug'
            )

    except Exception as e:
        if plugin:
            plugin.log(f"cl-hive: Fee intelligence broadcast error: {e}", level='warn')


def _broadcast_our_traffic_intelligence():
    """
    Broadcast our traffic intelligence profiles to the fleet.

    Called every 6 hours by the intelligence broadcast loop.
    Collects locally-stored traffic profiles and sends a
    TRAFFIC_INTELLIGENCE_BATCH message.
    """
    if not traffic_intel_mgr or not plugin or not outbox_mgr:
        return

    try:
        msg = traffic_intel_mgr.create_traffic_intelligence_batch_message(plugin.rpc)
        if msg:
            result = protocol_handlers._broadcast_member_message(
                message_bytes=msg,
                reliability="direct",
                failure_policy="best_effort",
                log_label="traffic_intelligence",
            )
            if result["sent"] > 0:
                plugin.log("cl-hive: Broadcast traffic intelligence to fleet", level='debug')
    except Exception as e:
        plugin.log(f"cl-hive: Traffic intelligence broadcast error: {e}", level='warn')


def _broadcast_our_stigmergic_markers():
    """
    Broadcast our stigmergic markers to hive members for fleet-wide learning.

    Stigmergic markers are signals left after routing attempts that encode
    success/failure, fee levels, and volume. Sharing these enables the fleet
    to learn from each other's routing outcomes without direct coordination.
    """
    if not fee_coordination_mgr or not plugin or not database or not our_pubkey:
        return

    try:
        from modules.protocol import (
            get_stigmergic_marker_batch_signing_payload,
            MIN_MARKER_STRENGTH,
            MAX_MARKER_AGE_HOURS,
            MAX_MARKERS_IN_BATCH
        )

        # Get shareable markers from our stigmergic coordinator
        shareable_markers = fee_coordination_mgr.stigmergic_coord.get_shareable_markers(
            our_pubkey=our_pubkey,
            min_strength=MIN_MARKER_STRENGTH,
            max_age_hours=MAX_MARKER_AGE_HOURS,
            max_markers=MAX_MARKERS_IN_BATCH
        )

        if not shareable_markers:
            return

        # Build payload and sign it
        timestamp = int(time.time())
        payload = {
            "reporter_id": our_pubkey,
            "timestamp": timestamp,
            "markers": shareable_markers
        }

        signing_payload = get_stigmergic_marker_batch_signing_payload(payload)
        try:
            sig_result = plugin.rpc.signmessage(signing_payload)
            signature = sig_result["zbase"]
        except Exception as e:
            plugin.log(f"cl-hive: Failed to sign stigmergic marker batch: {e}", level='warn')
            return

        payload["signature"] = signature
        broadcast_payload = protocol_handlers._prepare_broadcast_payload(dict(payload))
        msg = serialize(HiveMessageType.STIGMERGIC_MARKER_BATCH, broadcast_payload)

        if not msg:
            return

        result = protocol_handlers._broadcast_member_message(
            message_bytes=msg,
            reliability="direct",
            failure_policy="best_effort",
            log_label="stigmergic_markers",
        )
        broadcast_count = result["sent"]

        if broadcast_count > 0:
            plugin.log(
                f"cl-hive: Broadcast {len(shareable_markers)} stigmergic markers "
                f"to {broadcast_count} members",
                level='debug'
            )

    except Exception as e:
        if plugin:
            plugin.log(f"cl-hive: Stigmergic marker broadcast error: {e}", level='warn')


def _broadcast_our_pheromones():
    """
    Broadcast our pheromone levels to hive members for fleet-wide learning.

    Pheromones are the "memory" of successful fee levels for specific channels/peers.
    Sharing these enables the fleet to learn from each other's fee experiments
    without direct coordination.
    """
    if not fee_coordination_mgr or not plugin or not database or not our_pubkey:
        return

    try:
        from modules.protocol import (
            get_pheromone_batch_signing_payload,
            MIN_PHEROMONE_LEVEL,
            MAX_PHEROMONES_IN_BATCH
        )

        # Get our channels and update the channel-to-peer mapping
        funds = plugin.rpc.listfunds()
        channels = funds.get("channels", [])

        # Update channel-to-peer mappings in the adaptive controller
        channel_infos = []
        for ch in channels:
            if ch.get("state") == "CHANNELD_NORMAL":
                channel_infos.append({
                    "short_channel_id": ch.get("short_channel_id"),
                    "peer_id": ch.get("peer_id")
                })
        fee_coordination_mgr.adaptive_controller.update_channel_peer_mappings(channel_infos)
        if anticipatory_liquidity_mgr:
            anticipatory_liquidity_mgr.update_channel_peer_mappings(channel_infos)

        # Get hive member IDs to exclude from sharing
        members = database.get_all_members()
        member_ids = {m.get("peer_id") for m in members}

        # Get shareable pheromones (excluding hive members)
        shareable_pheromones = fee_coordination_mgr.adaptive_controller.get_shareable_pheromones(
            min_level=MIN_PHEROMONE_LEVEL,
            max_pheromones=MAX_PHEROMONES_IN_BATCH,
            exclude_peer_ids=member_ids
        )

        if not shareable_pheromones:
            return

        timestamp = int(time.time())
        payload = {
            "reporter_id": our_pubkey,
            "timestamp": timestamp,
            "signature": "",
            "pheromones": shareable_pheromones,
        }

        try:
            signing_payload = get_pheromone_batch_signing_payload(payload)
            sig_result = plugin.rpc.signmessage(signing_payload)
            payload["signature"] = sig_result.get("signature", sig_result.get("zbase", ""))
        except Exception as e:
            plugin.log(f"cl-hive: Failed to sign pheromone batch: {e}", level='warn')
            return

        broadcast_payload = protocol_handlers._prepare_broadcast_payload(dict(payload))
        msg = serialize(HiveMessageType.PHEROMONE_BATCH, broadcast_payload)

        if not msg:
            return

        result = protocol_handlers._broadcast_member_message(
            message_bytes=msg,
            reliability="direct",
            failure_policy="best_effort",
            log_label="pheromones",
        )
        broadcast_count = result["sent"]

        if broadcast_count > 0:
            plugin.log(
                f"cl-hive: Broadcast {len(shareable_pheromones)} pheromones "
                f"to {broadcast_count} members",
                level='debug'
            )

    except Exception as e:
        if plugin:
            plugin.log(f"cl-hive: Pheromone broadcast error: {e}", level='warn')


def _broadcast_our_yield_metrics():
    """
    Broadcast our yield metrics to hive members for fleet-wide learning.

    Yield metrics include per-channel ROI, capital efficiency, and profitability
    tier. Sharing these enables the fleet to learn which external peers are
    profitable and which should be avoided.
    """
    if not yield_metrics_mgr or not plugin or not database or not our_pubkey:
        return

    try:
        from modules.protocol import create_yield_metrics_batch, MAX_YIELD_METRICS_IN_BATCH

        # Get hive member IDs to exclude from sharing
        members = database.get_all_members()
        member_ids = {m.get("peer_id") for m in members}

        # Get shareable yield metrics (excluding hive members)
        shareable_metrics = yield_metrics_mgr.get_shareable_yield_metrics(
            period_days=30,
            exclude_peer_ids=member_ids,
            max_metrics=MAX_YIELD_METRICS_IN_BATCH
        )

        if not shareable_metrics:
            return

        # Create signed batch message
        msg = create_yield_metrics_batch(
            metrics=shareable_metrics,
            rpc=plugin.rpc,
            our_pubkey=our_pubkey
        )

        if not msg:
            return

        result = protocol_handlers._broadcast_member_message(
            message_bytes=msg,
            reliability="direct",
            failure_policy="best_effort",
            log_label="yield_metrics",
        )
        broadcast_count = result["sent"]

        if broadcast_count > 0:
            plugin.log(
                f"cl-hive: Broadcast {len(shareable_metrics)} yield metrics "
                f"to {broadcast_count} members",
                level='debug'
            )

    except Exception as e:
        if plugin:
            plugin.log(f"cl-hive: Yield metrics broadcast error: {e}", level='warn')


def _broadcast_circular_flow_alerts():
    """
    Broadcast detected circular flow alerts to hive members.

    Circular flows (A→B→C→A rebalancing patterns) waste fees without
    improving liquidity. Sharing detected flows enables fleet-wide
    prevention and coordination.
    """
    if not cost_reduction_mgr or not plugin or not database or not our_pubkey:
        return

    try:
        from modules.protocol import (
            create_circular_flow_alert,
            MIN_CIRCULAR_FLOW_SATS,
            MIN_CIRCULAR_FLOW_COST_SATS
        )

        # Get shareable circular flows
        shareable_flows = cost_reduction_mgr.circular_detector.get_shareable_circular_flows(
            min_cost_sats=MIN_CIRCULAR_FLOW_COST_SATS,
            min_amount_sats=MIN_CIRCULAR_FLOW_SATS
        )

        if not shareable_flows:
            return

        # Broadcast each flow as a separate alert (event-driven)
        total_broadcast = 0

        for flow in shareable_flows:
            msg = create_circular_flow_alert(
                members_involved=flow["members_involved"],
                total_amount_sats=flow["total_amount_sats"],
                total_cost_sats=flow["total_cost_sats"],
                cycle_count=flow["cycle_count"],
                detection_window_hours=flow["detection_window_hours"],
                recommendation=flow["recommendation"],
                rpc=plugin.rpc,
                our_pubkey=our_pubkey
            )

            if not msg:
                continue

            result = protocol_handlers._broadcast_member_message(
                message_bytes=msg,
                reliability="reliable",
                failure_policy="best_effort",
                log_label="circular_flow_alert",
            )
            total_broadcast += result["queued"] or result["sent"]

        if total_broadcast > 0:
            plugin.log(
                f"cl-hive: Broadcast {len(shareable_flows)} circular flow alerts",
                level='info'
            )

    except Exception as e:
        if plugin:
            plugin.log(f"cl-hive: Circular flow alert broadcast error: {e}", level='warn')


def _broadcast_our_temporal_patterns():
    """
    Broadcast our temporal patterns to hive members for fleet-wide learning.

    Temporal patterns include hour/day flow patterns that enable coordinated
    liquidity positioning and proactive fee optimization.
    """
    if not anticipatory_liquidity_mgr or not plugin or not database or not our_pubkey:
        return

    try:
        from modules.protocol import (
            create_temporal_pattern_batch,
            MAX_TEMPORAL_PATTERNS_IN_BATCH,
            MIN_TEMPORAL_PATTERN_CONFIDENCE,
            MIN_TEMPORAL_PATTERN_SAMPLES
        )

        # Get hive member IDs to exclude from sharing
        members = database.get_all_members()
        member_ids = {m.get("peer_id") for m in members}

        # Get shareable temporal patterns (excluding hive members)
        shareable_patterns = anticipatory_liquidity_mgr.get_shareable_patterns(
            min_confidence=MIN_TEMPORAL_PATTERN_CONFIDENCE,
            min_samples=MIN_TEMPORAL_PATTERN_SAMPLES,
            exclude_peer_ids=member_ids,
            max_patterns=MAX_TEMPORAL_PATTERNS_IN_BATCH
        )

        if not shareable_patterns:
            return

        # Create signed batch message
        msg = create_temporal_pattern_batch(
            patterns=shareable_patterns,
            rpc=plugin.rpc,
            our_pubkey=our_pubkey
        )

        if not msg:
            return

        result = protocol_handlers._broadcast_member_message(
            message_bytes=msg,
            reliability="direct",
            failure_policy="best_effort",
            log_label="temporal_patterns",
        )
        broadcast_count = result["sent"]

        if broadcast_count > 0:
            plugin.log(
                f"cl-hive: Broadcast {len(shareable_patterns)} temporal patterns "
                f"to {broadcast_count} members",
                level='debug'
            )

    except Exception as e:
        if plugin:
            plugin.log(f"cl-hive: Temporal patterns broadcast error: {e}", level='warn')


# ============================================================================
# Phase 14.2: Strategic Positioning & Rationalization Broadcasts
# ============================================================================


def _broadcast_our_corridor_values():
    """
    Broadcast our high-value corridor discoveries to hive members.

    Corridors are routing paths with high volume, margin, and low competition.
    Sharing enables coordinated strategic positioning across the fleet.
    """
    if not strategic_positioning_mgr or not plugin or not database or not our_pubkey:
        return

    try:
        from modules.protocol import (
            create_corridor_value_batch,
            MAX_CORRIDORS_IN_BATCH,
            MIN_CORRIDOR_VALUE_SCORE
        )

        # Get shareable corridor values
        shareable_corridors = strategic_positioning_mgr.get_shareable_corridors(
            min_value_score=MIN_CORRIDOR_VALUE_SCORE,
            max_corridors=MAX_CORRIDORS_IN_BATCH
        )

        if not shareable_corridors:
            return

        # Create signed batch message
        msg = create_corridor_value_batch(
            corridors=shareable_corridors,
            rpc=plugin.rpc,
            our_pubkey=our_pubkey
        )

        if not msg:
            return

        result = protocol_handlers._broadcast_member_message(
            message_bytes=msg,
            reliability="direct",
            failure_policy="best_effort",
            log_label="corridor_values",
        )
        broadcast_count = result["sent"]

        if broadcast_count > 0:
            plugin.log(
                f"cl-hive: Broadcast {len(shareable_corridors)} corridor values "
                f"to {broadcast_count} members",
                level='debug'
            )

    except Exception as e:
        if plugin:
            plugin.log(f"cl-hive: Corridor values broadcast error: {e}", level='warn')


def _broadcast_our_positioning_proposals():
    """
    Broadcast our channel open recommendations to hive members.

    Positioning proposals suggest strategic channel targets for optimal
    fleet placement based on exchange coverage and corridor value analysis.
    """
    if not strategic_positioning_mgr or not plugin or not database or not our_pubkey:
        return

    try:
        from modules.protocol import create_positioning_proposal, MAX_POSITIONING_PROPOSALS_PER_CYCLE

        # Get shareable positioning recommendations
        shareable_proposals = strategic_positioning_mgr.get_shareable_positioning_recommendations(
            max_recommendations=MAX_POSITIONING_PROPOSALS_PER_CYCLE
        )

        if not shareable_proposals:
            return

        total_broadcast = 0

        # Broadcast each proposal separately (they're targeted recommendations)
        for proposal in shareable_proposals:
            msg = create_positioning_proposal(
                target_pubkey=proposal["target_pubkey"],
                target_alias=proposal.get("target_alias", ""),
                reason=proposal["reason"],
                score=proposal["score"],
                suggested_amount_sats=proposal.get("suggested_amount_sats", 0),
                priority=proposal.get("priority", "medium"),
                rpc=plugin.rpc,
                our_pubkey=our_pubkey
            )

            if not msg:
                continue

            result = protocol_handlers._broadcast_member_message(
                message_bytes=msg,
                reliability="reliable",
                failure_policy="best_effort",
                log_label="positioning_proposal",
            )
            total_broadcast += result["queued"] or result["sent"]

        if total_broadcast > 0:
            plugin.log(
                f"cl-hive: Broadcast {len(shareable_proposals)} positioning proposals",
                level='debug'
            )

    except Exception as e:
        if plugin:
            plugin.log(f"cl-hive: Positioning proposals broadcast error: {e}", level='warn')


def _broadcast_our_physarum_recommendations():
    """
    Broadcast our Physarum (flow-based) channel lifecycle recommendations.

    Physarum recommendations use slime mold optimization principles:
    - strengthen: High flow channels that should be spliced larger
    - atrophy: Low flow channels that should be closed
    - stimulate: Young low flow channels that need fee reduction
    """
    if not strategic_positioning_mgr or not plugin or not database or not our_pubkey:
        return

    try:
        from modules.protocol import create_physarum_recommendation, MAX_PHYSARUM_RECOMMENDATIONS_PER_CYCLE

        # Get shareable Physarum recommendations (exclude 'hold')
        shareable_recommendations = strategic_positioning_mgr.get_shareable_physarum_recommendations(
            exclude_hold=True
        )

        if not shareable_recommendations:
            return

        # Limit to max per cycle
        shareable_recommendations = shareable_recommendations[:MAX_PHYSARUM_RECOMMENDATIONS_PER_CYCLE]

        total_broadcast = 0

        # Broadcast each recommendation separately
        for rec in shareable_recommendations:
            msg = create_physarum_recommendation(
                channel_id=rec.get("channel_id", ""),
                peer_id=rec["peer_id"],
                action=rec["action"],
                flow_intensity=rec["flow_intensity"],
                reason=rec["reason"],
                expected_yield_change_pct=rec.get("expected_yield_change_pct", 0.0),
                rpc=plugin.rpc,
                our_pubkey=our_pubkey,
                splice_amount_sats=rec.get("splice_amount_sats", 0)
            )

            if not msg:
                continue

            result = protocol_handlers._broadcast_member_message(
                message_bytes=msg,
                reliability="direct",
                failure_policy="best_effort",
                log_label="physarum_recommendation",
            )
            total_broadcast += result["sent"]

        if total_broadcast > 0:
            plugin.log(
                f"cl-hive: Broadcast {len(shareable_recommendations)} Physarum recommendations",
                level='debug'
            )

    except Exception as e:
        if plugin:
            plugin.log(f"cl-hive: Physarum recommendations broadcast error: {e}", level='warn')


def _broadcast_our_coverage_analysis():
    """
    Broadcast our peer coverage analysis to hive members.

    Coverage analysis shows which peers the fleet has channels to,
    ownership determination based on routing activity (stigmergic markers),
    and identifies redundant coverage for rationalization.
    """
    if not rationalization_mgr or not plugin or not database or not our_pubkey:
        return

    try:
        from modules.protocol import (
            create_coverage_analysis_batch,
            MAX_COVERAGE_ENTRIES_IN_BATCH,
            MIN_COVERAGE_OWNERSHIP_CONFIDENCE
        )

        # Get shareable coverage analysis
        shareable_coverage = rationalization_mgr.get_shareable_coverage_analysis(
            min_ownership_confidence=MIN_COVERAGE_OWNERSHIP_CONFIDENCE,
            max_entries=MAX_COVERAGE_ENTRIES_IN_BATCH
        )

        if not shareable_coverage:
            return

        # Create signed batch message
        msg = create_coverage_analysis_batch(
            coverage_entries=shareable_coverage,
            rpc=plugin.rpc,
            our_pubkey=our_pubkey
        )

        if not msg:
            return

        result = protocol_handlers._broadcast_member_message(
            message_bytes=msg,
            reliability="direct",
            failure_policy="best_effort",
            log_label="coverage_analysis",
        )
        broadcast_count = result["sent"]

        if broadcast_count > 0:
            plugin.log(
                f"cl-hive: Broadcast {len(shareable_coverage)} coverage entries "
                f"to {broadcast_count} members",
                level='debug'
            )

    except Exception as e:
        if plugin:
            plugin.log(f"cl-hive: Coverage analysis broadcast error: {e}", level='warn')


def _broadcast_our_close_proposals():
    """
    Broadcast our channel close recommendations to hive members.

    Close proposals suggest redundant channels that should be closed
    based on coverage analysis and ownership determination. The channel
    owner with less routing activity should close to improve capital efficiency.
    """
    if not rationalization_mgr or not plugin or not database or not our_pubkey:
        return

    try:
        from modules.protocol import create_close_proposal, MAX_CLOSE_PROPOSALS_PER_CYCLE

        # Get shareable close recommendations
        shareable_proposals = rationalization_mgr.get_shareable_close_recommendations(
            max_recommendations=MAX_CLOSE_PROPOSALS_PER_CYCLE
        )

        if not shareable_proposals:
            return

        total_broadcast = 0

        # Broadcast each proposal separately (targeted to specific member)
        for proposal in shareable_proposals:
            msg = create_close_proposal(
                target_member=proposal["target_member"],
                target_peer=proposal["target_peer"],
                reason=proposal["reason"],
                our_routing_share=proposal["our_routing_share"],
                their_routing_share=proposal["their_routing_share"],
                suggested_action=proposal.get("suggested_action", "close"),
                rpc=plugin.rpc,
                our_pubkey=our_pubkey
            )

            if not msg:
                continue

            result = protocol_handlers._broadcast_member_message(
                message_bytes=msg,
                reliability="reliable",
                failure_policy="best_effort",
                log_label="close_proposal",
            )
            total_broadcast += result["queued"] or result["sent"]

        if total_broadcast > 0:
            plugin.log(
                f"cl-hive: Broadcast {len(shareable_proposals)} close proposals",
                level='debug'
            )

    except Exception as e:
        if plugin:
            plugin.log(f"cl-hive: Close proposals broadcast error: {e}", level='warn')


def _broadcast_health_report():
    """
    Calculate and broadcast our health report for NNLB coordination.
    """
    if not fee_intel_mgr or not plugin or not database or not our_pubkey:
        return

    try:
        # Get our channel data
        funds = plugin.rpc.listfunds()
        channels = funds.get("channels", [])

        capacity_sats = sum(
            ch.get("amount_msat", 0) // 1000
            for ch in channels if ch.get("state") == "CHANNELD_NORMAL"
        )
        available_sats = sum(
            ch.get("our_amount_msat", 0) // 1000
            for ch in channels if ch.get("state") == "CHANNELD_NORMAL"
        )
        channel_count = len([ch for ch in channels if ch.get("state") == "CHANNELD_NORMAL"])

        # Calculate actual daily revenue from forwarding stats
        daily_revenue_sats = 0
        try:
            forwards = plugin.rpc.listforwards(status="settled")
            forwards_list = forwards.get("forwards", [])
            one_day_ago = time.time() - (24 * 3600)
            daily_revenue_sats = sum(
                fwd.get("fee_msat", 0) // 1000
                for fwd in forwards_list
                if fwd.get("received_time", 0) > one_day_ago
            )
        except Exception:
            pass

        # Get hive averages for comparison
        all_health = database.get_all_member_health()
        if all_health:
            hive_avg_capacity = sum(
                h.get("capacity_score", 50) for h in all_health
            ) / len(all_health) * 200000
            # Estimate hive average revenue from revenue scores
            hive_avg_revenue = sum(
                h.get("revenue_score", 50) for h in all_health
            ) / len(all_health) * 20  # Scale factor for reasonable default
        else:
            hive_avg_capacity = 10_000_000
            hive_avg_revenue = 1000  # Default 1000 sats/day

        # Calculate our health
        health = fee_intel_mgr.calculate_our_health(
            capacity_sats=capacity_sats,
            available_sats=available_sats,
            channel_count=channel_count,
            daily_revenue_sats=daily_revenue_sats,
            hive_avg_capacity=int(hive_avg_capacity),
            hive_avg_revenue=int(max(1, hive_avg_revenue))  # Avoid division by zero
        )

        # Store our own health record
        database.update_member_health(
            peer_id=our_pubkey,
            overall_health=health["overall_health"],
            capacity_score=health["capacity_score"],
            revenue_score=health["revenue_score"],
            connectivity_score=health["connectivity_score"],
            tier=health["tier"],
            needs_help=health["needs_help"],
            can_help_others=health["can_help_others"],
            needs_inbound=available_sats < capacity_sats * 0.3 if capacity_sats > 0 else False,
            needs_outbound=available_sats > capacity_sats * 0.7 if capacity_sats > 0 else False,
            needs_channels=channel_count < 5
        )

        # Create and broadcast health report
        msg = fee_intel_mgr.create_health_report_message(
            overall_health=health["overall_health"],
            capacity_score=health["capacity_score"],
            revenue_score=health["revenue_score"],
            connectivity_score=health["connectivity_score"],
            rpc=plugin.rpc,
            needs_inbound=available_sats < capacity_sats * 0.3 if capacity_sats > 0 else False,
            needs_outbound=available_sats > capacity_sats * 0.7 if capacity_sats > 0 else False,
            needs_channels=channel_count < 5,
            can_provide_assistance=health["can_help_others"]
        )

        if msg:
            result = protocol_handlers._broadcast_member_message(
                message_bytes=msg,
                reliability="direct",
                failure_policy="best_effort",
                log_label="health_report",
            )
            broadcast_count = result["sent"]

            if broadcast_count > 0:
                plugin.log(
                    f"cl-hive: Broadcast health report (health={health['overall_health']}, "
                    f"tier={health['tier']}, to {broadcast_count} members)",
                    level='debug'
                )

    except Exception as e:
        if plugin:
            plugin.log(f"cl-hive: Health report broadcast error: {e}", level='warn')


def _broadcast_liquidity_needs():
    """
    Assess and broadcast our liquidity needs to hive members.

    Identifies channels that need rebalancing and broadcasts
    LIQUIDITY_NEED messages for cooperative assistance.
    """
    if not liquidity_coord or not plugin or not database or not our_pubkey:
        return

    try:
        # Get our channel data
        funds = plugin.rpc.listfunds()

        # Assess our liquidity needs
        needs = liquidity_coord.assess_our_liquidity_needs(funds)

        if not needs:
            return

        # Note: Cooperative rebalancing removed - we don't transfer funds between nodes.
        # Set can_provide values to 0 since we're information-only.
        # Broadcasting liquidity needs is still useful for fee coordination.

        broadcast_count = 0
        for need in needs[:3]:  # Broadcast top 3 needs
            msg = liquidity_coord.create_liquidity_need_message(
                need_type=need["need_type"],
                target_peer_id=need["target_peer_id"],
                amount_sats=need["amount_sats"],
                urgency=need["urgency"],
                max_fee_ppm=100,  # Willing to pay 100ppm
                reason=need["reason"],
                current_balance_pct=need["current_balance_pct"],
                can_provide_inbound=0,   # No cooperative rebalancing
                can_provide_outbound=0,  # No cooperative rebalancing
                rpc=plugin.rpc
            )
            if msg:
                result = protocol_handlers._broadcast_member_message(
                    message_bytes=msg,
                    reliability="direct",
                    failure_policy="best_effort",
                    log_label="liquidity_need",
                )
                broadcast_count += result["sent"]

        if broadcast_count > 0:
            plugin.log(
                f"cl-hive: Broadcast {len(needs[:3])} liquidity needs to hive",
                level='debug'
            )

    except Exception as e:
        if plugin:
            plugin.log(f"cl-hive: Liquidity needs broadcast error: {e}", level='warn')
