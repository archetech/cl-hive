# Trusted Fleet Simplification — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reduce cl-hive from ~130k LOC protocol platform to ~25-30k LOC trusted fleet coordination layer by deleting all code not needed for sharing observations, generating recommendations, and coordinating a small trusted fleet.

**Architecture:** Staged deletion — first remove non-code files, then delete modules + update importers, then clean protocol/database/RPC/loops, then simplify remaining modules, then rewrite docs, then validate. Each task produces one commit.

**Tech Stack:** Python 3.10+, pytest, SQLite WAL mode, Core Lightning RPC, pyln-client

---

## Context for Implementers

**What cl-hive becomes:** A trusted fleet coordination layer that shares observations (fee intel, traffic patterns, liquidity, peer quality, health) and generates recommendations (corridor ownership, expansion targets, fee suggestions, channel rationalization) for a small fleet of CLN nodes. All local execution is handled by cl-revenue-ops.

**What's being deleted:** Settlement/fee redistribution, DID/Archon credentials, Cashu escrow, marketplace, complex governance (advisor/failsafe modes), cooperative expansion elections, MCF solver, splice coordination, VPN transport, MCP/AI control plane, task delegation, cost reduction engine, budget holds.

**Design doc:** `docs/plans/2026-03-19-trusted-fleet-simplification-design.md`

**Working directory:** `/home/sat/bin/cl-hive`

---

### Task 1: Delete Non-Code Files

Remove tools, production, scripts, MCP configs, AI docs, and other non-module files that support removed features.

**Delete entirely:**
- `tools/` (entire directory — 17 files: MCP server, AI advisor, simulators, scanners)
- `production/` (entire directory — AI advisor deployment, systemd, strategy prompts)
- `scripts/` (entire directory — 4 files: Phase 6, MCP rune, DID signing)
- `MOLTY.md` (AI agent instructions)
- `GEMINI.md` (Gemini AI integration)
- `.mcp.json` (MCP configuration)
- `manifest.json` (DID repo manifest)
- `config/mcp-config.example.json` (MCP config)
- `config/strategy-prompts.example/` (AI strategy prompts)
- `docs/MCP_SERVER.md` (MCP documentation)
- `docker/scripts/manual-install-archon.sh` (Archon installation)

**Step 1:** Delete the files and directories listed above.

**Step 2:** Run `python3 -m pytest tests/ -x -q` — should still pass (these are non-imported files).

**Step 3:** Commit:
```
git add -A
git commit -m "chore: delete MCP server, AI advisor, tools, production, scripts, and related configs"
```

---

### Task 2: Delete Removed Module Files

Delete 18 module files + remove their imports from all importers. This is the big surgery.

**Delete these module files from `modules/`:**
```
settlement.py
cashu_escrow.py
did_credentials.py
management_schemas.py
marketplace.py
liquidity_marketplace.py
routing_pool.py
nostr_transport.py
identity_adapter.py
vpn_transport.py
splice_manager.py
splice_coordinator.py
task_manager.py
cooperative_expansion.py
mcf_solver.py
rpc_pool.py
cost_reduction.py
budget_manager.py
```

**Then update these files to remove all references to deleted modules:**

**`cl-hive.py`:** Remove all import statements for deleted modules. Remove all initialization code that creates instances of deleted managers. Remove all thread startup for deleted loops. Remove all dependency injection lines that pass deleted managers. Remove all shutdown cleanup for deleted managers. The init() function should go from ~1000 lines to ~500 lines.

Key items to remove from init():
- SettlementManager initialization and settlement_loop thread
- CostReductionManager initialization and mcf_optimization_loop thread
- CooperativeExpansionManager initialization
- SpliceManager and SpliceCoordinator initialization
- TaskManager initialization
- VPNTransportManager initialization
- RpcPool initialization (revert to direct plugin.rpc usage)
- All Phase 5B/5C/6 conditional blocks (DID, cashu, marketplace, liquidity marketplace, nostr transport, identity adapter)
- Budget manager references
- Routing pool initialization
- All dependency injection lines for deleted modules in init_protocol_handlers() and init_background_loops()

**`modules/background_loops.py`:** Remove functions that call deleted modules. See Task 6 for full cleanup.

