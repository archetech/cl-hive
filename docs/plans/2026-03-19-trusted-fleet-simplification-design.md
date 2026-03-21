# Trusted Fleet Simplification — Design

**Date:** 2026-03-19
**Branch:** TBD (new branch from main)
**Status:** Approved

## Goal

Reduce cl-hive from a ~130k LOC protocol platform to a ~25-30k LOC trusted fleet coordination layer. Delete all code that does not directly serve: sharing observations, generating recommendations, coordinating a small trusted fleet, and handing off execution to local tools.

## Product Description

"cl-hive is a lean, safe, trusted fleet coordination layer for small fleets of Core Lightning nodes. It shares observations and recommendations across trusted members to reduce internal competition, improve coordination, and support fleet health, while leaving local execution to local node-side tools."

## Design Principles

1. **Trusted fleet only** — assume one operator or small trusted group
2. **Coordination, not execution** — publish observations and recommendations; don't own fee/rebalance/splice execution
3. **Read-mostly shared layer** — collect, normalize, share facts + lightweight recommendations
4. **Lean and safe by default** — boring, minimal, hard to misuse
5. **Delete rather than preserve** — no dead code, feature flags, or "maybe later" scaffolding

## Architecture After Simplification

```
cl-hive (Trusted Fleet Coordination Layer)
├── Membership    — Simple admin add/remove, PKI handshake
├── Shared State  — HiveMap, gossip, state sync
├── Observations  — Fee intel, traffic intel, liquidity awareness,
│                   peer quality, health, yield metrics
├── Recommendations — Corridor ownership, expansion targets,
│                     channel sizing, fee suggestions, rationalization
├── Bridge        — Integration with cl-revenue-ops
├── Persistence   — Minimal database (~20 tables)
└── Audit         — Operator-visible logging
```

## Module Inventory

### REMOVE — 18 modules (~14,500 LOC)

| Module | Lines | Reason |
|--------|-------|--------|
| settlement.py | 2,745 | Fee redistribution/settlement system |
| cashu_escrow.py | 937 | Cashu escrow/conditional payments |
| did_credentials.py | 1,500 | DID/verifiable credentials stack |
| management_schemas.py | 1,368 | Capability-based auth/risk scoring |
| marketplace.py | 369 | Advisor marketplace/contracts |
| liquidity_marketplace.py | 352 | Liquidity lease marketplace |
| routing_pool.py | 857 | Fee revenue pooling/redistribution |
| nostr_transport.py | 202 | External comms transport |
| identity_adapter.py | 91 | Archon identity delegation |
| vpn_transport.py | 724 | VPN transport layer |
| splice_manager.py | 1,078 | Splice coordination protocol |
| splice_coordinator.py | 398 | Splice safety checks |
| task_manager.py | 696 | Distributed task delegation |
| cooperative_expansion.py | 1,224 | Election-based expansion voting |
| mcf_solver.py | 1,702 | Min-cost-flow optimization engine |
| rpc_pool.py | 462 | Thread pool for RPC (MCP/multi-node) |
| cost_reduction.py | 2,198 | Multi-strategy rebalance routing |
| budget_manager.py | 464 | Budget hold/reserve management |

### SIMPLIFY — 13 modules + main plugin (currently ~53,000 LOC → target ~15,000)

| Module | Lines | Simplification |
|--------|-------|---------------|
| protocol.py | 7,136 | Remove ~60% of message types |
| protocol_handlers.py | 8,406 | Remove handlers for removed message types |
| rpc_commands.py | 6,117 | Remove ~150 of 235 RPC commands |
| database.py | 8,573 | Remove ~30 of 50 tables |
| background_loops.py | 2,949 | Remove loops for deleted features |
| membership.py | 715 | Simple admin add/remove, drop promotion pipeline |
| governance.py | 425 | Simple admin controls, drop advisor/failsafe modes |
| fee_coordination.py | 3,185 | Keep corridor + competition avoidance only |
| strategic_positioning.py | 2,320 | Keep corridor analysis, drop Physarum model |
| planner.py | 2,576 | Recommendation-only, no direct execution |
| anticipatory_liquidity.py | 2,808 | Keep pattern detection + sharing, drop prediction engine |
| liquidity_coordinator.py | 1,909 | Keep awareness/NNLB, drop MCF integration |
| plugin_options.py | 452 | Remove options for deleted features |
| cl-hive.py (main) | 9,578 | Remove wiring for deleted features |

### KEEP — 19 modules (~8,500 LOC)

