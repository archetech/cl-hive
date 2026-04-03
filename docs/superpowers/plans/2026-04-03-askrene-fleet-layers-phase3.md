# askrene Fleet Intelligence Layers — Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace legacy inbound fee estimation with fleet-aware getroutes, add temporal fee optimization from traffic patterns, and add routing-aware expansion scoring using virtual askrene channels.

**Architecture:** The rebalancer's 6-priority fee estimation collapses to: historical (1-3) → getroutes with all layers (4) → fallback (5). cl-hive exports peak_hours, drain_direction, fee_elasticity, optimal_fee_estimate in hints. The fee controller applies a temporal multiplier (0.9-1.1) from peak/quiet hours. The planner scores expansion targets by creating virtual askrene channels and measuring routing improvement.

**Tech Stack:** Python 3.12+, CLN askrene RPC (v24.11+), pyln-client

**Spec:** `docs/superpowers/specs/2026-04-03-askrene-fleet-layers-phase3-design.md`

---

### File Structure

| File | Repo | Action | Responsibility |
|------|------|--------|----------------|
| `modules/rebalancer.py` | cl-revenue-ops | Modify | Replace priorities 4-6 in `_estimate_inbound_fee()` with getroutes |
| `modules/rpc_commands.py` | cl-hive | Modify | Add peak_hours, drain_direction, fee_elasticity, optimal_fee to hints |
| `modules/hive_hints.py` | cl-revenue-ops | Modify | Add 4 new accessor methods |
| `modules/fee_controller.py` | cl-revenue-ops | Modify | Add `_get_temporal_fee_adjustment()` |
| `modules/planner.py` | cl-hive | Modify | Add routing-aware scoring to `get_expansion_recommendation()` |

---

### Task 1: Replace `_estimate_inbound_fee()` priorities 4-6 with getroutes (cl-revenue-ops)

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/modules/rebalancer.py`

- [ ] **Step 1: Replace the last-hop + route + fallback chain**

In `_estimate_inbound_fee()`, find the block starting at `# No historical data - fall back to heuristics` (around line 3631). Replace everything from that comment through the end of the method with:

```python
        # Priority 4: getroutes with fleet layers (replaces last-hop + route + fallback)
        # All fleet intelligence (corridors, traffic, reputation, profitability)
        # is encoded in askrene layers — a single getroutes call captures it all.
        if self.hive_router and self.hive_router.available:
            route = self.hive_router.discover_route(peer_id, amount_msat // 1000)
            if route and route.fee_ppm >= 0:
                estimate = route.fee_ppm
                # Ensure estimate respects failure floor
                if failed_floor > 0 and estimate <= failed_floor:
                    estimate = failed_floor + 25
                self.plugin.log(
                    f"INBOUND FEE EST [{peer_id[:12]}...]: Fleet-aware getroutes "
                    f"{estimate} PPM ({route.hops} hops, fail_floor={failed_floor})",
                    level='debug'
                )
                return estimate

        # Priority 5: Last-hop fee + buffer (legacy fallback when no fleet layers)
        if last_hop is not None:
            estimate = last_hop + self.config.inbound_fee_estimate_ppm
            if failed_floor > 0 and estimate <= failed_floor:
                estimate = failed_floor + 25
            self.plugin.log(
                f"INBOUND FEE EST [{peer_id[:12]}...]: Last-hop fallback "
                f"{estimate} PPM (last_hop={last_hop}, fail_floor={failed_floor})",
                level='debug'
            )
            return estimate

        # Priority 6: Configured default
        fallback = self.config.inbound_fee_estimate_ppm
        self.plugin.log(
            f"INBOUND FEE EST [{peer_id[:12]}...]: Default fallback {fallback} PPM",
            level='debug'
        )
        return fallback
```

This preserves the failed_floor logic while replacing the old `_get_route_fee_estimate()` with `hive_router.discover_route()`. The last-hop and default fallbacks remain for when askrene is unavailable.

- [ ] **Step 2: Run tests and commit**

```bash
cd /home/sat/bin/cl_revenue_ops
python3 -m pytest tests/ -x -q --tb=short
git add modules/rebalancer.py
git commit -m "feat: replace legacy inbound fee estimation with fleet-aware getroutes"
```