**`modules/protocol_handlers.py`:** Remove handlers that reference deleted modules. See Task 3 for full cleanup.

**`modules/rpc_commands.py`:** Remove commands for deleted features. See Task 5 for full cleanup.

**For this task:** Focus on making imports resolve. Comment out or stub handler/loop/command bodies that reference deleted globals — they'll be fully cleaned in subsequent tasks. The goal is: no ImportError on startup.

**Step 1:** Delete the 18 module files.

**Step 2:** In `cl-hive.py`, remove all import lines for deleted modules. Remove initialization blocks. Remove dependency injection lines. Stub any remaining references with `None` if needed to avoid NameError.

**Step 3:** In `modules/background_loops.py`, comment out or remove function bodies that call deleted managers.

**Step 4:** In `modules/protocol_handlers.py`, comment out or remove handler bodies that call deleted managers.

**Step 5:** In `modules/rpc_commands.py`, comment out or remove command handlers that call deleted managers.

**Step 6:** In any other module that imports deleted modules (check with `grep -r "from modules.settlement\|from modules.cashu\|from modules.did_credentials\|from modules.management_schemas\|from modules.marketplace\|from modules.liquidity_marketplace\|from modules.routing_pool\|from modules.nostr_transport\|from modules.identity_adapter\|from modules.vpn_transport\|from modules.splice_manager\|from modules.splice_coordinator\|from modules.task_manager\|from modules.cooperative_expansion\|from modules.mcf_solver\|from modules.rpc_pool\|from modules.cost_reduction\|from modules.budget_manager" modules/ cl-hive.py`), remove the import and any code that uses it.

**Step 7:** Run `python3 -c "import importlib; importlib.import_module('modules.protocol')"` and similar for each kept module to verify no ImportError.

**Step 8:** Run `python3 -m pytest tests/ -x -q` — many tests will fail (tests for deleted modules). That's expected. Verify no ImportError crashes in kept modules.

**Step 9:** Commit:
```
git commit -m "refactor: delete 18 modules for removed features (settlement, DID, cashu, marketplace, MCF, splice, VPN, etc.)"
```

---

### Task 3: Clean protocol.py

Remove message types, validators, creators, and rate limits for deleted features.

**File:** `modules/protocol.py`

**Remove these HiveMessageType enum values:**
- Settlement: SETTLEMENT_OFFER, FEE_REPORT, SETTLEMENT_PROPOSE, SETTLEMENT_READY, SETTLEMENT_EXECUTED, SETTLEMENT_RECEIPT, BOND_POSTING, BOND_SLASH, NETTING_PROPOSAL, NETTING_ACK, VIOLATION_REPORT, ARBITRATION_VOTE
- DID/Management: DID_CREDENTIAL_PRESENT, DID_CREDENTIAL_REVOKE, MGMT_CREDENTIAL_PRESENT, MGMT_CREDENTIAL_REVOKE
- Expansion elections: PEER_AVAILABLE, EXPANSION_NOMINATE, EXPANSION_ELECT, EXPANSION_DECLINE
- Task delegation: TASK_REQUEST, TASK_RESPONSE
- Splice: SPLICE_INIT_REQUEST, SPLICE_INIT_RESPONSE, SPLICE_UPDATE, SPLICE_SIGNED, SPLICE_ABORT
- MCF: MCF_NEEDS_BATCH, MCF_SOLUTION_BROADCAST, MCF_ASSIGNMENT_ACK, MCF_COMPLETION_REPORT
- Membership voting: VOUCH, PROMOTION, PROMOTION_REQUEST, BAN_PROPOSAL, BAN_VOTE
- Reliable delivery: MSG_ACK
- Fee coordination complexity: PHEROMONE_BATCH, STIGMERGIC_MARKER_BATCH, PHYSARUM_RECOMMENDATION, CIRCULAR_FLOW_ALERT

