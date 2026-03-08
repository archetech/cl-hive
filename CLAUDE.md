# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 📖 **For fleet monitoring**: See [MOLTY.md](MOLTY.md) for AI agent instructions on using cl-hive's MCP tools to monitor and manage Lightning node fleets.

## Project Overview

cl-hive is a Core Lightning plugin implementing distributed "Swarm Intelligence" for Lightning node fleets. It coordinates multiple nodes through PKI authentication, shared state gossip, and distributed governance. Designed to work alongside [cl-revenue-ops](https://github.com/lightning-goats/cl_revenue_ops) which handles local fee/rebalancing decisions.

## Commands

```bash
# Run all tests
python3 -m pytest tests/

# Run specific test file
python3 -m pytest tests/test_planner.py

# Run with verbose output
python3 -m pytest tests/ -v

# Run tests matching a pattern
python3 -m pytest tests/ -k "test_feerate"
```

No build system - this is a CLN plugin deployed by copying `cl-hive.py` and `modules/` to the plugin directory.

## Architecture

```
cl-hive (Coordination Layer - "The Diplomat")
    ↓
cl-revenue-ops (Execution Layer - "The CFO")
    ↓
Core Lightning
```

### Three-Layer Design
- **cl-hive**: Manages fleet topology, membership, and consensus decisions
- **cl-revenue-ops**: Executes fee policies and rebalancing (called via RPC)
- **Core Lightning**: Underlying node operations and HSM-based crypto

### Module Organization (41 modules)

| Module | Purpose |
|--------|---------|
| `protocol.py` | BOLT 8 custom messages (magic: "HIVE" = 0x48495645, types 32769-32845) |
| `handshake.py` | PKI auth using CLN signmessage/checkmessage |
| `state_manager.py` | HiveMap distributed state + anti-entropy sync |
| `gossip.py` | Threshold-based gossip (10% capacity change) with 5-min heartbeat |
| `intent_manager.py` | Intent Lock protocol - Announce-Wait-Commit with lexicographic tie-breaker |
| `bridge.py` | Circuit Breaker pattern for cl-revenue-ops integration |
| `membership.py` | Three-tier system: Admin → Member → Neophyte with vouch-based promotion |
| `contribution.py` | Forwarding stats and anti-leech detection |
| `planner.py` | Topology optimization - saturation analysis, expansion election, feerate gate |
| `splice_manager.py` | Coordinated splice operations between hive members (Phase 11) |
| `splice_coordinator.py` | High-level splice coordination and recommendation engine |
| `mcf_solver.py` | Min-Cost Max-Flow solver for global fleet rebalance optimization |
| `liquidity_coordinator.py` | Liquidity needs aggregation and rebalance assignment distribution |
| `cost_reduction.py` | Fleet rebalance routing with MCF/BFS fallback |
| `anticipatory_liquidity.py` | Kalman-filtered flow prediction, intra-day pattern detection |
| `fee_coordination.py` | Pheromone-based fee coordination + stigmergic markers |
| `fee_intelligence.py` | Fee intelligence aggregation and sharing across fleet |
| `cooperative_expansion.py` | Fleet-wide expansion election protocol (Nominate→Elect→Open) |
| `budget_manager.py` | Autonomous/failsafe mode budget tracking and enforcement |
| `idempotency.py` | Message deduplication via event ID tracking |
| `outbox.py` | Reliable message delivery with retry and exponential backoff |
| `routing_intelligence.py` | Routing path intelligence sharing across fleet |
| `routing_pool.py` | Routing pool management for fee distribution |
| `settlement.py` | BOLT12 settlement system - proposal/vote/execute consensus |
| `health_aggregator.py` | Fleet health scoring and NNLB status |
| `network_metrics.py` | Network-level metrics collection |
| `peer_reputation.py` | Peer reputation tracking and scoring |
| `quality_scorer.py` | Peer quality scoring for membership decisions |
| `relay.py` | Message relay logic for multi-hop fleet communication |
| `rpc_commands.py` | RPC command handlers for all hive-* commands |
| `channel_rationalization.py` | Channel optimization recommendations |
| `strategic_positioning.py` | Strategic network positioning analysis |
| `task_manager.py` | Background task coordination and scheduling |
| `vpn_transport.py` | VPN transport layer (WireGuard integration) |
| `yield_metrics.py` | Yield tracking and optimization metrics |
| `governance.py` | Decision engine (advisor/failsafe mode routing) |
| `config.py` | Hot-reloadable configuration with snapshot pattern |
| `did_credentials.py` | DID credential issuance, verification, reputation aggregation (Phase 16) |
| `management_schemas.py` | 15 management schema categories, danger scoring, credential lifecycle (Phase 2) |
| `database.py` | SQLite with WAL mode, thread-local connections, 50 tables |

### Key Patterns

**Thread Safety**:
- `RPC_LOCK` with 10-second timeout serializes all RPC calls
- `ThreadSafeRpcProxy` wraps the plugin.rpc object
- Thread-local SQLite connections with WAL mode

**Graceful Shutdown**:
- `shutdown_event` checked in all background loops
- Use `shutdown_event.wait(interval)` not `time.sleep()`

**Circuit Breaker** (in bridge.py):
- States: CLOSED → OPEN (after 3 failures) → HALF_OPEN (after 60s)
- All external plugin calls go through `safe_call()` wrapper

**Configuration Snapshot**:
- Use `config.snapshot()` at cycle start
- Never read mutable config mid-cycle

**Message Protocol**:
- 4-byte magic prefix filters non-Hive messages immediately
- "Peek & Check" pattern in custommsg hook
- JSON payload, max 65535 bytes per message

**Idempotent Delivery**:
- All protocol messages carry unique event IDs
- `proto_events` table tracks processed events
- `proto_outbox` table enables reliable retry with exponential backoff

**Relay Protocol**:
- Multi-hop message relay for peers not directly connected
- Relay logic in `relay.py` with TTL-based loop prevention

### Governance Modes

| Mode | Behavior |
|------|----------|
| `advisor` | **Primary mode** - Queue to pending_actions for AI/human approval via MCP server |
| `failsafe` | Emergency mode - Auto-execute only critical safety actions (bans) within strict limits |

### Database Tables (50 tables)

Key tables (see `database.py` for complete schema):

| Table | Purpose |
|-------|---------|
| `hive_members` | Member roster with tiers and stats |
| `intent_locks` | Active intent locks for conflict resolution |
| `hive_state` | Key-value store for persistent state |
| `contribution_ledger` | Forwarding contribution tracking |
| `hive_bans` | Ban proposals and votes |
| `ban_proposals` / `ban_votes` | Distributed ban voting |
| `promotion_requests` / `promotion_vouches` | Promotion workflow |
| `hive_planner_log` | Planner decision audit log |
| `pending_actions` | Actions awaiting approval (advisor mode) |
| `splice_sessions` | Active and historical splice operations |
| `peer_fee_profiles` | Fee profiles shared by fleet members |
| `fee_intelligence` | Aggregated fee intelligence data |
| `fee_reports` | Fee earnings for settlement calculations |
| `liquidity_needs` / `member_liquidity_state` | Liquidity coordination |
| `pool_contributions` / `pool_revenue` / `pool_distributions` | Routing pool management |
| `settlement_proposals` / `settlement_ready_votes` / `settlement_executions` | BOLT12 settlement |
| `flow_samples` / `temporal_patterns` | Anticipatory liquidity data |
| `peer_reputation` | Peer reputation scores |
| `member_health` | Fleet member health tracking |
| `budget_tracking` / `budget_holds` | Budget enforcement |
| `proto_events` | Processed event IDs for idempotency |
| `proto_outbox` | Reliable message delivery outbox |
| `peer_presence` | Peer online/offline tracking |
| `peer_capabilities` | Peer protocol capabilities |
| `did_credentials` | DID reputation credentials (issued and received) |
| `did_reputation_cache` | Cached aggregated reputation scores |
| `management_credentials` | Management credentials (operator → agent permission) |
| `management_receipts` | Signed receipts of management action executions |

## Safety Constraints

These are non-negotiable:

1. **Fail closed**: On invalid input, RPC errors, schema mismatches → do nothing, log
2. **Bound everything**: Message sizes, list sizes, DB growth, caches, loop runtime
3. **No silent fund actions**: Never move funds unless governance mode explicitly allows
4. **Identity binding**: Sender peer_id must match claimed pubkey in payload
5. **DoS protection**: Max 200 remote intents cached, rate limits on all loops
6. **Hive channels always zero fees**: Channels between hive fleet members MUST have 0 ppm fees (both base and proportional). Never apply static policies to hive channels.

## Planner Rules

The Planner proposes topology changes but cannot open channels directly:
- May log decisions to `hive_planner_log`
- May create `pending_actions` entries in advisor mode
- May broadcast INTENT messages when governance mode allows
- Max 5 ignores per cycle
- 20% market share cap per target

### Feerate Gate
- Expansions blocked when on-chain feerate > `hive-max-expansion-feerate` (default: 5000 sat/kB)
- Set to 0 to disable feerate checking
- Uses CLN `feerates` RPC to get current opening feerate

### Cooperative Expansion (Phase 6.4)
- Fleet-wide election for expansion targets
- Nomination → Election → Winner opens channel
- Prevents thundering herd via Intent Lock Protocol

## Optional Integrations

### Sling (Optional for cl-hive)
Sling rebalancer is optional for cl-hive. cl-revenue-ops handles rebalancing coordination.
Note: Sling IS required for cl-revenue-ops itself.

## Development Notes

- Only external dependency: `pyln-client>=24.0`
- All crypto done via CLN HSM (signmessage/checkmessage) - no crypto libs imported
- Plugin options defined at top of `cl-hive.py` (30 configurable parameters)
- Background loops (9): gossip_loop, membership_maintenance_loop, planner_loop, intent_monitor_loop, fee_intelligence_loop, settlement_loop, mcf_optimization_loop, outbox_retry_loop, did_maintenance_loop

## Testing Conventions

- Test files in `tests/` directory
- Use pytest fixtures for mocking (see `conftest.py`)
- Mock RPC calls, never hit real network
- Test categories: unit, integration, feerate, planner, membership

## File Structure

```
cl-hive/
├── cl-hive.py              # Main plugin entry point
├── modules/                # 41 modules
│   ├── protocol.py         # Message types and encoding
│   ├── handshake.py        # PKI authentication
│   ├── state_manager.py    # Distributed state (HiveMap)
│   ├── gossip.py           # Gossip protocol
│   ├── intent_manager.py   # Intent locks
│   ├── bridge.py           # cl-revenue-ops bridge (Circuit Breaker)
│   ├── membership.py       # Member management
│   ├── contribution.py     # Contribution tracking
│   ├── planner.py          # Topology planner
│   ├── cooperative_expansion.py  # Fleet expansion elections
│   ├── splice_manager.py   # Coordinated splice operations
│   ├── splice_coordinator.py    # Splice coordination engine
│   ├── mcf_solver.py       # Min-Cost Max-Flow solver
│   ├── liquidity_coordinator.py # Liquidity needs aggregation
│   ├── cost_reduction.py   # Fleet rebalance routing
│   ├── anticipatory_liquidity.py # Kalman-filtered flow prediction
│   ├── fee_coordination.py # Pheromone-based fee coordination
│   ├── fee_intelligence.py # Fee intelligence sharing
│   ├── settlement.py       # BOLT12 settlement system
│   ├── routing_intelligence.py  # Routing path intelligence
│   ├── routing_pool.py     # Routing pool management
│   ├── budget_manager.py   # Budget tracking and enforcement
│   ├── idempotency.py      # Message deduplication
│   ├── outbox.py           # Reliable message delivery
│   ├── relay.py            # Message relay logic
│   ├── health_aggregator.py    # Fleet health scoring
│   ├── network_metrics.py  # Network metrics collection
│   ├── peer_reputation.py  # Peer reputation tracking
│   ├── quality_scorer.py   # Peer quality scoring
│   ├── channel_rationalization.py # Channel optimization
│   ├── strategic_positioning.py   # Network positioning
│   ├── yield_metrics.py    # Yield tracking
│   ├── task_manager.py     # Background task coordination
│   ├── vpn_transport.py    # VPN transport layer
│   ├── rpc_commands.py     # RPC command handlers
│   ├── governance.py       # Decision engine (advisor/failsafe)
│   ├── did_credentials.py  # DID credential issuance + reputation (Phase 16)
│   ├── management_schemas.py # Management schemas + danger scoring (Phase 2)
│   ├── config.py           # Configuration
│   └── database.py         # Database layer (50 tables)
├── tools/
│   ├── mcp-hive-server.py  # MCP server for Claude Code integration
│   ├── hive-monitor.py     # Real-time monitoring daemon
│   └── ai_advisor.py       # AI advisor utility
├── config/
│   ├── nodes.rest.example.json    # REST API config example
│   └── nodes.docker.example.json  # Docker/Polar config example
├── tests/                  # 1,918 tests across 48 files
├── docs/                   # Documentation
│   ├── design/             # Design documents
│   ├── planning/           # Implementation plans
│   ├── security/           # Security docs
│   ├── specs/              # Specifications
│   └── testing/            # Testing guides & scripts
├── audits/                 # Security audits
└── docker/                 # Docker deployment
```
