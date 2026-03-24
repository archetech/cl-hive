# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

cl-hive is a Core Lightning plugin that provides trusted fleet coordination for small groups of Lightning nodes. It shares membership state, fee intelligence, liquidity observations, and topology recommendations across fleet members so each node can make better local decisions. Designed to work alongside [cl-revenue-ops](https://github.com/lightning-goats/cl_revenue_ops) which handles local fee/rebalancing execution.

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

No build system -- this is a CLN plugin deployed by copying `cl-hive.py` and `modules/` to the plugin directory.

## Architecture

```
cl-hive (Coordination Layer)
    |
cl-revenue-ops (Execution Layer)
    |
Core Lightning
```

### Data Flow

```
Membership --> State Sync --> Observations --> Recommendations --> Bridge (to cl-revenue-ops)
```

1. **Membership**: Peer opens channel + sends HELLO; existing member approves via `hive-approve`; handshake authenticates via CLN signmessage/checkmessage
2. **State Sync**: Gossip protocol shares capacity, fees, health, and liquidity across the fleet
3. **Observations**: Each node collects fee intelligence, traffic profiles, flow patterns, peer quality
4. **Recommendations**: Planner generates topology, fee, rebalancing, and positioning recommendations
5. **Bridge**: Coordinated signals are pushed to cl-revenue-ops for local execution

### Module Organization (31 modules)

| Module | Purpose |
|--------|---------|
| `protocol.py` | BOLT 8 custom messages (magic: "HIVE" = 0x48495645) |
| `protocol_handlers.py` | Dispatch handlers for each message type |
| `handshake.py` | PKI auth using CLN signmessage/checkmessage |
| `state_manager.py` | HiveMap distributed state + anti-entropy sync |
| `gossip.py` | Threshold-based gossip (10% capacity change) with heartbeat |
| `intent_manager.py` | Intent Lock protocol -- Announce-Wait-Commit with tie-breaker |
| `bridge.py` | Read-only cl-revenue-ops queries (fee config, profitability, Boltz activity) |
| `membership.py` | Single-role membership (all members equal) |
| `governance.py` | Recommendation logging (no modes, no budget gating) |
| `contribution.py` | Forwarding stats and anti-leech detection |
| `planner.py` | Topology optimization -- saturation analysis, expansion targeting |
| `fee_coordination.py` | Flow corridor management and competition avoidance |
| `fee_intelligence.py` | Fee intelligence aggregation and sharing across fleet |
| `liquidity_coordinator.py` | Liquidity needs aggregation and rebalance assignment |
| `traffic_intelligence.py` | Traffic profile sharing and demand forecasting |
| `health_aggregator.py` | Fleet health scoring and NNLB status |
| `network_metrics.py` | Network-level metrics collection |
| `peer_reputation.py` | Peer reputation tracking and scoring |
| `quality_scorer.py` | Peer quality scoring for membership decisions |
| `channel_rationalization.py` | Channel optimization recommendations |
| `strategic_positioning.py` | Strategic network positioning analysis |
| `yield_metrics.py` | Yield tracking and optimization metrics |
| `idempotency.py` | Message deduplication via event ID tracking |
| `outbox.py` | Reliable message delivery with retry and exponential backoff |
| `relay.py` | Message relay logic for multi-hop fleet communication |
| `log_writer.py` | Structured logging |
| `rpc_commands.py` | RPC command handler implementations |
| `background_loops.py` | Background loop definitions (gossip, planner, fee intel, etc.) |
| `plugin_options.py` | Plugin option registration and rate limiting |
| `config.py` | Hot-reloadable configuration with snapshot pattern |
| `database.py` | SQLite with WAL mode, thread-local connections, 28 tables |

### Key Patterns

**Thread Safety**:
- Thread-local SQLite connections with WAL mode

**Graceful Shutdown**:
- `shutdown_event` checked in all background loops
- Use `shutdown_event.wait(interval)` not `time.sleep()`

**Circuit Breaker** (in bridge.py):
- States: CLOSED -> OPEN (after 3 failures) -> HALF_OPEN (after 60s)
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

### Background Loops (6)

| Loop | Purpose |
|------|---------|
| `gossip_loop` | Heartbeat broadcast and state sync |
| `membership_maintenance_loop` | Pending request expiry, contribution tracking |
| `planner_loop` | Topology analysis and expansion recommendations |
| `fee_intelligence_loop` | Fee observation broadcast and aggregation |
| `intent_monitor_loop` | Intent lock expiry and cleanup |
| `outbox_retry_loop` | Reliable message delivery retries |

### Database Tables (28)

| Table | Purpose |
|-------|---------|
| `hive_members` | Member roster (single "member" tier) |
| `hive_state` | Key-value store for persistent state |
| `intent_locks` | Active intent locks for conflict resolution |
| `contribution_ledger` | Forwarding contribution tracking |
| `contribution_rate_limits` | Rate limiting for contribution updates |
| `contribution_daily_stats` | Daily contribution aggregates |
| `membership_audit_log` | Membership event audit trail (join/leave/ban/approve) |
| `membership_tombstones` | Removed member tombstones for anti-resurrection |
| `hive_bans` | Active bans |
| `local_fee_tracking` | Local fee change tracking |
| `peer_presence` | Peer online/offline tracking |
| `hive_planner_log` | Planner decision audit log |
| `planner_ignored_peers` | Planner ignore list |
| `fee_intelligence` | Aggregated fee intelligence data |
| `peer_fee_profiles` | Fee profiles shared by fleet members |
| `member_health` | Fleet member health tracking |
| `peer_reputation` | Peer reputation scores |
| `flow_samples` | Flow sample data |
| `temporal_patterns` | Intra-day flow pattern data |
| `peer_capabilities` | Peer protocol capabilities |
| `proto_events` | Processed event IDs for idempotency |
| `proto_outbox` | Reliable message delivery outbox |
| `traffic_profiles` | Traffic profile data shared across fleet |
| `liquidity_needs` | Aggregated liquidity need requests |
| `leech_flags` | Anti-leech flag tracking |
| `peer_events` | Peer lifecycle event log |
| `member_liquidity_state` | Per-member liquidity state snapshots |

### Primary RPC Commands (115 total)

**Membership**:
`hive-genesis`, `hive-approve`, `hive-pending`, `hive-leave`, `hive-members`, `hive-status`, `hive-remove-member`, `hive-ban`, `hive-ban-candidates`

**Configuration**:
`hive-config`, `hive-reload-config`, `hive-reinit-bridge`, `hive-bump-version`

**Fleet Intelligence**:
`hive-fee-recommendation`, `hive-coord-fee-recommendation`, `hive-fee-profiles`, `hive-fee-intelligence`, `hive-fee-intel-query`, `hive-fee-coordination-status`, `hive-aggregate-fees`, `hive-corridor-assignments`, `hive-egress-desaturation-bias`

**Topology & Planning**:
`hive-topology`, `hive-expansion-recommendations`, `hive-planner-log`, `hive-planner-ignore`, `hive-planner-unignore`, `hive-intent-status`, `hive-test-intent`

**Liquidity & Rebalancing**:
`hive-liquidity-needs`, `hive-liquidity-status`, `hive-liquidity-state`, `hive-rebalance-recommendations`, `hive-rebalance-hubs`, `hive-check-rebalance-conflict`

**Health & Monitoring**:
`hive-health`, `hive-fleet-health`, `hive-member-health`, `hive-connectivity-alerts`, `hive-member-connectivity`, `hive-nnlb-status`, `hive-network-metrics`

**Channel Analysis**:
`hive-close-recommendations`, `hive-coverage-analysis`, `hive-rationalization-summary`, `hive-rationalization-status`, `hive-valuable-corridors`, `hive-exchange-coverage`

**Peer & Quality**:
`hive-peer-quality`, `hive-get-peer-quality`, `hive-quality-check`, `hive-peer-reputations`, `hive-reputation-stats`, `hive-contribution`

**Traffic & Flow**:
`hive-traffic-intelligence`, `hive-report-traffic-profile`, `hive-fleet-demand-forecast`, `hive-velocity-prediction`, `hive-critical-velocity`, `hive-anticipatory-predictions`, `hive-predict-liquidity`

**Positioning**:
`hive-positioning-recommendations`, `hive-positioning-summary`, `hive-positioning-status`

**Local Integration**:
`hive-export-hints` — Compact short-lived per-peer hints for trusted local consumers (cl-revenue-ops). Read-only, no side effects. Returns per-peer member status, corridor role, competition bias, quality score, traffic confidence, rebalance preference, and topology-based channel-opening advisory hints. cl-hive does not directly open channels, set fees, or trigger rebalances — local execution remains the responsibility of cl-revenue-ops.

## Safety Constraints

These are non-negotiable:

1. **Fail closed**: On invalid input, RPC errors, schema mismatches -> do nothing, log
2. **Bound everything**: Message sizes, list sizes, DB growth, caches, loop runtime
3. **No silent fund actions**: Never move funds; coordination only
4. **Identity binding**: Sender peer_id must match claimed pubkey in payload
5. **DoS protection**: Max 200 remote intents cached, rate limits on all loops
6. **Hint-only posture**: cl-hive exports hints but does not set fees, trigger rebalances, or open channels

## Planner Rules

The Planner proposes topology changes but cannot open channels directly:
- May log decisions to `hive_planner_log`
- May log recommendations via `RecommendationLogger`
- May broadcast INTENT messages for conflict-free coordination
- Max 5 ignores per cycle
- 20% market share cap per target

### Feerate Gate
- Expansions blocked when on-chain feerate > `hive-max-expansion-feerate` (default: 5000 sat/kB)
- Set to 0 to disable feerate checking
- Uses CLN `feerates` RPC to get current opening feerate

## Development Notes

- Only external dependency: `pyln-client>=24.0`
- All crypto done via CLN HSM (signmessage/checkmessage) -- no crypto libs imported
- Plugin options defined in `modules/plugin_options.py`
- Background loops defined in `modules/background_loops.py`

## Testing Conventions

- Test files in `tests/` directory (38 test files)
- Use pytest fixtures for mocking (see `conftest.py`)
- Mock RPC calls, never hit real network
- Test categories: unit, integration, feerate, planner, membership

## File Structure

```
cl-hive/
├── cl-hive.py              # Main plugin entry point
├── cl-hive.conf.sample     # Production config sample
├── modules/                # 31 modules
│   ├── protocol.py         # Message types and encoding
│   ├── protocol_handlers.py # Message dispatch handlers
│   ├── handshake.py        # PKI authentication
│   ├── state_manager.py    # Distributed state (HiveMap)
│   ├── gossip.py           # Gossip protocol
│   ├── intent_manager.py   # Intent locks
│   ├── bridge.py           # cl-revenue-ops bridge (Circuit Breaker)
│   ├── membership.py       # Single-role membership management
│   ├── governance.py       # Recommendation logging
│   ├── contribution.py     # Contribution tracking
│   ├── planner.py          # Topology planner
│   ├── fee_coordination.py # Corridor fee coordination
│   ├── fee_intelligence.py # Fee intelligence sharing
│   ├── liquidity_coordinator.py # Liquidity needs aggregation
│   ├── traffic_intelligence.py  # Traffic profile sharing
│   ├── health_aggregator.py # Fleet health scoring
│   ├── network_metrics.py  # Network metrics collection
│   ├── peer_reputation.py  # Peer reputation tracking
│   ├── quality_scorer.py   # Peer quality scoring
│   ├── channel_rationalization.py # Channel optimization
│   ├── strategic_positioning.py # Network positioning
│   ├── yield_metrics.py    # Yield tracking
│   ├── idempotency.py      # Message deduplication
│   ├── outbox.py           # Reliable message delivery
│   ├── relay.py            # Message relay logic
│   ├── log_writer.py       # Structured logging
│   ├── rpc_commands.py     # RPC command handlers
│   ├── background_loops.py # Background loop definitions
│   ├── plugin_options.py   # Plugin option registration
│   ├── config.py           # Configuration
│   └── database.py         # Database layer (28 tables)
├── config/                 # Config examples
├── tests/                  # 38 test files
├── docs/                   # Documentation
└── docker/                 # Docker deployment
```
