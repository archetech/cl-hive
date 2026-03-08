# cl-hive Module Audit: Dead Code Removal & Correctness Review

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Systematically audit each of cl-hive's 34 core modules to remove dead code, fix correctness issues, and improve efficiency — with zero behavioral changes to live code paths.

**Architecture:** Per-module grep-based dead code identification, followed by correctness review of surviving methods. One commit per module. All 2,328 tests must pass after every commit.

**Tech Stack:** Python 3.12, pytest, grep/ripgrep for caller analysis

---

## Prerequisites

- Working directory: `/home/sat/bin/cl-hive/`
- Test command: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q --deselect tests/test_anticipatory_nnlb_bugs.py::TestHiveBridgeKeyFix`
- Current test count: 2,328 passing
- Cross-project check directory: `/home/sat/bin/cl_revenue_ops/`
- Design doc: `docs/plans/2026-03-08-module-audit-design.md`

## Out of Scope

These modules are NOT audited:
- **Companion-stack:** `did_credentials.py`, `cashu_escrow.py`, `marketplace.py`, `liquidity_marketplace.py`, `management_schemas.py`, `nostr_transport.py`, `identity_adapter.py`
- **Newly extracted (verbatim moves):** `protocol_handlers.py`, `background_loops.py`, `plugin_options.py`, `rpc_pool.py`, `log_writer.py`
- **Entry point:** `cl-hive.py`

## Per-Module Audit Process (for every task)

Each task follows this 5-step process:

### Step 1: Dead Code Identification
For every public method/function/class in the module:
```bash
# Check callers across cl-hive
rg "method_name" /home/sat/bin/cl-hive/ --type py -l | grep -v __pycache__ | grep -v .venv

