# Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every cl-hive module crash-proof and verify all intelligence pipelines produce correct results end-to-end.

**Architecture:** Two passes — Pass 1 hardens code safety module-by-module (3 tiers by risk), Pass 2 traces 6 intelligence pipelines from data source to cl-revenue-ops consumer verifying correctness at each stage. Pass 1 fixes must land before Pass 2 begins.

**Tech Stack:** Python 3.10+, pytest, Core Lightning plugin framework, SQLite

---

## PASS 1: CODE SAFETY HARDENING

### Task 1: Tier 1 — cl-hive.py

**Files:**
- Modify: `cl-hive.py`

- [ ] **Step 1: Audit all RPC handlers for null guard safety**

Read every `@plugin.method` handler. For each, verify:
- Globals accessed (database, our_pubkey, etc.) have null checks before use
- Parameters are validated before use
- Return value is always a dict (not None, not crash)

Flag and fix any handler that can crash when called during partial init (before all managers are initialized).

- [ ] **Step 2: Audit init sequence ordering**

Read the `init()` function (~lines 450-900). Verify:
- `planner.set_cooperation_modules()` is called only after all cooperation modules exist
- Background threads (`_deferred_threads`) are started AFTER `init_protocol_handlers()` and `init_background_loops()` complete
- No RPC handler can be invoked during init that depends on not-yet-initialized globals

- [ ] **Step 3: Audit shutdown path**

Read the shutdown handler. Verify:
- `shutdown_event.set()` is called first
- Background threads check `shutdown_event` before accessing globals
- Database writes during shutdown are wrapped in try/except

- [ ] **Step 4: Audit notification handlers**

Read `hive_channel_opened`, `hive_channel_closed`, `on_peer_connected`. Verify:
- Malformed CLN notification data doesn't crash (missing keys, None values)
- All dict access on notification payloads uses `.get()`

- [ ] **Step 5: Convert silent exception swallowing**

Search for bare `except: pass` or `except Exception: pass` without logging:
```bash
grep -n 'except.*pass$\|except.*:$' cl-hive.py | head -30
```
Convert to `except Exception as e: plugin.log(f"...: {e}", level='debug')` where the error should be visible.

- [ ] **Step 6: Run tests and commit**

```bash
python3 -m pytest tests/ --tb=short 2>&1 | tail -5
git add cl-hive.py && git commit -m "fix: harden cl-hive.py — null guards, init ordering, shutdown safety, exception logging"
```

---

### Task 2: Tier 1 — protocol_handlers.py

**Files:**
- Modify: `modules/protocol_handlers.py`

- [ ] **Step 1: Audit every handler for payload validation**

For each `handle_*` function, verify:
- Payload fields accessed with `.get()` not `["key"]`
- Type checks on critical fields (isinstance before arithmetic)
- `database.*` calls inside try/except or with null guard
- `plugin.rpc.*` calls have error handling

- [ ] **Step 2: Verify ban checks are consistent**

```bash
grep -n 'def handle_' modules/protocol_handlers.py
```

For each handler, check: does it verify `is_banned()` before processing? Create a table and fix any gaps.

- [ ] **Step 3: Verify FULL_SYNC membership merge safety**

Read `_apply_membership_sync()`. Verify:
- Malformed member entries (missing peer_id, wrong types) are skipped
- It can't delete or demote existing members
- Invalid tier values are normalized to "member"

- [ ] **Step 4: Convert silent exception swallowing**

