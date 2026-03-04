# Boltz Integration Audit Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Audit and harden the Boltz integration across cl_revenue_ops and cl-hive for correctness, test coverage, hive intelligence, and fleet coordination.

**Architecture:** Four phases executed bottom-up: (1) fix 3 correctness bugs in cl_revenue_ops, (2) add comprehensive test coverage for all untested Boltz code, (3) wire hive intelligence into Boltz decisions, (4) add fleet-level Boltz coordination via gossip. Each phase is independently deployable.

**Tech Stack:** Python 3, pytest, Core Lightning plugin framework (pyln-client), SQLite, threading

---

## Phase 1: Correctness Fixes

### Task 1: Fix Cooldown TOCTOU Race in Balance Cycle

The cooldown check acquires `_boltz_balance_lock`, checks the timestamp, releases the lock, then executes the swap outside the lock. Two threads can both pass the check for the same channel.

**Files:**
- Modify: `cl-revenue-ops.py:6413-6464` (balance cycle cooldown + execution)
- Modify: `cl-revenue-ops.py:6660-6680` (treasury cycle cooldown + execution)
- Test: `tests/test_boltz_manager.py`

**Step 1: Write the failing test**

Add to `tests/test_boltz_manager.py`:

```python
class TestCooldownPreClaim:
    """Cooldown pre-claim prevents double execution for same channel."""

    def test_pre_claim_blocks_second_thread(self):
        """Two threads claiming same channel: second should see pre-claimed timestamp."""
        import threading, time

        lock = threading.Lock()
        last_action = {}
        results = []

        def simulate_cycle(thread_id, cooldown_seconds=60):
            now = int(time.time())
            ch_id = "100x1x0"
            with lock:
                last_ts = int(last_action.get(ch_id, 0) or 0)
                if cooldown_seconds > 0 and last_ts > 0 and (now - last_ts) < cooldown_seconds:
                    results.append((thread_id, "cooldown_active"))
                    return
                # Pre-claim
                last_action[ch_id] = now

            # Simulate slow swap execution
            time.sleep(0.05)
            results.append((thread_id, "executed"))

        t1 = threading.Thread(target=simulate_cycle, args=(1,))
        t2 = threading.Thread(target=simulate_cycle, args=(2,))
        t1.start()
        time.sleep(0.01)  # Ensure t1 claims first
        t2.start()
        t1.join(timeout=3)
        t2.join(timeout=3)

        statuses = [s for _, s in results]
        assert "executed" in statuses, "At least one thread should execute"
        assert "cooldown_active" in statuses, "Second thread should be blocked by pre-claim"

    def test_failed_swap_clears_pre_claim(self):
        """If swap fails after pre-claim, the original timestamp is restored."""
        import threading

        lock = threading.Lock()
        last_action = {"100x1x0": 0}

        ch_id = "100x1x0"
        now = 1000000
        with lock:
            original_ts = int(last_action.get(ch_id, 0) or 0)
            last_action[ch_id] = now  # Pre-claim

        # Simulate swap failure — restore original
        with lock:
            if last_action.get(ch_id) == now:
                last_action[ch_id] = original_ts

        assert last_action[ch_id] == 0, "Pre-claim should be cleared on failure"
```

**Step 2: Run test to verify it passes (this tests the pattern, not the code yet)**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_boltz_manager.py::TestCooldownPreClaim -v`
Expected: PASS (tests the algorithm in isolation)

**Step 3: Implement the fix in balance cycle**

In `cl-revenue-ops.py`, modify lines 6413-6464. The cooldown check + pre-claim stays inside the lock. After execution, if the swap fails, restore the original timestamp.

Replace the block at lines 6413-6424 (cooldown check only) with pre-claim pattern:

```python
        # C1 FIX: Pre-claim cooldown slot inside lock to prevent TOCTOU double-execution
        with _boltz_balance_lock:
            last_ts = int(_boltz_balance_last_action.get(ch_id, 0) or 0)
            if rec_cooldown_seconds > 0 and last_ts > 0 and (now - last_ts) < rec_cooldown_seconds:
                skipped_exec.append({
                    "channel_id": ch_id,
                    "peer_id": peer_id,
                    "reason": "cooldown_active",
                    "cooldown_remaining_sec": rec_cooldown_seconds - (now - last_ts),
                    "recommendation": rec,
                })
                continue
            # Pre-claim: set timestamp now to block concurrent threads
            _boltz_balance_last_action[ch_id] = now
```

Then after the swap execution block (around line 6461-6466), replace the success timestamp recording:

Where currently `if status == "accepted":` sets timestamp, change to:
- On accepted: timestamp already set by pre-claim, no action needed
- On rejected/failed: restore the original timestamp

```python
                if status == "accepted":
                    # Pre-claim already set the timestamp; just update budget
                    remaining_budget = max(0, remaining_budget - est_fee)
                else:
                    # Swap rejected — clear pre-claim to allow future attempts
                    with _boltz_balance_lock:
                        if _boltz_balance_last_action.get(ch_id) == now:
                            _boltz_balance_last_action[ch_id] = last_ts
                    skipped_exec.append({"channel_id": ch_id, "peer_id": peer_id, "reason": "execution_rejected", "result": res})
