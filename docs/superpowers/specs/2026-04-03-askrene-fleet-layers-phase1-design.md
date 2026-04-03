# askrene Fleet Intelligence Layers — Phase 1: Core Layer Management

**Date:** 2026-04-03
**Status:** Approved
**Scope:** cl-hive (new background loop step + layer management), cl-revenue-ops (HiveRouter consumes layers)

## Problem

The askrene hive-fleet layer is currently created by cl-revenue-ops' HiveRouter with only zero-fee overrides and +5 node bias. It doesn't encode actual channel capacity, peer reputation, or fleet topology. Since both plugins run on the same CLN instance, cl-hive should manage the layers (it has the fleet intelligence) and cl-revenue-ops should consume them (it makes routing decisions).

## Goal

Move layer management to cl-hive and add two new layers — `hive-reputation` and enriched `hive-fleet` with capacity constraints — so every `getroutes` call across both plugins automatically benefits from fleet intelligence.

## Changes

### cl-hive: New askrene layer management

#### New module: `modules/askrene_layers.py`

Manages three askrene layers, refreshed from the fee_intelligence_loop:

**`hive-fleet` layer (enriched):**
- Zero-fee overrides for fleet member channels (both directions) — existing behavior
- `askrene-inform-channel` with actual capacity from `listpeerchannels` `to_us_msat` / `total_msat` — NEW
- `askrene-bias-node` +5 on fleet members — existing behavior
- `askrene-age` called with 15-minute cutoff to decay stale capacity info — NEW

**`hive-reputation` layer (new):**
- `askrene-disable-node` for peers with reputation warnings (force_close_count > 2 OR htlc_success_rate < 0.5)
- `askrene-bias-node` scaled by reputation score: +5 for score > 80, +2 for > 60, -3 for < 30, -5 for < 15
- Sourced from `peer_reputation_mgr.get_reputation_stats()` and per-peer aggregated data

**Interface:**
```python
class AskreneLayerManager:
    def __init__(self, plugin, database, peer_reputation_mgr):
        ...
    def refresh_all(self) -> Dict[str, bool]:
        """Refresh all managed layers. Returns {layer_name: success}."""
    def is_available(self) -> bool:
        """Whether askrene is usable on this CLN version."""
```

#### Hook into fee_intelligence_loop

Add as Step 5i (every cycle, ~60s):
```python
# Step 5i: Refresh askrene fleet intelligence layers
try:
    if askrene_layer_mgr:
        askrene_layer_mgr.refresh_all()
except Exception as e:
    plugin.log(f"cl-hive: askrene layer refresh error: {e}", level='debug')
```

### cl-revenue-ops: HiveRouter stops creating layers

`HiveRouter.refresh_layer()` changes from creating/managing the `hive-fleet` layer to checking if cl-hive's layers exist:

```python
def refresh_layer(self) -> bool:
    """Check if hive-managed askrene layers are available."""
    try:
        result = self.plugin.rpc.call("askrene-listlayers", {})
        layers = {l["layer"] for l in result.get("layers", [])}
        self.available = "hive-fleet" in layers
        # Still cache member IDs from hints
        ...
        return self.available
    except Exception:
        self.available = False
        return False
```

If cl-hive's layers don't exist (cl-hive not running), HiveRouter falls back to creating them itself — preserving standalone functionality.

### Layer naming convention

All cl-hive managed layers use prefix `hive-`:
- `hive-fleet` — fleet member channels (fees, capacity, node bias)
- `hive-reputation` — peer quality (disable bad nodes, bias by score)

All cl-revenue-ops managed layers use prefix `revenue-`:
- `revenue-local` (Phase 2) — local profitability biases and job reservations

Consumer pattern:
```python
layers = ["auto.localchans", "auto.sourcefree"]
# Add all available fleet layers
try:
    all_layers = plugin.rpc.call("askrene-listlayers", {})
    for l in all_layers.get("layers", []):
        if l["layer"].startswith("hive-"):
            layers.append(l["layer"])
except Exception:
    pass
```

## What Does NOT Change

- hive-export-hints (still needed for non-routing decisions like fee bias, corridor roles)
- HiveRouter.discover_route() (still calls getroutes, just with layers it didn't create)
- HiveRouter.score_channel_for_hive() (topology scoring, independent of layers)
- Rebalancer Tier 1 (hive member source bonus, independent of askrene)

## Testing

- cl-hive test suite passes
- cl-revenue-ops test suite passes (HiveRouter graceful degradation)
- After deployment: `askrene-listlayers` shows `hive-fleet` and `hive-reputation`
- `getroutes` with fleet layers returns routes that avoid disabled bad peers
