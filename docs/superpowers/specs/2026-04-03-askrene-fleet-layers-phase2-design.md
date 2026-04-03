# askrene Fleet Intelligence Layers — Phase 2: Corridor & Traffic Biases

**Date:** 2026-04-03
**Status:** Approved
**Scope:** cl-hive (extend layer manager), cl-revenue-ops (new revenue-local layer, fee controller integration)
**Depends on:** Phase 1

## Goal

Add corridor-value and traffic-pattern layers from cl-hive, a local profitability layer from cl-revenue-ops, and integrate routing topology into the fee controller's DTS/PID system.

## Changes

### cl-hive: Two new askrene layers

#### `hive-corridors` layer

Managed by `AskreneLayerManager`, sourced from `fee_coordination_mgr`:

- For each valuable corridor assignment (primary or secondary):
  - `askrene-bias-channel` on channels that serve the corridor, with bias scaled by corridor value_score:
    - value_score > 50: bias +8
    - value_score > 20: bias +4
    - value_score > 5: bias +2
  - `askrene-update-channel` with corridor-optimal fees on fleet member channels serving the corridor (`primary_fee_ppm` for primary, `secondary_fee_ppm` for secondary)
- Refreshed when corridor assignments change (fee_intelligence_loop cycle)

#### `hive-traffic` layer

Managed by `AskreneLayerManager`, sourced from `traffic_intel_mgr`:

- For each peer with aggregated traffic profile:
  - `askrene-bias-channel` based on drain direction:
    - `inbound_heavy`: bias outbound direction +3 (help rebalance naturally)
    - `outbound_heavy`: bias inbound direction +3
    - `balanced`: no bias
  - Bias multiplied by `traffic_confidence` (0-1)
- `askrene-age` with 6-hour cutoff (traffic patterns change slowly)

### cl-revenue-ops: `revenue-local` layer

New layer managed by HiveRouter (or a dedicated local layer manager):

- `askrene-inform-channel` with actual local capacity per direction from `listpeerchannels`
- `askrene-bias-channel` from profitability classification:
  - PROFITABLE: +3
  - BREAK_EVEN: 0
  - UNDERWATER: -3
  - STAGNANT_CANDIDATE: -5
  - ZOMBIE: -8
- `askrene-reserve` when rebalancer starts a job, `askrene-unreserve` when it completes
  - Coordinates with `job_manager.active_channels`
  - Prevents `getroutes` from over-committing capacity on channels with in-flight rebalances
- Refreshed each rebalance cycle

### cl-revenue-ops: Fee controller DTS/PID enhancement

#### Extend hive-export-hints with centrality

cl-hive adds two new hint fields:
- `external_centrality`: from `network_metrics_calculator` (0.0-0.1 range, betweenness approximation)
- `reputation_score`: from `peer_reputation_mgr` (0-100)

cl-revenue-ops `HiveHintAdapter` exposes:
- `get_centrality(peer_id) -> float` (returns 0.0 if unavailable)
- `get_reputation_score(peer_id) -> int` (returns 50 if unavailable)

#### Fee controller integration

In `_adjust_channel_fee()`, before DTS sampling:

- High-centrality corridor owners (`centrality > 0.03` AND `corridor_role == "owner"`): apply exploration boost to Thompson posterior (widen variance by 50%), allowing the DTS to discover higher-fee optima for structurally important peers
- Low-reputation peers (`reputation_score < 30`): no change to fees (don't subsidize bad peers)
- This is a gentle influence, not a hard override — the DTS posterior still converges on observed data

## Consumer Layer Pattern

After Phase 2, `getroutes` calls include:
```python
layers = ["auto.localchans", "auto.sourcefree",
          "hive-fleet", "hive-reputation",      # Phase 1
          "hive-corridors", "hive-traffic",      # Phase 2
          "revenue-local"]                       # Phase 2
```

Each layer is optional — if missing, `getroutes` ignores it gracefully.

## What Does NOT Change

- Boltz integration (already uses HiveRouter which benefits from all layers)
- Rebalancer source selection (Tier 1 bonuses independent of layers)
- Planner expansion logic (uses separate analysis functions)
- hive-export-hints existing fields (only adds centrality + reputation_score)

## Testing

- Verify corridor bias: channels on high-value corridors rank higher in `getroutes`
- Verify traffic bias: drain-direction channels preferred for rebalancing
- Verify reserve/unreserve: in-flight rebalance capacity not double-booked
- Verify DTS boost: high-centrality peers explore higher fees over time
