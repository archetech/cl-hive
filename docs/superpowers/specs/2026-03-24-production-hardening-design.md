# Production Hardening Design

## Goal

Make cl-hive production-ready through two mandatory passes: code safety hardening (Pass 1) then intelligence pipeline verification (Pass 2).

## Context

cl-hive is a 31-module, ~33K line Core Lightning plugin that coordinates a trusted fleet of Lightning nodes. It shares observations, produces intelligence, and exports hints to cl-revenue-ops. A comprehensive audit just fixed critical issues (memory exhaustion, undefined variables, missing DB methods). This hardening goes deeper — making every module bulletproof and verifying the entire intelligence pipeline produces correct results.

## Pass 1: Code Safety Hardening

Module-by-module review for defensive coding. Fix crashes, not architecture.

### Tier 1 (Highest risk — large, complex)

**cl-hive.py (4,245 lines):**
- All 93 RPC handlers: null guards on globals, safe parameter parsing, error returns not crashes
- Init sequence: verify ordering prevents use-before-init
- Message dispatch: verify all HiveMessageType cases are handled or explicitly ignored
- Notification handlers (channel_opened, channel_closed, peer_connected): verify they can't crash on malformed CLN data

**protocol_handlers.py (3,150 lines):**
- Every handler function: verify payload validation before field access
- All `database.*` calls: verify they're inside try/except or caller handles failure
- All `plugin.rpc.*` calls: verify timeout/failure handling
- Message relay: verify TTL, dedup, and ban checks are consistent across all handlers
- FULL_SYNC membership merge: verify it can't corrupt local state with malformed data

**database.py (3,730 lines):**
- Every method: verify returns safe defaults on exception (not None when caller expects dict)
- Schema: verify all CREATE TABLE IF NOT EXISTS are idempotent
- Thread safety: verify all connections use `_get_connection()` (thread-local)
- SQL injection: verify all queries use parameterized statements (no f-strings in SQL)
- Integer overflow: verify amounts/timestamps are bounded before INSERT

### Tier 2 (Medium risk — significant logic)

**planner.py (1,874 lines):**
- Network cache refresh: verify it handles empty/malformed listchannels
- Saturation calculation: verify division-by-zero protection on capacity=0
- Channel sizer: verify all arithmetic is bounded (no negative sizes, no overflow)
- Underserved targets: verify quality scorer failures don't crash the pipeline

**fee_coordination.py (963 lines):**
- Corridor identification: verify empty corridor list doesn't crash
- Fee recommendation: verify floor/ceiling are always applied
- Egress desaturation: verify balance percentage calculations handle 0 capacity

**liquidity_coordinator.py (1,088 lines):**
- Liquidity needs: verify amount calculations don't go negative
- Rebalancing activity: verify state dict access is safe
- Fleet state: verify member state updates can't corrupt the cache

**background_loops.py (1,283 lines):**
- Already audited — verify fixes from prior audit are solid
- Check for any remaining try/except gaps in the fee_intelligence_loop (15+ ops per iteration)

**gossip.py (551 lines):**
- Broadcast threshold: verify division-by-zero on capacity comparisons
- State hash: verify serialization handles missing fields gracefully

### Tier 3 (Lower risk — smaller, focused)

**handshake.py (454 lines):** Verify challenge generation, manifest verification, pending request bounds
**membership.py (196 lines):** Verify all DB calls have error handling
**contribution.py (246 lines):** Verify rate limiting arithmetic, leech detection thresholds
**bridge.py (589 lines):** Verify circuit breaker state transitions, timeout handling
**fee_intelligence.py (1,163 lines):** Verify aggregation handles missing/corrupt data
**yield_metrics.py (977 lines):** Verify flow direction calculation handles edge cases (0 in, 0 out)
**network_metrics.py (887 lines):** Verify centrality calculations handle disconnected graphs
**strategic_positioning.py (1,345 lines):** Verify corridor value calculations handle empty data
**channel_rationalization.py (1,281 lines):** Verify close recommendations handle missing channels
**quality_scorer.py (608 lines):** Verify scoring handles peers with no events
**peer_reputation.py (516 lines):** Verify aggregation handles missing reporters
**traffic_intelligence.py (494 lines):** Verify profile aggregation handles conflicting data
**intent_manager.py (695 lines):** Verify lock expiry, conflict detection edge cases
**relay.py (440 lines):** Verify dedup cleanup, TTL enforcement
**outbox.py (225 lines):** Verify retry backoff, max retries
**health_aggregator.py (387 lines):** Verify health scoring handles missing data
**config.py (212 lines):** Verify hot-reload validates all field types/ranges
**plugin_options.py (233 lines):** Verify option parsing handles invalid values
**governance.py (83 lines):** Verify recommendation logging handles DB failures
**log_writer.py (91 lines):** Verify batched logging handles queue overflow
**idempotency.py (71 lines):** Verify event ID generation is deterministic
**rpc_commands.py (2,075 lines):** Verify all exported functions return valid dicts on all code paths

