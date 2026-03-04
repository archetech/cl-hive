# Boltz Integration Audit — Systematic Correctness & Fleet Coordination

**Date:** 2026-03-04
**Status:** Approved
**Scope:** Audit and harden the Boltz integration in cl_revenue_ops for correctness, add comprehensive test coverage, wire hive intelligence into Boltz decisions, and add fleet-level Boltz coordination.

## Problem

The Boltz integration in cl_revenue_ops has correctness bugs (race conditions, budget gaps) and extremely thin test coverage (2 test classes for ~3000 lines of code). Additionally, Boltz operates in isolation from cl-hive's fleet intelligence — no temporal awareness, no fleet coordination, no structured AI advisor guidance, and Boltz costs invisible in settlement accounting.

## Current State

### cl_revenue_ops — substantial Boltz integration:
1. **BoltzCliManager** (1534 lines) — Full boltzd interaction layer with loop-in/out/chain swaps, budget enforcement, TOCTOU protection, external-pay fallback
2. **Balance planning engine** (~500 lines) — Profit-gated recommendations with dynamic per-channel tuning
3. **Expansion treasury** (~200 lines) — On-chain reserve building via reverse swaps
4. **Auto-cycle background loop** — 15-min fixed interval scheduler
5. **17 RPC methods** — Full operational API

### cl-hive — pass-through with opportunity scoring:
1. **17 MCP tools** — All delegating to cl-revenue-ops RPCs
2. **Opportunity scanner** — Scores Boltz as last-resort fallback after hive/market rebalancing
3. **Proactive advisor** — Conditionally gathers Boltz wallet/budget/recommendations
4. **No Boltz state** — All state lives in cl-revenue-ops

### Test coverage:
- `TestBudgetTOCTOU` — Verifies `_swap_creation_lock` exists
- `TestBackupMnemonicOmission` — Verifies `backup()` omits mnemonic by default
- **Nothing else** — Balance planning, auto-cycle, expansion treasury, budget accounting, external-pay fallback all untested

## Confirmed Issues

### Issue C1: Cooldown TOCTOU Race

In `revenue-boltz-balance-cycle` and `revenue-boltz-expansion-treasury-cycle`, the cooldown check acquires `_boltz_balance_lock`, checks the last-action timestamp, releases the lock, then executes the swap outside the lock. Two threads entering simultaneously for the same channel can both pass the cooldown check, causing double execution.

**Impact:** Same channel gets two Boltz swaps when only one was intended.

### Issue C2: Pending Swap Budget Reservation

`get_boltz_cost_components()` only counts completed swaps in `spent_24h_sats` and always returns `reserved_24h_sats: 0`. A pending swap's estimated fee is invisible to the budget. Two sequential swap requests can both pass the budget check if the first hasn't completed yet.

**Impact:** Budget overcommit when swaps overlap.

### Issue C3: Auto-Cycle Error Counter Reset on Blocked

When `_run_boltz_auto_cycle_once()` gets a `blocked` result (pending swaps exist), it resets `consecutive_errors` to 0. This hides real failures — if the previous cycle had a genuine error, the blocked state clears the counter, making monitoring unreliable.

**Impact:** Error monitoring gives false all-clear after blocked cycles.

### Issue H1: No Boltz Approval Criteria

`approval_criteria.md` has detailed structured criteria for channel opens, fee changes, fee anchors, and rebalances. There are no criteria for Boltz swaps. The AI advisor has no guidance for when to approve/reject Boltz proposals.

**Impact:** AI advisor makes ad-hoc Boltz decisions without structured framework.

### Issue H2: No Temporal Awareness

Auto-cycle runs on a fixed 15-minute interval. It doesn't leverage cl-hive's Kalman-filtered flow predictions or temporal depletion estimates. A channel predicted to deplete in 2 hours doesn't get faster Boltz attention than a stable one.

**Impact:** Reactive rather than proactive Boltz scheduling.

### Issue H3: Boltz Costs Invisible in Settlement

`_maybe_report_yield_and_costs()` reports total operating costs to cl-hive for fleet settlement, but doesn't break out Boltz spend as a separate category. Fleet settlement can't distinguish heavy Boltz spenders from non-spenders.

**Impact:** Unfair cost attribution in fleet settlement.

### Issue F1: No Fleet Boltz Visibility

