# cl-hive Module Audit: Dead Code Removal & Correctness Review

**Date**: 2026-03-08
**Status**: Approved

## Problem

cl-hive has 34 core modules totaling ~65,000 lines. After the monolith
decomposition and CLBoss removal, dead code likely remains in individual
modules — unused methods, dead protocol helpers, orphaned database queries.
No systematic audit has been performed.

## Goal

Systematically audit each core module to:
1. Remove dead code (methods with zero callers)
2. Fix correctness issues in live methods
3. Improve efficiency where obvious wins exist

Zero behavioral changes to live code paths. Zero new features.

## Scope

### In Scope (34 core modules)

**Tier 1 — Large (22K lines):**
- `database.py` (9,046 lines) — Dead table methods, unused queries
- `protocol.py` (7,324 lines) — Dead message types/enums, unused serialization
- `rpc_commands.py` (5,961 lines) — Unreachable RPC wrappers

**Tier 2 — Medium (21K lines, 13 modules):**
- `fee_coordination.py` (3,071)
- `anticipatory_liquidity.py` (2,789)
- `settlement.py` (2,699)
- `planner.py` (2,570)
- `strategic_positioning.py` (2,329)
- `cost_reduction.py` (2,192)
- `liquidity_coordinator.py` (1,922)
- `mcf_solver.py` (1,699)
- `channel_rationalization.py` (1,300)
- `fee_intelligence.py` (1,200)
- `cooperative_expansion.py` (1,224)
- `routing_intelligence.py` (1,034)
- `yield_metrics.py` (1,003)

**Tier 3 — Small (12K lines, 18 modules):**
- `splice_manager.py` (1,081)
- `state_manager.py` (924)
- `bridge.py` (903)
- `network_metrics.py` (891)
- `routing_pool.py` (876)
- `vpn_transport.py` (759)
- `membership.py` (751)
- `task_manager.py` (724)
- `intent_manager.py` (709)
- `peer_reputation.py` (617)
- `quality_scorer.py` (608)
- `gossip.py` (609)
- `handshake.py` (598)
- `budget_manager.py` (503)
- `relay.py` (449)
- `governance.py` (424)
- `splice_coordinator.py` (411)
- `config.py` (306)
- `outbox.py` (286)
- `contribution.py` (250)
- `health_aggregator.py` (387)
- `idempotency.py` (108)
- `phase6_ingest.py` (112)

### Out of Scope

- Companion-stack modules (DID, cashu, marketplace, liquidity marketplace,
  management schemas, nostr transport, identity adapter)
- Newly extracted modules (protocol_handlers, background_loops, plugin_options,
  rpc_pool, log_writer) — verbatim moves, correct by construction
- cl-hive.py entry point
- Docker files, MCP server, docs

## Per-Module Audit Process

### Step 1: Dead Code Identification
- Grep every public function/method/class for callers across cl-hive AND cl_revenue_ops
- Mark functions with 0 external callers as removal candidates
- Check internal-only callers — if the only caller is also dead, both are dead

### Step 2: Correctness Review
- For surviving methods: check bugs, off-by-one, missing error handling
- Compare against protocol spec where applicable
- Flag methods that do more than needed

### Step 3: Efficiency Improvements
- Redundant computations, unnecessary copies
- Overly broad try/except blocks
- Thread-safety issues

### Step 4: Cross-Dependency Safety Check
- Verify nothing breaks cl_revenue_ops RPC calls (10 known methods):
  - hive-fee-intel-query
  - hive-report-fee-observation
  - hive-member-health
  - hive-report-health
  - hive-liquidity-state
  - hive-report-liquidity-state
  - hive-anticipatory-status
  - hive-rebalance-recommendations
  - hive-channel-closed
  - hive-channel-opened
- Run full test suite

### Step 5: Commit
- One commit per module
- All tests must pass before each commit

## What We Do NOT Do

- No new features
- No API changes to live methods
- No module structure refactoring
- No function signature changes for methods with external callers

## Execution

~20 tasks executed via subagent-driven development:
- Batch 1: 3 tasks (T1 modules, one per module)
- Batch 2: 13 tasks (T2 modules, one per module)
- Batch 3: ~4 tasks (T3 modules, grouped by 4-5)

## Success Criteria

- All 2,328+ tests pass after every commit
- Zero behavioral changes to live code paths
- Estimated 3,000-6,000 lines removed across all modules

## Risk

Low. Pure dead code removal and localized fixes. Each commit is
independently reversible via git revert.
