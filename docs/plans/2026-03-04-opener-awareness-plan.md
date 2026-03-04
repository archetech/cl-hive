# Channel Opener Awareness — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ensure all relevant algorithms in cl-hive and cl_revenue_ops distinguish between channels we opened vs. channels others opened to us.

**Architecture:** Thread the `opener` field from CLN's `listpeerchannels` response through every decision point that should differ by channel direction. Changes are isolated — each fix touches one method/function with no cross-dependencies between fixes.

**Tech Stack:** Python 3, pytest, SQLite (WAL mode), Core Lightning RPC (`listpeerchannels` provides `opener: "local"|"remote"`)

**Design doc:** `docs/plans/2026-03-04-opener-awareness-design.md`

---

### Task 1: Add opener breakdown to compute_node_summary()

Add `we_opened` and `they_opened` counts by reading the `opener` field from `listpeerchannels`.

**Files:**
- Modify: `modules/planner.py:1157-1225` (compute_node_summary)
- Test: `tests/test_planner.py` (TestComputeNodeSummary, lines 1710-1835)

**Step 1: Write the failing test**

Add to `TestComputeNodeSummary` in `tests/test_planner.py`:

```python
def test_opener_breakdown(self, mock_plugin, mock_state_manager,
                           mock_database, mock_bridge, mock_clboss_bridge):
    """Counts we_opened vs they_opened from opener field."""
    planner = self._make_planner(mock_plugin, mock_state_manager,
                                  mock_database, mock_bridge, mock_clboss_bridge)
    mock_plugin.rpc.listpeerchannels.return_value = {
        'channels': [
            {'state': 'CHANNELD_NORMAL', 'total_msat': 5_000_000_000, 'opener': 'local'},
            {'state': 'CHANNELD_NORMAL', 'total_msat': 3_000_000_000, 'opener': 'local'},
            {'state': 'CHANNELD_NORMAL', 'total_msat': 2_000_000_000, 'opener': 'remote'},
            {'state': 'CHANNELD_AWAITING_LOCKIN', 'total_msat': 1_000_000_000, 'opener': 'local'},
            {'state': 'ONCHAIN', 'total_msat': 500_000_000, 'opener': 'remote'},
        ]
    }
    mock_bridge.safe_call.return_value = {'channels': []}

    result = planner.compute_node_summary()
    assert result['we_opened'] == 2   # only active CHANNELD_NORMAL with opener=local
    assert result['they_opened'] == 1  # only active CHANNELD_NORMAL with opener=remote
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_planner.py::TestComputeNodeSummary::test_opener_breakdown -v`
Expected: FAIL — KeyError: 'we_opened'

**Step 3: Implement**

In `modules/planner.py`, in `compute_node_summary()`:

Add two counters after `closing = 0` (around line 1189):
```python
        we_opened = 0
        they_opened = 0
```

Inside the `if state == 'CHANNELD_NORMAL':` block (line 1193), add:
```python
                opener = ch.get('opener', 'local')
                if opener == 'local':
                    we_opened += 1
                else:
                    they_opened += 1
```

Add to the return dict (after `underwater_pct`):
```python
            'we_opened': we_opened,
            'they_opened': they_opened,
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_planner.py::TestComputeNodeSummary -v`
Expected: PASS (all 6 tests)

**Step 5: Commit**

```bash
git add modules/planner.py tests/test_planner.py
git commit -m "feat(planner): add we_opened/they_opened breakdown to compute_node_summary()"
```

---

### Task 2: Allow planner expansion to peers who opened to us

Currently `_has_existing_or_pending_channel()` blocks expansion if ANY channel exists. Change it to also return the `opener` field so the caller can decide. Then update `get_underserved_targets()` to skip only peers where we have a locally-opened channel.

**Files:**
- Modify: `modules/planner.py:1264-1297` (_has_existing_or_pending_channel)
- Modify: `modules/planner.py:1660-1672` (get_underserved_targets existing_channel_peers loop)
- Test: `tests/test_planner.py`

**Step 1: Write the failing tests**