---

### Task 2: Extend hive-export-hints with traffic + fee intelligence (cl-hive)

**Files:**
- Modify: `/home/sat/bin/cl-hive/modules/rpc_commands.py`

- [ ] **Step 1: Add traffic profile data to hint construction**

In the export_hints function, find the block that adds `traffic_confidence` (around line 2077). After the traffic_confidence block and before the reputation_score block, add:

```python
        # Traffic profile data (peak hours, drain direction)
        if ctx.traffic_intel_mgr:
            try:
                profiles = ctx.traffic_intel_mgr.get_all_profiles(peer_id=peer_id)
                if profiles:
                    # Use highest-confidence profile
                    best = max(profiles, key=lambda p: float(p.get("confidence", 0)))
                    peak = best.get("peak_hours_utc")
                    if isinstance(peak, list) and peak:
                        hint["peak_hours_utc"] = [int(h) for h in peak[:6]]
                    drain = best.get("drain_direction")
                    if drain in ("inbound_heavy", "outbound_heavy", "balanced"):
                        hint["drain_direction"] = drain
            except Exception:
                pass

        # Fee intelligence (elasticity, optimal fee estimate)
        if ctx.fee_intel_mgr:
            try:
                profile = ctx.fee_intel_mgr.get_aggregated_profile(peer_id)
                if profile:
                    elasticity = profile.get("estimated_elasticity")
                    if isinstance(elasticity, (int, float)):
                        hint["fee_elasticity"] = round(float(elasticity), 3)
                    optimal = profile.get("optimal_fee_estimate")
                    if isinstance(optimal, (int, float)) and optimal > 0:
                        hint["optimal_fee_estimate_ppm"] = int(optimal)
            except Exception:
                pass
```

Note: `ctx.fee_intel_mgr` should already be available on HiveContext. If not, check the context — it may be accessed via `fee_intel_mgr` module-level global. Adapt the access pattern to match existing code.

- [ ] **Step 2: Run tests and commit**

```bash
cd /home/sat/bin/cl-hive
.venv/bin/python -m pytest tests/ -x -q --tb=short
git add modules/rpc_commands.py
git commit -m "feat: add peak_hours, drain_direction, fee_elasticity to export_hints"
```

---

### Task 3: Add HiveHintAdapter accessors + temporal fee adjustment (cl-revenue-ops)

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/modules/hive_hints.py`
- Modify: `/home/sat/bin/cl_revenue_ops/modules/fee_controller.py`

- [ ] **Step 1: Add 4 new accessor methods to HiveHintAdapter**

In `/home/sat/bin/cl_revenue_ops/modules/hive_hints.py`, after `get_reputation_score()`, add:

```python
    def get_peak_hours(self, peer_id: str) -> list:
        """Return peak traffic hours UTC (empty list if unavailable)."""
        hint = self._get_peer_hint(peer_id)
        val = hint.get("peak_hours_utc")
        if isinstance(val, list):
            return [int(h) for h in val if isinstance(h, (int, float)) and 0 <= h <= 23]
        return []

    def get_drain_direction(self, peer_id: str) -> str:
        """Return drain direction: inbound_heavy|outbound_heavy|balanced."""
        hint = self._get_peer_hint(peer_id)
        val = hint.get("drain_direction")
        if val in ("inbound_heavy", "outbound_heavy", "balanced"):
            return val
        return "balanced"

    def get_fee_elasticity(self, peer_id: str) -> float:
        """Return estimated price elasticity (0.0 if unavailable)."""
        hint = self._get_peer_hint(peer_id)
        val = hint.get("fee_elasticity")
        if isinstance(val, (int, float)):
            return float(val)
        return 0.0

    def get_optimal_fee_estimate(self, peer_id: str) -> int:
        """Return fleet-estimated optimal fee PPM (0 if unavailable)."""
        hint = self._get_peer_hint(peer_id)
        val = hint.get("optimal_fee_estimate_ppm")
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
        return 0