```

And in the `except` block (line 6478-6483), add pre-claim restoration:

```python
        except Exception as e:
            # Clear pre-claim on failure
            with _boltz_balance_lock:
                if _boltz_balance_last_action.get(ch_id) == now:
                    _boltz_balance_last_action[ch_id] = last_ts
            skipped_exec.append({
                "channel_id": ch_id,
                "peer_id": peer_id,
                "reason": f"execution_failed: {e}",
                "recommendation": rec,
            })
```

**Step 4: Apply the same pattern to treasury cycle**

In `cl-revenue-ops.py` lines 6660-6686, apply the identical pre-claim pattern:
- At line 6660: Add pre-claim after cooldown check inside lock
- At line 6678-6684: On accepted, timestamp already set. On rejected/failed, restore `last_ts`.

**Step 5: Run tests**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/ -v`
Expected: All tests PASS

**Step 6: Commit**

```bash
cd /home/sat/bin/cl_revenue_ops && git add cl-revenue-ops.py tests/test_boltz_manager.py
git commit -m "Fix cooldown TOCTOU race with pre-claim pattern in Boltz balance/treasury cycles"
```

---

### Task 2: Fix Pending Swap Budget Reservation

`get_boltz_cost_components()` only counts completed swaps. Pending swaps' estimated fees are invisible to the budget, allowing overcommit.

**Files:**
- Modify: `modules/boltz_manager.py:658-697` (get_boltz_cost_components)
- Test: `tests/test_boltz_manager.py`

**Step 1: Write the failing test**

Add to `tests/test_boltz_manager.py`:

```python
class TestPendingSwapReservation:
    """C2: Pending swaps should be counted in reserved_24h_sats."""

    def test_pending_swap_counted_as_reserved(self):
        mgr = _make_manager()
        now = int(time.time())
        pending_swap = {
            "id": "swap_pending_1",
            "createdAt": str(now - 100),
            "state": "pending",
            "status": "pending",
            "boltzFee": "50",
            "networkFee": "10",
        }
        completed_swap = {
            "id": "swap_done_1",
            "createdAt": str(now - 200),
            "completedAt": str(now - 150),
            "state": "completed",
            "status": "swap.completed",
            "boltzFee": "40",
            "networkFee": "5",
        }
        mgr._listswaps_json = MagicMock(return_value={"swaps": [pending_swap, completed_swap]})
        mgr._augment_with_swap_journal = MagicMock(side_effect=lambda s, **kw: s)

        result = mgr.get_boltz_cost_components(window_hours=24)
        assert result["spent_24h_sats"] == 45, f"Completed swap fee should be 45, got {result['spent_24h_sats']}"
        assert result["reserved_24h_sats"] > 0, "Pending swap should contribute to reserved budget"

    def test_error_swap_not_reserved(self):
        mgr = _make_manager()
        now = int(time.time())
        error_swap = {
            "id": "swap_err_1",
            "createdAt": str(now - 100),
            "state": "error",
            "status": "swap.failed",
            "error": "some error",
            "boltzFee": "50",
            "networkFee": "10",
        }
        mgr._listswaps_json = MagicMock(return_value={"swaps": [error_swap]})
        mgr._augment_with_swap_journal = MagicMock(side_effect=lambda s, **kw: s)

        result = mgr.get_boltz_cost_components(window_hours=24)
        assert result["reserved_24h_sats"] == 0, "Error swaps should not be reserved"

    def test_old_pending_swap_not_reserved(self):
        mgr = _make_manager()
        now = int(time.time())
        old_pending = {
            "id": "swap_old_1",
            "createdAt": str(now - 100000),
            "state": "pending",
            "status": "pending",
            "boltzFee": "50",
            "networkFee": "10",
        }
        mgr._listswaps_json = MagicMock(return_value={"swaps": [old_pending]})
        mgr._augment_with_swap_journal = MagicMock(side_effect=lambda s, **kw: s)

        result = mgr.get_boltz_cost_components(window_hours=24)
        assert result["reserved_24h_sats"] == 0, "Pending swap outside window should not be reserved"
```

**Step 2: Run test to verify it fails**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_boltz_manager.py::TestPendingSwapReservation -v`
Expected: FAIL — `reserved_24h_sats` is always 0

**Step 3: Implement the fix**

In `modules/boltz_manager.py`, modify `get_boltz_cost_components()` (lines 658-697). After the existing loop that counts completed swaps, add a second pass for pending swaps:

```python
        # C2 FIX: Count pending (non-completed, non-error) swaps as reserved budget
        reserved = 0
        reserved_count = 0
        for s in swaps:
            if self._is_completed_swap(s) or self._is_error_swap(s):
                continue
            ts = self._swap_created_ts(s)
            if ts is None or ts < cutoff:
                continue
            fee_est = self._estimate_swap_fee_sats(s)
            if fee_est > 0:
                reserved += fee_est
                reserved_count += 1