```python
class TestOpenerAwareExpansion:
    """Tests for Fix H2: Allow expansion to peers who opened channels to us."""

    def _make_planner(self, mock_plugin, mock_state_manager, mock_database,
                      mock_bridge, mock_clboss_bridge):
        return Planner(
            plugin=mock_plugin,
            state_manager=mock_state_manager,
            database=mock_database,
            bridge=mock_bridge,
            clboss_bridge=mock_clboss_bridge,
        )

    def test_has_existing_returns_opener(self, mock_plugin, mock_state_manager,
                                          mock_database, mock_bridge, mock_clboss_bridge):
        """_has_existing_or_pending_channel returns opener field."""
        planner = self._make_planner(mock_plugin, mock_state_manager,
                                      mock_database, mock_bridge, mock_clboss_bridge)
        mock_plugin.rpc.listpeerchannels.return_value = {
            'channels': [
                {'state': 'CHANNELD_NORMAL', 'total_msat': 5_000_000_000, 'opener': 'remote'}
            ]
        }
        has, state, capacity, opener = planner._has_existing_or_pending_channel('target_peer')
        assert has is True
        assert opener == 'remote'

    def test_has_existing_local_opener(self, mock_plugin, mock_state_manager,
                                        mock_database, mock_bridge, mock_clboss_bridge):
        """Local-opened channel returns opener='local'."""
        planner = self._make_planner(mock_plugin, mock_state_manager,
                                      mock_database, mock_bridge, mock_clboss_bridge)
        mock_plugin.rpc.listpeerchannels.return_value = {
            'channels': [
                {'state': 'CHANNELD_NORMAL', 'total_msat': 3_000_000_000, 'opener': 'local'}
            ]
        }
        has, state, capacity, opener = planner._has_existing_or_pending_channel('target_peer')
        assert has is True
        assert opener == 'local'

    def test_no_channel_returns_none_opener(self, mock_plugin, mock_state_manager,
                                             mock_database, mock_bridge, mock_clboss_bridge):
        """No channel returns opener=None."""
        planner = self._make_planner(mock_plugin, mock_state_manager,
                                      mock_database, mock_bridge, mock_clboss_bridge)
        mock_plugin.rpc.listpeerchannels.return_value = {'channels': []}
        has, state, capacity, opener = planner._has_existing_or_pending_channel('target_peer')
        assert has is False
        assert opener is None

    def test_underserved_includes_remote_opened_peers(self, mock_plugin, mock_state_manager,
                                                        mock_database, mock_bridge, mock_clboss_bridge):
        """Peers who only have remote-opened channels to us are NOT excluded from underserved targets."""
        planner = self._make_planner(mock_plugin, mock_state_manager,
                                      mock_database, mock_bridge, mock_clboss_bridge)
        # listpeerchannels returns one channel from peer 'T' who opened to us
        mock_plugin.rpc.listpeerchannels.return_value = {
            'channels': [
                {'peer_id': 'T', 'state': 'CHANNELD_NORMAL', 'total_msat': 5_000_000_000, 'opener': 'remote'},
                {'peer_id': 'L', 'state': 'CHANNELD_NORMAL', 'total_msat': 3_000_000_000, 'opener': 'local'},
            ]
        }
        # Build existing_channel_peers set
        all_peer_channels = mock_plugin.rpc.listpeerchannels()
        locally_opened_peers = set()
        for ch in all_peer_channels.get('channels', []):
            state = ch.get('state', '')
            if state in ('CHANNELD_NORMAL', 'CHANNELD_AWAITING_LOCKIN',
                         'DUALOPEND_AWAITING_LOCKIN', 'DUALOPEND_OPEN_INIT'):
                if ch.get('opener', 'local') == 'local':
                    peer_id = ch.get('peer_id', '')
                    if peer_id:
                        locally_opened_peers.add(peer_id)
        # 'T' should NOT be in locally_opened_peers (remote opener)
        assert 'T' not in locally_opened_peers
        # 'L' SHOULD be in locally_opened_peers (local opener)
        assert 'L' in locally_opened_peers
```

**Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_planner.py::TestOpenerAwareExpansion -v`
Expected: FAIL — tuple unpacking error (current returns 3-tuple, test expects 4-tuple)

**Step 3: Implement**

**A.** In `_has_existing_or_pending_channel()` (line 1264), change return type to 4-tuple:

```python
def _has_existing_or_pending_channel(self, target: str) -> Tuple[bool, Optional[str], Optional[int], Optional[str]]:
```

In the channel match block (line 1288-1290), add opener:
```python
                opener = ch.get('opener', 'local')
                return (True, state, capacity_sats, opener)
```

Update the fail-closed return (line 1295):
```python
        return (True, None, None, None)
```

Update the no-channel return (line 1297):
```python
        return (False, None, None, None)
```

**B.** Find all callers of `_has_existing_or_pending_channel()` and update their tuple unpacking. Search the codebase with:
```
grep -n "_has_existing_or_pending_channel" modules/planner.py
```
Each call site that does `has, state, cap = self._has_existing_or_pending_channel(...)` must become `has, state, cap, opener = ...` (or use `_` for unused opener).

**C.** In `get_underserved_targets()` (line 1660-1670), change the `existing_channel_peers` logic to only add peers where we have a locally-opened channel:

```python
        existing_channel_peers: Set[str] = set()
        if self.plugin:
            try:
                all_peer_channels = self.plugin.rpc.listpeerchannels()
                for ch in all_peer_channels.get('channels', []):
                    state = ch.get('state', '')
                    if state in ('CHANNELD_NORMAL', 'CHANNELD_AWAITING_LOCKIN',
                                 'DUALOPEND_AWAITING_LOCKIN', 'DUALOPEND_OPEN_INIT'):
                        # Only skip peers where WE opened the channel.
                        # Remote-opened channels don't prevent us from proposing expansion.
                        if ch.get('opener', 'local') == 'local':
                            peer_id = ch.get('peer_id', '')
                            if peer_id:
                                existing_channel_peers.add(peer_id)
            except Exception as e:
                self._log(f"Batch listpeerchannels failed: {e}", level='debug')
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_planner.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add modules/planner.py tests/test_planner.py
git commit -m "feat(planner): allow expansion to peers who opened channels to us (Fix H2)"
```

---

### Task 3: Quality scorer — remote opens as positive signal

Add `remote_open_count` to `get_peer_event_summary()` and use it as a quality bonus in the scorer.

**Files:**
- Modify: `modules/database.py:3779-3780` (get_peer_event_summary aggregation)
- Modify: `modules/quality_scorer.py:120-217` (calculate_score)
- Test: `tests/test_planner.py` or `tests/test_quality_scorer.py` (whichever exists)

**Step 1: Write the failing test**

Check if `tests/test_quality_scorer.py` exists. If not, add tests to an appropriate file.

```python
class TestOpenerQualityBonus:
    """Tests for Fix H3: Remote channel opens boost quality score."""

    def test_remote_opens_increase_score(self, mock_database):
        """Peers who opened channels to us get a quality bonus."""
        from modules.quality_scorer import PeerQualityScorer
        scorer = PeerQualityScorer(database=mock_database)

        # Base case: no remote opens
        base_summary = {
            "peer_id": "peer_A",
            "event_count": 5,
            "open_count": 3,
            "remote_open_count": 0,
            "close_count": 2,
            "remote_close_count": 0,
            "local_close_count": 1,
            "mutual_close_count": 1,
            "total_revenue_sats": 5000,
            "total_rebalance_cost_sats": 1000,
            "total_net_pnl_sats": 4000,
            "total_forward_count": 100,
            "avg_routing_score": 0.7,
            "avg_profitability_score": 0.6,
            "avg_duration_days": 90,
            "reporters": ["node1"],
            "reporter_scores": {"node1": {"event_count": 5, "avg_routing_score": 0.7, "avg_profitability_score": 0.6}},
        }
        mock_database.get_peer_event_summary.return_value = base_summary
        base_result = scorer.calculate_score("peer_A")

        # With remote opens
        remote_summary = dict(base_summary)
        remote_summary["remote_open_count"] = 2
        mock_database.get_peer_event_summary.return_value = remote_summary
        remote_result = scorer.calculate_score("peer_A")

        assert remote_result.overall_score > base_result.overall_score