**Keep these enum values (29 types):**
- Handshake: HELLO, CHALLENGE, ATTEST, WELCOME
- Gossip: GOSSIP, STATE_HASH, FULL_SYNC
- Intent: INTENT, INTENT_ABORT
- Membership: BAN, MEMBER_LEFT
- Intelligence: LIQUIDITY_NEED, HEALTH_REPORT, ROUTE_PROBE, FEE_INTELLIGENCE_SNAPSHOT, PEER_REPUTATION_SNAPSHOT, ROUTE_PROBE_BATCH, LIQUIDITY_SNAPSHOT, YIELD_METRICS_BATCH, TEMPORAL_PATTERN_BATCH, CORRIDOR_VALUE_BATCH, POSITIONING_PROPOSAL, COVERAGE_ANALYSIS_BATCH, CLOSE_PROPOSAL, TRAFFIC_INTELLIGENCE_BATCH

**Also remove:**
- All `validate_*` and `create_*` functions for removed message types
- All `RELIABLE_MESSAGE_TYPES` entries for removed types
- All `IMPLICIT_ACK_MAP` entries for removed types
- All rate limit constants for removed types
- Signing payload builders for removed types

**Step 1:** Remove enum values, keeping comments noting the gaps in numbering for protocol compatibility.

**Step 2:** Delete all validate/create functions for removed types (search for function names containing: settlement, splice, mcf, expansion_nominate, expansion_elect, task_request, did_credential, mgmt_credential, bond, netting, arbitration, violation, vouch, promotion, ban_proposal, ban_vote, pheromone, stigmergic, physarum, circular_flow, msg_ack).

**Step 3:** Clean up RELIABLE_MESSAGE_TYPES and IMPLICIT_ACK_MAP.

**Step 4:** Run `python3 -m pytest tests/test_protocol.py -x -q` to check for obvious breakage.

**Step 5:** Commit:
```
git commit -m "refactor: remove 37 protocol message types for deleted features"
```

---

### Task 4: Clean protocol_handlers.py

Remove message handlers for deleted message types.

**File:** `modules/protocol_handlers.py` (~8,400 lines)

**Remove all handler functions for removed message types.** The handler dispatch is typically a large if/elif chain or dict lookup. Remove each branch for removed types.

Search for handler functions matching: `_handle_settlement`, `_handle_splice`, `_handle_mcf`, `_handle_expansion_nominate`, `_handle_expansion_elect`, `_handle_expansion_decline`, `_handle_task`, `_handle_did`, `_handle_mgmt`, `_handle_bond`, `_handle_netting`, `_handle_violation`, `_handle_arbitration`, `_handle_vouch`, `_handle_promotion`, `_handle_ban_proposal`, `_handle_ban_vote`, `_handle_msg_ack`, `_handle_pheromone`, `_handle_stigmergic`, `_handle_physarum`, `_handle_circular_flow`, `_handle_fee_report`, `_handle_peer_available`.

Also remove any references to deleted global managers (settlement_mgr, cashu_escrow_mgr, did_credential_mgr, management_schema_registry, marketplace_mgr, liquidity_marketplace_mgr, routing_pool_mgr, splice_mgr, task_mgr, cooperative_expansion_mgr, mcf_coordinator, cost_reduction_mgr, budget_mgr, vpn_transport_mgr, nostr_transport, identity_adapter).

**Step 1:** Remove handler functions for all removed message types.

**Step 2:** Remove global variable declarations for deleted managers.

**Step 3:** Clean the init_protocol_handlers() function to not accept deleted manager references.

**Step 4:** Run `python3 -m pytest tests/test_protocol.py tests/test_protocol_versioning.py -x -q`.

**Step 5:** Commit:
```
git commit -m "refactor: remove protocol handlers for deleted message types"
```

---

### Task 5: Clean database.py

Remove 49 database tables for deleted features.

**File:** `modules/database.py` (~8,573 lines)

**Remove CREATE TABLE statements for these 49 tables:**

Settlement: settlement_proposals, settlement_ready_votes, settlement_executions, settled_periods, settlement_sub_payments, settlement_bonds, settlement_obligations, settlement_disputes

DID/Management: did_credentials, did_reputation_cache, management_credentials, management_receipts

Marketplace: marketplace_profiles, marketplace_contracts, marketplace_trials, liquidity_offers, liquidity_leases, liquidity_heartbeats, liquidity_needs

Cashu: escrow_tickets, escrow_secrets, escrow_receipts