### Pass 1 method

For each module:
1. Read every function
2. Identify: missing null guards, unsafe dict access, unhandled exceptions, arithmetic edge cases, race conditions
3. Fix immediately — defensive coding, not redesign
4. Run tests after each tier
5. Commit per tier

## Pass 2: Intelligence Pipeline Verification

Trace data from source to consumer. Verify the math, not just the code safety.

### Pipeline 1: Fee Intelligence → Fee Hints

**Source:** Each node observes its own channel fees and forward activity
**Collection:** fee_intelligence.py aggregates fee profiles from fleet members
**Processing:** fee_coordination.py identifies corridors, assigns ownership, calculates competition
**Export:** export_hints() derives corridor_role and competition_bias per peer
**Consumer:** cl_revenue_ops HiveHintAdapter.get_fee_bias() applies bounded multiplier

**Verify at each stage:**
- Are fee observations based on real data (listpeerchannels, listforwards)?
- Does aggregation correctly merge reports from multiple fleet members?
- Are corridor assignments stable (not oscillating)?
- Does competition_level correctly reflect the number of capable members?
- Does export_hints() correctly map corridor state to the hint schema?
- Does the consumer's math produce correct bias direction for -1/0/1?

### Pipeline 2: Yield Metrics → Rebalance Hints

**Source:** Each node observes channel balance and forward volume
**Collection:** yield_metrics.py calculates flow direction (source/sink/balanced)
**Export:** export_hints() derives rebalance_preference per peer
**Consumer:** cl_revenue_ops HiveHintAdapter.get_rebalance_bias() applies bounded multiplier

**Verify at each stage:**
- Is flow direction calculated from actual in_sats vs out_sats (not just capacity)?
- Are the 1.5x thresholds for source/sink correct?
- Does export_hints() correctly map flow_direction to rebalance_preference?
- Does the consumer's math produce correct bias direction for sink/source?

### Pipeline 3: Planner → Channel-Open Hints

**Source:** Public channel graph (listchannels)
**Collection:** planner.py calculates saturation, underserved targets, quality scores
**Processing:** get_expansion_recommendation() combines coverage, competition, bottleneck signals
**Export:** _derive_channel_open_hints() maps to open/neutral/avoid with size bucket
**Consumer:** cl_revenue_ops capacity_planner uses hints for candidate discovery + scoring

**Verify at each stage:**
- Is the network cache based on real listchannels data?
- Is hive share calculation correct (hive capacity / total capacity)?
- Are underserved thresholds (5% share, 1 BTC min capacity) appropriate?
- Does the sizer produce reasonable buckets (small/medium/large)?
- Does confidence correctly blend quality + data availability?
- Does the consumer correctly filter by open_preference == "open"?

### Pipeline 4: Membership → Member Hints

**Source:** Membership database (hive_members table)
**Export:** export_hints() sets member: true/false per peer
**Consumer:** cl_revenue_ops HiveHintAdapter.is_hive_member() → 0-PPM policy

**Verify:**
- Does member flag accurately reflect database state?
- Does the consumer correctly apply 0-PPM only when hints are fresh?
- Does gossip oscillation protection work (hold for 2x TTL)?

### Pipeline 5: Fleet Gossip → Shared State

**Source:** Each node broadcasts capacity, availability, topology via GOSSIP messages
**Collection:** state_manager.py merges gossip into HiveMap
**Consumers:** planner, fee_coordination, network_metrics all read from HiveMap

**Verify:**
- Is gossip data based on real listfunds/listpeerchannels?
- Does merge correctly handle version conflicts (higher version wins)?
- Is anti-entropy (STATE_HASH) detecting divergence correctly?
- Are stale entries cleaned up?

### Pass 2 method

For each pipeline:
1. Trace data from source function to export function
2. At each stage, verify the transformation is mathematically correct
3. Check edge cases: empty data, single member fleet, all peers already covered
4. Verify the export schema matches what the consumer expects
5. Run end-to-end with the golden fixture test in cl_revenue_ops
6. Document any incorrect transformations found and fix them

## Constraints

- Fix bugs, not architecture
- No new features
- No performance optimization unless it prevents a bug
- Commit frequently (per tier for Pass 1, per pipeline for Pass 2)
- Run tests after every commit
- Do not touch test files unless they test removed functionality

## Success Criteria

After both passes:
- Every module handles all error paths without crashing
- Every intelligence pipeline produces correct results from real data
- The hint export schema accurately reflects internal state
- cl_revenue_ops consumes hints correctly
- All tests pass
- No known correctness issues remain
