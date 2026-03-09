# Traffic Intelligence: Fleet-Shared Traffic Profiles & Predictive Demand Forecast

**Date**: 2026-03-09
**Status**: Approved
**Issue**: lightning-goats/cl-hive#88 (Phases 2 + 3a only)
**Dependency**: lightning-goats/cl_revenue_ops#58 (closed — local traffic profiling done)

## Goal

Extend cl-hive with fleet-shared traffic intelligence and a fleet demand
forecast. Fleet members share per-peer traffic profiles so new nodes opening
channels to known peers don't start blind, rebalances avoid peak-hour
conflicts, and the fleet can predict demand before channels deplete.

## Scope

### In Scope (Phases 2 + 3a)

- **Phase 2a**: `hive-report-traffic-profile` RPC + DB storage
- **Phase 2b**: `TRAFFIC_INTELLIGENCE_BATCH` (32905) gossip + handler
- **Phase 2c**: `hive-traffic-intelligence` query RPC
- **Phase 2d**: `hive-check-rebalance-conflict` RPC (time-aware)
- **Phase 3a**: `hive-fleet-demand-forecast` RPC (Kalman + fleet traffic)

### Out of Scope (deferred to future)

- Phase 3b: Scheduled MCF assignments (time-windowed execution)
- Phase 3c: Size-aware fee coordination gossip

## Architecture

New dedicated `traffic_intelligence.py` module following the proven
`fee_intelligence.py` pattern: RPC ingest → DB store → background loop
broadcast → fleet handler → aggregated query.

### New Module

`modules/traffic_intelligence.py` — single `TrafficIntelligenceManager` class.

Methods:
- `store_local_profile(peer_id, profile_data)` — store profiles from local
  cl-revenue-ops
- `create_traffic_intelligence_batch_message()` — serialize for fleet gossip
- `handle_traffic_intelligence_batch(peer_id, payload)` — receive fleet gossip
- `get_aggregated_profile(peer_id)` — merge reporters weighted by confidence +
  recency
- `get_all_profiles(peer_id=None, profile_type=None)` — query backing
- `check_rebalance_conflict(peer_id, direction, amount_sats)` — temporal
  conflict detection
- `get_fleet_demand_forecast(hours_ahead=6)` — Kalman predictions + fleet
  traffic
- `cleanup_expired_profiles()` — evict past TTL

### New DB Table

```sql
CREATE TABLE IF NOT EXISTS fleet_traffic_intelligence (
    peer_id TEXT NOT NULL,
    reporter_id TEXT NOT NULL,
    profile_type TEXT,
    peak_hours_utc TEXT,
    quiet_hours_utc TEXT,
    avg_forward_size_sats REAL,
    daily_volume_sats REAL,
    drain_direction TEXT,
    confidence REAL,
    observation_window_hours INTEGER,
    received_at REAL,
    ttl_hours REAL DEFAULT 168.0,
    PRIMARY KEY (peer_id, reporter_id)
);
```

### New Gossip Message

`TRAFFIC_INTELLIGENCE_BATCH = 32905` (next available odd number after
`ARBITRATION_VOTE = 32903`).

- Payload: list of traffic profiles (up to 200 peers per batch)
- Rate limit: 1 batch per 6 hours per member
- Added to `RELIABLE_MESSAGE_TYPES` for guaranteed delivery
- Broadcast trigger: `_broadcast_our_traffic_intelligence()` in background loops

### New RPCs

| RPC | Direction | Purpose |
|-----|-----------|---------|
| `hive-report-traffic-profile` | revenue-ops → hive | Ingest local traffic profiles |
| `hive-traffic-intelligence` | revenue-ops → hive | Query aggregated fleet data |
| `hive-check-rebalance-conflict` | revenue-ops → hive | Pre-rebalance temporal check |
| `hive-fleet-demand-forecast` | revenue-ops → hive | Fleet depletion predictions |

#### hive-report-traffic-profile

Args: `peer_id`, `profile_type`, `peak_hours_utc`, `quiet_hours_utc`,
`avg_forward_size_sats`, `daily_volume_sats`, `drain_direction`, `confidence`,
`observation_window_hours`

Returns: `{"status": "accepted", "peer_id": ...}`