Same as Task 1 Step 5 but for protocol_handlers.py.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m pytest tests/ --tb=short 2>&1 | tail -5
git add modules/protocol_handlers.py && git commit -m "fix: harden protocol_handlers.py — payload validation, ban consistency, exception logging"
```

---

### Task 3: Tier 1 — protocol.py

**Files:**
- Modify: `modules/protocol.py`

- [ ] **Step 1: Audit deserialize() for malformed data safety**

Read the `deserialize()` function. Verify:
- Oversized payloads rejected (max message size enforced)
- Malformed JSON in payload returns (None, None) not crash
- Missing "type" field returns (None, None)
- Invalid message type integers return (None, None)

- [ ] **Step 2: Audit all validate_*_payload functions**

For each `validate_*` function, verify:
- Returns False (not crash) for missing required fields
- Returns False for wrong types (string where int expected)
- Returns False for oversized arrays/strings

- [ ] **Step 3: Audit signature canonicalization**

For each `get_*_signing_payload` function, verify:
- Uses `json.dumps(sort_keys=True, separators=(',', ':'))` consistently
- Handles missing optional fields without crash

- [ ] **Step 4: Run tests and commit**

```bash
python3 -m pytest tests/ --tb=short 2>&1 | tail -5
git add modules/protocol.py && git commit -m "fix: harden protocol.py — deserialize safety, validation completeness"
```

---

### Task 4: Tier 1 — database.py

**Files:**
- Modify: `modules/database.py`

- [ ] **Step 1: Audit every method for safe return values**

For each public method, verify:
- Methods that return `List` return `[]` on error (not None)
- Methods that return `Dict` return `{}` on error (not None)
- Methods that return `Optional[Dict]` return `None` on error (not crash)
- Methods that return `bool` return `False` on error (not crash)
- Methods that return `int` return `0` on error (not crash)

- [ ] **Step 2: Verify parameterized queries**

```bash
grep -n 'execute.*f"\|execute.*format\|execute.*%' modules/database.py | head -20
```

Any f-string or format() in SQL = injection vulnerability. Fix with parameterized queries.

- [ ] **Step 3: Verify integer/amount bounds**

Search for INSERT/UPDATE statements that store amounts or timestamps. Verify large values don't cause SQLite overflow (max 64-bit signed int).

- [ ] **Step 4: Verify CREATE TABLE idempotency**

```bash
grep -c 'CREATE TABLE IF NOT EXISTS' modules/database.py
grep -c 'CREATE TABLE ' modules/database.py
```

All CREATE TABLE must have IF NOT EXISTS.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m pytest tests/ --tb=short 2>&1 | tail -5
git add modules/database.py && git commit -m "fix: harden database.py — safe returns, parameterized queries, bounds checking"
```

---

### Task 5: Tier 2 — planner, state_manager, fee_coordination, liquidity, background_loops, gossip

**Files:**
- Modify: `modules/planner.py`, `modules/state_manager.py`, `modules/fee_coordination.py`, `modules/liquidity_coordinator.py`, `modules/background_loops.py`, `modules/gossip.py`

- [ ] **Step 1: Audit planner.py**

- Division-by-zero: search for `/` and `//` operators, verify denominators can't be 0
- Negative values: verify channel sizes, amounts, scores can't go negative
- Quality scorer failures: verify try/except around quality_scorer.calculate_score()
- Empty listchannels: verify network cache refresh handles empty response

- [ ] **Step 2: Audit state_manager.py**

- Thread safety: verify HiveMap reads/writes use proper locking
- from_dict: verify malformed gossip data doesn't corrupt state
- Version merge: verify higher-version-wins logic is correct
- Stale cleanup: verify old entries are removed

- [ ] **Step 3: Audit fee_coordination.py**

- Empty corridors: verify get_assignments() returns [] not crash
- Division-by-zero: verify capacity/volume denominators checked
- Floor/ceiling: verify fee bounds are always enforced

- [ ] **Step 4: Audit liquidity_coordinator.py**

- Negative amounts: verify balance calculations can't produce negative needs
- Dict access safety: verify member state lookups use .get()
- Cache corruption: verify concurrent updates don't corrupt _member_liquidity_state

- [ ] **Step 5: Audit background_loops.py**

- Verify fee_intelligence_loop has individual try/except for each of its 15+ operations
- Verify silent continue paths log at debug level
- Verify 50ms RPC relief yields are present

- [ ] **Step 6: Audit gossip.py**

- Division-by-zero: verify capacity threshold comparison
- Missing fields: verify gossip payload construction handles None values

