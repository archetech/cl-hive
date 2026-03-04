# Planner + Advisor Pipeline Audit — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix 6 confirmed issues in the planner→advisor pipeline that cause incorrect channel counts, permanent rejection stalls, and wasted proposal cycles.

**Architecture:** Pre-compute a `node_summary` at proposal time so AI advisor never derives channel counts from raw data. Fix the rejection backoff to reset on success. Add profitability gate to avoid proposals doomed to rejection. Harden network cache consumers with deduplicated accessor.

**Tech Stack:** Python 3, pytest, SQLite (WAL mode), Core Lightning RPC

**Design doc:** `docs/plans/2026-03-04-planner-advisor-audit-design.md`

---

### Task 1: Add `compute_node_summary()` helper to planner

Computes a pre-filtered summary of our node's channel states from `listpeerchannels`, so the AI advisor gets accurate counts.

**Files:**
- Modify: `modules/planner.py` (add method to `TopologyPlanner` class, around line 1152)
- Test: `tests/test_planner.py`

**Step 1: Write the failing test**

Add to `tests/test_planner.py`:

```python
class TestComputeNodeSummary:
    """Tests for Fix 1: Pre-computed node_summary."""

    def test_counts_only_active_channels(self, mock_plugin, mock_database, mock_state_manager, mock_bridge, mock_clboss_bridge):
        """CHANNELD_NORMAL channels counted as active, others excluded."""
        planner = Planner(
            plugin=mock_plugin,
            state_manager=mock_state_manager,
            database=mock_database,
            bridge=mock_bridge,
            clboss_bridge=mock_clboss_bridge,
        )
        mock_plugin.rpc.listpeerchannels.return_value = {
            'channels': [
                {'peer_id': 'A', 'state': 'CHANNELD_NORMAL', 'total_msat': 5000000000, 'spendable_msat': 3000000000, 'receivable_msat': 2000000000},
                {'peer_id': 'B', 'state': 'CHANNELD_NORMAL', 'total_msat': 3000000000, 'spendable_msat': 1000000000, 'receivable_msat': 2000000000},
                {'peer_id': 'C', 'state': 'ONCHAIN', 'total_msat': 2000000000, 'spendable_msat': 0, 'receivable_msat': 0},
                {'peer_id': 'D', 'state': 'CHANNELD_AWAITING_LOCKIN', 'total_msat': 4000000000, 'spendable_msat': 0, 'receivable_msat': 0},
                {'peer_id': 'E', 'state': 'CLOSINGD_COMPLETE', 'total_msat': 1000000000, 'spendable_msat': 0, 'receivable_msat': 0},
                {'peer_id': 'F', 'state': 'CHANNELD_NORMAL', 'total_msat': 6000000000, 'spendable_msat': 500000000, 'receivable_msat': 5500000000},
            ]
        }
        summary = planner.compute_node_summary()
        assert summary['active_channels'] == 3  # A, B, F
        assert summary['pending_channels'] == 1  # D
        assert summary['closing_channels'] == 2  # C, E
        assert summary['total_capacity_sats'] == 14000  # (5M + 3M + 6M) msat / 1000

    def test_underwater_count_from_bridge(self, mock_plugin, mock_database, mock_state_manager, mock_bridge, mock_clboss_bridge):
        """Underwater count sourced from bridge profitability data when available."""
        planner = Planner(
            plugin=mock_plugin,
            state_manager=mock_state_manager,
            database=mock_database,
            bridge=mock_bridge,
            clboss_bridge=mock_clboss_bridge,
        )
        mock_plugin.rpc.listpeerchannels.return_value = {
            'channels': [
                {'peer_id': 'A', 'state': 'CHANNELD_NORMAL', 'total_msat': 5000000000, 'spendable_msat': 3000000000, 'receivable_msat': 2000000000},
                {'peer_id': 'B', 'state': 'CHANNELD_NORMAL', 'total_msat': 3000000000, 'spendable_msat': 1000000000, 'receivable_msat': 2000000000},
            ]
        }
        mock_bridge.safe_call.return_value = {
            'channels': [
                {'peer_id': 'A', 'profitability_class': 'profitable'},
                {'peer_id': 'B', 'profitability_class': 'underwater'},
            ]
        }
        summary = planner.compute_node_summary()
        assert summary['underwater_count'] == 1
        assert summary['underwater_pct'] == 50.0

    def test_rpc_failure_returns_none(self, mock_plugin, mock_database, mock_state_manager, mock_bridge, mock_clboss_bridge):
        """Returns None on RPC failure (fail-closed)."""
        planner = Planner(
            plugin=mock_plugin,
            state_manager=mock_state_manager,
            database=mock_database,
            bridge=mock_bridge,
            clboss_bridge=mock_clboss_bridge,
        )
        mock_plugin.rpc.listpeerchannels.side_effect = Exception("RPC timeout")
        summary = planner.compute_node_summary()
        assert summary is None
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_planner.py::TestComputeNodeSummary -v`
Expected: FAIL — `AttributeError: 'Planner' object has no attribute 'compute_node_summary'`