Routing pool: pool_contributions, pool_revenue, pool_distributions, pool_settlement_markers

Budget: budget_tracking, budget_holds, delegation_attempts

Task: task_requests_outgoing, task_requests_incoming

Other: member_liquidity_state, peer_events, route_probes, pheromone_levels, stigmergic_markers, remote_pheromones, fee_observations, defense_warning_reports, defense_active_fees, fee_reports, nostr_state, splice_sessions, leech_flags, pending_actions, fleet_traffic_intelligence

**Also remove:**
- All methods that ONLY operate on removed tables (INSERT, SELECT, UPDATE, DELETE methods)
- Keep methods that operate on kept tables

**Safety:** No foreign keys cross the KEEP/REMOVE boundary, so removal is clean.

**Step 1:** Remove all CREATE TABLE statements for the 49 removed tables.

**Step 2:** Remove all methods that only operate on removed tables. Search for method names containing: settlement, splice, mcf, expansion_elect, task_request, did_credential, mgmt_credential, bond, netting, arbitration, cashu, escrow, marketplace, liquidity_lease, liquidity_offer, routing_pool, pool_, budget_hold, budget_track, delegation, pheromone, stigmergic, defense, fee_report, nostr_state, pending_action, leech_flag, peer_event, traffic_intelligence (be careful — keep methods for KEPT features).

**Step 3:** Run `python3 -m pytest tests/test_database_audit.py -x -q`.

**Step 4:** Commit:
```
git commit -m "refactor: remove 49 database tables for deleted features"
```

---

### Task 6: Clean background_loops.py

Remove background loops and broadcast functions for deleted features.

**File:** `modules/background_loops.py` (~2,949 lines)

**Remove these loops entirely:**
- `settlement_loop()` — settlement feature removed
- `mcf_optimization_loop()` — MCF solver removed
- `did_maintenance_loop()` — DID credentials removed
- `escrow_maintenance_loop()` — Cashu escrow removed
- `marketplace_maintenance_loop()` — Marketplace removed
- `liquidity_maintenance_loop()` — Liquidity marketplace removed

**Remove these broadcast functions:**
- `_broadcast_mcf_solution()`, `_broadcast_mcf_needs()`, `_broadcast_mcf_ack()` — MCF removed
- `_broadcast_our_pheromones()` — Pheromone system removed
- `_broadcast_our_stigmergic_markers()` — Stigmergy removed
- `_broadcast_circular_flow_alerts()` — cost_reduction removed
- `_broadcast_our_physarum_recommendations()` — Physarum removed
- `_check_settlement_gaming_and_propose_bans()` — Settlement removed
- `_propose_settlement_gaming_ban()` — Settlement removed

**Remove from fee_intelligence_loop():** The calls to removed broadcast functions. Keep the calls to: `_broadcast_our_fee_intelligence()`, `_broadcast_health_report()`, `_broadcast_liquidity_needs()`, `_broadcast_our_yield_metrics()`, `_broadcast_our_temporal_patterns()`, `_broadcast_our_corridor_values()`, `_broadcast_our_positioning_proposals()`, `_broadcast_our_coverage_analysis()`, `_broadcast_our_close_proposals()`, `_broadcast_our_traffic_intelligence()`.

**Clean init_background_loops():** Remove parameters for deleted managers.

**Step 1:** Delete removed loop functions and broadcast functions.

**Step 2:** Clean fee_intelligence_loop() to remove calls to deleted broadcasts.

**Step 3:** Clean init_background_loops() signature.

**Step 4:** Run `python3 -m pytest tests/ -x -q --ignore=tests/test_mcp_hive_server.py --ignore=tests/test_did_credentials.py --ignore=tests/test_did_protocol.py --ignore=tests/test_cashu_escrow.py --ignore=tests/test_marketplace.py --ignore=tests/test_liquidity_marketplace.py --ignore=tests/test_distributed_settlement.py --ignore=tests/test_extended_settlements.py --ignore=tests/test_management_schemas.py --ignore=tests/test_identity_adapter.py`.

**Step 5:** Commit:
```
git commit -m "refactor: remove background loops for settlement, MCF, DID, cashu, marketplace"
```

---

### Task 7: Clean rpc_commands.py

