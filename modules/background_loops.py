"""
background_loops - Background daemon loop functions for cl-hive.

This module contains all *_loop functions and their private helper functions
that run as background daemon threads.  These were extracted verbatim from
cl-hive.py during the monolith decomposition.

Dependencies are injected at startup via init_background_loops() to avoid
rewriting every function body during the extraction.
"""

import json
import secrets
import time
from typing import Dict, Optional, Any, List

from modules import protocol_handlers


def init_background_loops(deps: dict):
    """Inject dependency references into this module's namespace.

    Called once from cl-hive.py init() after all managers are created.
    Every key in *deps* becomes a module-level name so that the moved
    loop functions can reference the exact same variable names they
    always did.
    """
    globals().update(deps)


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

        # Commit the intent after hold period
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
                # Membership data pruning
                database.prune_old_contributions(older_than_days=45)
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

                # Data pruning
                database.prune_planner_logs(older_than_days=30)
                database.cleanup_proto_events(max_age_seconds=30 * 86400)
                database.prune_old_flow_samples(days_to_keep=30)

                # Issue #38: Auto-connect to hive members we're not connected to
                reconnected = _auto_connect_to_all_members()
                if reconnected > 0 and plugin:
                    plugin.log(f"Auto-connected to {reconnected} hive member(s)", level='info')

                # Auto-remove members whose node is no longer in the gossip
                # graph.  The gossip graph retains node announcements for ~2
                # weeks, so absence from the graph is a strong signal the node
                # is permanently gone.  This prevents ghost members from
                # polluting hive policies.
                protocol_handlers._cleanup_ghost_members()

                # Expire stale pending join requests (older than 24 hours)
                if handshake_mgr:
                    try:
                        expired = handshake_mgr.expire_pending_requests()
                        if expired > 0 and plugin:
                            plugin.log(f"cl-hive: Expired {expired} pending join request(s)", level='info')
                    except Exception as expire_err:
                        if plugin:
                            plugin.log(f"cl-hive: Pending request expiry error: {expire_err}", level='warn')

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

            # Step 5a: Broadcast yield metrics (Daily, only once per day)
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

            # Step 5b: Broadcast temporal patterns (Weekly)
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

            # Step 5c: Broadcast corridor values (Weekly)
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

            # Step 5d: Broadcast positioning proposals (Event-driven)
            _broadcast_our_positioning_proposals()
            shutdown_event.wait(0.05)

            # Step 5e: Broadcast coverage analysis (Weekly)
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

            # Step 5f: Broadcast close proposals (Event-driven)
            _broadcast_our_close_proposals()
            shutdown_event.wait(0.05)

            # Step 5g: Broadcast traffic intelligence (every 6 hours)
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

            # Step 7: Cleanup stale peer states (memory management)
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

            # Step 10: Cleanup old remote yield metrics
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


def _broadcast_our_coverage_analysis():
    """
    Broadcast our peer coverage analysis to hive members.

    Coverage analysis shows which peers the fleet has channels to,
    ownership determination based on routing activity,
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