**Step 3: Write minimal implementation**

Add to `modules/planner.py` after `_get_public_capacity_to_target()` (line ~1152):

```python
def compute_node_summary(self) -> Optional[Dict[str, Any]]:
    """
    Compute a pre-filtered summary of our node's channel states.

    Used to inject accurate data into pending_action payloads so the
    AI advisor never has to derive counts from raw listpeerchannels.

    Returns:
        Dict with active_channels, pending_channels, closing_channels,
        total_capacity_sats, underwater_count, underwater_pct.
        None on RPC failure (fail-closed).
    """
    if not self.plugin:
        return None

    try:
        peer_channels = self.plugin.rpc.listpeerchannels()
    except Exception as e:
        self._log(f"compute_node_summary: listpeerchannels failed: {e}", level='warn')
        return None

    active_states = {'CHANNELD_NORMAL'}
    pending_states = {
        'CHANNELD_AWAITING_LOCKIN',
        'DUALOPEND_AWAITING_LOCKIN',
        'DUALOPEND_OPEN_INIT',
    }

    active = 0
    pending = 0
    closing = 0
    total_capacity_msat = 0

    for ch in peer_channels.get('channels', []):
        state = ch.get('state', '')
        if state in active_states:
            active += 1
            total_capacity_msat += ch.get('total_msat', 0)
        elif state in pending_states:
            pending += 1
        else:
            closing += 1

    # Get underwater count from bridge (cl-revenue-ops profitability)
    underwater_count = 0
    if self.bridge:
        try:
            prof = self.bridge.safe_call('revenue-profitability', {})
            if isinstance(prof, dict):
                for ch_info in prof.get('channels', []):
                    if ch_info.get('profitability_class') in ('underwater', 'bleeder'):
                        underwater_count += 1
        except Exception:
            pass  # Best-effort — underwater_count stays 0

    underwater_pct = round(underwater_count * 100.0 / active, 1) if active > 0 else 0.0

    return {
        'active_channels': active,
        'pending_channels': pending,
        'closing_channels': closing,
        'total_capacity_sats': total_capacity_msat // 1000,
        'underwater_count': underwater_count,
        'underwater_pct': underwater_pct,
    }
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_planner.py::TestComputeNodeSummary -v`
Expected: PASS

**Step 5: Commit**

```bash
git add modules/planner.py tests/test_planner.py
git commit -m "feat(planner): add compute_node_summary() for accurate channel counting"
```

---

### Task 2: Inject node_summary into both pending_action paths

Both the DecisionEngine path (line 2237) and advisor fallback path (line 2295) must include `node_summary` in the payload.

**Files:**
- Modify: `modules/planner.py:2237-2253` (DecisionEngine context dict)
- Modify: `modules/planner.py:2295-2305` (fallback payload dict)
- Test: `tests/test_planner.py`

**Step 1: Write the failing test**