```

- [ ] **Step 2: Add `_get_temporal_fee_adjustment()` to fee controller**

In `/home/sat/bin/cl_revenue_ops/modules/fee_controller.py`, find the `_get_hive_fee_bias()` method (around line 1672). After it, add:

```python
    def _get_temporal_fee_adjustment(self, peer_id: str) -> float:
        """Return temporal fee multiplier (0.9-1.1) based on traffic patterns.

        During peak hours for a peer: maintain/increase fees (1.0-1.1)
        During quiet hours: reduce slightly to attract flow (0.9-1.0)
        Drain direction: inbound_heavy peers get slight reduction, outbound_heavy get increase.

        Gated by traffic_confidence > 0.5 to avoid acting on noisy data.
        """
        if not self.hive_hints:
            return 1.0

        try:
            confidence = self.hive_hints.get_traffic_confidence(peer_id)
            if not isinstance(confidence, (int, float)) or confidence <= 0.5:
                return 1.0

            import time as _time
            current_hour = int(_time.strftime("%H"))

            multiplier = 1.0

            # Peak/quiet hour adjustment
            peak_hours = self.hive_hints.get_peak_hours(peer_id)
            if peak_hours:
                if current_hour in peak_hours:
                    multiplier *= 1.05  # +5% during peak
                else:
                    multiplier *= 0.97  # -3% during quiet

            # Drain direction adjustment
            drain = self.hive_hints.get_drain_direction(peer_id)
            if drain == "inbound_heavy":
                multiplier *= 0.97  # Slight discount to attract inbound
            elif drain == "outbound_heavy":
                multiplier *= 1.03  # Slight premium for outbound service

            # Clamp to [0.9, 1.1]
            return max(0.9, min(1.1, multiplier))

        except Exception:
            return 1.0
```

- [ ] **Step 3: Apply temporal adjustment in `_adjust_channel_fee()`**

Find the hive_fee_bias application block (around line 3792-3794):

```python
            hive_fee_bias = self._get_hive_fee_bias(peer_id)
            if hive_fee_bias != 1.0:
                post_pid_target_ppm = int(post_pid_target_ppm * hive_fee_bias)
```

After it, add:

```python
            # Temporal fee adjustment from traffic patterns
            temporal_adj = self._get_temporal_fee_adjustment(peer_id)
            if temporal_adj != 1.0:
                post_pid_target_ppm = int(post_pid_target_ppm * temporal_adj)