Remove RPC commands for deleted features.

**File:** `modules/rpc_commands.py` (~6,117 lines)

**Remove all command handler functions for these categories:**
- Settlement (~16 commands): settlement-*, pool-*, distributed-settlement-*
- Splice (~5 commands): splice-*
- MCF (~7 commands): mcf-*, cost-reduction-*
- DID (~5 commands): did-*
- Management (~3 commands): mgmt-credential-*, schema-*
- Cashu (~6 commands): escrow-*
- Marketplace (~13 commands): marketplace-*, liquidity-discover, liquidity-offer, liquidity-request, liquidity-lease, liquidity-heartbeat, liquidity-lease-status, liquidity-terminate
- VPN (~3 commands): vpn-*
- Bonds/Disputes (~5 commands): bond-*, dispute-*, credit-tier
- Cooperative expansion (~3 commands): expansion-nominate, expansion-elect, expansion-status
- Budget (~3 commands): budget-summary, report-period-costs
- Complex governance (~5 commands): pending-actions, approve-action, reject-action, test-pending-action, set-mode
- Defense (~4 commands): defense-*, broadcast-warning, accumulated-warnings
- Promotion pipeline (~6 commands): vouch, propose-promotion, vote-promotion, sync-promotion, execute-promotion, pending-promotions, force-promote, neophyte-rankings
- Complex ban voting (~3 commands): propose-ban, vote-ban, pending-bans
- Phase 6 plugins (~2 commands): phase6-plugins, plugin-list
- Circular flows (~2 commands): circular-flow-*, execute-circular-rebalance
- Pheromone/stigmergic (~3 commands): pheromone-levels, deposited-marker, stigmergic-markers
- Physarum (~2 commands): physarum-cycle, physarum-status
- Sling direct control (~4 commands): sling-stats, sling-status, sling-deletejob (these are execution, not coordination)

**Keep command handlers for:**
- Status/info: getinfo, status, config, reload-config, reinit-bridge
- Membership (simplified): members, genesis, invite, join, ban, remove-member, leave, promote-admin
- Fee intelligence: fee-profiles, fee-recommendation, fee-intelligence, fee-intel-query, aggregate-fees, coord-fee-recommendation, fee-coordination-status
- Health: health, member-health, report-health, calculate-health, trigger-health-report
- Yield: yield-metrics, yield-summary, velocity-prediction, critical-velocity
- Routing: get-routing-intelligence, routing-stats, route-suggest, record-routing-outcome, routing-intelligence-status, backfill-routing-intelligence
- Reputation: peer-reputations, reputation-stats, peer-quality, quality-check, get-peer-quality
- Traffic: traffic-intelligence, fleet-demand-forecast, report-traffic-profile
- Liquidity: liquidity-needs, liquidity-status, liquidity-state, report-liquidity-state
- Rebalance recommendations: rebalance-recommendations, rebalance-hubs, check-rebalance-conflict
- Network: topology, connectivity-alerts, member-connectivity, fleet-health, network-metrics, gossip-stats
- Strategic: valuable-corridors, exchange-coverage, positioning-recommendations, positioning-summary, positioning-status, corridor-assignments
- Channel: coverage-analysis, close-recommendations, rationalization-summary, rationalization-status, get-channel-flags, get-channel-ages
- Anticipatory: fleet-anticipation, anticipatory-status, anticipatory-predictions, detect-patterns, predict-liquidity
- Planner: expansion-recommendations, planner-log, planner-ignore, planner-unignore, planner-ignored-peers, calculate-size
- Intent: test-intent, intent-status
- NNLB: nnlb-status, get-nnlb-opportunities
- CLN passthrough: listpeers, listpeerchannels, listforwards, listchannels, listfunds, listnodes, connect, open-channel, close-channel, setchannel, askrene-listlayers, askrene-listreservations
- Contribution: contribution
- Egress: egress-desaturation-bias
- Flow: record-flow
- Inject: inject-packet
- Kalman: report-kalman-velocity, query-kalman-velocity

**Also clean:** `init_rpc_commands()` / `HiveContext` to not include deleted managers.

**Step 1:** Remove command handler functions for deleted categories.

**Step 2:** Clean HiveContext and init function.