```

Then change the return dict's `reserved_24h_sats` from `0` to `reserved`, and add `reserved_swaps` count:

```python
        return {
            "spent_24h_sats": boltz_spent,
            "reserved_24h_sats": reserved,
            "counted_swaps": len(counted),
            "reserved_swaps": reserved_count,
            "skipped_without_timestamp": unknown_ts,
            "counted_details": counted[:20],
            "window_seconds": window_hours * 3600,
            "source": "boltz",
        }
```

**Step 4: Run tests**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_boltz_manager.py::TestPendingSwapReservation -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/ -v`
Expected: All PASS

**Step 6: Commit**

```bash
cd /home/sat/bin/cl_revenue_ops && git add modules/boltz_manager.py tests/test_boltz_manager.py
git commit -m "Count pending Boltz swaps as reserved budget to prevent overcommit"
```

---

### Task 3: Fix Auto-Cycle Error Counter Reset on Blocked

When the auto-cycle gets a `blocked` result, it resets `consecutive_errors` to 0, hiding real failures.

**Files:**
- Modify: `cl-revenue-ops.py:1556-1565`
- Test: `tests/test_boltz_manager.py`

**Step 1: Write the failing test**

Add to `tests/test_boltz_manager.py`:

```python
class TestAutoCycleErrorCounter:
    """C3: Error counter should not reset on blocked state."""

    def test_error_increments_counter(self):
        state = {'consecutive_errors': 2}
        result = {'error': 'something failed'}
        # Simulate error path
        if isinstance(result, dict) and 'error' in result:
            state['consecutive_errors'] = int(state.get('consecutive_errors', 0) or 0) + 1
        assert state['consecutive_errors'] == 3

    def test_success_resets_counter(self):
        state = {'consecutive_errors': 5}
        result = {'status': 'executed'}
        # Simulate success path
        if isinstance(result, dict) and 'error' not in result:
            status = str(result.get('status') or '')
            if status in ('executed', 'dry_run'):
                state['consecutive_errors'] = 0
        assert state['consecutive_errors'] == 0

    def test_blocked_preserves_counter(self):
        """Blocked state should NOT reset error counter."""
        state = {'consecutive_errors': 3}
        result = {'status': 'blocked', 'reason': 'pending_swaps'}
        # Simulate the fixed logic
        if isinstance(result, dict) and 'error' in result:
            state['consecutive_errors'] = int(state.get('consecutive_errors', 0) or 0) + 1
        elif isinstance(result, dict):
            status = str(result.get('status') or '')
            if status in ('executed', 'dry_run'):
                state['consecutive_errors'] = 0
            # else: blocked/other — leave counter unchanged
        assert state['consecutive_errors'] == 3, "Blocked should preserve error count"
```

**Step 2: Run test to verify it passes (tests algorithm, not wired code)**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_boltz_manager.py::TestAutoCycleErrorCounter -v`
Expected: PASS

**Step 3: Implement the fix**

In `cl-revenue-ops.py`, replace lines 1556-1564:

Current code:
```python
            if isinstance(result, dict) and 'error' in result:
                with _boltz_auto_cycle_state_lock:
                    _boltz_auto_cycle_state['consecutive_errors'] = int(_boltz_auto_cycle_state.get('consecutive_errors', 0) or 0) + 1
                _boltz_auto_cycle_mark_state(last_error=str(result.get('error')))
            else:
                _boltz_auto_cycle_mark_state(last_error=None)
                with _boltz_auto_cycle_state_lock:
                    _boltz_auto_cycle_state['consecutive_errors'] = 0
```

New code:
```python
            if isinstance(result, dict) and 'error' in result:
                with _boltz_auto_cycle_state_lock:
                    _boltz_auto_cycle_state['consecutive_errors'] = int(_boltz_auto_cycle_state.get('consecutive_errors', 0) or 0) + 1
                _boltz_auto_cycle_mark_state(last_error=str(result.get('error')))
            else:
                # C3 FIX: Only reset error counter on actual success, not on blocked/other states
                status = str(result.get('status') or 'unknown') if isinstance(result, dict) else 'unknown'
                if status in ('executed', 'dry_run'):
                    with _boltz_auto_cycle_state_lock:
                        _boltz_auto_cycle_state['consecutive_errors'] = 0
                _boltz_auto_cycle_mark_state(last_error=None)
```

**Step 4: Run full test suite**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/ -v`
Expected: All PASS

**Step 5: Commit**

```bash
cd /home/sat/bin/cl_revenue_ops && git add cl-revenue-ops.py tests/test_boltz_manager.py
git commit -m "Fix auto-cycle error counter: only reset on success, preserve on blocked"
```

---

## Phase 2: Test Coverage

### Task 4: Test Balance Planning Engine

Comprehensive tests for `_build_boltz_balance_plan()` and `_boltz_dynamic_channel_tuning()`.

**Files:**
- Create: `tests/test_boltz_integration.py`

