# Channel Opener Awareness — Systematic Audit

**Date:** 2026-03-04
**Status:** Approved
**Scope:** Ensure all relevant algorithms in cl-hive and cl_revenue_ops distinguish between channels we opened vs. channels others opened to us.

## Problem

Both plugins treat all channels identically regardless of who opened them. This causes:
- Fee floors that overcharge on remote-opened channels (recovering open costs we never paid)
- The planner blocking expansion to peers who opened channels to us (treating inbound as "already covered")
- The AI advisor having no visibility into channel direction
- Quality scoring ignoring the positive signal that a peer chose to open to us
- Close recommendations that don't account for remote channels being "free" capacity

## Current State

### cl_revenue_ops — uses opener in 3 places:
1. **Profitability cost attribution** — correctly zeros open_cost for remote channels
2. **Virgin channel amnesty** — suppresses scarcity pricing on new remote channels with 0 outbound
3. **Channel open hook** — passes opener to cl-hive

### cl-hive — stores opener but never uses it:
- `peer_events` table has `opener` column (written, never queried for decisions)
- `compute_node_summary()` counts all channels equally
- Planner blocks expansion to any peer with existing channel regardless of direction
- Quality scorer ignores opener signal
- MCP server doesn't expose opener to AI advisor

## Fixes

### Fix H1: compute_node_summary() — opener breakdown

Add `we_opened` and `they_opened` counts to the summary dict by reading the `opener` field from CLN's `listpeerchannels` response.

```python
# Added to return dict:
'we_opened': 20,       # channels where opener == 'local'
'they_opened': 9,      # channels where opener == 'remote'
```

Flows into AI advisor context via the `node_summary` payload.

**Files:** `modules/planner.py`

### Fix H2: Planner expansion — allow opening to peers who opened to us

Currently `_has_existing_or_pending_channel()` and `get_underserved_targets()` skip any peer with any existing channel. Change: if the only channel(s) to a peer were remote-opened, allow proposing an outbound channel.

**Files:** `modules/planner.py`

### Fix H3: Quality scorer — "they opened to us" is positive signal

The `peer_events` table already stores `opener`. When computing quality scores, treat `opener == 'remote'` channel opens as a +0.1 quality bonus (they chose us as a routing partner — positive indicator of routing demand).

**Files:** `modules/quality_scorer.py`

### Fix H4: MCP channel deep-dive — expose opener

Add `opener` to the channel info returned by the MCP server's channel deep-dive so the AI advisor can see who opened each channel.

**Files:** `tools/mcp-hive-server.py`

### Fix R1: Fee floor — discount remote opens

In `_calculate_floor()`, when `opener == "remote"`, use only `close_cost_sats` instead of `open_cost + close_cost`. We didn't pay to open the channel, so the replacement cost floor should only recover close cost.

Also update `ChainCostDefaults.calculate_floor_ppm()` to accept an optional `opener` parameter — when `"remote"`, use `CHANNEL_CLOSE_COST_SATS` only.

**Files:** `cl_revenue_ops/modules/fee_controller.py`, `cl_revenue_ops/modules/config.py`

### Fix R2: set_initial_fee() — include opener in channel_info

The `channel_info` dict built in `set_initial_fee()` is missing `opener`. Add it so downstream logic (including Virgin Channel Amnesty) can fire correctly from the initial fee path.

**Files:** `cl_revenue_ops/modules/fee_controller.py`

### Fix R3: Close recommendations — factor in zero open cost

In the capacity planner's `_identify_losers()`, remote-opened channels have zero sunk open cost. The break-even calculation should reflect this, making remote channels slightly harder to recommend for closing — they're "free" capacity.

**Files:** `cl_revenue_ops/modules/capacity_planner.py`

## Out of Scope

- Changing rebalancer priority based on opener (flow state and bleeder status already handle this adequately through cost attribution)
- Changing Boltz decisions based on opener (liquidity-driven, not opener-driven)
- Building a centralized OpenerAwareness abstraction (over-engineered; each fix is small and isolated)

## Testing

- Unit tests for `compute_node_summary()` opener breakdown
- Unit tests for planner allowing expansion to remote-opened peers
- Unit tests for quality scorer opener bonus
- Unit tests for fee floor discount on remote opens
- Unit tests for `set_initial_fee()` passing opener through
- Verify existing test suites pass in both plugins
