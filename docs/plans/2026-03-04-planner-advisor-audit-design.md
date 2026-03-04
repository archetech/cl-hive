# Planner + Advisor Pipeline Audit

**Date:** 2026-03-04
**Status:** Approved
**Scope:** Systematic audit and fix of the expansion proposal pipeline from planner through AI advisor evaluation

## Problem

The planner proposes channel expansions that the AI advisor rejects with incorrect data. Specific symptoms:

- AI advisor reports "36 channels (>30 limit)" when the node has 29 active channels
- 18 consecutive rejections, 24h cooldown — permanently stuck
- "31.2% underwater channels" cited as reason to block expansion

Root causes: the AI advisor derives channel counts from raw data (including non-active channels), the rejection backoff can never escape, and the planner lacks profitability awareness.

## Pipeline Overview

```
Planner.run_cycle()
  → get_underserved_targets()     # identifies expansion candidates
  → _propose_expansion()          # creates pending_action
  → pending_actions table         # stored in database
  ↓
MCP Server auto_evaluate_proposal()
  → hive-getinfo                  # our num_active_channels
  → advisor_get_peer_intel()      # target's channel count + quality
  → hard-coded thresholds         # 10/15/50 channel gates
  ↓
AI Advisor (LLM)
  → reads strategy prompt         # approval_criteria.md
  → reads fleet health data       # underwater %, channel lists
  → applies its own reasoning     # where "36 channels >30 limit" comes from
```

## Confirmed Issues

### Issue A: AI advisor receives wrong channel count

The LLM counts channels from raw `listpeerchannels` output which includes ONCHAIN, CLOSING, and other non-active states. It sees 36 total channels instead of 29 active ones.

**Impact:** Expansions rejected based on incorrect data.

### Issue B: Approval criteria mismatch

- `approval_criteria.md` says >50 channels → REJECT
- `auto_evaluate_proposal()` code says >50 channels → ESCALATE

**Impact:** Inconsistent behavior depending on whether the auto-evaluator or AI advisor processes the proposal.

### Issue C: Incomplete advisor fallback payload

The planner has two code paths for creating pending_actions:
- **DecisionEngine path** (line ~2248): includes `target_channel_count`, `quality_score`, `quality_recommendation`
- **Advisor fallback path** (line ~2295): omits all three

**Impact:** Approval decisions lack context when using the fallback path.

### Issue D: Rejection backoff permanently stalls

The exponential backoff checks `recent_rejections` within a time window that caps at 24h. Once enough rejections accumulate, the window always has >= threshold rejections and the planner never escapes.

**Impact:** Expansion proposals permanently disabled after a streak of rejections.

### Issue E: Network cache fragility

The planner's network cache indexes each channel under both endpoints (source and destination). SCID-level dedup prevents actual double-counting today, but any consumer that sums the cache directly without understanding this will get inflated results.

**Impact:** Latent bug risk. Current code works but is fragile.

### Issue F: No profitability awareness

The planner proposes expansions without checking underwater/bleeder channel percentage. The AI advisor then rejects because of underwater channels — wasting a proposal cycle and incrementing the rejection counter.

**Impact:** Proposals that will obviously be rejected still get created, accelerating the backoff stall.

## Fixes

### Fix 1: Pre-computed node summary in proposal context

Add a `node_summary` dict to every pending_action payload for channel_open actions. Computed at proposal creation time from `listpeerchannels` with proper state filtering.

```python
node_summary = {
    "active_channels": 29,          # CHANNELD_NORMAL only
    "pending_channels": 2,          # AWAITING_LOCKIN states
    "closing_channels": 5,          # ONCHAIN/closing states
    "total_capacity_sats": 45000000,
    "underwater_count": 5,
    "underwater_pct": 17.2,
}
```

The AI advisor and auto_evaluate_proposal both use this instead of deriving counts from raw data.

**Files:** `modules/planner.py` (compute summary), `cl-hive.py` (include in payload)

### Fix 2: Align approval_criteria.md with code

Update the strategy prompt to match the code: >50 channels → ESCALATE (not REJECT). The code behavior (escalate for human review) is the more conservative and correct choice.

**Files:** `production/strategy-prompts/approval_criteria.md`

### Fix 3: Complete advisor fallback payload

Unify both pending_action creation paths to include the same fields:
- `target_channel_count`
- `quality_score`
- `quality_recommendation`
- `node_summary` (from Fix 1)

**Files:** `modules/planner.py` (around line 2295)

### Fix 4: Fix rejection backoff stall

Replace the time-window approach:

**Current (broken):**
- Count rejections in last N hours
- If >= threshold, pause
- N grows exponentially but caps at 24h
- Once 24h has enough rejections, stuck forever

**New:**
- Track `last_expansion_success_at` timestamp (approval or execution)
- Only count rejections AFTER that timestamp
- Keep exponential backoff for spacing between attempts
- Manual reset via `hive-planner-reset-backoff` RPC command

**Files:** `modules/planner.py`, `modules/database.py` (new timestamp field)

### Fix 5: Profitability gate in planner

Before proposing expansions in `_propose_expansion()`, check underwater channel percentage via the bridge (cl-revenue-ops profitability data). If >40% underwater, skip with a clear log message matching the approval_criteria.md DEFER threshold.

This prevents creating proposals that will be rejected, avoiding unnecessary rejection counter increments.

**Files:** `modules/planner.py`

### Fix 6: Harden network cache consumers

Add `get_unique_channels_for(target)` method that returns deduplicated channel list regardless of cache indexing strategy. Replace direct `self._network_cache.get(target, [])` calls.

**Files:** `modules/planner.py`

## Out of Scope

- Redesigning the network cache storage format (too much churn for the benefit)
- Changing the AI advisor's LLM prompt structure beyond threshold alignment
- Adding new approval criteria categories

## Testing

- Unit tests for `node_summary` computation with mixed channel states
- Unit tests for rejection backoff reset behavior
- Unit test verifying `get_unique_channels_for()` dedup correctness
- Integration test: planner cycle with underwater gate
- Verify existing planner tests still pass