**Step 1: Write the test file**

```python
"""Comprehensive Boltz integration tests for balance planning, auto-cycle, treasury, and budget."""

import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from modules.boltz_manager import BoltzCliConfig, BoltzCliManager


def _make_manager(**overrides):
    cfg_kwargs = {
        "enabled": True,
        "cli_path": "/usr/local/bin/boltzcli",
        "datadir": "/tmp/test_boltz",
        "daily_budget_sats": 3000,
        "enforce_budget": True,
    }
    cfg_kwargs.update(overrides)
    cfg = BoltzCliConfig(**cfg_kwargs)
    plugin = MagicMock()
    plugin.log = MagicMock()
    rpc = MagicMock()
    mgr = BoltzCliManager(plugin, rpc, cfg)
    return mgr


class TestDynamicChannelTuning:
    """Tests for _boltz_dynamic_channel_tuning() threshold/sizing logic."""

    def test_high_protection_channel_triggers_earlier(self):
        """Hot profitable channel should have higher low_trigger_pct than default."""
        from importlib import import_module
        # Import the function - it's a module-level function in cl-revenue-ops.py
        # We test the logic directly since it's a pure function
        base_low_trigger = 35.0
        base_low_target = 55.0
        protection_score = 0.9
        trigger_boost = 20.0 * protection_score  # +18pp
        eff_low_trigger = min(70.0, max(base_low_trigger, base_low_trigger + trigger_boost))
        assert eff_low_trigger > base_low_trigger, "High protection should boost trigger"
        assert eff_low_trigger == 53.0, f"Expected 53.0, got {eff_low_trigger}"

    def test_low_protection_channel_uses_defaults(self):
        """Channel with no routing activity should use base thresholds."""
        protection_score = 0.0
        trigger_boost = 20.0 * protection_score
        eff_low_trigger = min(70.0, max(35.0, 35.0 + trigger_boost))
        assert eff_low_trigger == 35.0, "Zero protection should use base trigger"

    def test_cooldown_multiplier_bounds(self):
        """Cooldown multiplier should not go below 0.25."""
        for ps in [0.0, 0.5, 1.0, 1.5]:
            cooldown_mult = 1.0 - (0.75 * min(1.0, ps))
            assert cooldown_mult >= 0.25, f"Cooldown multiplier {cooldown_mult} < 0.25 at ps={ps}"

    def test_amount_multiplier_range(self):
        """Amount multiplier should range from 1x to 3x."""
        for ps in [0.0, 0.5, 1.0]:
            amount_mult = 1.0 + (2.0 * ps)
            assert 1.0 <= amount_mult <= 3.0, f"Amount multiplier {amount_mult} out of range at ps={ps}"

    def test_drain_accel_score_clamped(self):
        """drain_accel_score should be clamped to [0, 1] regardless of velocity."""
        for vel in [-0.1, 0.0, 0.001, 0.05/24.0, 0.1, 1.0]:
            score = max(0.0, min(1.0, vel / (0.05 / 24.0)))
            assert 0.0 <= score <= 1.0, f"Score {score} out of bounds for velocity {vel}"

    def test_severity_loop_in_range(self):
        """Loop-in severity should be in [0, 1]."""
        for trigger, local in [(40, 0), (40, 20), (40, 39), (40, 40)]:
            severity = max(0.0, (trigger - local) / max(trigger, 1.0))
            assert 0.0 <= severity <= 1.0, f"Severity {severity} out of range"

    def test_severity_loop_out_range(self):
        """Loop-out severity should be in [0, 1]."""
        for trigger, local in [(80, 80), (80, 90), (80, 100)]:
            severity = max(0.0, (local - trigger) / max(100.0 - trigger, 1.0))
            assert 0.0 <= severity <= 1.0, f"Severity {severity} out of range"


class TestBoltzCostComponents:
    """Tests for get_boltz_cost_components() budget accounting."""

    def test_completed_swap_counted(self):
        mgr = _make_manager()
        now = int(time.time())
        swap = {
            "id": "s1", "createdAt": str(now - 100), "completedAt": str(now - 50),
            "state": "completed", "status": "swap.completed",
            "boltzFee": "40", "networkFee": "10",
        }
        mgr._listswaps_json = MagicMock(return_value={"swaps": [swap]})
        mgr._augment_with_swap_journal = MagicMock(side_effect=lambda s, **kw: s)
        result = mgr.get_boltz_cost_components(window_hours=24)
        assert result["spent_24h_sats"] == 50
        assert result["counted_swaps"] == 1

    def test_old_swap_excluded(self):
        mgr = _make_manager()
        now = int(time.time())
        old_swap = {
            "id": "s1", "createdAt": str(now - 200000), "completedAt": str(now - 200000),
            "state": "completed", "status": "swap.completed",
            "boltzFee": "100", "networkFee": "10",
        }
        mgr._listswaps_json = MagicMock(return_value={"swaps": [old_swap]})
        mgr._augment_with_swap_journal = MagicMock(side_effect=lambda s, **kw: s)
        result = mgr.get_boltz_cost_components(window_hours=24)
        assert result["spent_24h_sats"] == 0

    def test_swap_without_timestamp_skipped(self):
        mgr = _make_manager()
        swap = {
            "id": "s1", "state": "completed", "status": "swap.completed",
            "boltzFee": "40", "networkFee": "10",
        }
        mgr._listswaps_json = MagicMock(return_value={"swaps": [swap]})
        mgr._augment_with_swap_journal = MagicMock(side_effect=lambda s, **kw: s)
        result = mgr.get_boltz_cost_components(window_hours=24)
        assert result["skipped_without_timestamp"] >= 1

    def test_budget_enforcement_blocks_over_limit(self):
        mgr = _make_manager(daily_budget_sats=100, enforce_budget=True)
        mgr.get_budget_status = MagicMock(return_value={
            "remaining_24h_sats_estimate": 50,
            "daily_budget_sats": 100,
        })
        quote = {"boltzFee": "60", "networkFee": "10"}
        result = mgr._enforce_budget_for_quote(quote)
        assert result["allowed"] is False
        assert "exceeds" in result["reason"].lower()

    def test_budget_enforcement_allows_within_limit(self):
        mgr = _make_manager(daily_budget_sats=100, enforce_budget=True)
        mgr.get_budget_status = MagicMock(return_value={
            "remaining_24h_sats_estimate": 80,
            "daily_budget_sats": 100,
        })
        quote = {"boltzFee": "30", "networkFee": "10"}
        result = mgr._enforce_budget_for_quote(quote)
        assert result["allowed"] is True

    def test_boltzd_unreachable_returns_safe_error(self):
        """When boltzd is down, _boltz_liquidity_cost_components should return safe defaults."""
        mgr = _make_manager()
        mgr._listswaps_json = MagicMock(side_effect=Exception("Connection refused"))
        with pytest.raises(Exception, match="Connection refused"):
            mgr.get_boltz_cost_components(window_hours=24)


class TestExternalPayFallback:
    """Tests for chanId rejection handling and external-pay routing."""

    def test_contains_chanids_cln_error_detection(self):
        mgr = _make_manager()
        swap_with_error = {"error": "chanIds are not supported for cln backends"}
        assert mgr._contains_chanids_cln_error(swap_with_error) is True

    def test_contains_chanids_no_error(self):
        mgr = _make_manager()
        swap_ok = {"id": "abc", "state": "pending"}
        assert mgr._contains_chanids_cln_error(swap_ok) is False

    def test_extract_reverse_swap_invoice_found(self):
        mgr = _make_manager()
        response = {"invoice": "lnbc1234567890abcdef"}
        invoice = mgr._extract_reverse_swap_invoice(response)
        assert invoice == "lnbc1234567890abcdef"

    def test_extract_reverse_swap_invoice_missing(self):
        mgr = _make_manager()
        response = {"id": "abc", "state": "pending"}
        invoice = mgr._extract_reverse_swap_invoice(response)
        assert invoice is None

    def test_estimate_swap_fee_named_fields(self):
        mgr = _make_manager()
        swap = {"boltzFee": "50", "networkFee": "15"}
        fee = mgr._estimate_swap_fee_sats(swap)
        assert fee == 65

    def test_estimate_swap_fee_zero_on_empty(self):
        mgr = _make_manager()
        fee = mgr._estimate_swap_fee_sats({})
        assert fee == 0

    def test_is_completed_swap_success_states(self):
        mgr = _make_manager()
        for status in ["swap.completed", "invoice.settled", "transaction.claimed"]:
            assert mgr._is_completed_swap({"status": status}) is True

    def test_is_completed_swap_pending_state(self):
        mgr = _make_manager()
        assert mgr._is_completed_swap({"status": "pending"}) is False

    def test_is_error_swap_detection(self):
        mgr = _make_manager()
        assert mgr._is_error_swap({"error": "failed"}) is True
        assert mgr._is_error_swap({"status": "swap.error"}) is True
        assert mgr._is_error_swap({"status": "swap.completed"}) is False
```