- [ ] **Step 7: Run tests and commit**

```bash
python3 -m pytest tests/ --tb=short 2>&1 | tail -5
git add modules/planner.py modules/state_manager.py modules/fee_coordination.py modules/liquidity_coordinator.py modules/background_loops.py modules/gossip.py
git commit -m "fix: harden Tier 2 modules — arithmetic safety, thread safety, null guards"
```

---

### Task 6: Tier 3 — All remaining modules

**Files:**
- Modify: all Tier 3 modules listed in spec

- [ ] **Step 1: Audit each Tier 3 module**

For each of the 18 Tier 3 modules, check:
- All `.get()` on external data
- All try/except on DB and RPC calls
- No bare `except: pass`
- No division-by-zero
- No unbounded data structures

Specific focus areas per module:
- **traffic_intelligence.py**: `json.loads()` on `peak_hours_utc` from DB needs try/except
- **quality_scorer.py**: `get_peer_event_summary()` must handle peers with zero events
- **health_aggregator.py**: health tier thresholds must handle NaN/None inputs
- **config.py**: hot-reload must validate types and ranges
- **rpc_commands.py**: every exported function must return a valid dict on all code paths

- [ ] **Step 2: Run tests and commit**

```bash
python3 -m pytest tests/ --tb=short 2>&1 | tail -5
git add modules/ && git commit -m "fix: harden Tier 3 modules — defensive coding across 18 modules"
```

---

## CHECKPOINT: Pass 1 complete. Run full test suite before proceeding to Pass 2.

```bash
python3 -m pytest tests/ -v --tb=short 2>&1 | tail -10
```

All tests must pass before continuing.

---

## PASS 2: INTELLIGENCE PIPELINE VERIFICATION

### Task 7: Pipeline 1 — Fee Intelligence → Fee Hints

**Files:**
- Audit: `modules/fee_intelligence.py`, `modules/fee_coordination.py`, `modules/rpc_commands.py` (_derive_corridor_roles, _derive_competition_bias), `/home/sat/bin/cl_revenue_ops/modules/hive_hints.py`

- [ ] **Step 1: Trace fee observation source**

Read `_broadcast_our_fee_intelligence()` in background_loops.py. Verify it calls real CLN RPCs (listpeerchannels, listforwards) to get actual channel data.

- [ ] **Step 2: Verify corridor assignment logic**

Read `fee_coordination.py` `identify_corridors()` and `assign_corridor()`. Verify:
- Corridors based on real flow data, not stale cache
- Primary assignment logic is deterministic
- Competition level reflects actual capable_members count

- [ ] **Step 3: Verify _derive_corridor_roles() transformation**

Read rpc_commands.py `_derive_corridor_roles()`. Verify:
- Primary member → "owner"
- Secondary member → "secondary"
- Both primary and secondary → "contested"
- No corridor → "none" (not exported, defaults to "none" in export_hints)

- [ ] **Step 4: Verify _derive_competition_bias() transformation**

Read rpc_commands.py `_derive_competition_bias()`. Verify:
- High/medium competition corridors → -1
- Low/none competition corridors → 1
- Equal mix → 0
- Math is correct for all edge cases

- [ ] **Step 5: Verify consumer-side math**

Read `/home/sat/bin/cl_revenue_ops/modules/hive_hints.py` `get_fee_bias()`. Verify:
- "owner" → positive bias (correct: +3%)
- "secondary" → negative bias (correct: -3%)
- "contested" → 0 bias (verify this is intentional)
- competition_bias -1 → negative, 0 → zero, 1 → positive
- traffic_confidence gates everything correctly

- [ ] **Step 6: Verify traffic_confidence is populated for corridor peers**

Check if `traffic_confidence` is populated for peers that have corridor/competition data. If not, those hints are dead on the consumer side.

- [ ] **Step 7: Document findings and fix issues**

```bash
git add -A && git commit -m "fix: Pipeline 1 (fee hints) — correctness fixes"
```

---

### Task 8: Pipeline 2 — Yield Metrics + Quality → Rebalance Hints