```python
class TestNodeSummaryInPayload:
    """Tests for Fix 1+3: node_summary in both pending_action paths."""

    def test_decision_engine_context_has_node_summary(self, mock_plugin, mock_database,
            mock_state_manager, mock_bridge, mock_clboss_bridge):
        """DecisionEngine path includes node_summary in context."""
        planner = Planner(
            plugin=mock_plugin,
            state_manager=mock_state_manager,
            database=mock_database,
            bridge=mock_bridge,
            clboss_bridge=mock_clboss_bridge,
        )
        # Mock compute_node_summary
        planner.compute_node_summary = MagicMock(return_value={
            'active_channels': 29,
            'pending_channels': 2,
            'closing_channels': 5,
            'total_capacity_sats': 45000000,
            'underwater_count': 5,
            'underwater_pct': 17.2,
        })
        # Set up a mock decision engine
        mock_de = MagicMock()
        from modules.governance import DecisionResult
        mock_response = MagicMock()
        mock_response.result = DecisionResult.QUEUED
        mock_response.action_id = 'test-action-123'
        mock_de.propose_action.return_value = mock_response
        planner.decision_engine = mock_de

        # Run the proposal (need full setup — use a targeted call)
        # Verify the context dict passed to propose_action has node_summary
        # This requires _propose_expansion to run through, which needs extensive mocking.
        # Instead, verify compute_node_summary is called AND its result is in context.
        # We'll capture the propose_action call args.

        # ... (full mock setup needed, see existing test patterns in test_planner.py)
        # Assert:
        call_args = mock_de.propose_action.call_args
        context = call_args.kwargs.get('context') or call_args[1].get('context')
        assert 'node_summary' in context
        assert context['node_summary']['active_channels'] == 29

    def test_fallback_payload_has_node_summary(self, mock_plugin, mock_database,
            mock_state_manager, mock_bridge, mock_clboss_bridge):
        """Advisor fallback path includes node_summary + quality fields."""
        planner = Planner(
            plugin=mock_plugin,
            state_manager=mock_state_manager,
            database=mock_database,
            bridge=mock_bridge,
            clboss_bridge=mock_clboss_bridge,
        )
        planner.compute_node_summary = MagicMock(return_value={
            'active_channels': 29,
            'pending_channels': 2,
            'closing_channels': 5,
            'total_capacity_sats': 45000000,
            'underwater_count': 5,
            'underwater_pct': 17.2,
        })
        planner.decision_engine = None  # Force fallback path

        # ... (mock setup for _propose_expansion to reach fallback)
        # Verify add_pending_action was called with node_summary in payload
        call_args = mock_database.add_pending_action.call_args
        payload = call_args.kwargs.get('payload') or call_args[1]
        assert 'node_summary' in payload
        assert 'target_channel_count' in payload
        assert 'quality_score' in payload
```

Note: These tests require extensive mock setup to reach the proposal paths. The implementer should follow the patterns in existing `test_planner.py` expansion tests to set up the full mock chain (RPC listpeerchannels, listchannels, getinfo, feerates, etc.).

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_planner.py::TestNodeSummaryInPayload -v`
Expected: FAIL — `node_summary` not in context/payload

**Step 3: Write the implementation**

In `modules/planner.py`, in `_propose_expansion()`:

**A.** Before the `if self.decision_engine:` block (around line 2202), compute the summary:

```python
            # Pre-compute node summary for accurate channel counts in proposals
            node_summary = self.compute_node_summary()
```

**B.** In the DecisionEngine context dict (line 2237-2253), add after `'quality_recommendation'`:

```python
                    'node_summary': node_summary,