```

- [ ] **Step 4: Run tests and commit**

```bash
cd /home/sat/bin/cl_revenue_ops
python3 -m pytest tests/ -x -q --tb=short
git add modules/hive_hints.py modules/fee_controller.py
git commit -m "feat: add temporal fee optimization from traffic patterns"
```

---

### Task 4: Planner routing-aware expansion scoring (cl-hive)

**Files:**
- Modify: `/home/sat/bin/cl-hive/modules/planner.py`

- [ ] **Step 1: Add `_score_routing_improvement()` method**

In planner.py, find `get_expansion_recommendation()` (around line 838). Before it, add a helper:

```python
    def _score_routing_improvement(self, target: str, cfg) -> float:
        """Score how much a virtual channel to target would improve routing.

        Creates a temporary askrene layer with a virtual channel to the
        target, calls getroutes to high-value destinations, and measures
        whether routes improve (lower fees, fewer hops).

        Returns:
            Score multiplier (1.0 = no improvement, up to 1.5 for significant gains).
        """
        if not self.plugin:
            return 1.0

        our_id = None
        try:
            info = self.plugin.rpc.getinfo()
            our_id = info.get("id")
        except Exception:
            return 1.0
        if not our_id:
            return 1.0

        temp_layer = f"hive-expansion-test-{target[:8]}"

        try:
            # Create temporary layer with virtual channel
            self.plugin.rpc.call("askrene-create-layer", {"layer": temp_layer})
            virtual_scid = "999x999x0"
            self.plugin.rpc.call("askrene-create-channel", {
                "layer": temp_layer,
                "source": our_id,
                "destination": target,
                "short_channel_id": virtual_scid,
                "capacity_msat": "5000000000",  # 5M sats virtual capacity
            })
            # Set reasonable fees on the virtual channel
            for direction in (0, 1):
                self.plugin.rpc.call("askrene-update-channel", {
                    "layer": temp_layer,
                    "short_channel_id_dir": f"{virtual_scid}/{direction}",
                    "fee_base_msat": 0,
                    "fee_proportional_millionths": 100,
                    "cltv_expiry_delta": 18,
                })

            # Pick a few well-known high-value destinations to test routes
            # Use peers from our own channels that have high capacity
            test_destinations = []
            try:
                channels = self.plugin.rpc.listpeerchannels()
                peers_by_cap = []
                for ch in channels.get("channels", []):
                    if ch.get("state") != "CHANNELD_NORMAL":
                        continue
                    pid = ch.get("peer_id", "")
                    total = ch.get("total_msat", 0)
                    if isinstance(total, str):
                        total = int(total.rstrip("msat"))
                    if pid and pid != target:
                        peers_by_cap.append((total, pid))
                peers_by_cap.sort(reverse=True)
                test_destinations = [pid for _, pid in peers_by_cap[:3]]
            except Exception:
                pass

            if not test_destinations:
                return 1.0

            # Compare route quality with and without the virtual channel
            improvement_count = 0
            layers_base = ["auto.localchans", "auto.sourcefree",
                           "hive-fleet", "hive-reputation"]
            layers_with = layers_base + [temp_layer]

            for dest in test_destinations:
                try:
                    # Route without virtual channel
                    base_result = self.plugin.rpc.call("getroutes", {
                        "source": our_id,
                        "destination": dest,
                        "amount_msat": 100000000,  # 100k sats test
                        "layers": layers_base,
                        "maxfee_msat": 10000000,  # 10k sats max fee
                        "final_cltv": 18,
                    })
                    base_routes = base_result.get("routes", [])

                    # Route with virtual channel
                    with_result = self.plugin.rpc.call("getroutes", {
                        "source": our_id,
                        "destination": dest,
                        "amount_msat": 100000000,
                        "layers": layers_with,
                        "maxfee_msat": 10000000,
                        "final_cltv": 18,
                    })
                    with_routes = with_result.get("routes", [])

                    # Compare: better probability or fewer hops = improvement
                    base_prob = base_result.get("probability_ppm", 0)
                    with_prob = with_result.get("probability_ppm", 0)

                    base_hops = len(base_routes[0].get("path", [])) if base_routes else 99
                    with_hops = len(with_routes[0].get("path", [])) if with_routes else 99

                    if with_prob > base_prob * 1.1 or with_hops < base_hops:
                        improvement_count += 1

                except Exception:
                    continue

            # Score: 1.0 base, +0.15 per destination that improved, max 1.5
            score = 1.0 + (improvement_count * 0.15)
            return min(1.5, score)

        except Exception:
            return 1.0
        finally:
            # Always clean up temporary layer
            try:
                self.plugin.rpc.call("askrene-remove-layer", {"layer": temp_layer})
            except Exception:
                pass
```

- [ ] **Step 2: Apply routing score in get_expansion_recommendation()**

In `get_expansion_recommendation()`, find where `adjusted_score` is calculated (around line 878: `adjusted_score = base_score * competition_factor`). After the bottleneck bonus block (after `adjusted_score *= BOTTLENECK_BONUS_MULTIPLIER`), add:

```python
        # Routing improvement bonus: does this target create better paths?
        routing_multiplier = self._score_routing_improvement(target, cfg)
        if routing_multiplier > 1.0:
            adjusted_score *= routing_multiplier
            reasoning_parts.append(
                f"Virtual channel test shows {(routing_multiplier-1)*100:.0f}% "
                f"routing improvement"
            )
```

- [ ] **Step 3: Run tests and commit**

```bash
cd /home/sat/bin/cl-hive
.venv/bin/python -m pytest tests/ -x -q --tb=short
git add modules/planner.py
git commit -m "feat: add routing-aware expansion scoring using virtual askrene channels"
```

---

### Task 5: Push both repos

- [ ] **Step 1: Final verification and push**

```bash
cd /home/sat/bin/cl-hive
.venv/bin/python -m pytest tests/ -x -q --tb=short
git push

cd /home/sat/bin/cl_revenue_ops
python3 -m pytest tests/ -x -q --tb=short
git push
```