```

**Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_planner.py::TestOpenerQualityBonus -v` (or test_quality_scorer.py)
Expected: FAIL

**Step 3: Implement**

**A.** In `modules/database.py`, in `get_peer_event_summary()`, after `open_events` is computed (line 3780), add:

```python
        remote_opens = [e for e in open_events if e.get('opener') == 'remote']
```

Add to the return dict (around line 3812):
```python
            "remote_open_count": len(remote_opens),
```

Also add to the empty return dict (around line 3764):
```python
                "remote_open_count": 0,
```

**B.** In `modules/quality_scorer.py`, in `calculate_score()`, after the weighted combination (around line 189), add an opener bonus:

```python
        # Bonus: peers who opened channels to us chose us as a routing partner
        remote_open_count = summary.get('remote_open_count', 0)
        if remote_open_count > 0:
            opener_bonus = min(0.1, remote_open_count * 0.05)  # +0.05 per remote open, cap at +0.10
            overall = min(1.0, overall + opener_bonus)
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/ -k "quality" -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add modules/database.py modules/quality_scorer.py tests/test_planner.py
git commit -m "feat(quality): boost score for peers who opened channels to us (Fix H3)"
```

---

### Task 4: MCP channel deep-dive — expose opener

Add the `opener` field to the channel info returned by the MCP server.

**Files:**
- Modify: `tools/mcp-hive-server.py:7797-7824` (handle_channel_deep_dive response dict)

**Step 1: Read the code**

The response dict at line 7801 builds a `"basic"` sub-dict. The channel data comes from `listpeerchannels` (via the target_channel variable resolved earlier in the function).

**Step 2: Implement**

In the `"basic"` dict (line 7801-7813), add after `"closer": channel_closer,`:

```python
                "opener": target_channel.get("opener", "unknown"),
```

Where `target_channel` is the raw CLN channel dict resolved at the top of the function.

**Step 3: Verify no syntax errors**

Run: `python3 -c "import ast; ast.parse(open('tools/mcp-hive-server.py').read()); print('OK')"`
Expected: `OK`

**Step 4: Commit**

```bash
git add tools/mcp-hive-server.py
git commit -m "feat(mcp): expose opener field in channel deep-dive (Fix H4)"
```

---

### Task 5: Fee floor — discount remote opens (cl_revenue_ops)

Modify `_calculate_floor()` and `ChainCostDefaults.calculate_floor_ppm()` to use only close cost when `opener == "remote"`.

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/modules/config.py:904-922` (ChainCostDefaults.calculate_floor_ppm)
- Modify: `/home/sat/bin/cl_revenue_ops/modules/fee_controller.py:7527-7622` (_calculate_floor)
- Modify: `/home/sat/bin/cl_revenue_ops/modules/fee_controller.py:5430` (_adjust_channel_fee call to _calculate_floor)
- Test: `/home/sat/bin/cl_revenue_ops/tests/test_fee_controller.py`

**Step 1: Write the failing tests**

Add to `/home/sat/bin/cl_revenue_ops/tests/test_fee_controller.py`:

```python
class TestCalculateFloorOpener:
    """Tests for Fix R1: Fee floor discount for remote-opened channels."""

    def test_local_opener_uses_full_cost(self, mock_plugin, mock_database):
        """Local opener floor includes open + close cost."""
        from modules.fee_controller import HillClimbingFeeController
        config = MagicMock()
        clboss = MagicMock()
        fc = HillClimbingFeeController(mock_plugin, config, mock_database, clboss)

        chain_costs = {"open_cost_sats": 5000, "close_cost_sats": 3000, "sat_per_vbyte": 5.0}
        floor_local = fc._calculate_floor(5_000_000, chain_costs=chain_costs, opener="local")
        floor_default = fc._calculate_floor(5_000_000, chain_costs=chain_costs)
        assert floor_local == floor_default  # local is the default

    def test_remote_opener_uses_close_only(self, mock_plugin, mock_database):
        """Remote opener floor uses only close cost (we didn't pay to open)."""
        from modules.fee_controller import HillClimbingFeeController
        config = MagicMock()
        clboss = MagicMock()
        fc = HillClimbingFeeController(mock_plugin, config, mock_database, clboss)

        chain_costs = {"open_cost_sats": 5000, "close_cost_sats": 3000, "sat_per_vbyte": 5.0}
        floor_local = fc._calculate_floor(5_000_000, chain_costs=chain_costs, opener="local")
        floor_remote = fc._calculate_floor(5_000_000, chain_costs=chain_costs, opener="remote")
        assert floor_remote < floor_local  # remote floor should be lower

    def test_static_floor_remote_discount(self, mock_plugin, mock_database):
        """Static ChainCostDefaults floor also discounts remote opens."""
        from modules.config import ChainCostDefaults
        floor_local = ChainCostDefaults.calculate_floor_ppm(5_000_000, opener="local")
        floor_remote = ChainCostDefaults.calculate_floor_ppm(5_000_000, opener="remote")
        assert floor_remote < floor_local
