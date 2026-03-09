# Phases 3b, 3c & Revenue-Ops Traffic Intelligence Integration

**Date**: 2026-03-09
**Status**: Approved
**Issue**: lightning-goats/cl-hive#88 (continuation)
**Dependency**: Phases 2+3a (closed — traffic intelligence module complete)

## Goal

Complete the traffic intelligence feature set: (1) revenue-ops feeds local
traffic profiles to cl-hive and consumes fleet intelligence, (2) MCF
assignment execution respects fleet peak/quiet hours, (3) fee coordination
incorporates fleet-wide forward size data.

## Scope

### In Scope

- **Phase 3b**: Decentralized MCF scheduling — members check
  `hive-check-rebalance-conflict` before claiming assignments
- **Phase 3c**: Size-aware fee enrichment — `FeeCoordinationManager` queries
  traffic intelligence for forward size multipliers
- **Revenue-ops integration**: 4 new hive bridge methods calling the 4 traffic
  intelligence RPCs

### Out of Scope

- Centralized time windows on MCFAssignment (YAGNI — decentralized is simpler)
- New gossip message types (reuse existing TRAFFIC_INTELLIGENCE_BATCH)
- Changes to existing RPC signatures

## Architecture

### Approach: Traffic-Intel-First

Order: revenue-ops profile reporting → 3b scheduling → 3c fee enrichment →
remaining rev-ops integration. Data pipeline first, incremental value at each
step.

### Decision: Decentralized MCF Scheduling

Members decide *when* to execute assignments by checking
`hive-check-rebalance-conflict` before claiming. The coordinator still decides
*what* to rebalance. No protocol changes needed.

### Decision: Enrich Existing Fee Recommendations

No new gossip message. `FeeCoordinationManager.get_fee_recommendation()`
queries `traffic_intel_mgr.get_aggregated_profile()` for forward size data and
applies a bounded multiplier (0.8x-1.3x).

## Section 1: cl-revenue-ops Traffic Intelligence Integration

### hive_bridge.py — 4 New Methods

| Method | RPC | Called From | Trigger |
|--------|-----|------------|---------|
| `report_traffic_profile()` | `hive-report-traffic-profile` | `flow_analysis.py` | Flow analysis cycle (1h), after profiles graduate (7+ days) |
| `query_traffic_intelligence()` | `hive-traffic-intelligence` | `fee_controller.py` | Before fee adjustments (30 min) |
| `check_rebalance_conflict()` | `hive-check-rebalance-conflict` | `rebalancer.py` | Pre-rebalance check |
| `query_fleet_demand_forecast()` | `hive-fleet-demand-forecast` | `capacity_planner.py` | Capacity planning cycle |

### Profile Graduation from flow_analysis.py

Revenue-ops FlowAnalyzer already computes per-peer: avg_forward_size_sats,
daily_forward_volume_sats, flow_direction (sink/source/balanced), peak_hours,
quiet_hours.

Field mapping:
- `flow_direction` → `drain_direction`: source→outbound_heavy,
  sink→inbound_heavy, balanced→balanced
- `peak_hours`/`quiet_hours` → `peak_hours_utc`/`quiet_hours_utc`
- Profile type: volume/forward-size heuristic (high volume + small forwards =
  retail, low volume + large forwards = wholesale, etc.)

### Rebalancer Integration

Before initiating any rebalance, call
`hive_bridge.check_rebalance_conflict()`. If `peer_in_peak_hours` and
`suggested_window_utc` exists, defer. If `conflict` (another fleet member
actively rebalancing through same peer), skip entirely.

### Circuit Breaker Policy

All 4 new methods use `optional_read` policy — hive being down never blocks
revenue-ops core operation. Cache with stale fallback (30 min fresh, 24h
stale).

## Section 2: Phase 3b — Decentralized MCF Scheduling

### Change Location

`modules/background_loops.py` → `_process_mcf_assignments()`

### Current Flow