**Files:**
- Audit: `modules/yield_metrics.py`, `modules/quality_scorer.py`, `modules/rpc_commands.py` (_derive_rebalance_preferences), `/home/sat/bin/cl_revenue_ops/modules/hive_hints.py`

- [ ] **Step 1: Trace flow direction calculation**

Read yield_metrics.py `get_channel_yield_metrics()`. Verify:
- `flow_direction` derived from actual `in_sats` vs `out_sats`
- Threshold: `in_sats > out_sats * 1.5` → "sink"; `out_sats > in_sats * 1.5` → "source"; else "balanced"
- Data comes from real listpeerchannels/profitability data

- [ ] **Step 2: Verify quality scorer handles edge cases**

Read quality_scorer.py `calculate_score()`. Verify:
- Peer with zero events → returns neutral score (0.5) with confidence 0.0
- Peer with only failures → returns low score, not crash
- Summary dict has expected shape

- [ ] **Step 3: Verify _derive_rebalance_preferences() transformation**

Read rpc_commands.py `_derive_rebalance_preferences()`. Verify:
- "source" → "source", "sink" → "sink", "balanced" → not included (defaults to "neutral")

- [ ] **Step 4: Verify consumer-side math**

Read `get_rebalance_bias()`. Verify:
- "sink" → positive bias (correct: +5%)
- "source" → negative bias (correct: -5%)
- peer_quality_score above 0.5 → positive, below 0.5 → negative
- traffic_confidence gates everything

- [ ] **Step 5: Document findings and fix issues**

```bash
git add -A && git commit -m "fix: Pipeline 2 (rebalance hints) — correctness fixes"
```

---

### Task 9: Pipeline 3 — Planner → Channel-Open Hints

**Files:**
- Audit: `modules/planner.py`, `modules/rpc_commands.py` (_derive_channel_open_hints), `/home/sat/bin/cl_revenue_ops/modules/hive_hints.py`

- [ ] **Step 1: Verify planner data source**

Read planner `_refresh_network_cache()`. Verify it calls real `listchannels` and the cache is fresh.

- [ ] **Step 2: Verify underserved target math**

Read `get_underserved_targets()`. Verify:
- Hive share = hive capacity / total public capacity (not reversed)
- Underserved threshold (5%) is correct
- Min capacity (1 BTC) is enforced
- Quality scores are correctly integrated

- [ ] **Step 3: Verify _derive_channel_open_hints() mappings**

Verify specific invariants:
- `recommendation_type == "open_channel"` → `open_preference = "open"`
- `hive_coverage_pct >= 0.50` → `open_preference = "avoid"`
- `topology_confidence < 0.15` → downgrades "open" to "neutral"
- Size buckets: small < midpoint(min, default), large >= midpoint(default, max)

- [ ] **Step 4: Verify consumer-side parsing**

Read capacity_planner `_discover_from_hive()` and `_score_candidate()`. Verify hints are parsed and applied correctly.

- [ ] **Step 5: Document findings and fix issues**

```bash
git add -A && git commit -m "fix: Pipeline 3 (channel-open hints) — correctness fixes"
```

---

### Task 10: Pipeline 4 — Membership → Member Hints

**Files:**
- Audit: `modules/rpc_commands.py` (export_hints), `/home/sat/bin/cl_revenue_ops/modules/hive_hints.py`, `/home/sat/bin/cl_revenue_ops/modules/fee_controller.py`

- [ ] **Step 1: Verify member flag accuracy**

In export_hints(), verify `hint["member"] = is_member` correctly reflects `database.get_all_members()`.

- [ ] **Step 2: Verify consumer 0-PPM policy**

Read fee_controller `_check_hive_member_fee()`. Verify:
- Returns 0 when `is_hive_member()` is True
- Returns None when False
- Grace period: holds 0-PPM for `ttl * 2` after hints go stale

- [ ] **Step 3: Resolve TTL documentation discrepancy**

