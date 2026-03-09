# Liquidity-Aware Proposal Gate Design

**Date**: 2026-03-09
**Status**: Approved

## Problem

The topology planner's budget check evaluates each expansion proposal independently. When multiple proposals are queued in advisor mode, each one passes the budget check individually but collectively they exceed available on-chain funds. This leads to proposals that can't be funded when approved.

## Solution

Deduct the sum of already-pending `channel_open` proposals from the available budget before proposing new expansions. One new database query, one subtraction in the existing budget calculation. No new tables, no new config.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Gate location | Proposal time only | Prevent noisy unfundable proposals from cluttering the advisor queue |
| Approach | Deduct pending from available | Right granularity — accounts for actual committed amounts without over-engineering |
| Storage | Query existing `pending_actions` table | Data already exists; just needs a SUM query |
| Config | Zero new options | Simple enough to not need tuning |

## Budget Calculation Change

In `_propose_expansion()` (planner.py), the existing three-way budget calc becomes:

```python
daily_remaining = self.db.get_available_budget(daily_budget)
spendable_onchain = int(onchain_balance * (1.0 - budget_reserve_pct))
max_per_channel = int(daily_budget * budget_max_per_channel_pct)

# Deduct funds already committed to pending proposals
pending_committed = self.db.get_pending_channel_open_total()
gross_available = min(daily_remaining, spendable_onchain, max_per_channel)
available_budget = max(0, gross_available - pending_committed)
```

When `available_budget < min_channel_size`, the planner logs why and skips — same as today, but now the log message includes the pending commitment amount.

## Database Method

One new method on the database class:

```python
def get_pending_channel_open_total(self) -> int:
    """Sum of proposed_size_sats from all pending channel_open actions."""
```

SQL:

```sql
SELECT COALESCE(SUM(
    COALESCE(
        json_extract(payload, '$.proposed_size_sats'),
        json_extract(payload, '$.channel_size_sats')
    )
), 0) AS total
FROM pending_actions
WHERE action_type = 'channel_open'
  AND status = 'pending'
  AND (expires_at IS NULL OR expires_at > ?)
```

Timestamp parameter = `int(time.time())`. Returns 0 when no pending proposals — existing behavior unchanged. Expired actions excluded automatically.

## Logging

When the gate blocks, enrich the existing skip log:

```
EXPANSION GATE: available_budget=800000 < min_channel_size=1000000
  (gross=1800000, pending_committed=1000000 from 1 pending proposals)
```

## Integration Points

### Files Modified (2 + 1 test file)

| File | Change |
|------|--------|
| `modules/database.py` | Add `get_pending_channel_open_total()` method |
| `modules/planner.py` | Deduct pending committed from available budget in `_propose_expansion()`, enrich log message |

### What Stays the Same

- `pending_actions` table schema (no migration)
- Budget hold system (Phase 8 cooperative expansion — independent)
- Governance flow (advisor/failsafe — unchanged)
- All existing budget checks (daily, reserve, per-channel — unchanged, just fed adjusted number)
- Feerate gate, profitability gate, constraint backoff — unchanged
- Preflight checks at execution time — unchanged

## Testing Strategy

- `test_pending_total_empty` — no pending actions returns 0
- `test_pending_total_sums_correctly` — two pending proposals sum their sizes
- `test_pending_total_excludes_expired` — expired proposals not counted
- `test_expansion_blocked_by_pending` — planner skips when pending commits exhaust budget