**Step 3:** Run `python3 -m pytest tests/test_rpc.py tests/test_rpc_commands_audit.py -x -q`.

**Step 4:** Commit:
```
git commit -m "refactor: remove ~150 RPC commands for deleted features"
```

---

### Task 8: Delete Test Files for Removed Features

Delete test files that exclusively test removed features.

**Delete these test files:**
- `tests/test_mcp_hive_server.py` (MCP server)
- `tests/test_marketplace.py` (marketplace)
- `tests/test_liquidity_marketplace.py` (liquidity marketplace)
- `tests/test_did_credentials.py` (DID credentials)
- `tests/test_did_protocol.py` (DID protocol)
- `tests/test_identity_adapter.py` (Archon identity)
- `tests/test_distributed_settlement.py` (settlement)
- `tests/test_extended_settlements.py` (settlement)
- `tests/test_settlement_protocol_handlers.py` (settlement handlers)
- `tests/test_settlement_db_integrity.py` (settlement DB)
- `tests/test_settlement_8_fixes.py` (settlement fixes)
- `tests/test_routing_settlement_bugfixes.py` (settlement bugs)
- `tests/test_cashu_escrow.py` (Cashu escrow)
- `tests/test_management_schemas.py` (management schemas)
- `tests/test_vpn_transport.py` (VPN transport)
- `tests/test_nostr_transport.py` (Nostr transport)
- `tests/test_cooperative_expansion.py` (expansion elections)
- `tests/test_mcf_solver.py` (MCF solver)
- `tests/test_cost_reduction.py` (cost reduction)
- `tests/test_budget_manager.py` (budget manager)
- `tests/test_splice_manager.py` (splice manager)
- `tests/test_splice_bugs.py` (splice bugs)
- `tests/test_proactive_advisor.py` (AI advisor)

**Step 1:** Delete the test files listed above.

**Step 2:** Run `python3 -m pytest tests/ -x -q` — fix any remaining import errors in kept test files that reference deleted modules.

**Step 3:** Commit:
```
git commit -m "test: delete ~600 tests for removed features"
```

---

### Task 9: Clean plugin_options.py and cl-hive.py Residual

Remove plugin options (CLI flags) for deleted features.

**File:** `modules/plugin_options.py`

Remove options related to: settlement, VPN, MCP, marketplace, DID, escrow, expansion-elections, MCF, splice, budget, governance modes (advisor/failsafe), promotion pipeline.

**File:** `cl-hive.py`

Remove any residual references to deleted features not caught in Task 2. Clean up the @plugin.method registrations that point to deleted rpc_commands handlers. Clean up subscription handlers that reference deleted managers.

**Also clean:** `cl-hive.conf.sample` — remove config keys for deleted features.

**Step 1:** Remove plugin options.

**Step 2:** Clean cl-hive.py residual.

**Step 3:** Update cl-hive.conf.sample.

**Step 4:** Run `python3 -m pytest tests/ -x -q`.

**Step 5:** Commit:
```
git commit -m "refactor: remove plugin options and config for deleted features"
```

---

### Task 10: Simplify membership.py

Replace the 4-tier promotion pipeline with simple admin/member model.

**File:** `modules/membership.py` (~715 lines)

**Target state:**
- Two roles: `admin` and `member`
- Admin can add/remove members via RPC
- No promotion pipeline, vouch graph, quorum calculations
- Keep: PKI handshake verification, member list management, auto-connect, uptime tracking
- Remove: MembershipTier enum complexity (keep just ADMIN/MEMBER), promotion logic, vouch tracking, contribution ratio thresholds, leech detection gating on tier

**Changes:**
1. Simplify MembershipTier to just `ADMIN = "admin"` and `MEMBER = "member"`
2. Remove `_check_promotion_eligibility()`, `_process_promotion()`, vouch handling
3. Keep `add_member()`, `remove_member()`, `get_members()`, `is_member()`, uptime tracking
4. Simplify `_cleanup_ghost_members()` to just check if peer is still connected

**Step 1:** Simplify the module.

**Step 2:** Update any references to old MembershipTier values in other kept modules.

**Step 3:** Run `python3 -m pytest tests/test_membership.py -x -q`.

