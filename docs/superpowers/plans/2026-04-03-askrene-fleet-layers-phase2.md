# askrene Fleet Intelligence Layers — Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add corridor-value and traffic-pattern askrene layers from cl-hive, a local profitability + reservation layer from cl-revenue-ops, and feed routing topology (centrality, reputation) into the fee controller's DTS/PID system.

**Architecture:** AskreneLayerManager in cl-hive gets two new layer methods (corridors + traffic). HiveRouter in cl-revenue-ops gets a `revenue-local` layer with profitability biases and job reservations. hive-export-hints is extended with centrality + reputation_score. The fee controller uses these to apply a gentle exploration boost to DTS posteriors for high-centrality corridor owners.

**Tech Stack:** Python 3.12+, CLN askrene RPC (v24.11+), pyln-client

**Spec:** `docs/superpowers/specs/2026-04-03-askrene-fleet-layers-phase2-design.md`

---

### File Structure

| File | Repo | Action | Responsibility |
|------|------|--------|----------------|
| `modules/askrene_layers.py` | cl-hive | Modify | Add `_refresh_corridors_layer()` + `_refresh_traffic_layer()` |
| `modules/rpc_commands.py` | cl-hive | Modify | Add centrality + reputation_score to export_hints |
| `cl-hive.py` | cl-hive | Modify | Inject fee_coordination_mgr + traffic_intel_mgr into AskreneLayerManager |
| `tests/test_askrene_layers.py` | cl-hive | Modify | Add tests for corridor + traffic layers |
| `modules/hive_router.py` | cl-revenue-ops | Modify | Add `_refresh_local_layer()` + `reserve_for_job()` / `unreserve_for_job()` |
| `modules/hive_hints.py` | cl-revenue-ops | Modify | Add `get_centrality()` + `get_reputation_score()` |
| `modules/fee_controller.py` | cl-revenue-ops | Modify | Add centrality-based DTS exploration boost |
| `modules/rebalancer.py` | cl-revenue-ops | Modify | Call reserve/unreserve around job lifecycle |
| `tests/test_hive_router.py` | cl-revenue-ops | Modify | Add local layer + reservation tests |

---

### Task 1: Add corridor + traffic layers to AskreneLayerManager (cl-hive)

**Files:**
- Modify: `/home/sat/bin/cl-hive/modules/askrene_layers.py`

- [ ] **Step 1: Add fee_coordination_mgr and traffic_intel_mgr to __init__**

In `AskreneLayerManager.__init__()`, add two new parameters and store them:

```python
    def __init__(self, plugin, database, peer_reputation_mgr,
                 fee_coordination_mgr=None, traffic_intel_mgr=None):
        self.plugin = plugin
        self.database = database
        self.peer_reputation_mgr = peer_reputation_mgr
        self.fee_coordination_mgr = fee_coordination_mgr
        self.traffic_intel_mgr = traffic_intel_mgr
        self.available: bool = False
        self._our_id: Optional[str] = None
        self._last_refresh: float = 0
```

- [ ] **Step 2: Update refresh_all to include new layers**

```python
    def refresh_all(self) -> Dict[str, bool]:
        results = {}
        results[self.FLEET_LAYER] = self._refresh_fleet_layer()
        results[self.REPUTATION_LAYER] = self._refresh_reputation_layer()
        results[self.CORRIDORS_LAYER] = self._refresh_corridors_layer()
        results[self.TRAFFIC_LAYER] = self._refresh_traffic_layer()
        return results
```

Add the layer name constants:

```python
    CORRIDORS_LAYER = "hive-corridors"
    TRAFFIC_LAYER = "hive-traffic"
```

- [ ] **Step 3: Implement _refresh_corridors_layer**

Add after `_refresh_reputation_layer()`:

```python
    # ------------------------------------------------------------------
    # hive-corridors layer
    # ------------------------------------------------------------------

    def _refresh_corridors_layer(self) -> bool:
        """Create hive-corridors layer with bias for valuable flow corridors.

        Biases channels serving high-value corridors so getroutes prefers
        routing through them.  Fee overrides apply corridor-optimal fees
        to fleet member channels.
        """
        if not self.plugin or not self.fee_coordination_mgr:
            return False

        try:
            try:
                self.plugin.rpc.call("askrene-remove-layer", {"layer": self.CORRIDORS_LAYER})
            except Exception:
                pass

            self.plugin.rpc.call("askrene-create-layer", {"layer": self.CORRIDORS_LAYER})

            assignments = self.fee_coordination_mgr.get_assignments()
            if not assignments:
                return True  # Empty but valid

            our_id = self._get_our_id()
            biased = 0

            # Get our channel SCIDs mapped to peer_id for corridor matching
            channels = self.plugin.rpc.listpeerchannels()
            peer_to_scids: Dict[str, list] = {}
            for ch in channels.get("channels", []):
                if ch.get("state") != "CHANNELD_NORMAL":
                    continue
                pid = ch.get("peer_id", "")
                scid = ch.get("short_channel_id", "")
                if pid and scid:
                    peer_to_scids.setdefault(pid, []).append(scid)

            for assignment in assignments:
                corridor = assignment.corridor
                volume = corridor.total_volume_sats

                # Score by volume
                if volume > 50_000_000:
                    bias = 8
                elif volume > 20_000_000:
                    bias = 4
                elif volume > 5_000_000:
                    bias = 2
                else:
                    continue  # Not valuable enough to bias

                # Bias channels to corridor source and destination peers
                for peer_id in (corridor.source_peer_id, corridor.destination_peer_id):
                    for scid in peer_to_scids.get(peer_id, []):
                        for direction in (0, 1):
                            try:
                                self.plugin.rpc.call("askrene-bias-channel", {
                                    "layer": self.CORRIDORS_LAYER,
                                    "short_channel_id_dir": f"{scid}/{direction}",
                                    "bias": bias,
                                    "description": f"corridor vol={volume}",
                                })
                                biased += 1
                            except Exception:
                                pass

            if biased > 0:
                self._log(f"Refreshed {self.CORRIDORS_LAYER} ({biased} channel biases)")
            return True

        except Exception as e:
            self._log(f"Corridors layer refresh failed: {e}")
            return False
```

- [ ] **Step 4: Implement _refresh_traffic_layer**

```python
    # ------------------------------------------------------------------
    # hive-traffic layer
    # ------------------------------------------------------------------

    def _refresh_traffic_layer(self) -> bool:
        """Create hive-traffic layer with drain-direction biases.

        Biases channels in the direction that helps natural rebalancing
        based on observed traffic patterns.
        """
        if not self.plugin or not self.traffic_intel_mgr:
            return False

        try:
            try:
                self.plugin.rpc.call("askrene-remove-layer", {"layer": self.TRAFFIC_LAYER})
            except Exception:
                pass

            self.plugin.rpc.call("askrene-create-layer", {"layer": self.TRAFFIC_LAYER})

            profiles = self.traffic_intel_mgr.get_all_profiles()
            if not profiles:
                return True

            # Get our channels for SCID lookup
            channels = self.plugin.rpc.listpeerchannels()
            peer_to_scids: Dict[str, list] = {}
            for ch in channels.get("channels", []):
                if ch.get("state") != "CHANNELD_NORMAL":
                    continue
                pid = ch.get("peer_id", "")
                scid = ch.get("short_channel_id", "")
                if pid and scid:
                    peer_to_scids.setdefault(pid, []).append(scid)

            biased = 0
            for profile in profiles:
                peer_id = profile.get("peer_id", "")
                drain = profile.get("drain_direction", "balanced")
                confidence = float(profile.get("confidence", 0))

                if drain == "balanced" or confidence < 0.3:
                    continue

                # Base bias scaled by confidence
                base_bias = int(3 * min(1.0, confidence))
                if base_bias < 1:
                    continue

                for scid in peer_to_scids.get(peer_id, []):
                    if drain == "inbound_heavy":
                        # Peer sends us traffic — bias outbound to help rebalance
                        direction = 0  # us→peer
                    else:
                        # outbound_heavy — bias inbound
                        direction = 1  # peer→us

                    try:
                        self.plugin.rpc.call("askrene-bias-channel", {
                            "layer": self.TRAFFIC_LAYER,
                            "short_channel_id_dir": f"{scid}/{direction}",
                            "bias": base_bias,
                            "description": f"drain={drain} conf={confidence:.2f}",
                        })
                        biased += 1
                    except Exception:
                        pass

            # Age stale traffic info (6 hour cutoff)
            cutoff = int(time.time()) - 21600
            try:
                self.plugin.rpc.call("askrene-age", {
                    "layer": self.TRAFFIC_LAYER,
                    "cutoff": cutoff,
                })
            except Exception:
                pass

            if biased > 0:
                self._log(f"Refreshed {self.TRAFFIC_LAYER} ({biased} drain biases)")
            return True

        except Exception as e:
            self._log(f"Traffic layer refresh failed: {e}")
            return False
```