```

**Step 2: Run to verify failure**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_fee_controller.py::TestCalculateFloorOpener -v`
Expected: FAIL — `_calculate_floor()` doesn't accept `opener` parameter

**Step 3: Implement**

**A.** In `ChainCostDefaults.calculate_floor_ppm()` (`config.py:904`), add `opener` parameter:

```python
    @classmethod
    def calculate_floor_ppm(cls, capacity_sats: int, opener: str = "local") -> int:
        if opener == "remote":
            total_chain_cost = cls.CHANNEL_CLOSE_COST_SATS  # We didn't pay to open
        else:
            total_chain_cost = cls.CHANNEL_OPEN_COST_SATS + cls.CHANNEL_CLOSE_COST_SATS
        estimated_lifetime_volume = cls.DAILY_VOLUME_SATS * cls.CHANNEL_LIFETIME_DAYS
        if estimated_lifetime_volume > 0:
            floor_ppm = (total_chain_cost / estimated_lifetime_volume) * 1_000_000
            return max(1, int(floor_ppm))
        return 1
```

**B.** In `_calculate_floor()` (`fee_controller.py:7527`), add `opener` parameter:

```python
    def _calculate_floor(self, capacity_sats: int,
                         chain_costs: Optional[Dict[str, int]] = None,
                         peer_id: Optional[str] = None,
                         opener: str = "local") -> int:
```

Update the static fallback call (line 7557):
```python
        floor_ppm = ChainCostDefaults.calculate_floor_ppm(capacity_sats, opener=opener)
```

Update the replacement cost calculation (line 7563-7566):
```python
            open_cost = dynamic_costs.get("open_cost_sats", ChainCostDefaults.CHANNEL_OPEN_COST_SATS)
            close_cost = dynamic_costs.get("close_cost_sats", ChainCostDefaults.CHANNEL_CLOSE_COST_SATS)

            if opener == "remote":
                total_chain_cost = close_cost  # We didn't pay to open
            else:
                total_chain_cost = open_cost + close_cost
```

**C.** In `_adjust_channel_fee()`, at the call site (line 5430), pass opener:

```python
        opener = channel_info.get("opener", "local")
        base_floor_ppm = self._calculate_floor(capacity, chain_costs=chain_costs, peer_id=peer_id, opener=opener)
```

**Step 4: Run tests**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_fee_controller.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
cd /home/sat/bin/cl_revenue_ops && git add modules/config.py modules/fee_controller.py tests/test_fee_controller.py
git commit -m "fix(fees): discount fee floor for remote-opened channels (Fix R1)"
```

---

### Task 6: set_initial_fee() — include opener in channel_info (cl_revenue_ops)

The `channel_info` dict built in `set_initial_fee()` is missing `opener`.

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/modules/fee_controller.py:7442-7450` (channel_info dict)