**Step 4:** Commit:
```
git commit -m "refactor: simplify membership to admin/member model"
```

---

### Task 11: Simplify governance.py

Replace advisor/failsafe decision engine with simple recommendation logging.

**File:** `modules/governance.py` (~425 lines)

**Target state:**
- No GovernanceMode enum (no advisor/failsafe)
- No pending_actions queue
- No AI approval workflow
- Recommendations are generated, logged, and optionally pushed to cl-revenue-ops via bridge
- Simple enable/disable coordination per-node via config flag

**Changes:**
1. Remove DecisionEngine class
2. Remove GovernanceMode enum
3. Remove budget tracking, rate limiting, executor registration
4. Replace with a simple `RecommendationLogger` that logs recommendations to database and optionally pushes via bridge
5. Or if governance.py becomes trivially small, inline its remaining logic into the callers and delete the module

**Step 1:** Simplify or delete the module.

**Step 2:** Update callers (planner.py, background_loops.py, cl-hive.py).

**Step 3:** Run `python3 -m pytest tests/test_governance.py -x -q` (update or delete tests as needed).

**Step 4:** Commit:
```
git commit -m "refactor: replace governance engine with simple recommendation logging"
```

---

### Task 12: Simplify fee_coordination.py

Keep corridor ownership and competition avoidance; remove pheromone, stigmergy, mycelium defense, and time-based fee adjustment.

**File:** `modules/fee_coordination.py` (~3,185 lines)

**Remove:**
- `AdaptiveFeeController` (pheromone-based fee adjustment)
- `StigmergicCoordinator` (route marker system)
- `MyceliumDefenseSystem` (threat detection)
- `TimeBasedFeeAdjuster` (peak/off-peak modulation)

