# askrene Fleet Intelligence Layers — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move askrene layer management to cl-hive and add a hive-reputation layer so every getroutes call benefits from fleet intelligence, while cl-revenue-ops degrades gracefully when cl-hive is absent.

**Architecture:** New `modules/askrene_layers.py` in cl-hive manages `hive-fleet` (zero-fee + capacity) and `hive-reputation` (peer quality) layers via a background loop step. cl-revenue-ops HiveRouter detects cl-hive-managed layers and stops self-managing, falling back to self-creation when cl-hive is absent.

**Tech Stack:** Python 3.12+, CLN askrene RPC (v24.11+), pyln-client

**Spec:** `docs/superpowers/specs/2026-04-03-askrene-fleet-layers-phase1-design.md`

---

### File Structure

| File | Repo | Action | Responsibility |
|------|------|--------|----------------|
| `modules/askrene_layers.py` | cl-hive | Create | AskreneLayerManager — layer lifecycle, fleet + reputation layers |
| `modules/background_loops.py` | cl-hive | Modify | Hook layer refresh as Step 5i |
| `cl-hive.py` | cl-hive | Modify | Create manager, inject into background_loops |
| `tests/test_askrene_layers.py` | cl-hive | Create | Unit tests for AskreneLayerManager |
| `modules/hive_router.py` | cl-revenue-ops | Modify | Detect cl-hive layers, fallback to self-creation |
| `tests/test_hive_router.py` | cl-revenue-ops | Modify | Add tests for layer detection fallback |

---

### Task 1: Create `modules/askrene_layers.py` in cl-hive

**Files:**
- Create: `/home/sat/bin/cl-hive/modules/askrene_layers.py`

- [ ] **Step 1: Create the module**

