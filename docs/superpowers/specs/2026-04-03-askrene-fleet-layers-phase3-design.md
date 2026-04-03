# askrene Fleet Intelligence Layers — Phase 3: Full Routing Integration

**Date:** 2026-04-03
**Status:** Approved
**Scope:** cl-revenue-ops (replace legacy fee estimation with getroutes, extend hints)
**Depends on:** Phase 1 + Phase 2

## Goal

Replace the rebalancer's multi-priority inbound fee estimation chain with a single `getroutes` call that automatically incorporates all fleet intelligence layers. Extend hints with remaining high-value fields. Feed routing topology into planner expansion decisions.

## Changes

### Replace `_estimate_inbound_fee()` with getroutes

The current `_estimate_inbound_fee()` has a 6-priority fallback chain:
1. Historical data (high confidence)
2. Historical data (medium) blended with last-hop
3. Historical data (low) with buffer
4. Last-hop fee + buffer (50 ppm)
5. Route estimation via `getroute`
6. Default fallback (50 ppm)

Replace priorities 4-6 with a single `getroutes` call using all available layers:

```python
def _estimate_inbound_fee(self, peer_id, amount_msat=100000000):
    # Priority 0: Hive fleet member (0 fee) — existing
    # Priority 1-3: Historical data — existing, unchanged

    # Priority 4: getroutes with fleet layers (replaces last-hop + buffer + fallback)
    if self.hive_router and self.hive_router.available:
        route = self.hive_router.discover_route(peer_id, amount_msat // 1000)
        if route:
            return route.fee_ppm

    # Fallback: configured default
    return self.config.inbound_fee_estimate_ppm
```

This collapses three fallback priorities into one that's fleet-aware by default. Historical data (priorities 1-3) still takes precedence when available since it reflects actual observed costs.

### Extend hive-export-hints with remaining high-value fields

cl-hive adds to the per-peer hint:
- `peak_hours_utc`: list of ints (0-23) from traffic_intelligence
- `drain_direction`: "inbound_heavy" | "outbound_heavy" | "balanced" from traffic_intelligence
- `fee_elasticity`: float from fee_intelligence (estimated price elasticity)
- `optimal_fee_estimate_ppm`: int from fee_intelligence

cl-revenue-ops `HiveHintAdapter` exposes:
- `get_peak_hours(peer_id) -> List[int]`
- `get_drain_direction(peer_id) -> str`
- `get_fee_elasticity(peer_id) -> float`
- `get_optimal_fee_estimate(peer_id) -> Optional[int]`

### Fee controller: temporal fee optimization

Using `get_peak_hours()` and `get_drain_direction()`, the fee controller can:
- During peak hours for a peer: maintain or increase fees (demand is high)
- During quiet hours: reduce fees slightly to attract flow
- For inbound-heavy peers: slightly lower fees to encourage more inbound (we want the traffic)
- For outbound-heavy peers: slightly raise fees (we're providing a valuable service)

Implementation: a `_get_temporal_fee_adjustment()` method that returns a multiplier (0.9-1.1) applied to the DTS-recommended fee, gated by `traffic_confidence > 0.5`.

### Planner: routing-aware expansion scoring

The planner's `get_expansion_recommendation()` currently scores targets by capacity, competition, and hive coverage. Add a routing value component:

For each expansion target, call `getroutes` to see if adding a channel to this target would create valuable new paths (short routes to high-centrality nodes that currently require many hops). This is a "what-if" analysis:
1. Create a temporary askrene layer with a virtual channel to the target
2. Call `getroutes` to several high-value destinations
3. Compare route quality (fee, hops) with and without the virtual channel
4. Score the improvement

This uses `askrene-create-channel` (virtual channel in a temporary layer) — a capability we haven't used yet. The temporary layer is removed after scoring.

## Graceful Degradation

All changes follow the same pattern:
- When cl-hive is running and askrene is available: full fleet intelligence
- When cl-hive is absent: `getroutes` uses default gossip, hints return neutral defaults
- When askrene is unavailable: legacy fallback chains work as before
- cl-revenue-ops functions independently at every level

## What Does NOT Change

- The core DTS/PID algorithm (temporal adjustment is a post-multiplier)
- Boltz execution (already uses HiveRouter)
- Sling job parameters (still controlled by rebalancer)
- Database schemas in either plugin

## Testing

- `_estimate_inbound_fee()` returns fleet-aware estimates when layers available
- `_estimate_inbound_fee()` falls back correctly when no layers
- Temporal fee adjustment stays within 0.9-1.1 range
- Planner virtual channel scoring doesn't leave orphan layers
- Full test suites pass in both codebases
