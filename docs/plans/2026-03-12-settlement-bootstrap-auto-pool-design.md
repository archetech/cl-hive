# Settlement Bootstrap Auto Pool Design

## Problem

Routing-pool settlements are not finalizing automatically. The current code auto-snapshots contributions in the hourly settlement loop, but actual pool finalization only happens through the manual `hive-pool-settle` RPC. At the same time, distributed settlement proposals remain blocked in a two-member hive because settlement quorum still requires strict majority, so `1/2` votes is insufficient.

The immediate operational symptoms are:

- routing-pool distributions calculate successfully but do not finalize on cadence
- backlog weeks can accumulate with no automatic catch-up
- distributed settlement proposals can remain `pending` at `1/2`
- missing auto-votes are hard to diagnose because proposal receipt and vote rejection reasons are not surfaced clearly

## Current Code Seams

- [modules/background_loops.py](/home/sat/bin/cl-hive/.worktrees/settlement-bootstrap-auto-pool-20260312/modules/background_loops.py#L893) runs the hourly settlement loop and already auto-snapshots routing-pool contributions plus proposal creation, voting, and execution.
- [modules/rpc_commands.py](/home/sat/bin/cl-hive/.worktrees/settlement-bootstrap-auto-pool-20260312/modules/rpc_commands.py#L2288) exposes `pool_settle`, but it is manual-only.
- [modules/routing_pool.py](/home/sat/bin/cl-hive/.worktrees/settlement-bootstrap-auto-pool-20260312/modules/routing_pool.py#L467) finalizes pool distributions for a weekly period and already guards against double-settlement.
- [modules/settlement.py](/home/sat/bin/cl-hive/.worktrees/settlement-bootstrap-auto-pool-20260312/modules/settlement.py#L1243) implements `verify_and_vote()`, but today it only returns `None` on rejection paths.
- [modules/settlement.py](/home/sat/bin/cl-hive/.worktrees/settlement-bootstrap-auto-pool-20260312/modules/settlement.py#L1374) computes readiness quorum with `(active_count // 2) + 1`, which means a two-member hive still needs both votes.
- [modules/protocol_handlers.py](/home/sat/bin/cl-hive/.worktrees/settlement-bootstrap-auto-pool-20260312/modules/protocol_handlers.py#L1994) sends settlement traffic via `sendcustommsg` through the outbox/direct broadcast path, so missing votes cannot be blamed on optional companion comms alone.

## Approved Behavior

### 1. Automatic routing-pool settlement

Extend the existing hourly settlement cadence so it also finalizes routing-pool distributions for the previous completed week. This must not settle the current in-progress week.

### 2. Backfill missed weeks

If one or more completed weeks were missed, settle them oldest-first until the backlog is cleared. The flow must be idempotent:

- skip weeks that already have finalized pool distributions
- auto-clear weeks with no revenue or no contributions so they do not block later weeks
- process only completed weeks up to the prior week

### 3. Settlement-only bootstrap quorum

Apply a settlement-specific bootstrap quorum exception only when the active hive size is exactly two members:

- distributed settlement proposals treat `1/2` votes as sufficient for readiness
- all other vote-based workflows keep their existing majority/quorum rules

### 4. Explicit settlement diagnostics

Make it immediately visible whether a proposal:

- was never received
- was received but rejected during `verify_and_vote()`
- was voted on but did not advance to ready/executed state

The rejection path should report a structured reason such as:

- `expired`
- `already_voted`
- `period_already_settled`
- `hash_mismatch`
- `plan_hash_mismatch`
- `sign_failed`

## Architecture

### Settlement loop extension

Keep all orchestration inside the existing hourly `settlement_loop()` rather than adding another scheduler. Add a new automatic pool-finalization phase adjacent to the existing backlog-first proposal logic.

That phase should:

1. derive completed candidate periods up to the previous week
2. inspect routing-pool settlement state for each candidate
3. finalize the oldest unsettled pool period first
4. continue backlog replay on later loop cycles until caught up

This keeps cadence, retries, and logging in one place.

### Pool-settlement state handling

Reuse `RoutingPool.settle_period()` as the recording path rather than duplicating settlement math in the background loop. The loop should decide *when* to settle; `RoutingPool` should remain responsible for *how* a period is recorded.

Zero-revenue and no-contribution periods need an explicit “cleared” path so backlog replay can advance without creating misleading distribution rows.

### Quorum handling

Keep quorum math centralized in `SettlementManager.check_quorum_and_mark_ready()`, but add a narrow helper for settlement bootstrap quorum. The helper should only alter readiness threshold when:

- the active member count is exactly `2`
- the call is for distributed settlement readiness

No shared quorum utility should be generalized across unrelated governance features in this change.

### Diagnostics

Add structured logging around proposal receipt and vote attempts in the settlement receive path and `verify_and_vote()`. The intent is not to guess that transport failed, but to surface the exact drop-off point:

- received proposal
- rejected proposal with reason
- vote broadcast attempted
- quorum check result

If a small status surface is helpful, keep it settlement-specific and lightweight.

## Safety Constraints

- Never auto-settle the current week.
- Never double-settle a week with existing distributions.
- Do not change quorum rules for bans, promotions, or other governance decisions.
- Do not rely on external companion comms status as proof of settlement transport failure.

## Verification Notes

Baseline verification in this worktree:

- Targeted settlement test baseline passed:
  - `/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_distributed_settlement.py tests/test_routing_pool.py tests/test_routing_settlement_bugfixes.py tests/test_protocol.py tests/test_outbox.py tests/test_outbox_7_fixes.py -q`
  - Result: `196 passed in 7.37s`

Observed preexisting verification issue:

- The full repo baseline command `/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/ -q` advanced cleanly through `57%` but did not complete in a reasonable window during planning. That should be treated as a preexisting suite/runtime issue until reproduced and debugged separately.