```python
"""
askrene_layers — Manage askrene routing layers from fleet intelligence.

Creates and maintains askrene layers that encode fleet knowledge:
- hive-fleet: Zero-fee fleet channels + actual capacity constraints
- hive-reputation: Peer quality biases + bad-peer blocking

Layers are visible to any plugin on the same CLN instance, enabling
cl-revenue-ops to benefit from fleet intelligence via getroutes.

Degrades gracefully when askrene is unavailable (CLN < 24.11).
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Set


class AskreneLayerManager:
    """Manage askrene layers encoding fleet routing intelligence."""

    FLEET_LAYER = "hive-fleet"
    REPUTATION_LAYER = "hive-reputation"

    # Reputation thresholds for node bias
    REPUTATION_EXCELLENT = 80   # bias +5
    REPUTATION_GOOD = 60        # bias +2
    REPUTATION_POOR = 30        # bias -3
    REPUTATION_BAD = 15         # bias -5

    # Reputation thresholds for node disabling
    DISABLE_FORCE_CLOSE_THRESHOLD = 2
    DISABLE_HTLC_SUCCESS_THRESHOLD = 0.5

    def __init__(self, plugin, database, peer_reputation_mgr):
        """
        Args:
            plugin: CLN plugin reference for RPC + logging
            database: HiveDatabase for member queries
            peer_reputation_mgr: PeerReputationManager for reputation data
        """
        self.plugin = plugin
        self.database = database
        self.peer_reputation_mgr = peer_reputation_mgr
        self.available: bool = False
        self._our_id: Optional[str] = None
        self._last_refresh: float = 0

    def _log(self, msg: str, level: str = "debug") -> None:
        if self.plugin:
            self.plugin.log(f"[AskreneLayerManager] {msg}", level=level)

    def _get_our_id(self) -> Optional[str]:
        if self._our_id:
            return self._our_id
        try:
            info = self.plugin.rpc.getinfo()
            self._our_id = info.get("id")
        except Exception:
            pass
        return self._our_id

    def is_available(self) -> bool:
        """Whether askrene is usable on this CLN version."""
        return self.available

    def refresh_all(self) -> Dict[str, bool]:
        """Refresh all managed layers.

        Returns:
            Dict mapping layer name to success bool.
        """
        results = {}
        results[self.FLEET_LAYER] = self._refresh_fleet_layer()
        results[self.REPUTATION_LAYER] = self._refresh_reputation_layer()
        return results

    # ------------------------------------------------------------------
    # hive-fleet layer
    # ------------------------------------------------------------------

    def _refresh_fleet_layer(self) -> bool:
        """Recreate hive-fleet layer with zero-fee overrides and capacity info.

        For each fleet member channel:
        - askrene-update-channel: fee_base_msat=0, fee_proportional=0
        - askrene-inform-channel: actual capacity in each direction
        - askrene-bias-node: +5 on fleet member nodes
        - askrene-age: decay info older than 15 minutes
        """
        if not self.plugin or not self.database:
            return False

        try:
            # Remove and recreate
            try:
                self.plugin.rpc.call("askrene-remove-layer", {"layer": self.FLEET_LAYER})
            except Exception:
                pass

            self.plugin.rpc.call("askrene-create-layer", {"layer": self.FLEET_LAYER})

            our_id = self._get_our_id()
            if not our_id:
                return False

            # Get fleet members
            members = self.database.get_all_members()
            member_ids: Set[str] = {m.get("peer_id") for m in members if m.get("peer_id")}

            # Get channel data
            channels = self.plugin.rpc.listpeerchannels()
            updated = 0

            for ch in channels.get("channels", []):
                if ch.get("state") != "CHANNELD_NORMAL":
                    continue
                peer_id = ch.get("peer_id", "")
                scid = ch.get("short_channel_id", "")
                if not peer_id or not scid or peer_id not in member_ids:
                    continue

                # Zero-fee overrides (both directions)
                for direction in (0, 1):
                    scid_dir = f"{scid}/{direction}"
                    try:
                        self.plugin.rpc.call("askrene-update-channel", {
                            "layer": self.FLEET_LAYER,
                            "short_channel_id_dir": scid_dir,
                            "fee_base_msat": 0,
                            "fee_proportional_millionths": 0,
                            "cltv_expiry_delta": 6,
                        })
                        updated += 1
                    except Exception:
                        pass

                # Capacity constraints via inform-channel
                to_us_msat = ch.get("to_us_msat", 0)
                total_msat = ch.get("total_msat", 0)
                if isinstance(to_us_msat, str):
                    to_us_msat = int(to_us_msat.rstrip("msat"))
                if isinstance(total_msat, str):
                    total_msat = int(total_msat.rstrip("msat"))
                their_msat = max(0, total_msat - to_us_msat)

                # Direction 0 = us→peer (capacity = our local balance)
                # Direction 1 = peer→us (capacity = their balance)
                for direction, cap_msat in ((0, to_us_msat), (1, their_msat)):
                    if cap_msat > 0:
                        try:
                            self.plugin.rpc.call("askrene-inform-channel", {
                                "layer": self.FLEET_LAYER,
                                "short_channel_id_dir": f"{scid}/{direction}",
                                "amount_msat": cap_msat,
                                "inform": "succeeded",
                            })
                        except Exception:
                            pass

            # Node-level bias for fleet members
            for mid in member_ids:
                for direction in ("in", "out"):
                    try:
                        self.plugin.rpc.call("askrene-bias-node", {
                            "layer": self.FLEET_LAYER,
                            "node": mid,
                            "direction": direction,
                            "bias": 5,
                            "description": "hive fleet preference",
                        })
                    except Exception:
                        pass

            # Age stale information (15 minute cutoff)
            cutoff = int(time.time()) - 900
            try:
                self.plugin.rpc.call("askrene-age", {
                    "layer": self.FLEET_LAYER,
                    "cutoff": cutoff,
                })
            except Exception:
                pass

            self.available = updated > 0
            self._last_refresh = time.time()

            if updated > 0:
                self._log(
                    f"Refreshed {self.FLEET_LAYER} ({updated} channel dirs, "
                    f"{len(member_ids)} fleet nodes)",
                )
            return self.available

        except Exception as e:
            self._log(f"Fleet layer refresh failed: {e}")
            self.available = False
            return False

    # ------------------------------------------------------------------
    # hive-reputation layer
    # ------------------------------------------------------------------

    def _refresh_reputation_layer(self) -> bool:
        """Recreate hive-reputation layer with peer quality biases.

        - disable-node for peers with bad reputation (high force closes, low HTLC success)
        - bias-node scaled by reputation score
        """
        if not self.plugin or not self.peer_reputation_mgr:
            return False

        try:
            try:
                self.plugin.rpc.call("askrene-remove-layer", {"layer": self.REPUTATION_LAYER})
            except Exception:
                pass

            self.plugin.rpc.call("askrene-create-layer", {"layer": self.REPUTATION_LAYER})

            all_reps = self.peer_reputation_mgr.get_all_reputations()
            disabled = 0
            biased = 0

            for peer_id, rep in all_reps.items():
                # Disable nodes with dangerous behavior
                if (rep.total_force_closes >= self.DISABLE_FORCE_CLOSE_THRESHOLD
                        or rep.avg_htlc_success < self.DISABLE_HTLC_SUCCESS_THRESHOLD):
                    try:
                        self.plugin.rpc.call("askrene-disable-node", {
                            "layer": self.REPUTATION_LAYER,
                            "node": peer_id,
                        })
                        disabled += 1
                        self._log(
                            f"Disabled {peer_id[:12]}... "
                            f"(force_closes={rep.total_force_closes}, "
                            f"htlc_success={rep.avg_htlc_success:.2f})",
                        )
                    except Exception:
                        pass
                    continue  # Don't bias a disabled node

                # Bias by reputation score
                score = rep.reputation_score
                if score >= self.REPUTATION_EXCELLENT:
                    bias = 5
                elif score >= self.REPUTATION_GOOD:
                    bias = 2
                elif score < self.REPUTATION_BAD:
                    bias = -5
                elif score < self.REPUTATION_POOR:
                    bias = -3
                else:
                    continue  # Score 30-59: no bias

                for direction in ("in", "out"):
                    try:
                        self.plugin.rpc.call("askrene-bias-node", {
                            "layer": self.REPUTATION_LAYER,
                            "node": peer_id,
                            "direction": direction,
                            "bias": bias,
                            "description": f"reputation score {score}",
                        })
                        biased += 1
                    except Exception:
                        pass

            # Age stale reputation info (1 hour cutoff)
            cutoff = int(time.time()) - 3600
            try:
                self.plugin.rpc.call("askrene-age", {
                    "layer": self.REPUTATION_LAYER,
                    "cutoff": cutoff,
                })
            except Exception:
                pass

            if disabled > 0 or biased > 0:
                self._log(
                    f"Refreshed {self.REPUTATION_LAYER} "
                    f"({disabled} disabled, {biased} biased)",
                )
            return True

        except Exception as e:
            self._log(f"Reputation layer refresh failed: {e}")
            return False
```