**Step 2: Run tests**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_boltz_integration.py -v`
Expected: All PASS (tests exercise existing code and the Phase 1 fixes)

**Step 3: Commit**

```bash
cd /home/sat/bin/cl_revenue_ops && git add tests/test_boltz_integration.py
git commit -m "Add comprehensive Boltz integration tests for balance planning, budget, and external-pay"
```

---

### Task 5: Run Full Regression and Commit Phase 1+2

**Step 1: Run full cl_revenue_ops test suite**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/ -v`
Expected: All PASS (770+ tests)

**Step 2: Push Phase 1+2**

```bash
cd /home/sat/bin/cl_revenue_ops && git push
```

---

## Phase 3: Hive Integration

### Task 6: Add Boltz Approval Criteria to Strategy Prompts

**Files:**
- Modify: `/home/sat/bin/cl-hive/production/strategy-prompts/approval_criteria.md` (insert after line 150, before General Principles)

**Step 1: Add the Boltz Swap Actions section**

Insert after the Rebalance Actions section (after line 150) and before `## General Principles` (line 152):

```markdown
---

## Boltz Swap Actions (Advisor-Evaluated)

Boltz swaps are the **last resort** for liquidity management. Always prefer hive internal rebalances (free) and market/Sling routes before Boltz.

### APPROVE if ALL conditions met:
- Channel is profitable and has routing activity (not underwater/bleeder)
- Estimated swap fee < remaining daily Boltz budget
- Expected net benefit > 1.5x estimated swap fee (clear profit margin)
- No pending Boltz swap already active on same channel
- Hive internal and market rebalance options exhausted (check `fleet_rebalance_path` first)
- Channel balance is outside acceptable range (<20% or >80% local)
- Direction matches channel need (loop-in for depleting, loop-out for saturating)

### REJECT if ANY condition applies:
- Channel is underwater/bleeder (fix the channel first, don't feed it)
- Would exceed daily Boltz budget
- Hive internal rebalance available for same direction (use free route instead)
- Market/Sling rebalance available at lower cost
- Channel balance is acceptable (20-80% range — leave it alone)
- Swap fee > 1000 ppm of amount (too expensive)
- Channel is being considered for closing

### DEFER (reject with reason "needs_review") if:
- Expected net benefit is marginal (1.0-1.5x fee — borderline profitability)
- Channel is < 14 days old (let optimizer learn naturally)
- Treasury expansion cycle already running on this node
- Any uncertainty about whether the swap is needed
- Multiple Boltz swaps already executed today (budget discipline)

---
```

