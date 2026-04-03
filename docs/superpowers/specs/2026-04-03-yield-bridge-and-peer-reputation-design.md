# Yield Bridge + Peer Reputation Broadcast

**Date:** 2026-04-03
**Status:** Approved
**Scope:** cl-hive (`modules/bridge.py`, `modules/background_loops.py`, `cl-hive.py`)

## Problem

Two data pipeline gaps discovered during fleet audit:

1. **yield_summary returns all zeros** despite revenue_dashboard showing 1,986 sats / 210 forwards. `YieldMetricsManager` expects `bridge.get_profitability()` but the Bridge class doesn't implement it, and the bridge isn't passed at init.

2. **peer_reputation has zero tracked peers** despite 45 external peers. The receive/aggregate/query pipeline is fully implemented, but no code generates or broadcasts `PEER_REPUTATION_SNAPSHOT` messages. The sender was never written.

## Fix 1: Yield Bridge

### bridge.py — add `get_profitability()`

Add a method that calls `safe_call("revenue-profitability")` with circuit breaker protection. Follows the same pattern as `get_fee_config()`. Returns the profitability result dict or None on failure.

```python
def get_profitability(self) -> Optional[Dict[str, Any]]:
    if self._status == BridgeStatus.DISABLED:
        return None
    try:
        result = self.safe_call("revenue-profitability")
        if isinstance(result, dict) and "error" not in result:
            return result
        return None
    except Exception:
        return None
```

### cl-hive.py:730-734 — pass bridge to YieldMetricsManager

Change:
```python
yield_metrics_mgr = YieldMetricsManager(
    database=database,
    plugin=plugin,
    state_manager=state_manager
)
```

To:
```python
yield_metrics_mgr = YieldMetricsManager(
    database=database,
    plugin=plugin,
    state_manager=state_manager,
    bridge=bridge
)
```

The `bridge` variable is already initialized earlier in `init()`. The existing code at `yield_metrics.py:398` handles the rest — it checks `hasattr(self.bridge, 'get_profitability')` and populates channel profitability data from the result.

## Fix 2: Peer Reputation Broadcast

### background_loops.py — add `_broadcast_our_peer_reputation()`

New function following the exact pattern of `_broadcast_our_fee_intelligence()` (lines 646-797):

1. Guard: return if `peer_reputation_mgr`, `plugin`, `database`, or `our_pubkey` is None
2. Call `listpeerchannels()` to get channel data (age, HTLC counts, fee rates)
3. Call `listforwards(status="settled")` and `listforwards(status="failed")` for 7-day window to compute per-peer HTLC success rate and volume
4. Call `listpeers()` for connection status (basic uptime signal)
5. Exclude hive member peer_ids
6. For each external peer, build observation:
   - `peer_id`: the peer's pubkey
   - `uptime_pct`: 1.0 if connected, 0.5 if disconnected (conservative — we don't have historical uptime tracking)
   - `response_time_ms`: 0 (not available from RPC — field exists for future use)
   - `force_close_count`: count channels in force-close states for this peer
   - `fee_stability`: 1.0 (default — would need historical tracking for real values)
   - `htlc_success_rate`: settled / (settled + failed) for this peer over 7 days
   - `channel_age_days`: from `funding_txid` confirmation time or channel open time
   - `total_routed_sats`: sum of settled forward volume through this peer (7 days)
   - `warnings`: empty list (no warning detection in v1)
   - `observation_days`: 7
7. Sign payload using `get_peer_reputation_snapshot_signing_payload()` + `plugin.rpc.signmessage()`
8. Create message via `create_peer_reputation_snapshot()`
9. Broadcast via `_broadcast_member_message()` with `reliability="direct"`, `failure_policy="best_effort"`

### background_loops.py — hook into fee_intelligence_loop

Add as Step 5h (daily, like yield metrics):

```python
# Step 5h: Broadcast peer reputation (Daily)
today = time.strftime("%Y-%m-%d")
last_rep_broadcast = getattr(_broadcast_our_peer_reputation, '_last_broadcast', None)
if last_rep_broadcast != today:
    _broadcast_our_peer_reputation()
    _broadcast_our_peer_reputation._last_broadcast = today
```

Daily frequency matches the rate limit of 2 snapshots/day/sender and aligns with the fact that reputation changes slowly.

## What does NOT change

- `yield_metrics.py` — already handles bridge data correctly at line 398
- `peer_reputation.py` — receive/aggregate pipeline already works
- `protocol.py` — message type, signing, validation, serialization all implemented
- `protocol_handlers.py` — handler for incoming snapshots already works
- `database.py` — `peer_reputation` table and `store_peer_reputation()` already exist

## Testing

- Run cl-hive test suite — no regressions
- After deployment: `yield_summary` should show non-zero revenue matching `revenue_dashboard`
- After deployment + 1 gossip cycle: `hive_reputation_stats` should show `total_peers_tracked > 0`