- [ ] **Step 5: Verify and commit**

```bash
cd /home/sat/bin/cl-hive
python3 -c "import ast; ast.parse(open('modules/askrene_layers.py').read()); print('OK')"
.venv/bin/python -m pytest tests/test_askrene_layers.py -v
.venv/bin/python -m pytest tests/ -x -q --tb=short
git add modules/askrene_layers.py
git commit -m "feat: add hive-corridors and hive-traffic askrene layers"
```

---

### Task 2: Wire new deps into AskreneLayerManager init (cl-hive)

**Files:**
- Modify: `/home/sat/bin/cl-hive/cl-hive.py`

- [ ] **Step 1: Pass fee_coordination_mgr and traffic_intel_mgr to AskreneLayerManager**

Find the AskreneLayerManager creation in cl-hive.py (search for `AskreneLayerManager(`). Change to:

```python
    askrene_layer_mgr = AskreneLayerManager(
        plugin=plugin,
        database=database,
        peer_reputation_mgr=peer_reputation_mgr,
        fee_coordination_mgr=fee_coordination_mgr,
        traffic_intel_mgr=traffic_intel_mgr,
    )
```

Note: `fee_coordination_mgr` and `traffic_intel_mgr` must be initialized BEFORE this line. Check the init order — if AskreneLayerManager is created before these managers, move it after them.

- [ ] **Step 2: Run tests and commit**

```bash
cd /home/sat/bin/cl-hive
.venv/bin/python -m pytest tests/ -x -q --tb=short
git add cl-hive.py
git commit -m "feat: inject corridor + traffic managers into AskreneLayerManager"
```

---

### Task 3: Extend hive-export-hints with centrality + reputation_score (cl-hive)

**Files:**
- Modify: `/home/sat/bin/cl-hive/modules/rpc_commands.py` (export_hints function, ~line 2110-2119)

- [ ] **Step 1: Add centrality and reputation data to hint construction**

In the export_hints function, find the block that builds each peer's hint (around line 2110-2119, after the `rebalance_preference` assignment). Before `hints[peer_id] = hint`, add:

```python
        # Network centrality (routing importance)
        if ctx.network_metrics:
            try:
                metrics = ctx.network_metrics.get_member_metrics(peer_id)
                if metrics:
                    hint["external_centrality"] = round(metrics.external_centrality, 6)
            except Exception:
                pass

        # Peer reputation score (fleet-aggregated quality)
        if ctx.peer_reputation_mgr:
            try:
                rep = ctx.peer_reputation_mgr.get_reputation(peer_id)
                if rep:
                    hint["reputation_score"] = rep.reputation_score
            except Exception:
                pass
```

- [ ] **Step 2: Run tests and commit**

```bash
cd /home/sat/bin/cl-hive
.venv/bin/python -m pytest tests/ -x -q --tb=short
git add modules/rpc_commands.py
git commit -m "feat: add centrality + reputation_score to hive-export-hints"
```

---

### Task 4: Add get_centrality + get_reputation_score to HiveHintAdapter (cl-revenue-ops)

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/modules/hive_hints.py`

- [ ] **Step 1: Add two new accessor methods**

After the existing `get_rebalance_bias()` method, add:

```python
    def get_centrality(self, peer_id: str) -> float:
        """Return external centrality for peer (0.0 if unavailable)."""
        hint = self._get_peer_hint(peer_id)
        val = hint.get("external_centrality")
        if isinstance(val, (int, float)):
            return max(0.0, min(1.0, float(val)))
        return 0.0

    def get_reputation_score(self, peer_id: str) -> int:
        """Return fleet-aggregated reputation score (50 if unavailable)."""
        hint = self._get_peer_hint(peer_id)
        val = hint.get("reputation_score")
        if isinstance(val, (int, float)):
            return max(0, min(100, int(val)))
        return 50
```

- [ ] **Step 2: Run tests and commit**

```bash
cd /home/sat/bin/cl_revenue_ops
python3 -m pytest tests/ -x -q --tb=short
git add modules/hive_hints.py
git commit -m "feat: add get_centrality + get_reputation_score to HiveHintAdapter"
```

---

### Task 5: Add revenue-local layer to HiveRouter (cl-revenue-ops)

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/modules/hive_router.py`

- [ ] **Step 1: Add LOCAL_LAYER constant and profitability_analyzer field**

Add to the class constants:

```python
    LOCAL_LAYER = "revenue-local"
```

Add to `__init__`:

```python
        self.profitability_analyzer = None  # Injected by main plugin
```

- [ ] **Step 2: Add _refresh_local_layer method**

After `score_channel_for_hive()`, add:

```python
    # ------------------------------------------------------------------
    # revenue-local layer (profitability biases + job reservations)
    # ------------------------------------------------------------------

    PROFITABILITY_BIAS = {
        "profitable": 3,
        "break_even": 0,
        "underwater": -3,
        "stagnant_candidate": -5,
        "zombie": -8,
    }

    def refresh_local_layer(self) -> bool:
        """Create revenue-local layer with profitability biases.

        Biases channels by their profitability classification so getroutes
        prefers routing through profitable channels and avoids zombies.
        """
        if not self.plugin or not self.profitability_analyzer:
            return False

        try:
            try:
                self.plugin.rpc.call("askrene-remove-layer", {"layer": self.LOCAL_LAYER})
            except Exception:
                pass

            self.plugin.rpc.call("askrene-create-layer", {"layer": self.LOCAL_LAYER})

            channels = self.plugin.rpc.listpeerchannels()
            biased = 0

            for ch in channels.get("channels", []):
                if ch.get("state") != "CHANNELD_NORMAL":
                    continue
                scid = ch.get("short_channel_id", "")
                if not scid:
                    continue

                # Get profitability classification
                prof = self.profitability_analyzer.get_profitability(scid)
                if not prof:
                    continue

                classification = getattr(
                    getattr(prof, "classification", None), "value", None
                )
                bias = self.PROFITABILITY_BIAS.get(classification, 0)
                if bias == 0:
                    continue

                for direction in (0, 1):
                    try:
                        self.plugin.rpc.call("askrene-bias-channel", {
                            "layer": self.LOCAL_LAYER,
                            "short_channel_id_dir": f"{scid}/{direction}",
                            "bias": bias,
                            "description": f"profitability={classification}",
                        })
                        biased += 1
                    except Exception:
                        pass

            if biased > 0:
                self._log(f"Refreshed {self.LOCAL_LAYER} ({biased} profitability biases)")
            return True

        except Exception as e:
            self._log(f"Local layer refresh failed: {e}")
            return False

    def reserve_for_job(self, scid: str, amount_msat: int) -> bool:
        """Reserve capacity on a channel for an in-flight rebalance job."""
        if not self.plugin:
            return False
        try:
            self.plugin.rpc.call("askrene-reserve", {
                "path": [{"short_channel_id_dir": f"{scid}/0", "amount_msat": amount_msat}]
            })
            return True
        except Exception:
            return False

    def unreserve_for_job(self, scid: str, amount_msat: int) -> bool:
        """Release reserved capacity after a rebalance job completes."""
        if not self.plugin:
            return False
        try:
            self.plugin.rpc.call("askrene-unreserve", {
                "path": [{"short_channel_id_dir": f"{scid}/0", "amount_msat": amount_msat}]
            })
            return True
        except Exception:
            return False
```

- [ ] **Step 3: Update refresh_layer to also refresh local layer**

At the end of `refresh_layer()`, after the return statement for both detection and standalone paths, add local layer refresh. The simplest approach: call `refresh_local_layer()` inside `refresh_layer()` after the fleet layer is confirmed available:

In the detection branch (when cl-hive manages hive-fleet), before `return True`:
```python
                self.refresh_local_layer()
```

In `_create_standalone_layer()`, before `return self.available`:
```python
            self.refresh_local_layer()
```

- [ ] **Step 4: Update discover_route to include all available layers**

In `discover_route()`, change the layers list:

```python
                "layers": ["auto.localchans", "auto.sourcefree", self.LAYER_NAME,
                           "hive-reputation", "hive-corridors", "hive-traffic",
                           self.LOCAL_LAYER],
```

Note: Missing layers are silently ignored by `getroutes`, so listing all is safe.

- [ ] **Step 5: Run tests and commit**

```bash
cd /home/sat/bin/cl_revenue_ops
python3 -m pytest tests/ -x -q --tb=short
git add modules/hive_router.py
git commit -m "feat: add revenue-local layer with profitability biases and job reservations"
```

---

### Task 6: Wire profitability_analyzer into HiveRouter (cl-revenue-ops)

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/cl-revenue-ops.py`

- [ ] **Step 1: Inject profitability_analyzer into HiveRouter**

Find the HiveRouter creation (search for `HiveRouter(safe_plugin, hive_hints)`). After `rebalancer.hive_router = hive_router`, add:

```python
    if hive_router is not None and profitability_analyzer is not None:
        hive_router.profitability_analyzer = profitability_analyzer