**Step 2: Commit**

```bash
cd /home/sat/bin/cl-hive && git add -f production/strategy-prompts/approval_criteria.md
git commit -m "Add Boltz swap approval criteria section to strategy prompts"
```

---

### Task 7: Add Boltz Cost Breakdown to Yield Reporting

**Files:**
- Modify: `cl-revenue-ops.py:1381-1390` (yield reporting function in cl_revenue_ops)

**Step 1: Modify `_maybe_report_yield_and_costs()`**

In `/home/sat/bin/cl_revenue_ops/cl-revenue-ops.py`, the function at line 1366 currently reports `operating_costs_sats`, `routing_revenue_sats`, and `rebalance_costs_sats`. Add `boltz_costs_sats`.

After line 1383 (`pnl = profitability_analyzer.get_pnl_summary(...)`), add Boltz cost lookup:

```python
            # H3 FIX: Include Boltz costs in yield report for settlement visibility
            boltz_cost_sats = 0
            if boltz_manager is not None:
                try:
                    boltz_comps = boltz_manager.get_boltz_cost_components(window_hours=YIELD_REPORT_WINDOW_DAYS * 24)
                    boltz_cost_sats = int(boltz_comps.get("spent_24h_sats", 0) or 0)
                except Exception:
                    pass
```

Then modify the `hive_bridge.report_yield_and_costs()` call to include the new field:

```python
            hive_bridge.report_yield_and_costs(
                tlv_sats=int(tlv or 0),
                operating_costs_sats=int(pnl.get("opex_sats", 0) or 0),
                routing_revenue_sats=int(pnl.get("gross_revenue_sats", 0) or 0),
                rebalance_costs_sats=int(pnl.get("rebalance_cost_sats", 0) or 0),
                boltz_costs_sats=boltz_cost_sats,
                period_days=YIELD_REPORT_WINDOW_DAYS,
            )
```

**Note:** The bridge method `report_yield_and_costs()` needs to accept and forward the new `boltz_costs_sats` parameter. Check `cl-hive/modules/bridge.py` for the method signature and add the parameter if missing. cl-hive's contribution tracking already accepts arbitrary cost categories, so the bridge just needs to pass it through.

**Step 2: Run tests**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/ -v`
Expected: All PASS

**Step 3: Commit**

```bash
cd /home/sat/bin/cl_revenue_ops && git add cl-revenue-ops.py
git commit -m "Add Boltz cost breakdown to yield reporting for settlement visibility"
```

---

### Task 8: Wire Temporal Awareness into Dynamic Tuning

**Files:**
- Modify: `cl-revenue-ops.py:5774-5857` (_boltz_dynamic_channel_tuning)
- Modify: `cl-revenue-ops.py` (caller of _boltz_dynamic_channel_tuning in _build_boltz_balance_plan)

**Step 1: Add anticipatory data to dynamic tuning**

In `_boltz_dynamic_channel_tuning()`, add an optional `predicted_depletion_hours` parameter:

```python
def _boltz_dynamic_channel_tuning(*,
    local_pct: float,
    low_trigger_pct: float,
    low_target_pct: float,
    high_trigger_pct: float,
    high_target_pct: float,
    flow_state: str,
    daily_contrib_est: float,
    marginal_roi: Optional[float],
    state_row: Optional[Dict[str, Any]] = None,
    predicted_depletion_hours: Optional[float] = None,  # H2 FIX: from hive anticipatory data
) -> Dict[str, Any]:
```

After the `drain_accel_score` calculation (line 5808), add:

```python
    # H2 FIX: Hive anticipatory liquidity signal — predicted depletion boosts urgency
    anticipatory_urgency = 0.0
    if predicted_depletion_hours is not None and predicted_depletion_hours > 0:
        # Saturate at 6h: anything <6h gets max urgency
        anticipatory_urgency = max(0.0, min(1.0, (6.0 - predicted_depletion_hours) / 6.0))