| Module | Lines | Purpose |
|--------|-------|---------|
| config.py | 302 | Hot-reloadable configuration |
| bridge.py | 922 | cl-revenue-ops integration, circuit breaker |
| gossip.py | 552 | State broadcasting |
| state_manager.py | 873 | Fleet state cache |
| handshake.py | 598 | PKI authentication |
| intent_manager.py | 696 | Coordination intents |
| idempotency.py | 111 | Message deduplication |
| outbox.py | 287 | Reliable message delivery |
| relay.py | 441 | Multi-hop relay |
| health_aggregator.py | 388 | Fleet health awareness |
| quality_scorer.py | 609 | Peer quality evaluation |
| fee_intelligence.py | 1,164 | Fee intelligence sharing |
| traffic_intelligence.py | 582 | Traffic pattern sharing |
| network_metrics.py | 888 | Fleet topology analysis |
| peer_reputation.py | 517 | Peer quality tracking |
| contribution.py | 251 | Forwarding contribution tracking |
| yield_metrics.py | 982 | Channel profitability metrics |
| channel_rationalization.py | 1,212 | Channel recommendations |
| log_writer.py | 92 | Log batching |

## Non-Module Deletions

### tools/ — REMOVE ALL (17 files)
MCP server, AI advisor, simulation, monitoring infrastructure.

### production/ — REMOVE ENTIRELY
AI advisor deployment infrastructure.

### scripts/ — REMOVE ALL (4 files)
Phase 6, MCP rune, DID signing scripts.

### config/
- REMOVE: mcp-config.example.json, strategy-prompts.example/
- KEEP: nodes.*.example.json (simplified)

### docs/
- REMOVE: MCP_SERVER.md, all settlement/DID/governance planning docs
- REWRITE: JOINING_THE_HIVE.md, README, CLAUDE.md

### Top-level
- REMOVE: MOLTY.md, GEMINI.md, .mcp.json, manifest.json
- REWRITE: CLAUDE.md, README.md, cl-hive.conf.sample, CHANGELOG.md
- SIMPLIFY: requirements.txt (remove websockets/coincurve)

### docker/
- REMOVE: Archon install script, Phase 6 references
- SIMPLIFY: README.md, entrypoint (remove Phase 6 plugin references)

### Tests
- REMOVE: ~600 tests for deleted features (MCP, settlement, DID, cashu, marketplace, management schemas)
- KEEP: ~1,850 tests (fix broken references)

## Risky Dependencies

These are places where removed features are tangled into core code:

1. **protocol.py + protocol_handlers.py** — Message types for settlement, DID, MCF, expansion election, splice, bonds, disputes are interspersed with core types. Need careful surgical removal.

2. **background_loops.py** — Single 2,949-line file orchestrates ALL background work. Loops for removed features reference removed modules. Each loop removal must be verified for side effects.

3. **database.py** — 50 tables. ~30 serve removed features. Table creation is in a single method. Some core queries may JOIN to removed tables.

4. **rpc_commands.py** — 235 commands in one file. Commands for removed features may share fixtures/context with kept commands.

5. **cl-hive.py** — Main plugin wires everything. Thread startup, hook registration, and initialization code references all modules. Import removal must be systematic.

6. **governance.py** — Currently gates all actions. Removing advisor/failsafe modes requires ensuring kept features (planner recommendations, intent coordination) don't depend on governance approval flow.

7. **planner.py** — Currently uses cooperative_expansion, mcf_solver, and budget_manager. After removal, planner needs to generate recommendations without these dependencies.

## Membership Simplification

Replace current 4-tier system (neophyte → initiate → full member → elder) with:

- **admin** — can add/remove members, configure fleet
- **member** — participates in gossip, receives/sends observations

No promotion pipeline, vouch graph, or quorum calculations. Admin adds members explicitly via RPC command.

## Governance Simplification

Replace advisor/failsafe decision engine with:

- Recommendations are always generated and logged
- Bridge pushes recommendations to cl-revenue-ops if enabled
- No action queue, no AI approval, no budget gating
- Admin can enable/disable hive coordination per-node via config

## Estimated Outcome

- ~130k LOC → ~25-30k LOC (75-80% reduction)
- ~53 modules → ~32 modules
- ~235 RPC commands → ~50-80 RPC commands
- ~50 DB tables → ~20 DB tables
- ~2,450 tests → ~1,850 tests (then fix broken refs)
- 3 external deps → 1 (pyln-client only)

## Constraints

- No new features
- Prefer deletion over abstraction
- No compatibility layers for deleted features
- No unused DB fields, API routes, event types, or schemas
- Minimize dependencies