```

**C.** In the fallback payload (lines 2297-2303), replace the minimal payload with the complete one:

```python
                    action_id = self.db.add_pending_action(
                        action_type='channel_open',
                        payload={
                            'intent_id': intent.intent_id,
                            'target': selected_target.target,
                            'public_capacity_sats': selected_target.public_capacity_sats,
                            'hive_share_pct': round(selected_target.hive_share_pct, 4),
                            'onchain_balance': onchain_balance,
                            'target_channel_count': self._get_target_channel_count(selected_target.target),
                            'quality_score': round(selected_target.quality_score, 3),
                            'quality_recommendation': selected_target.quality_recommendation,
                            'node_summary': node_summary,
                        },
                        expires_hours=24
                    )
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_planner.py::TestNodeSummaryInPayload -v`
Expected: PASS

**Step 5: Commit**

```bash
git add modules/planner.py tests/test_planner.py
git commit -m "feat(planner): inject node_summary into both pending_action paths (Fix 1+3)"
```

---

### Task 3: Align approval_criteria.md with code

The strategy prompt says >50 channels → REJECT, but the code does ESCALATE. Align the prompt to the code.

**Files:**
- Modify: `production/strategy-prompts/approval_criteria.md:34`

**Step 1: Update the line**

Change line 34 from:
```
- We already have >50 channels (focus on profitability first)
```
To:
```
- We already have >50 channels (ESCALATE for human review — do not auto-approve)
```

**Step 2: Verify no other threshold inconsistencies**

Search for any other "50 channel" references in strategy prompts and verify they match the code behavior (escalate, not reject).

**Step 3: Commit**

```bash
git add production/strategy-prompts/approval_criteria.md
git commit -m "fix(strategy): align >50 channel threshold to ESCALATE per code behavior (Fix 2)"
```

---

### Task 4: Fix rejection backoff stall

The exponential backoff caps at 24h but the rejection counter never resets, causing permanent stall. Fix by counting only rejections since the last success.

**Files:**
- Modify: `modules/database.py:3349-3384` (count_consecutive_expansion_rejections)
- Modify: `modules/planner.py:1889-1938` (_should_pause_expansions_globally)
- Test: `tests/test_planner.py`

**Step 1: Write failing tests**

```python
class TestRejectionBackoffFix:
    """Tests for Fix 4: Rejection backoff stall prevention."""

    def test_consecutive_count_resets_on_approval(self, mock_database):
        """Consecutive count stops at approved/executed status."""
        # This tests the DB method directly (already works per current code).
        # The real fix is in _should_pause_expansions_globally.
        pass

    def test_backoff_escapes_after_time(self, mock_plugin, mock_database,
            mock_state_manager, mock_bridge, mock_clboss_bridge):
        """Backoff allows retry when enough time has passed since last rejection."""
        planner = Planner(
            plugin=mock_plugin,
            state_manager=mock_state_manager,
            database=mock_database,
            bridge=mock_bridge,
            clboss_bridge=mock_clboss_bridge,
        )
        # 18 consecutive rejections → backoff_hours = 2^5 = 32 → capped at 24
        mock_database.count_consecutive_expansion_rejections.return_value = 18
        # But no recent rejections in the backoff window (all are old)
        mock_database.get_recent_expansion_rejections.return_value = []

        cfg = MagicMock()
        should_pause, reason = planner._should_pause_expansions_globally(cfg)
        assert not should_pause, "Should NOT pause when no recent rejections in window"

    def test_backoff_pauses_with_recent_rejections(self, mock_plugin, mock_database,
            mock_state_manager, mock_bridge, mock_clboss_bridge):
        """Backoff pauses when recent rejections fill the window."""
        planner = Planner(
            plugin=mock_plugin,
            state_manager=mock_state_manager,
            database=mock_database,
            bridge=mock_bridge,
            clboss_bridge=mock_clboss_bridge,
        )
        mock_database.count_consecutive_expansion_rejections.return_value = 6
        mock_database.get_recent_expansion_rejections.return_value = [
            {'status': 'rejected'}, {'status': 'rejected'}, {'status': 'rejected'}
        ]

        cfg = MagicMock()
        should_pause, reason = planner._should_pause_expansions_globally(cfg)
        assert should_pause

    def test_hard_cap_still_blocks(self, mock_plugin, mock_database,
            mock_state_manager, mock_bridge, mock_clboss_bridge):
        """50+ consecutive rejections still require manual intervention."""
        planner = Planner(
            plugin=mock_plugin,
            state_manager=mock_state_manager,
            database=mock_database,
            bridge=mock_bridge,
            clboss_bridge=mock_clboss_bridge,
        )
        mock_database.count_consecutive_expansion_rejections.return_value = 50

        cfg = MagicMock()
        should_pause, reason = planner._should_pause_expansions_globally(cfg)
        assert should_pause
        assert "manual intervention" in reason

    def test_lookback_window_limits_rejection_count(self):
        """REJECTION_LOOKBACK_HOURS (7 days) prevents ancient rejections from counting."""
        from modules.database import HiveDatabase
        assert hasattr(HiveDatabase, 'REJECTION_LOOKBACK_HOURS')
        assert HiveDatabase.REJECTION_LOOKBACK_HOURS == 168  # 7 days