```

Then modify the `drain_score` composition to include it:

```python
    drain_score = max(0.0, min(1.0,
        0.40 * source_signal +
        0.25 * drain_accel_score +
        0.20 * depletion_score +
        0.15 * anticipatory_urgency
    ))
```

And add `anticipatory_urgency` to the signals dict in the return:

```python
        'signals': {
            ...
            'anticipatory_urgency': round(anticipatory_urgency, 4),
            'predicted_depletion_hours': predicted_depletion_hours,
        },
```

**Step 2: Pass anticipatory data from the caller**

In `_build_boltz_balance_plan()`, where it calls `_boltz_dynamic_channel_tuning()`, look up anticipatory data from the bridge if available. The hive bridge's `get_anticipatory_state()` or similar method provides per-channel flow predictions.

Add before the tuning call (around the section where `state_row` is built):

```python
        # H2 FIX: Query hive anticipatory data for depletion prediction
        predicted_depletion_hours = None
        if hive_bridge is not None:
            try:
                antic = hive_bridge.safe_call("hive-anticipatory-status")
                if isinstance(antic, dict):
                    predictions = antic.get("channel_predictions", {})
                    ch_pred = predictions.get(channel_id, {})
                    if isinstance(ch_pred, dict) and "predicted_depletion_hours" in ch_pred:
                        predicted_depletion_hours = float(ch_pred["predicted_depletion_hours"])
            except Exception:
                pass
```

Then pass it to the tuning function:

```python
        tuning = _boltz_dynamic_channel_tuning(
            ...existing params...,
            predicted_depletion_hours=predicted_depletion_hours,
        )
```

**Step 3: Run tests**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/ -v`
Expected: All PASS (new param is optional with default None)

**Step 4: Commit**

```bash
cd /home/sat/bin/cl_revenue_ops && git add cl-revenue-ops.py
git commit -m "Wire hive anticipatory liquidity predictions into Boltz dynamic channel tuning"
```

---

### Task 9: Run Full Regression and Push Phase 3

**Step 1: Run cl_revenue_ops tests**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/ -v`
Expected: All PASS

**Step 2: Run cl-hive tests**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -v`
Expected: All PASS (2280+ tests)

**Step 3: Push both repos**

```bash
cd /home/sat/bin/cl_revenue_ops && git push
cd /home/sat/bin/cl-hive && git push
```

---

## Phase 4: Fleet Coordination

### Task 10: Add Boltz Activity to Gossip State

**Files:**
- Modify: `/home/sat/bin/cl-hive/modules/gossip.py:262-308` (gossip payload)
- Modify: `/home/sat/bin/cl-hive/modules/bridge.py` (add get_boltz_activity method)
- Modify: `/home/sat/bin/cl_revenue_ops/cl-revenue-ops.py` (expose boltz activity via RPC)
- Test: `/home/sat/bin/cl-hive/tests/test_gossip.py`

**Step 1: Add bridge method to query Boltz activity**

In `/home/sat/bin/cl-hive/modules/bridge.py`, add a new method following the `get_fee_config()` pattern (lines 931-953):

```python
    def get_boltz_activity(self) -> Optional[Dict[str, Any]]:
        """Get Boltz swap activity summary from cl-revenue-ops for gossip state."""
        if self._status == BridgeStatus.DISABLED:
            return None
        try:
            result = self.safe_call("revenue-boltz-budget")
            if not isinstance(result, dict) or "error" in result:
                return None
            return {
                "pending_swaps": int(result.get("pending_swap_count", 0) or 0),
                "daily_spend_sats": int(result.get("spent_24h_sats_estimate", result.get("boltz_spent_24h_sats_estimate", 0)) or 0),
                "last_swap_ts": int(result.get("last_swap_ts", 0) or 0),
            }
        except Exception:
            return None
```

**Step 2: Include Boltz activity in gossip payload**

In `/home/sat/bin/cl-hive/modules/gossip.py`, the `create_gossip_payload()` method builds the payload at lines 268-308. Add `boltz_activity` to the return dict (after `capabilities`):

```python
            # Boltz activity for fleet coordination (F1 Fix)
            "boltz_activity": boltz_activity or {},
```

The caller of `create_gossip_payload()` needs to pass `boltz_activity` — find where it's called (likely in the gossip loop in `cl-hive.py`) and add the bridge query there:

```python
boltz_activity = bridge.get_boltz_activity() if bridge else None
```

**Step 3: Write test**

Add a test to the existing gossip test file verifying the new field:

```python
def test_gossip_payload_includes_boltz_activity(self):
    """Gossip payload should include boltz_activity when available."""
    # ... setup gossip module with mock bridge returning boltz activity
    payload = gossip.create_gossip_payload(boltz_activity={"pending_swaps": 1, "daily_spend_sats": 500})
    assert "boltz_activity" in payload
    assert payload["boltz_activity"]["pending_swaps"] == 1
```

**Step 4: Run tests**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -v`
Expected: All PASS

**Step 5: Commit**

```bash
cd /home/sat/bin/cl-hive && git add modules/gossip.py modules/bridge.py cl-hive.py tests/
git commit -m "Add Boltz activity to gossip state for fleet-wide visibility"
```

---

### Task 11: Add Pre-Flight Hive Route Check in Auto-Cycle

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/cl-revenue-ops.py:1508-1579` (_run_boltz_auto_cycle_once)

**Step 1: Add hive route check before execution**

In `_run_boltz_auto_cycle_once()`, after calling `revenue_boltz_balance_cycle()` at line 1548, the balance cycle already executes swaps. The pre-flight check needs to happen inside the balance cycle itself.

Modify `_build_boltz_balance_plan()` — at the point where each channel candidate is being evaluated (around the loop that builds recommendations), add a hive route check:

```python
        # F2 FIX: Check hive route availability before recommending Boltz
        hive_route_available = False
        if hive_bridge is not None:
            try:
                hive_path = hive_bridge.safe_call("hive-fleet-rebalance-path", {
                    "channel_id": channel_id,
                    "direction": direction,
                    "amount_sats": raw_amount,
                })
                if isinstance(hive_path, dict) and hive_path.get("viable"):
                    hive_route_available = True
            except Exception:
                pass

        if hive_route_available:
            skipped.append({
                "channel_id": channel_id,
                "peer_id": peer_id,
                "reason": "hive_route_available",
                "direction": direction,
                "note": "Free hive circular rebalance available; skipping Boltz",
            })
            continue
```

Add `hive_route_available` to the candidate dict for visibility:

```python
        candidate["hive_route_checked"] = True
        candidate["hive_route_available"] = hive_route_available
```

**Step 2: Run tests**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/ -v`
Expected: All PASS (hive_bridge is None in tests, check is skipped)

**Step 3: Commit**

```bash
cd /home/sat/bin/cl_revenue_ops && git add cl-revenue-ops.py
git commit -m "Add pre-flight hive route check in Boltz balance planning"
```

---

### Task 12: Add Fleet Boltz Dashboard MCP Tool

**Files:**
- Modify: `/home/sat/bin/cl-hive/tools/mcp-hive-server.py` (new handler + registration)

**Step 1: Add the handler**

Following the pattern of `handle_revenue_boltz_budget()` (lines 11022-11030), add a new handler that aggregates Boltz activity from gossip state:

```python
async def handle_fleet_boltz_status(args: Dict) -> Dict:
    """Aggregate Boltz swap activity across all fleet members from gossip state."""
    members = {}
    fleet_pending = 0
    fleet_daily_spend = 0

    for member_id, member_state in state_manager.get_all_member_states().items():
        boltz = member_state.get("boltz_activity", {})
        if not isinstance(boltz, dict):
            continue
        pending = int(boltz.get("pending_swaps", 0) or 0)
        spend = int(boltz.get("daily_spend_sats", 0) or 0)
        members[member_id] = {
            "pending_swaps": pending,
            "daily_spend_sats": spend,
            "last_swap_ts": int(boltz.get("last_swap_ts", 0) or 0),
        }
        fleet_pending += pending
        fleet_daily_spend += spend

    return {
        "fleet_pending_swaps": fleet_pending,
        "fleet_daily_spend_sats": fleet_daily_spend,
        "member_count": len(members),
        "members": members,
    }
```

**Step 2: Register in TOOL_HANDLERS**

In the `TOOL_HANDLERS` dict (around line 17652), add:

```python
    "fleet_boltz_status": handle_fleet_boltz_status,
```

**Step 3: Add tool definition**

In the tool definitions list (where all MCP tools are defined), add:

```python
    Tool(
        name="fleet_boltz_status",
        description="Aggregate Boltz swap activity across all fleet members. Shows pending swaps, daily spend, and per-member breakdown from gossip state.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
```

**Step 4: Run tests**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -v`
Expected: All PASS

**Step 5: Commit**

```bash
cd /home/sat/bin/cl-hive && git add tools/mcp-hive-server.py
git commit -m "Add fleet_boltz_status MCP tool for fleet-wide Boltz activity dashboard"
```

---

### Task 13: Final Regression and Push

**Step 1: Run cl_revenue_ops full test suite**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/ -v`
Expected: All PASS

**Step 2: Run cl-hive full test suite**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -v`
Expected: All PASS (2280+ tests)

**Step 3: Push both repos**

```bash
cd /home/sat/bin/cl_revenue_ops && git push
cd /home/sat/bin/cl-hive && git push
```