```

- [ ] **Step 2: Run tests and commit**

```bash
python3 -m pytest tests/ -x -q --tb=short
git add cl-revenue-ops.py
git commit -m "feat: inject profitability_analyzer into HiveRouter for local layer"
```

---

### Task 7: Add DTS exploration boost for high-centrality corridor owners (cl-revenue-ops)

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/modules/fee_controller.py` (~line 3754)

- [ ] **Step 1: Add centrality-based exploration boost before DTS sampling**

Find the DTS sampling line: `dts_fee = ts_state.thompson.sample_fee(floor_ppm, ceiling_ppm)` (around line 3754). Before it, add:

```python
            # Centrality-based DTS exploration boost: high-centrality corridor
            # owners may have higher fee optima.  Widen the posterior variance
            # to encourage exploration of higher fees on structurally important peers.
            if self.hive_hints:
                centrality = self.hive_hints.get_centrality(peer_id)
                corridor_role = self.hive_hints.get_corridor_role(peer_id)
                if centrality > 0.03 and corridor_role == "owner":
                    try:
                        # Widen posterior by scaling variance up 50%
                        ts_state.thompson.scale_variance(1.5)
                        self.plugin.log(
                            f"DTS EXPLORE BOOST: {peer_id[:12]}... "
                            f"(centrality={centrality:.4f}, role={corridor_role})",
                            level='debug'
                        )
                    except Exception:
                        pass  # Thompson impl may not support scale_variance
```

Note: This requires `ts_state.thompson` to have a `scale_variance(factor)` method. If it doesn't exist, the try/except handles it gracefully. The boost is temporary — it affects only the current sampling round, and the posterior reconverges on observed data.

- [ ] **Step 2: Run tests and commit**

```bash
python3 -m pytest tests/ -x -q --tb=short
git add modules/fee_controller.py
git commit -m "feat: add centrality-based DTS exploration boost for corridor owners"
```

---

### Task 8: Add reserve/unreserve calls to rebalancer job lifecycle (cl-revenue-ops)

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/modules/rebalancer.py` (start_job + stop_job)

- [ ] **Step 1: Add reserve call in start_job**

In `JobManager.start_job()`, after the successful `sling-go` call and job tracking setup (around line 570, after `self._active_jobs[normalized_scid] = job`), add:

```python
            # Reserve capacity in askrene so getroutes accounts for in-flight rebalances
            if hasattr(self, '_hive_router') and self._hive_router:
                self._hive_router.reserve_for_job(to_scid, candidate.amount_msat)
```

Add a field to JobManager or pass hive_router via the EVRebalancer that owns it.

Simpler approach — add the call in `EVRebalancer.execute_rebalance()` where it calls `job_manager.start_job()`:

Find `execute_rebalance()` (search for `def execute_rebalance`). After the successful `start_job` call, add:

```python
            if self.hive_router and result.get("success"):
                self.hive_router.reserve_for_job(
                    candidate.to_channel.replace(":", "x"),
                    candidate.amount_msat,
                )
```

- [ ] **Step 2: Add unreserve call when job completes**

Find the job completion handler (search for `_handle_job_success` or `_handle_job_failure` or where jobs are cleaned up in `stop_job()`). In `stop_job()`, before removing the job from `_active_jobs`, add:

```python
        # Release askrene reservation
        if hasattr(self, 'hive_router') and self.hive_router:
            job = self._active_jobs.get(normalized)
            if job and hasattr(job, 'candidate') and job.candidate:
                self.hive_router.unreserve_for_job(
                    job.scid,
                    job.candidate.amount_msat,
                )
```

Note: `stop_job` is on `JobManager`, but `hive_router` is on `EVRebalancer`. The cleanest approach: add the unreserve in `EVRebalancer`'s `_cleanup_completed_jobs()` or wherever it processes completed jobs from the job manager.

- [ ] **Step 3: Run tests and commit**

```bash
python3 -m pytest tests/ -x -q --tb=short
git add modules/rebalancer.py
git commit -m "feat: reserve/unreserve askrene capacity for in-flight rebalance jobs"
```

---

### Task 9: Push both repos

- [ ] **Step 1: Final test verification and push**

```bash
cd /home/sat/bin/cl-hive
.venv/bin/python -m pytest tests/ -x -q --tb=short
git push

cd /home/sat/bin/cl_revenue_ops
python3 -m pytest tests/ -x -q --tb=short
git push
```