- [ ] **Step 2: Verify syntax**

```bash
cd /home/sat/bin/cl-hive
python3 -c "import ast; ast.parse(open('modules/askrene_layers.py').read()); print('OK')"
```

Expected: OK

- [ ] **Step 3: Commit**

```bash
git add modules/askrene_layers.py
git commit -m "feat: add AskreneLayerManager for fleet intelligence layers"
```

---

### Task 2: Create tests for AskreneLayerManager

**Files:**
- Create: `/home/sat/bin/cl-hive/tests/test_askrene_layers.py`

- [ ] **Step 1: Create test file**

```python
"""Tests for AskreneLayerManager."""

import time
from unittest.mock import MagicMock, call
import pytest

from modules.askrene_layers import AskreneLayerManager


class MockPeerReputationMgr:
    def __init__(self, reps=None):
        self._reps = reps or {}

    def get_all_reputations(self):
        return self._reps


class MockReputation:
    def __init__(self, score=50, force_closes=0, htlc_success=1.0):
        self.reputation_score = score
        self.total_force_closes = force_closes
        self.avg_htlc_success = htlc_success


class MockDatabase:
    def __init__(self, members=None):
        self._members = members or []

    def get_all_members(self):
        return self._members


class TestAskreneLayerManagerInit:
    def test_defaults(self):
        mgr = AskreneLayerManager(MagicMock(), MockDatabase(), MockPeerReputationMgr())
        assert mgr.available is False
        assert mgr.is_available() is False

    def test_no_plugin(self):
        mgr = AskreneLayerManager(None, MockDatabase(), MockPeerReputationMgr())
        result = mgr.refresh_all()
        assert result["hive-fleet"] is False
        assert result["hive-reputation"] is False


class TestFleetLayer:
    def test_creates_layer_with_zero_fee_and_capacity(self):
        plugin = MagicMock()
        plugin.rpc.getinfo.return_value = {"id": "our_id"}
        plugin.rpc.listpeerchannels.return_value = {
            "channels": [
                {
                    "state": "CHANNELD_NORMAL",
                    "peer_id": "fleet_a",
                    "short_channel_id": "100x1x0",
                    "to_us_msat": 500000000,
                    "total_msat": 1000000000,
                },
                {
                    "state": "CHANNELD_NORMAL",
                    "peer_id": "external",
                    "short_channel_id": "200x1x0",
                    "to_us_msat": 300000000,
                    "total_msat": 1000000000,
                },
            ]
        }
        plugin.rpc.call.return_value = {}

        db = MockDatabase(members=[{"peer_id": "fleet_a"}])
        mgr = AskreneLayerManager(plugin, db, MockPeerReputationMgr())
        result = mgr._refresh_fleet_layer()

        assert result is True
        assert mgr.available is True

        # Verify askrene calls were made
        call_args = [c[0] for c in plugin.rpc.call.call_args_list]
        methods = [args[0] for args in call_args]
        assert "askrene-create-layer" in methods
        assert "askrene-update-channel" in methods
        assert "askrene-inform-channel" in methods
        assert "askrene-bias-node" in methods
        assert "askrene-age" in methods

    def test_fails_gracefully_when_askrene_unavailable(self):
        plugin = MagicMock()
        plugin.rpc.getinfo.return_value = {"id": "our_id"}
        plugin.rpc.call.side_effect = Exception("askrene not available")

        mgr = AskreneLayerManager(plugin, MockDatabase([{"peer_id": "fleet_a"}]), MockPeerReputationMgr())
        result = mgr._refresh_fleet_layer()

        assert result is False
        assert mgr.available is False


class TestReputationLayer:
    def test_disables_bad_peers(self):
        plugin = MagicMock()
        plugin.rpc.call.return_value = {}

        reps = {
            "bad_peer": MockReputation(score=10, force_closes=3, htlc_success=0.3),
            "good_peer": MockReputation(score=85, force_closes=0, htlc_success=0.99),
        }

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr(reps))
        result = mgr._refresh_reputation_layer()

        assert result is True

        # Check disable-node was called for bad_peer
        disable_calls = [
            c for c in plugin.rpc.call.call_args_list
            if c[0][0] == "askrene-disable-node"
        ]
        assert len(disable_calls) == 1
        assert disable_calls[0][0][1]["node"] == "bad_peer"

        # Check bias-node was called for good_peer with positive bias
        bias_calls = [
            c for c in plugin.rpc.call.call_args_list
            if c[0][0] == "askrene-bias-node" and c[0][1].get("node") == "good_peer"
        ]
        assert len(bias_calls) == 2  # in + out
        assert all(c[0][1]["bias"] == 5 for c in bias_calls)

    def test_no_reputation_data_succeeds(self):
        plugin = MagicMock()
        plugin.rpc.call.return_value = {}

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr())
        result = mgr._refresh_reputation_layer()
        assert result is True

    def test_no_reputation_manager(self):
        mgr = AskreneLayerManager(MagicMock(), MockDatabase(), None)
        result = mgr._refresh_reputation_layer()
        assert result is False


class TestRefreshAll:
    def test_returns_both_results(self):
        plugin = MagicMock()
        plugin.rpc.getinfo.return_value = {"id": "our_id"}
        plugin.rpc.listpeerchannels.return_value = {"channels": []}
        plugin.rpc.call.return_value = {}

        mgr = AskreneLayerManager(plugin, MockDatabase(), MockPeerReputationMgr())
        results = mgr.refresh_all()

        assert "hive-fleet" in results
        assert "hive-reputation" in results
```