**Step 1: Read the current code**

The dict at line 7442-7450 builds channel_info from a `target_ch` variable that comes from `listpeerchannels`.

**Step 2: Implement**

Add to the channel_info dict after `'fee_proportional_millionths'`:

```python
                'opener': target_ch.get('opener', 'local'),
```

Where `target_ch` is the raw CLN channel dict available in the same scope.

**Step 3: Run tests**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_fee_controller.py -v`
Expected: ALL PASS

**Step 4: Commit**

```bash
cd /home/sat/bin/cl_revenue_ops && git add modules/fee_controller.py
git commit -m "fix(fees): include opener in set_initial_fee() channel_info (Fix R2)"
```

---

### Task 7: Close recommendations — factor in zero open cost (cl_revenue_ops)

In `_identify_losers()`, expose `opener` to the AI advisor by including it in the recommendation output. The capacity planner doesn't have direct access to `opener`, so this requires threading it through from the profitability data.

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/modules/capacity_planner.py:172-265` (_identify_losers)
- Modify: `/home/sat/bin/cl_revenue_ops/modules/profitability_analyzer.py` (ensure opener is in profitability result)

**Step 1: Investigate what data `_identify_losers()` receives**

The method receives `all_profitability` — a dict of channel profitability results. Check if `opener` is already in the profitability result dict. If not, add it.

**Step 2: Implement**

**A.** In `profitability_analyzer.py`, in the `analyze_channel()` return dict (or the `ChannelProfitability` result), ensure `opener` is included. It's already read at line 490 — verify it makes it into the output dict.

**B.** In `_identify_losers()`, when building the recommendation output (lines 233-259), add `opener` to the dict:

```python
                "opener": prof.opener if hasattr(prof, 'opener') else channel_info.get("opener", "local"),
```

**C.** When computing whether to recommend closing, give remote-opened channels a higher threshold for closing (they cost us nothing to acquire):

```python
                # Remote-opened channels are "free" capacity — raise the bar for closing
                opener = getattr(prof, 'opener', 'local')
                if opener == 'remote' and prof.marginal_roi_percent > -75.0:
                    # Skip close recommendation for remote channels unless deeply underwater
                    continue
```

This means remote channels need to be at -75% marginal ROI (vs -50% for local) before recommending closure.

**Step 3: Run tests**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_capacity_planner.py -v`
Expected: ALL PASS

**Step 4: Commit**

```bash
cd /home/sat/bin/cl_revenue_ops && git add modules/capacity_planner.py modules/profitability_analyzer.py
git commit -m "fix(capacity): raise close threshold for remote-opened channels (Fix R3)"
```

---

### Task 8: Full regression test suite

Run both plugin test suites to verify all changes.

**Step 1: cl-hive tests**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/ --tb=short`
Expected: 2276+ passed

**Step 2: cl_revenue_ops tests**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/ --tb=short`
Expected: ALL PASS

**Step 3: Push both repos**

```bash
cd /home/sat/bin/cl-hive && git push
cd /home/sat/bin/cl_revenue_ops && git push
```

---

## Summary of Changes

| Fix | Task | Plugin | Files | What Changes |
|-----|------|--------|-------|--------------|
| H1 | 1 | cl-hive | planner.py | we_opened/they_opened in node_summary |
| H2 | 2 | cl-hive | planner.py | Allow expansion to remote-opened peers |
| H3 | 3 | cl-hive | database.py, quality_scorer.py | remote_open_count + quality bonus |
| H4 | 4 | cl-hive | mcp-hive-server.py | Expose opener in channel deep-dive |
| R1 | 5 | cl_revenue_ops | config.py, fee_controller.py | Fee floor discount for remote opens |
| R2 | 6 | cl_revenue_ops | fee_controller.py | Include opener in set_initial_fee() |
| R3 | 7 | cl_revenue_ops | capacity_planner.py, profitability_analyzer.py | Higher close threshold for remote channels |