Fleet members have no visibility into each other's Boltz activity. Two nodes might loop-out on the same corridor simultaneously, or one node does a costly Boltz swap when a peer could serve the need via free hive rebalance.

**Impact:** Wasted Boltz spend when free alternatives exist.

### Issue F2: Auto-Cycle Skips Hive Route Check

The opportunity scanner deprioritizes Boltz when hive routes exist, but the auto-cycle execution path doesn't perform this check. Balance cycle can execute Boltz when a hive circular rebalance would be free.

**Impact:** Unnecessary Boltz cost.

## Fixes

### Phase 1: Correctness

#### Fix C1: Cooldown Pre-Claim

Pre-claim the cooldown slot inside `_boltz_balance_lock` before releasing it for execution. If the swap fails or is skipped, clear the claim.

```python
with _boltz_balance_lock:
    last_ts = int(_boltz_balance_last_action.get(ch_id, 0) or 0)
    if cooldown_active:
        continue
    # Pre-claim to prevent double execution
    _boltz_balance_last_action[ch_id] = now

# Execute swap outside lock...
# If swap fails, clear the pre-claim:
if not success:
    with _boltz_balance_lock:
        if _boltz_balance_last_action.get(ch_id) == now:
            _boltz_balance_last_action[ch_id] = last_ts  # Restore
```

**Files:** `cl-revenue-ops.py` (balance cycle ~line 6414, treasury cycle ~line 6660)

#### Fix C2: Pending Swap Reservation

In `get_boltz_cost_components()`, after counting completed swaps, iterate remaining non-completed, non-error swaps and estimate their fees for `reserved_24h_sats`.

```python
reserved = 0
for s in swaps:
    if self._is_completed_swap(s) or self._is_error_swap(s):
        continue
    ts = self._swap_created_ts(s)
    if ts and ts >= cutoff:
        reserved += max(0, self._estimate_swap_fee_sats(s))
```

**Files:** `modules/boltz_manager.py` (~line 658-697)

#### Fix C3: Error Counter Blocked State

Only reset `consecutive_errors` on actual success. Leave unchanged on blocked state.

```python
if isinstance(result, dict) and 'error' in result:
    consecutive_errors += 1
elif result.get('status') in ('executed', 'dry_run'):
    consecutive_errors = 0
# else: blocked/other — leave counter unchanged
```

**Files:** `cl-revenue-ops.py` (~line 1556-1565)

### Phase 2: Test Coverage

New test file `tests/test_boltz_integration.py` with comprehensive coverage:

#### T1: TestBoltzBalancePlan
- Depleting channels trigger loop-in recommendations
- Saturating channels trigger loop-out recommendations
- Dynamic tuning adjusts thresholds for high-contribution channels
- Profit guard rejects unprofitable rebalances
- Policy direction filtering
- Severity calculation for both directions
- Sorting: profit-safe channels first
- Budget-exhausted plan returns no recommendations

#### T2: TestBoltzAutoCycle
- Successful cycle executes one swap and records timestamp
- Cycle respects `max_actions` limit
- Cycle skips channels on cooldown
- Cycle blocked when pending swaps exist
- Error counter: increments on failure, unchanged on blocked, resets on success
- Startup delay respected
- Shutdown event stops the loop

#### T3: TestBoltzExpansionTreasury
- Treasury plan recommends reverse swaps when deficit exists
- Plan respects profit filter
- Deficit tracking within cycle execution
- No recommendations when balance meets target

#### T4: TestBoltzBudgetAccounting
- Completed swaps counted in `spent_24h_sats`
- Pending swaps counted in `reserved_24h_sats`
- Rolling 24h window excludes old swaps
- Unified budget combines all cost categories
- Budget enforcement blocks swap when remaining < estimated fee
- Boltzd unreachable returns safe error dict

#### T5: TestBoltzCooldownPreClaim
- Pre-claimed cooldown prevents double execution
- Failed swap clears the pre-claim

#### T6: TestBoltzExternalPayFallback
- chanId rejection triggers external-pay retry
- First-hop pinning builds correct exclude list
- Invoice extraction from various response formats

**Files:** `tests/test_boltz_integration.py` (new)

### Phase 3: Hive Integration

#### Fix H1: Boltz Approval Criteria

Add "Boltz Swap Actions" section to `approval_criteria.md`:

**APPROVE** if ALL conditions met:
- Channel is profitable and has routing activity
- Estimated swap fee < remaining daily Boltz budget
- Expected net benefit > 1.5x estimated fee
- No pending Boltz swap on same channel
- Hive internal and market rebalance options exhausted (fallback chain)
- Channel balance outside acceptable range (<20% or >80% local)

**REJECT** if ANY condition applies:
- Channel is underwater/bleeder (fix the channel, don't feed it)
- Would exceed daily Boltz budget
- Hive internal rebalance available for same direction (use free route)
- Market rebalance available at lower cost
- Channel balance is acceptable (20-80% range)
- Swap fee > 1000 ppm of amount

**DEFER** if:
- Expected net benefit is marginal (1.0-1.5x fee)
- Channel is < 14 days old (let optimizer learn)
- Treasury cycle already running
- Any uncertainty about need

**Files:** `cl-hive/production/strategy-prompts/approval_criteria.md`

#### Fix H2: Temporal Awareness in Dynamic Tuning

In `_boltz_dynamic_channel_tuning()`, query cl-hive's bridge for anticipatory liquidity data. Use predicted depletion time as an additional urgency signal alongside existing `kalman_velocity`.

Channels with predicted depletion < 6h get a higher `drain_accel_score`. Channels with stable predicted flow get a lower score, deferring their Boltz swap.

**Files:** `cl-revenue-ops.py` (~line 5774, `_boltz_dynamic_channel_tuning()`)

#### Fix H3: Boltz Cost in Yield Reporting

Add `boltz_cost_sats` to the yield report payload sent via bridge to cl-hive. cl-hive's `contribution.py` already accepts arbitrary cost categories.

```python
costs = {
    "rebalance_cost_sats": rebalance_spend,
    "boltz_cost_sats": boltz_spend,  # NEW
    "open_cost_sats": open_costs,
    "close_cost_sats": close_costs,
}
```

**Files:** `cl-revenue-ops.py` (~line 1366, `_maybe_report_yield_and_costs()`)

### Phase 4: Fleet Coordination

#### Fix F1: Boltz Activity Gossip

Add a `boltz_activity` block to member state shared via existing gossip heartbeat:

```python
"boltz_activity": {
    "pending_swaps": 1,
    "last_swap_direction": "loop_out",
    "daily_spend_sats": 150,
    "last_swap_ts": 1709510400,
}
```

cl-revenue-ops reports this via bridge. cl-hive includes it in gossip state. Fleet members receive it automatically through existing gossip protocol.

**Files:** `cl-revenue-ops.py` (bridge reporting), `cl-hive/modules/gossip.py` (include in state), `cl-hive/modules/bridge.py` (query method)

#### Fix F2: Pre-Flight Hive Route Check in Auto-Cycle

Before executing each recommended Boltz swap in `_run_boltz_auto_cycle_once()`, query the bridge for `fleet_rebalance_path` for the target channel. If a viable hive route exists, skip the Boltz swap and log a suggestion to use hive rebalance instead.

Lightweight check — bridge call is fast and cached. Swap is skipped (not blocked permanently), retried next cycle if hive route doesn't materialize.

**Files:** `cl-revenue-ops.py` (~line 1508, `_run_boltz_auto_cycle_once()`)

#### Fix F3: Fleet Boltz Dashboard MCP Tool

New `fleet_boltz_status` MCP tool that aggregates Boltz activity from gossip state across all fleet members. Returns per-member spend, pending swaps, and fleet totals.

**Files:** `cl-hive/tools/mcp-hive-server.py` (new handler), `cl-hive/modules/state_manager.py` (read Boltz gossip from HiveMap)

## Out of Scope

- Redesigning the Boltz budget architecture (callback structure is sound, not circular)
- Changing Boltz as a fallback-last design (correct per approval_criteria.md's "hive routes first" principle)
- Adding Boltz auto-execution capability (QUEUE_FOR_REVIEW is the right safety posture)
- Redesigning the opportunity scanner priority formula (existing scoring is well-calibrated)

## Testing

- Phase 1: Tests for each correctness fix (pre-claim, pending reservation, error counter)
- Phase 2: Comprehensive test suite covering all untested Boltz code paths
- Phase 3: Tests for approval criteria alignment, temporal tuning, yield reporting
- Phase 4: Tests for gossip integration, pre-flight check, dashboard tool
- Full regression on both cl-hive and cl_revenue_ops test suites