1. `get_pending_mcf_assignments()`
2. `claim_pending_assignment()` — immediate
3. Execute via sling

### New Flow

1. `get_pending_mcf_assignments()`
2. **Extract target peer from `to_channel`**
3. **`traffic_intel_mgr.check_rebalance_conflict(peer_id, direction, amount)`**
4. **If `peer_in_peak_hours` + `suggested_window_utc` → skip, log reason**
5. **If `conflict` → skip, log reason**
6. If clear → claim and execute

### Assignment Aging

`max_defer_cycles = 3` (~90 minutes across 3 MCF cycles). After 3 deferrals,
execute regardless. Stale assignments are worse than suboptimal timing. Track
defer count per assignment_id in a dict.

### No Protocol Changes

Entirely local to `background_loops.py`. No changes to MCFAssignment,
MCFSolution, or gossip messages.

## Section 3: Phase 3c — Size-Aware Fee Enrichment

### New Method

```
FeeCoordinationManager.get_size_aware_adjustment(peer_id) -> float
```

Returns a multiplier (0.8-1.3) based on fleet traffic intelligence:

| Condition | Multiplier | Rationale |
|-----------|------------|-----------|
| avg_forward_size > 500k sats | 0.9x | Attract whale traffic |
| avg_forward_size < 10k sats | 1.1x | HTLC slot cost for small forwards |
| daily_volume > 10M sats | +0.05 floor boost | Protect capacity for valuable peer |
| No traffic data | 1.0x (neutral) | Preserve current behavior |

### Integration Point

Called from `get_fee_recommendation()`, applied alongside existing
`time_adjustment_pct` and `centrality_adjustment_pct`. The multiplier is
bounded to [0.8, 1.3] and stored in `FeeRecommendation.size_adjustment_pct`.

### Revenue-Ops Transparency

Revenue-ops already calls `hive-coordinated-fee-recommendation` via
`query_coordinated_fee_recommendation()`. The size-aware adjustment is
transparently included. Revenue-ops can also query `hive-traffic-intelligence`
directly for its own fee decisions.

## Files Touched

### cl-hive (this repo)

| File | Changes |
|------|---------|
| `modules/background_loops.py` | Phase 3b: conflict check before MCF claim |
| `modules/fee_coordination.py` | Phase 3c: `get_size_aware_adjustment()` method |
| `tests/test_traffic_intelligence.py` | Tests for 3b scheduling + 3c fee enrichment |

### cl-revenue-ops (separate repo)

| File | Changes |
|------|---------|
| `modules/hive_bridge.py` | 4 new methods + circuit breaker policies |
| `modules/flow_analysis.py` | Profile graduation → `report_traffic_profile()` |
| `modules/rebalancer.py` | Pre-rebalance conflict check |
| `modules/fee_controller.py` | Query fleet traffic intelligence for fee sizing |
| `modules/capacity_planner.py` | Query fleet demand forecast |
| `tests/test_hive_bridge.py` | Tests for 4 new bridge methods |
| `tests/test_rebalancer.py` | Tests for conflict-aware rebalancing |

## Cross-Module Dependencies

Phase 3b requires: `traffic_intel_mgr` (already injected into background_loops)
Phase 3c requires: `traffic_intel_mgr` (needs injection into fee_coordination)
Revenue-ops requires: cl-hive traffic intelligence RPCs (already deployed)

No circular dependencies.

## Error Handling

- All hive bridge methods use `optional_read` circuit breaker policy
- Revenue-ops never blocks on hive being unavailable
- Conflict check failure → proceed with rebalance (fail-open)
- Missing traffic data → neutral multiplier (1.0x) for fees
- MCF defer count overflow → execute regardless after max_defer_cycles

## What We Do NOT Do

- No centralized time windows on MCFAssignment
- No new gossip message types
- No changes to existing RPC signatures
- No changes to MCF solver algorithm
- No changes to existing fee coordination gossip