- [ ] **Step 2: Run tests**

```bash
cd /home/sat/bin/cl-hive
.venv/bin/python -m pytest tests/test_askrene_layers.py -v
```

Expected: All pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_askrene_layers.py
git commit -m "test: add AskreneLayerManager unit tests"
```

---

### Task 3: Wire AskreneLayerManager into cl-hive init and background loop

**Files:**
- Modify: `/home/sat/bin/cl-hive/cl-hive.py` (init + deps injection)
- Modify: `/home/sat/bin/cl-hive/modules/background_loops.py` (Step 5i)

- [ ] **Step 1: Create manager in cl-hive.py init**

In `cl-hive.py`, find the peer_reputation_mgr initialization (around line 710-717). After it, add:

```python
    # Initialize askrene layer manager (manages hive-fleet + hive-reputation layers)
    from modules.askrene_layers import AskreneLayerManager
    global askrene_layer_mgr
    askrene_layer_mgr = AskreneLayerManager(
        plugin=plugin,
        database=database,
        peer_reputation_mgr=peer_reputation_mgr,
    )
    plugin.log("cl-hive: askrene layer manager initialized")
```

- [ ] **Step 2: Inject into background_loops deps**

In the `init_background_loops({...})` call (line 853-873), add to the dict:

```python
        'askrene_layer_mgr': askrene_layer_mgr,