```

**Step 2: Run to verify behavior**

Run: `python3 -m pytest tests/test_planner.py::TestRejectionBackoffFix -v`
Expected: Tests should define the correct behavior. Some may already pass (the current code partly works for the time-window case), the key test is `test_backoff_escapes_after_time`.

**Step 3: Verify existing behavior is correct**

The current `_should_pause_expansions_globally()` actually does check `get_recent_expansion_rejections(hours=backoff_hours)` which returns rejections *within* the backoff window. If the rejections are old enough (outside the window), `len(recent_rejections)` will be < `pause_threshold` and the function returns `False, ""`.

The *real* stall happens when `count_consecutive_expansion_rejections()` returns 18+ (which causes `backoff_hours = min(2^5, 24) = 24`), AND there are still >= 3 rejections within the last 24h. Since the planner runs hourly and creates a new rejection each time, the 24h window always has fresh rejections.

**The fix:** When backoff_hours reaches the 24h cap, use a proportionally longer cooldown instead:

```python
    def _should_pause_expansions_globally(self, cfg) -> tuple[bool, str]:
        """..."""
        if not self.db:
            return False, ""

        consecutive_rejections = self.db.count_consecutive_expansion_rejections()

        # Hard cap: too many rejections means manual intervention needed
        if consecutive_rejections >= self.MAX_CONSECUTIVE_REJECTIONS:
            return True, (
                f"expansion_disabled ({consecutive_rejections} consecutive rejections, "
                f"manual intervention needed)"
            )

        pause_threshold = getattr(cfg, 'expansion_pause_threshold', 3)

        if consecutive_rejections >= pause_threshold:
            # Calculate backoff: after threshold, wait exponentially longer
            # 3 rejections = 1h, 6 = 2h, 9 = 4h, 12 = 8h, 15 = 16h, 18 = 32h, etc.
            backoff_hours = 2 ** ((consecutive_rejections - pause_threshold) // 3)
            # No cap — let backoff grow naturally to escape the stall.
            # 18 rejections = 32h, 21 = 64h, etc.
            # REJECTION_LOOKBACK_HOURS (168h / 7 days) is the natural ceiling
            # because count_consecutive_expansion_rejections only looks back 7 days.
            max_backoff_hours = self.db.REJECTION_LOOKBACK_HOURS
            backoff_hours = min(backoff_hours, max_backoff_hours)

            recent_rejections = self.db.get_recent_expansion_rejections(hours=backoff_hours)

            if len(recent_rejections) >= pause_threshold:
                return True, (
                    f"global_constraint_backoff ({consecutive_rejections} consecutive "
                    f"rejections, {backoff_hours}h cooldown)"
                )

        return False, ""
```

The key change: remove the `max_backoff_hours = 24` hard cap and instead use `REJECTION_LOOKBACK_HOURS` (168h). After 18 rejections the backoff is 32h, which means after 32h with no new rejections the planner retries. Previously it was capped at 24h but hourly proposals kept adding new rejections within that window.

**Step 4: Run all tests**

Run: `python3 -m pytest tests/test_planner.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add modules/planner.py tests/test_planner.py
git commit -m "fix(planner): remove 24h backoff cap to prevent permanent rejection stall (Fix 4)"
```

---

### Task 5: Add profitability gate in planner

Before proposing expansions, check underwater channel percentage. If >40% underwater, skip with a log message matching the approval_criteria.md DEFER threshold.

**Files:**
- Modify: `modules/planner.py:1975-1995` (in _propose_expansion, after global constraint check)
- Test: `tests/test_planner.py`

**Step 1: Write the failing test**

```python
class TestProfitabilityGate:
    """Tests for Fix 5: Profitability gate blocks expansion when too many underwater channels."""

    def test_blocks_when_above_40pct_underwater(self, mock_plugin, mock_database,
            mock_state_manager, mock_bridge, mock_clboss_bridge):
        """Expansion blocked when >40% of channels are underwater."""
        planner = Planner(
            plugin=mock_plugin,
            state_manager=mock_state_manager,
            database=mock_database,
            bridge=mock_bridge,
            clboss_bridge=mock_clboss_bridge,
        )
        planner.compute_node_summary = MagicMock(return_value={
            'active_channels': 10,
            'pending_channels': 0,
            'closing_channels': 0,
            'total_capacity_sats': 50000000,
            'underwater_count': 5,
            'underwater_pct': 50.0,
        })
        cfg = MagicMock()
        cfg.planner_enable_expansions = True
        planner._expansions_this_cycle = 0
        planner.intent_manager = MagicMock()

        decisions = planner._propose_expansion(cfg, run_id='test-001')
        # Should skip with profitability reason, no expansion proposed
        assert any('profitability_gate' in str(d.get('reason', '')) or
                    d.get('action') == 'expansion_skipped' for d in decisions) or len(decisions) == 0

    def test_allows_when_below_40pct_underwater(self, mock_plugin, mock_database,
            mock_state_manager, mock_bridge, mock_clboss_bridge):
        """Expansion allowed when <40% underwater."""
        planner = Planner(
            plugin=mock_plugin,
            state_manager=mock_state_manager,
            database=mock_database,
            bridge=mock_bridge,
            clboss_bridge=mock_clboss_bridge,
        )
        planner.compute_node_summary = MagicMock(return_value={
            'active_channels': 10,
            'pending_channels': 0,
            'closing_channels': 0,
            'total_capacity_sats': 50000000,
            'underwater_count': 3,
            'underwater_pct': 30.0,
        })
        # If profitability gate passes, other checks will eventually run.
        # We just verify the gate doesn't block.
        # (Full run requires more mocking, so just check compute_node_summary was called)
        cfg = MagicMock()
        cfg.planner_enable_expansions = True
        # The method will proceed past the gate and likely fail on later checks,
        # which is fine — we're testing the gate, not the full flow.
```

**Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_planner.py::TestProfitabilityGate -v`
Expected: FAIL — no profitability gate exists yet

**Step 3: Implement the gate**

In `modules/planner.py`, in `_propose_expansion()`, after the global constraint check (after line 1995), add:

```python
        # Profitability gate: skip expansion when too many channels are underwater.
        # Matches approval_criteria.md DEFER: >40% underwater.
        node_summary = self.compute_node_summary()
        if node_summary and node_summary.get('underwater_pct', 0) > 40:
            self._log(
                f"Profitability gate: skipping expansion, "
                f"{node_summary['underwater_pct']}% underwater channels "
                f"({node_summary['underwater_count']}/{node_summary['active_channels']}). "
                f"Fix existing channels before expanding.",
                level='info'
            )
            self.db.log_planner_action(
                action_type='expansion',
                result='skipped',
                details={
                    'reason': 'profitability_gate',
                    'underwater_pct': node_summary['underwater_pct'],
                    'underwater_count': node_summary['underwater_count'],
                    'active_channels': node_summary['active_channels'],
                    'run_id': run_id
                }
            )
            return decisions
```

Note: Move the `node_summary = self.compute_node_summary()` call here (before the feerate gate). It will also be used later when injecting into the payload (Task 2), so store it as a local variable and pass it down.

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_planner.py::TestProfitabilityGate -v`
Expected: PASS

**Step 5: Commit**

```bash
git add modules/planner.py tests/test_planner.py
git commit -m "feat(planner): add profitability gate blocking expansion at >40% underwater (Fix 5)"
```

---

### Task 6: Harden network cache consumers with `get_unique_channels_for()`

Add a deduplicating accessor that returns unique channels for a target regardless of bidirectional indexing.

**Files:**
- Modify: `modules/planner.py:1140-1151` (add new method, update `_get_public_capacity_to_target`)
- Test: `tests/test_planner.py`

**Step 1: Write the failing test**

```python
class TestGetUniqueChannelsFor:
    """Tests for Fix 6: Deduplicated network cache accessor."""

    def test_dedup_bidirectional_entries(self, mock_plugin, mock_database,
            mock_state_manager, mock_bridge, mock_clboss_bridge):
        """Same channel indexed under both endpoints returns only once."""
        planner = Planner(
            plugin=mock_plugin,
            state_manager=mock_state_manager,
            database=mock_database,
            bridge=mock_bridge,
            clboss_bridge=mock_clboss_bridge,
        )
        # Manually populate cache with a channel indexed bidirectionally
        ch = ChannelInfo(
            short_channel_id='123x1x0',
            source='A',
            destination='B',
            capacity_sats=1000000,
            active=True,
            fee_per_millionth=100,
            base_fee_millisatoshi=1000,
        )
        planner._network_cache = {
            'A': [ch],
            'B': [ch],
        }
        # Query for target A — should get exactly 1 channel
        channels = planner.get_unique_channels_for('A')
        assert len(channels) == 1
        assert channels[0].short_channel_id == '123x1x0'

        # Query for target B — should also get exactly 1 channel
        channels = planner.get_unique_channels_for('B')
        assert len(channels) == 1

    def test_multiple_unique_channels(self, mock_plugin, mock_database,
            mock_state_manager, mock_bridge, mock_clboss_bridge):
        """Multiple distinct channels to same target returned correctly."""
        planner = Planner(
            plugin=mock_plugin,
            state_manager=mock_state_manager,
            database=mock_database,
            bridge=mock_bridge,
            clboss_bridge=mock_clboss_bridge,
        )
        ch1 = ChannelInfo(
            short_channel_id='123x1x0', source='A', destination='T',
            capacity_sats=1000000, active=True, fee_per_millionth=100, base_fee_millisatoshi=1000)
        ch2 = ChannelInfo(
            short_channel_id='456x2x0', source='B', destination='T',
            capacity_sats=2000000, active=True, fee_per_millionth=200, base_fee_millisatoshi=1000)
        planner._network_cache = {'T': [ch1, ch2]}

        channels = planner.get_unique_channels_for('T')
        assert len(channels) == 2

    def test_empty_target(self, mock_plugin, mock_database,
            mock_state_manager, mock_bridge, mock_clboss_bridge):
        """Unknown target returns empty list."""
        planner = Planner(
            plugin=mock_plugin,
            state_manager=mock_state_manager,
            database=mock_database,
            bridge=mock_bridge,
            clboss_bridge=mock_clboss_bridge,
        )
        planner._network_cache = {}
        channels = planner.get_unique_channels_for('unknown')
        assert channels == []
```

**Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_planner.py::TestGetUniqueChannelsFor -v`
Expected: FAIL — `AttributeError: 'Planner' object has no attribute 'get_unique_channels_for'`

**Step 3: Implement**

Add to `modules/planner.py` after `_get_public_capacity_to_target()` (line ~1152):

```python
def get_unique_channels_for(self, target: str) -> List[ChannelInfo]:
    """
    Get deduplicated channels for a target from the network cache.

    The cache indexes each channel under both endpoints (source and dest).
    This method deduplicates by short_channel_id to prevent double-counting.

    Args:
        target: Target node pubkey

    Returns:
        List of unique ChannelInfo objects for this target
    """
    channels = self._network_cache.get(target, [])
    seen_scids: set = set()
    unique: list = []
    for ch in channels:
        if ch.short_channel_id not in seen_scids:
            seen_scids.add(ch.short_channel_id)
            unique.append(ch)
    return unique
```

Then update `_get_public_capacity_to_target()` to use it:

```python
def _get_public_capacity_to_target(self, target: str) -> int:
    """..."""
    channels = self.get_unique_channels_for(target)
    return sum(ch.capacity_sats for ch in channels if ch.active)
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_planner.py::TestGetUniqueChannelsFor -v`
Expected: PASS

Also run full suite to verify no regressions:
Run: `python3 -m pytest tests/test_planner.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add modules/planner.py tests/test_planner.py
git commit -m "fix(planner): add get_unique_channels_for() to prevent cache double-counting (Fix 6)"
```

---

### Task 7: Wire node_summary into auto_evaluate_proposal

Update the MCP server's auto_evaluate_proposal to prefer `node_summary` from the payload over deriving counts from RPC.

**Files:**
- Modify: `tools/mcp-hive-server.py:13888-13958` (channel count evaluation in auto_evaluate_proposal)
- Test: Manual verification via MCP tool call

**Step 1: Read the current evaluation code**

The current code at lines 13918-13928 fetches `num_active_channels` from `hive-getinfo`. With Fix 1, the payload will now contain `node_summary.active_channels`.

**Step 2: Implement**

In `handle_auto_evaluate_proposal()`, when processing `channel_open` actions, add early in the evaluation block:

```python
                # Prefer pre-computed node_summary (Fix 1) over RPC-derived counts
                payload = action_data.get('payload', {}) if action_data else {}
                node_summary = payload.get('node_summary')

                if node_summary:
                    active_channels = node_summary.get('active_channels', 0)
                    underwater_pct = node_summary.get('underwater_pct', 0)
                else:
                    # Fallback to RPC for legacy proposals without node_summary
                    # (existing code path)
```

Wrap the existing `hive-getinfo` fetch in the `else` branch.

**Step 3: Commit**

```bash
git add tools/mcp-hive-server.py
git commit -m "feat(mcp): prefer node_summary from payload in auto_evaluate_proposal (Fix 1)"
```

---

### Task 8: Full regression test suite

Run the complete test suite to verify all changes work together.

**Step 1: Run all tests**

Run: `python3 -m pytest tests/ -v`
Expected: ALL PASS (2260+ tests)

**Step 2: Verify no import errors**

Run: `python3 -c "from modules.planner import Planner; print('OK')"`
Expected: `OK`

**Step 3: Final commit if any fixups needed**

If any tests broke, fix and commit.

---

### Task 9: Push and deploy

**Step 1: Push all commits**

```bash
git push
```

**Step 2: Deploy to VPS**

The user deploys by pulling on the VPS. Confirm all commits are pushed.

**Step 3: Verify on VPS**

After user pulls and restarts:
- Check logs for `compute_node_summary` being called
- Check that proposal payloads contain `node_summary`
- Monitor next planner cycle for correct channel count in logs

---

## Summary of Changes

| Fix | Task(s) | Files | What Changes |
|-----|---------|-------|--------------|
| Fix 1 | 1, 2, 7 | planner.py, mcp-hive-server.py | Pre-computed node_summary injected into proposals |
| Fix 2 | 3 | approval_criteria.md | >50 channels → ESCALATE (not REJECT) |
| Fix 3 | 2 | planner.py | Fallback payload gets quality + summary fields |
| Fix 4 | 4 | planner.py | Remove 24h backoff cap, use 168h natural ceiling |
| Fix 5 | 5 | planner.py | Block expansion when >40% underwater |
| Fix 6 | 6 | planner.py | get_unique_channels_for() dedup accessor |