# Check callers across cl_revenue_ops
rg "method_name" /home/sat/bin/cl_revenue_ops/ --type py -l | grep -v __pycache__ | grep -v .venv
```
- Mark functions with 0 external callers as removal candidates
- If the only caller is also dead, both are dead (transitive dead code)
- **Never remove**: `__init__`, `__repr__`, `to_dict` on dataclasses used in serialization, anything called via `getattr`/dynamic dispatch

### Step 2: Remove Dead Code
- Delete dead methods, classes, standalone functions
- Delete associated imports that become unused
- Delete dead constants/module-level variables

### Step 3: Correctness Review
For surviving methods:
- Off-by-one errors, wrong comparisons
- Missing error handling at boundaries
- Unreachable branches / dead `elif`/`else`
- Overly broad `try/except Exception` that swallows real bugs
- Thread-safety issues (shared mutable state without locks)

### Step 4: Efficiency Improvements
- Redundant computations (same value computed twice)
- Unnecessary copies (`.copy()` on read-only data)
- O(n²) where O(n) is trivial
- **No API changes** — same function signatures, same return types

### Step 5: Run Tests & Commit
```bash
cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q --deselect tests/test_anticipatory_nnlb_bugs.py::TestHiveBridgeKeyFix
```
All 2,328+ tests must pass. One commit per module:
```
audit: <module_name> — remove dead code, fix <brief description>
```

---

## Batch 1: Tier 1 Modules (3 tasks, ~22K lines)

### Task 1: Audit database.py (9,046 lines)

**Files:**
- Modify: `modules/database.py`
- Test: `tests/test_database_audit.py`, `tests/test_settlement_db_integrity.py`

**Context:**
Single `HiveDatabase` class with ~120 methods. Many table-specific CRUD methods may be dead after CLBoss removal and protocol changes. The 10 sacred RPC methods that cl-revenue-ops calls use specific database queries — those must survive.

**Step 1: Dead code scan**
- List every method in `HiveDatabase` class
- For each method, grep for callers in `modules/`, `cl-hive.py`, `tests/`, and `/home/sat/bin/cl_revenue_ops/`
- Pay special attention to methods related to removed features: CLBoss, deprecated settlement types, old protocol versions
- Check for dead table creation in `_init_tables()` — tables that no surviving code reads/writes

**Step 2: Remove dead methods**
- Delete methods with zero callers
- If removing a table-specific method leaves a table with zero remaining accessors, flag it (do NOT drop the table — data preservation)

**Step 3: Correctness review**
- Check all SQL queries for injection risk (should use parameterized queries)
- Verify WAL mode is set correctly
- Check connection handling / thread-safety

**Step 4: Efficiency**
- Look for N+1 query patterns
- Redundant SELECT before INSERT/UPDATE

**Step 5: Test and commit**
```bash
cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q --deselect tests/test_anticipatory_nnlb_bugs.py::TestHiveBridgeKeyFix
git add modules/database.py && git commit -m "audit: database.py — remove dead code, correctness fixes"
```

---

### Task 2: Audit protocol.py (7,324 lines)

**Files:**
- Modify: `modules/protocol.py`
- Test: `tests/test_protocol.py`, `tests/test_protocol_versioning.py`

**Context:**
Contains `HiveMessageType` enum, `HiveProtocol` class, message serialization/deserialization, and protocol version negotiation. After CLBoss removal and INTENT_ACK cleanup, more dead message types and serialization helpers likely remain.

**Step 1: Dead code scan**
- List all message types in `HiveMessageType` enum
- For each, grep for usage in `protocol_handlers.py`, `background_loops.py`, `cl-hive.py`, and all modules
- List all serialization/deserialization methods
- Check which protocol version negotiation paths are still reachable

**Step 2: Remove dead code**
- Remove unused message types from enum (preserve numeric gaps with comments for wire compatibility)
- Remove unused serialize/deserialize methods
- Remove dead protocol negotiation branches

**Step 3: Correctness review**
- Verify message type → handler mapping is complete (no unhandled types that should be handled)
- Check for integer overflow in message length fields
- Verify signature verification logic

**Step 4: Efficiency**
- Redundant serialization (serialize then immediately deserialize)
- Unnecessary copies of byte buffers

**Step 5: Test and commit**
```bash
cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q --deselect tests/test_anticipatory_nnlb_bugs.py::TestHiveBridgeKeyFix
git add modules/protocol.py && git commit -m "audit: protocol.py — remove dead message types and serialization helpers"
```

---

### Task 3: Audit rpc_commands.py (5,961 lines)

**Files:**
- Modify: `modules/rpc_commands.py`
- Test: `tests/test_rpc_commands_audit.py`, `tests/test_rpc.py`

**Context:**
Contains RPC command implementations called from `@plugin.method()` decorators in `cl-hive.py`. Each RPC command wraps a call to one or more module managers. Some commands may reference removed features (CLBoss, deprecated protocol operations). The 10 cl-revenue-ops RPC methods are sacred and must not change signatures.

**Sacred RPC methods (do NOT modify signatures):**
- `hive-fee-intel-query`, `hive-report-fee-observation`
- `hive-member-health`, `hive-report-health`
- `hive-liquidity-state`, `hive-report-liquidity-state`
- `hive-anticipatory-status`, `hive-rebalance-recommendations`
- `hive-channel-closed`, `hive-channel-opened`

**Step 1: Dead code scan**
- List every function in `rpc_commands.py`
- For each, grep for callers in `cl-hive.py` (the `@plugin.method()` registrations)
- Functions not referenced by any `@plugin.method()` or called by other live functions are dead
- Check for dead helper functions only called by dead RPC commands

**Step 2: Remove dead code**
- Delete unreachable RPC implementations and their helpers
- Delete unused imports

**Step 3: Correctness review**
- Verify all live RPC commands have proper error handling (return error dict, don't crash plugin)
- Check parameter validation on sacred methods
- Look for missing `try/except` around RPC calls to other plugins

**Step 4: Efficiency**
- RPC commands that fetch data they don't use
- Redundant manager lookups

**Step 5: Test and commit**
```bash
cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q --deselect tests/test_anticipatory_nnlb_bugs.py::TestHiveBridgeKeyFix
git add modules/rpc_commands.py && git commit -m "audit: rpc_commands.py — remove dead RPC wrappers and helpers"
```

---

## Batch 2: Tier 2 Modules (13 tasks, ~21K lines)

### Task 4: Audit fee_coordination.py (3,071 lines)

**Files:**
- Modify: `modules/fee_coordination.py`
- Test: `tests/test_fee_coordination.py`, `tests/test_fee_coordination_polish.py`, `tests/test_fee_coordination_10_fixes.py`

**Context:**
Contains 6 classes: FlowCorridorManager, AdaptiveFeeController, StigmergicCoordinator, MyceliumDefenseSystem, TimeBasedFeeAdjuster, FeeCoordinationManager. The main manager orchestrates the others. Look for dead coordinator classes or methods that were superseded.

**Audit:** Follow the standard 5-step process. Pay attention to:
- Are all 6 classes actually instantiated and used?
- Are `get_shareable_*` / `receive_*_from_fleet` methods all called by protocol handlers?
- Dead `elif` branches in fee calculation logic

---

### Task 5: Audit anticipatory_liquidity.py (2,789 lines)

**Files:**
- Modify: `modules/anticipatory_liquidity.py`
- Test: `tests/test_anticipatory_13_fixes.py`, `tests/test_anticipatory_nnlb_bugs.py`

**Context:**
Single `AnticipatoryLiquidityManager` class with 35+ methods for predictive liquidity positioning. NNLB (Nearest Neighbor Load Balancing) integration.

**Audit:** Follow standard 5-step process. Pay attention to:
- Methods related to removed CLBoss integration
- Dead prediction model variants
- Whether all fleet sharing methods have protocol handler callers

---

### Task 6: Audit settlement.py (2,699 lines)

**Files:**
- Modify: `modules/settlement.py`
- Test: `tests/test_settlement_8_fixes.py`, `tests/test_extended_settlements.py`, `tests/test_settlement_db_integrity.py`, `tests/test_distributed_settlement.py`, `tests/test_routing_settlement_bugfixes.py`

**Context:**
Contains SettlementManager, NettingEngine, BondManager, DisputeResolver + 9 handler classes. Complex multi-party settlement logic.

**Audit:** Follow standard 5-step process. Pay attention to:
- Are all 9 handler classes reachable from the SettlementManager dispatch logic?
- Dead settlement types/states
- Bond lifecycle completeness (create → lock → release/slash)

---

### Task 7: Audit planner.py (2,570 lines)

**Files:**
- Modify: `modules/planner.py`
- Test: `tests/test_planner.py`, `tests/test_planner_simulation.py`, `tests/test_state_planner_bugs.py`

**Context:**
Contains ChannelSizer and Planner classes. Already cleaned up during CLBoss removal (removed clboss_bridge parameter). Check for remaining dead code.

**Audit:** Follow standard 5-step process. Pay attention to:
- Dead channel sizing heuristics from removed feature flags
- Methods that reference removed CLBoss saturation data
- Planner methods only called by dead background loops

---

### Task 8: Audit strategic_positioning.py (2,329 lines)

**Files:**
- Modify: `modules/strategic_positioning.py`
- Test: `tests/test_strategic_positioning.py`

**Context:**
Contains RouteValueAnalyzer, FleetPositioningStrategy, PhysarumChannelManager, StrategicPositioningManager. Physarum (slime mold) network optimization.

**Audit:** Follow standard 5-step process. Pay attention to:
- Is the PhysarumChannelManager actually used or was it experimental?
- Dead route scoring heuristics
- Fleet sharing methods with no protocol handler callers

---

### Task 9: Audit cost_reduction.py (2,192 lines)

**Files:**
- Modify: `modules/cost_reduction.py`
- Test: `tests/test_cost_reduction.py`

**Context:**
Contains PredictiveRebalancer, FleetRebalanceRouter, CircularFlowDetector, CostReductionManager. Fleet-wide cost optimization.

**Audit:** Follow standard 5-step process. Pay attention to:
- Dead prediction model variants
- CircularFlowDetector — is it actually triggered by any code path?
- Methods that reference removed CLBoss rebalancing data

---

### Task 10: Audit liquidity_coordinator.py (1,922 lines)

**Files:**
- Modify: `modules/liquidity_coordinator.py`
- Test: `tests/test_liquidity_coordinator.py`

**Context:**
Single `LiquidityCoordinator` class with 45+ methods. NNLB priority-based liquidity allocation.

**Audit:** Follow standard 5-step process. Pay attention to:
- Dead allocation strategies
- Methods superseded by MCF solver
- Fleet coordination methods with no callers

---

### Task 11: Audit mcf_solver.py (1,699 lines)

**Files:**
- Modify: `modules/mcf_solver.py`
- Test: `tests/test_mcf_solver.py`, `tests/test_intent_mcf_bugs.py`

**Context:**
Contains MCFCircuitBreaker, MCFHealthMetrics, MCFNetwork, SSPSolver, MCFNetworkBuilder, MCFCoordinator. Multi-commodity flow optimization.

**Audit:** Follow standard 5-step process. Pay attention to:
- Dead solver variants or fallback algorithms
- MCFCircuitBreaker — is it used or was it experimental?
- Dead debug/diagnostic methods

---

### Task 12: Audit channel_rationalization.py (1,300 lines)

**Files:**
- Modify: `modules/channel_rationalization.py`
- Test: `tests/test_channel_rationalization.py`

**Context:**
Contains RedundancyAnalyzer, ChannelRationalizer, RationalizationManager. Identifies and recommends closing redundant channels.

**Audit:** Follow standard 5-step process. Pay attention to:
- Dead scoring heuristics
- Methods referencing removed CLBoss data
- Rationalization rules that are never triggered

---

### Task 13: Audit fee_intelligence.py (1,200 lines)

**Files:**
- Modify: `modules/fee_intelligence.py`
- Test: `tests/test_fee_intelligence.py`

**Context:**
Single `FeeIntelligenceManager` class. Fee observation collection and analysis for fleet coordination. Called by sacred RPC method `hive-fee-intel-query`.

**Audit:** Follow standard 5-step process. Pay attention to:
- Dead analysis methods not called by any RPC or background loop
- Ensure `hive-fee-intel-query` and `hive-report-fee-observation` code paths are intact
- Fleet sharing methods with no protocol handler callers

---

### Task 14: Audit cooperative_expansion.py (1,224 lines)

**Files:**
- Modify: `modules/cooperative_expansion.py`
- Test: `tests/test_cooperative_expansion.py`

**Context:**
Single `CooperativeExpansionManager` class. Coordinated channel opening across fleet.

**Audit:** Follow standard 5-step process. Pay attention to:
- Dead expansion strategies
- Methods referencing removed CLBoss channel recommendations
- Fleet coordination methods with no callers

---

### Task 15: Audit routing_intelligence.py (1,034 lines)

**Files:**
- Modify: `modules/routing_intelligence.py`
- Test: `tests/test_routing_intelligence.py`, `tests/test_routing_intelligence_10_fixes.py`

**Context:**
Single `HiveRoutingMap` class with `score_route` and `score_fallback` inner functions. Fleet-aware routing optimization.

**Audit:** Follow standard 5-step process. Pay attention to:
- Dead scoring functions
- Inner functions that are never called
- Fleet data methods with no protocol handler callers

---

### Task 16: Audit yield_metrics.py (1,003 lines)

**Files:**
- Modify: `modules/yield_metrics.py`
- Test: `tests/test_yield_metrics.py`

**Context:**
Single `YieldMetricsManager` class with ~20 methods. Yield calculation and reporting.

**Audit:** Follow standard 5-step process. Pay attention to:
- Dead metric calculations not used by any RPC or background loop
- Metrics referencing removed features

---

## Batch 3: Tier 3 Modules (5 tasks, ~12K lines)

### Task 17: Audit splice_manager.py, state_manager.py, bridge.py (2,908 lines)

**Files:**
- Modify: `modules/splice_manager.py` (1,081 lines), `modules/state_manager.py` (924 lines), `modules/bridge.py` (903 lines)
- Test: `tests/test_splice_manager.py`, `tests/test_splice_bugs.py`, `tests/test_state.py`, `tests/test_bridge.py`

**Context:**
- `splice_manager.py`: Splice operation management. Recently cleaned during CLBoss removal.
- `state_manager.py`: Hive state persistence and recovery.
- `bridge.py`: cl-revenue-ops integration bridge. Recently cleaned (CLBoss removal, internal flag fix).

**Audit:** Follow standard 5-step process for each module. Pay attention to:
- splice_manager: Dead splice types, unused lifecycle methods
- state_manager: Dead state keys, unused recovery methods
- bridge: Already cleaned — focus on correctness review of surviving methods

---

### Task 18: Audit network_metrics.py, routing_pool.py, vpn_transport.py (2,526 lines)

**Files:**
- Modify: `modules/network_metrics.py` (891 lines), `modules/routing_pool.py` (876 lines), `modules/vpn_transport.py` (759 lines)
- Test: `tests/test_network_metrics.py`, `tests/test_routing_pool.py`, `tests/test_vpn_transport.py`

**Context:**
- `network_metrics.py`: Network topology metrics and analysis.
- `routing_pool.py`: Route caching and pool management.
- `vpn_transport.py`: VPN tunnel management for cross-network fleet communication.

**Audit:** Follow standard 5-step process for each module. Pay attention to:
- network_metrics: Dead metric types, unused aggregation methods
- routing_pool: Dead cache strategies, unused eviction logic
- vpn_transport: Dead VPN protocol handlers, unused tunnel types

---

### Task 19: Audit membership.py, task_manager.py, intent_manager.py, peer_reputation.py, quality_scorer.py (3,409 lines)

**Files:**
- Modify: `modules/membership.py` (751 lines), `modules/task_manager.py` (724 lines), `modules/intent_manager.py` (709 lines), `modules/peer_reputation.py` (617 lines), `modules/quality_scorer.py` (608 lines)
- Test: `tests/test_membership.py`, `tests/test_intent.py`, `tests/test_peer_reputation.py`

**Context:**
- `membership.py`: Hive member tracking and lifecycle.
- `task_manager.py`: Distributed task assignment and tracking.
- `intent_manager.py`: Intent-based liquidity request system.
- `peer_reputation.py`: Peer scoring based on behavior.
- `quality_scorer.py`: Channel quality scoring.

**Audit:** Follow standard 5-step process for each module. Pay attention to:
- membership: Dead member states, unused lifecycle transitions
- task_manager: Dead task types, unused scheduling logic
- intent_manager: Dead intent types, unused matching logic
- peer_reputation: Dead reputation signals, unused decay logic
- quality_scorer: Dead scoring dimensions, unused aggregation

---

### Task 20: Audit gossip.py, handshake.py, budget_manager.py, relay.py, governance.py, splice_coordinator.py, config.py, outbox.py, contribution.py, health_aggregator.py, idempotency.py, phase6_ingest.py (5,521 lines)

**Files:**
- Modify: `modules/gossip.py` (609), `modules/handshake.py` (598), `modules/budget_manager.py` (503), `modules/relay.py` (449), `modules/governance.py` (424), `modules/splice_coordinator.py` (411), `modules/config.py` (306), `modules/outbox.py` (286), `modules/contribution.py` (250), `modules/health_aggregator.py` (387), `modules/idempotency.py` (108), `modules/phase6_ingest.py` (112)
- Test: `tests/test_gossip.py`, `tests/test_budget_manager.py`, `tests/test_relay.py`, `tests/test_governance.py`, `tests/test_config_governance_alias.py`, `tests/test_outbox.py`, `tests/test_outbox_7_fixes.py`, `tests/test_health_aggregator.py`, `tests/test_idempotency.py`, `tests/test_phase6_ingest.py`

**Context:**
12 small modules (100-600 lines each). These are mostly self-contained utilities and coordinators.

**Audit:** Follow standard 5-step process for each module. Due to small size, focus primarily on dead code removal — correctness issues in <300-line modules are rare. Pay attention to:
- gossip: Dead gossip message types
- handshake: Dead handshake protocol versions
- budget_manager: Dead budget categories
- relay: Dead relay modes
- governance: Dead voting mechanisms
- splice_coordinator: Dead coordination states
- config: Dead config keys
- outbox: Dead message types
- contribution: Dead contribution types
- health_aggregator: Dead health signals
- idempotency: Likely clean (108 lines) — quick scan
- phase6_ingest: Likely clean (112 lines) — quick scan

---

## Cross-Dependency Safety Checklist

After ALL tasks complete, verify these 10 cl-revenue-ops RPC methods still work:

| RPC Method | Primary Module(s) |
|------------|--------------------|
| `hive-fee-intel-query` | fee_intelligence.py, rpc_commands.py |
| `hive-report-fee-observation` | fee_intelligence.py, rpc_commands.py |
| `hive-member-health` | health_aggregator.py, rpc_commands.py |
| `hive-report-health` | health_aggregator.py, rpc_commands.py |
| `hive-liquidity-state` | liquidity_coordinator.py, rpc_commands.py |
| `hive-report-liquidity-state` | liquidity_coordinator.py, rpc_commands.py |
| `hive-anticipatory-status` | anticipatory_liquidity.py, rpc_commands.py |
| `hive-rebalance-recommendations` | cost_reduction.py, rpc_commands.py |
| `hive-channel-closed` | rpc_commands.py |
| `hive-channel-opened` | rpc_commands.py |

---

## Success Criteria

- All 2,328+ tests pass after every commit
- Zero behavioral changes to live code paths
- Zero API changes to methods with external callers
- Estimated 3,000-6,000 lines removed across all modules
- 20 commits, one per task (some tasks cover multiple small modules)