```

- [ ] **Step 3: Add Step 5i to fee_intelligence_loop**

In `modules/background_loops.py`, after the Step 5h block (peer reputation broadcast, ends at line 454), add:

```python
            # Step 5i: Refresh askrene fleet intelligence layers
            try:
                if askrene_layer_mgr:
                    layer_results = askrene_layer_mgr.refresh_all()
                    shutdown_event.wait(0.05)
            except Exception as e:
                plugin.log(f"cl-hive: askrene layer refresh error: {e}", level='debug')
```

- [ ] **Step 4: Run cl-hive tests**

```bash
cd /home/sat/bin/cl-hive
.venv/bin/python -m pytest tests/ -x -q --tb=short
```

Expected: 813+ passed.

- [ ] **Step 5: Commit**

```bash
git add cl-hive.py modules/background_loops.py
git commit -m "feat: wire AskreneLayerManager into init and background loop"
```

---

### Task 4: Update cl-revenue-ops HiveRouter to detect cl-hive layers

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/modules/hive_router.py` (refresh_layer)
- Modify: `/home/sat/bin/cl_revenue_ops/tests/test_hive_router.py` (add detection tests)

- [ ] **Step 1: Update refresh_layer to detect cl-hive managed layers**

In `/home/sat/bin/cl_revenue_ops/modules/hive_router.py`, replace the `refresh_layer()` method with:

```python
    def refresh_layer(self) -> bool:
        """Check for cl-hive managed layers, fall back to self-creation.

        If cl-hive is running, it manages hive-fleet and hive-reputation
        layers.  We detect them via askrene-listlayers and skip our own
        layer creation.  If absent, we create hive-fleet ourselves
        (standalone mode).

        Returns:
            True if hive-fleet layer is available (managed or self-created).
        """
        if not self.hive_hints or not self.plugin:
            return False

        # Try to detect cl-hive managed layers first
        try:
            result = self.plugin.rpc.call("askrene-listlayers", {})
            layer_names = {l.get("layer") for l in result.get("layers", [])}
            if self.LAYER_NAME in layer_names:
                # cl-hive is managing the layer — just cache member IDs
                self._cache_member_ids()
                self.available = True
                self._last_refresh = time.time()
                return True
        except Exception:
            pass

        # cl-hive not managing layers — create our own (standalone mode)
        return self._create_standalone_layer()

    def _cache_member_ids(self) -> None:
        """Populate _member_ids from hive_hints."""
        try:
            channels = self.plugin.rpc.listpeerchannels()
            member_ids: Set[str] = set()
            for ch in channels.get("channels", []):
                if ch.get("state") != "CHANNELD_NORMAL":
                    continue
                peer_id = ch.get("peer_id", "")
                if peer_id and self.hive_hints.is_hive_member(peer_id):
                    member_ids.add(peer_id)
            self._member_ids = member_ids
        except Exception:
            pass

    def _create_standalone_layer(self) -> bool:
        """Create hive-fleet layer ourselves (when cl-hive is absent)."""
```

Then move the existing layer creation code (the body of the old `refresh_layer()`) into `_create_standalone_layer()`, keeping all the askrene-update-channel and askrene-bias-node logic identical.

- [ ] **Step 2: Add test for layer detection**

In `/home/sat/bin/cl_revenue_ops/tests/test_hive_router.py`, add:

```python
class TestHiveRouterLayerDetection:
    def test_detects_cl_hive_managed_layer(self):
        plugin = MagicMock()
        plugin.rpc.call.return_value = {
            "layers": [
                {"layer": "hive-fleet", "persistent": False},
                {"layer": "hive-reputation", "persistent": False},
            ]
        }
        plugin.rpc.listpeerchannels.return_value = {
            "channels": [
                {"state": "CHANNELD_NORMAL", "peer_id": "fleet_a", "short_channel_id": "100x1x0"},
            ]
        }

        hints = MockHiveHints(members=["fleet_a"])
        router = HiveRouter(plugin, hints)
        result = router.refresh_layer()

        assert result is True
        assert router.available is True
        assert router.is_hive_member("fleet_a") is True
        # Should NOT have called askrene-create-layer (cl-hive manages it)
        create_calls = [
            c for c in plugin.rpc.call.call_args_list
            if c[0][0] == "askrene-create-layer"
        ]
        assert len(create_calls) == 0

    def test_falls_back_to_standalone_when_no_cl_hive(self):
        plugin = MagicMock()
        plugin.rpc.getinfo.return_value = {"id": "our_id"}
        plugin.rpc.listpeerchannels.return_value = {
            "channels": [
                {"state": "CHANNELD_NORMAL", "peer_id": "fleet_a", "short_channel_id": "100x1x0"},
            ]
        }

        # First call (listlayers) returns empty, subsequent calls succeed
        def side_effect(method, params=None):
            if method == "askrene-listlayers":
                return {"layers": []}
            return {}

        plugin.rpc.call.side_effect = side_effect

        hints = MockHiveHints(members=["fleet_a"])
        router = HiveRouter(plugin, hints)
        result = router.refresh_layer()

        assert result is True
        assert router.available is True
```

- [ ] **Step 3: Run cl-revenue-ops tests**

```bash
cd /home/sat/bin/cl_revenue_ops
python3 -m pytest tests/ -x -q --tb=short
```

Expected: 919+ passed.

- [ ] **Step 4: Commit**

```bash
git add modules/hive_router.py tests/test_hive_router.py
git commit -m "feat: HiveRouter detects cl-hive managed layers, falls back to standalone"
```

---

### Task 5: Push both repos

- [ ] **Step 1: Push cl-hive**

```bash
cd /home/sat/bin/cl-hive
.venv/bin/python -m pytest tests/ -x -q --tb=short  # Final verification
git push
```

- [ ] **Step 2: Push cl-revenue-ops**

```bash
cd /home/sat/bin/cl_revenue_ops
python3 -m pytest tests/ -x -q --tb=short  # Final verification
git push
```