#### hive-traffic-intelligence

Args: `peer_id` (optional), `profile_type` (optional)

Returns: aggregated traffic intelligence from all fleet members.

#### hive-check-rebalance-conflict

Args: `peer_id`, `direction` (inbound|outbound), `amount_sats`

Returns:
- `conflict`: bool — any fleet member actively rebalancing through this peer
- `conflicting_member`: str | null
- `peer_in_peak_hours`: bool — any reporter says this peer is in peak hours now
- `suggested_window_utc`: [start, end] | null — optimal window from quiet hours
- `fleet_drain_forecast_sats`: int — combined fleet drain prediction

Logic: queries active MCF assignments (via liquidity_coordinator), then fleet
traffic intelligence for peak hour conflicts, and suggests an optimal window
from the intersection of reporters' quiet hours.

#### hive-fleet-demand-forecast

Args: `hours_ahead` (default 6)

Returns per-member:
- `predicted_depleted_channels[]` with channel_id, predicted_depletion_utc,
  current_local_pct, drain_rate_sats_per_hour
- `predicted_surplus_channels[]`
- `rebalance_demand_sats`
- `optimal_rebalance_window_utc`

Built on AnticipatoryLiquidityManager's existing Kalman velocity predictions,
enriched with fleet traffic intelligence drain rates and peak/quiet windows.

## Data Flow

```
cl-revenue-ops (local traffic profiling)
    │
    ├─ hive-report-traffic-profile ──→ store_local_profile()
    │                                        │
    │                                        ├─→ DB: fleet_traffic_intelligence
    │                                        │
    │                                        └─→ background loop (6h):
    │                                              TRAFFIC_INTELLIGENCE_BATCH
    │                                                → all fleet members
    │                                                  → handle + store
    │
    ├─ hive-traffic-intelligence ────→ get_all_profiles()
    │
    ├─ hive-check-rebalance-conflict → check_rebalance_conflict()
    │                                    ├─ active MCF assignments
    │                                    ├─ fleet peak hours
    │                                    └─ suggest quiet window
    │
    └─ hive-fleet-demand-forecast ──→ get_fleet_demand_forecast()
                                       ├─ Kalman predictions
                                       ├─ fleet drain rates
                                       └─ per-member forecast
```

## Aggregation Strategy

When multiple reporters observe the same peer:
- Peak/quiet hours: confidence-weighted union
- Volume/size metrics: confidence-weighted average
- Profile type: highest-confidence reporter wins
- Drain direction: majority vote, weighted by confidence

## Files Touched

| File | Changes |
|------|---------|
| `modules/traffic_intelligence.py` | **New** — TrafficIntelligenceManager |
| `modules/protocol.py` | Add enum + validate/sign/create functions |
| `modules/protocol_handlers.py` | Add handler for 32905 |
| `modules/background_loops.py` | Add broadcast helper |
| `modules/database.py` | Add table + CRUD methods |
| `modules/rpc_commands.py` | Add 4 RPC implementations |
| `cl-hive.py` | Register RPCs, instantiate manager, wire dispatch |
| `tests/test_traffic_intelligence.py` | **New** — full test suite |

## Cross-Module Dependencies

TrafficIntelligenceManager receives via `__init__`:
- `database` — storage
- `plugin` — logging, RPC
- `anticipatory_liquidity_mgr` — Kalman predictions for forecast
- `liquidity_coordinator` — active MCF assignments for conflict check
- `membership_mgr` — member verification

No circular dependencies.

## Error Handling

- All RPCs return error dicts on failure (never crash plugin)
- Gossip handler validates: signature, timestamp freshness (48h), membership,
  payload schema
- Missing/malformed profiles silently dropped with warning log

## cl-revenue-ops Contract

Not implemented here, but the expected integration:
- Calls `hive-report-traffic-profile` after temporal profiles graduate (7+ days)
- Calls `hive-check-rebalance-conflict` before initiating rebalances
- Calls `hive-fleet-demand-forecast` from capacity planner

## What We Do NOT Do

- No scheduled MCF assignments (Phase 3b — deferred)
- No size-aware fee coordination (Phase 3c — deferred)
- No changes to existing RPC signatures
- No cl-revenue-ops code changes (separate repo)