Code uses `ttl * 2` (2x TTL). CLAUDE.md says "one additional TTL period". Fix the documentation to match the code (2x TTL = TTL for fresh + TTL grace).

- [ ] **Step 4: Document findings and fix issues**

```bash
git add -A && git commit -m "fix: Pipeline 4 (membership hints) — verify correctness, fix TTL docs"
```

---

### Task 11: Pipeline 5 — Fleet Gossip → Shared State

**Files:**
- Audit: `modules/gossip.py`, `modules/state_manager.py`, `modules/background_loops.py`

- [ ] **Step 1: Verify gossip data is real**

Read `_broadcast_gossip()` in background_loops.py gossip_loop. Verify the gossip payload is built from real `listfunds` / `listpeerchannels` data, not stale cache.

- [ ] **Step 2: Verify state merge correctness**

Read state_manager `process_gossip()`. Verify:
- Higher version wins
- State hash comparison works
- Missing fields in gossip payload don't corrupt existing state

- [ ] **Step 3: Verify anti-entropy**

Read STATE_HASH handler. Verify:
- Hash mismatch triggers FULL_SYNC request
- FULL_SYNC restores divergent state

- [ ] **Step 4: Verify stale cleanup**

Check if old peer states are cleaned up (peers gone for days shouldn't remain in HiveMap forever).

- [ ] **Step 5: Document findings and fix issues**

```bash
git add -A && git commit -m "fix: Pipeline 5 (fleet gossip) — verify state correctness"
```

---

### Task 12: Pipeline 6 — Traffic Intelligence → Confidence Gate

**Files:**
- Audit: `modules/traffic_intelligence.py`, `modules/rpc_commands.py` (export_hints), `/home/sat/bin/cl_revenue_ops/modules/hive_hints.py`

- [ ] **Step 1: Verify traffic profile source**

Read traffic_intelligence `store_local_profile()`. Verify profiles are based on real forward/routing data.

- [ ] **Step 2: Verify aggregation math**

Read `get_aggregated_profile()`. Verify:
- Confidence-weighted merging is correct
- Multiple reporters produce higher confidence
- `json.loads()` on peak_hours_utc is inside try/except

- [ ] **Step 3: Verify traffic_confidence population coverage**

Critical question: For peers that have corridor_role/competition_bias/rebalance_preference data, do they also have traffic_confidence? If not, all their hints are dead on the consumer side.

Check by reading export_hints(): which peers get traffic_confidence set, and which get corridor/rebalance data set. Are the sets the same?

- [ ] **Step 4: Verify consumer gating**

Read hive_hints.py `get_fee_bias()` lines 100-103 and `get_rebalance_bias()` lines 132-135. Verify:
- `traffic_confidence` missing or 0 → return 1.0 (neutral)
- This correctly prevents unvalidated hints from affecting decisions

- [ ] **Step 5: Document findings and fix issues**

If traffic_confidence is NOT populated for peers that have other hint data, this is a critical pipeline gap. Options:
- Set a default traffic_confidence for members (e.g., 0.5)
- Only export corridor/rebalance hints when traffic data exists
- Document as known limitation

```bash
git add -A && git commit -m "fix: Pipeline 6 (traffic confidence gate) — verify coverage and correctness"
```

---

### Task 13: Final Validation

- [ ] **Step 1: Run full cl-hive test suite**

```bash
python3 -m pytest tests/ -v --tb=short 2>&1 | tail -10
```

- [ ] **Step 2: Run cl_revenue_ops contract test**

```bash
cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_hive_contract.py -v 2>&1
```

- [ ] **Step 3: Verify no remaining stale references**

```bash
grep -rn 'except.*pass$' modules/*.py cl-hive.py | grep -v __pycache__ | wc -l
```

Should be zero or near-zero (all converted to logging).

- [ ] **Step 4: Push all fixes**

```bash
git push origin main
```

- [ ] **Step 5: Compile final hardening report**

Summary with:
- Total findings by severity per tier/pipeline
- Fixes applied
- Known limitations documented
- Security posture assessment
- Intelligence pipeline correctness assessment