**Keep:**
- `FlowCorridorManager` (corridor identification, member assignment)
- `FeeCoordinationManager` (simplified to just corridor-based recommendations)
- Competition avoidance logic (don't undercut fleet members)

**Target:** ~800-1000 lines (from 3,185)

**Step 1:** Delete the 4 removed classes.

**Step 2:** Simplify FeeCoordinationManager to only use FlowCorridorManager.

**Step 3:** Remove references to deleted classes from background_loops.py broadcasts (already partially done in Task 6).

**Step 4:** Run `python3 -m pytest tests/test_fee_coordination.py -x -q` — fix/delete broken tests.

**Step 5:** Commit:
```
git commit -m "refactor: simplify fee coordination to corridor ownership and competition avoidance"
```

---

### Task 13: Simplify Remaining Modules

Simplify planner, strategic_positioning, anticipatory_liquidity, and liquidity_coordinator.

**`modules/planner.py`** (~2,576 lines → ~1,200):
- Remove: direct intent execution, cooperative_expansion integration, mcf_solver integration
- Keep: recommendation generation, channel sizing, saturation detection, expansion recommendations
- Planner becomes recommendation-only: it generates suggestions but does not execute

**`modules/strategic_positioning.py`** (~2,320 lines → ~800):
- Remove: `PhysarumChannelManager` (bio-inspired model)
- Keep: `RouteValueAnalyzer` (corridor analysis), `FleetPositioningStrategy` (positioning recommendations)
- Remove: `StrategicPositioningManager` complexity, splice recommendations

**`modules/anticipatory_liquidity.py`** (~2,808 lines → ~1,000):
- Remove: complex prediction engine, fleet anticipation coordination
- Keep: temporal pattern detection, intra-day phase detection, pattern sharing

**`modules/liquidity_coordinator.py`** (~1,909 lines → ~800):
- Remove: MCF integration, assignment distribution, complex internal competition resolution
- Keep: liquidity need assessment, NNLB priority scoring, basic awareness

**Step 1:** Simplify each module as described.

**Step 2:** Update cross-references between simplified modules.

**Step 3:** Run `python3 -m pytest tests/ -x -q` — fix broken tests.

**Step 4:** Commit:
```
git commit -m "refactor: simplify planner, positioning, anticipatory, and liquidity modules"
```

---

### Task 14: Fix Remaining Test Failures

After all code changes, systematically fix remaining test failures.

**Step 1:** Run `python3 -m pytest tests/ -v 2>&1 | tail -100` and catalog all failures.

**Step 2:** For each failure:
- If test references a deleted feature → delete the test
- If test references a renamed/simplified API → update the test
- If test is a regression test that still applies → fix the import/reference

**Step 3:** Run `python3 -m pytest tests/ -v` until all tests pass.

**Step 4:** Commit:
```
git commit -m "test: fix remaining test failures after simplification"
```

---

### Task 15: Rewrite Documentation

**Rewrite `README.md`:**
- Product description: trusted fleet coordination layer
- Quick-start: genesis, invite, join
- Feature list: observations, recommendations, coordination
- Integration: cl-revenue-ops bridge
- No mention of removed features

**Rewrite `CLAUDE.md`:**
- Module list: only kept modules (~32)
- Architecture: membership → state → observations → recommendations → bridge
- Database: only kept tables (~26)
- Commands: only kept RPC commands
- Safety constraints: kept constraints only
- Remove all mention of: settlement, DID, cashu, MCP, marketplace, governance modes, cooperative expansion, MCF, splice, VPN, Archon

**Rewrite `cl-hive.conf.sample`:**
- Only kept configuration options

**Rewrite `docs/JOINING_THE_HIVE.md`:**
- Simplified membership (admin adds member, no promotion pipeline)

**Update `requirements.txt`:**
- Remove websockets, coincurve (Nostr transport removed)
- Keep only: pyln-client>=24.0

**Update `docker/README.md`:**
- Remove Phase 6 plugin references
- Remove Archon references
- Simplify to just cl-hive deployment

**Delete remaining obsolete docs:**
- Any planning docs in `docs/plans/` that reference deleted features (keep the simplification design/plan docs)
- Settlement audit doc

**Step 1:** Rewrite each doc as described.

**Step 2:** Verify no remaining references to deleted features: `grep -ri "settlement\|cashu\|escrow\|did_credential\|archon\|marketplace\|mcp.*server\|governance.*mode\|advisor.*mode\|failsafe.*mode\|mcf_solver\|cooperative_expansion\|splice_manager\|vpn_transport" docs/ README.md CLAUDE.md`

**Step 3:** Commit:
```
git commit -m "docs: rewrite documentation for trusted fleet coordination layer"
```

---

### Task 16: Final Validation

**Step 1:** Run full test suite:
```
python3 -m pytest tests/ -v
```
All tests must pass.

**Step 2:** Verify no orphaned references:
```
grep -r "from modules.settlement\|from modules.cashu\|from modules.did_credentials\|from modules.management_schemas\|from modules.marketplace\|from modules.liquidity_marketplace\|from modules.routing_pool\|from modules.nostr_transport\|from modules.identity_adapter\|from modules.vpn_transport\|from modules.splice_manager\|from modules.splice_coordinator\|from modules.task_manager\|from modules.cooperative_expansion\|from modules.mcf_solver\|from modules.rpc_pool\|from modules.cost_reduction\|from modules.budget_manager" .
```
Should return zero results.

**Step 3:** Count final LOC:
```
find modules/ -name "*.py" | xargs wc -l
wc -l cl-hive.py
```
Target: ~25-30k total.

**Step 4:** Count remaining tests:
```
python3 -m pytest tests/ --collect-only -q | tail -1
```

**Step 5:** Push:
```
git push
```

---

## Summary of Changes

| Metric | Before | After | Reduction |
|--------|--------|-------|-----------|
| Modules | 53 | ~32 | 40% |
| Total LOC | ~130k | ~25-30k | 75-80% |
| RPC commands | 235 | ~80 | 66% |
| DB tables | 75 | 26 | 65% |
| Message types | 65 | 29 | 55% |
| Tests | ~2,450 | ~1,850 | 24% |
| Background threads | 13-16 | 6-7 | 54% |
| External deps | 3 | 1 | 67% |

## Deferred / Manual Follow-Up

- Production deployment: operator must update cl-hive.conf to remove deleted options
- Database: existing databases will have orphaned tables (harmless, can be cleaned with manual DROP TABLE)
- Fleet peers: all fleet members must upgrade simultaneously (protocol version change)
- Docker: rebuild Docker image after changes
