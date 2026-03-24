# Complete cl-hive Codebase Audit Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Systematically audit cl-hive for runtime bugs, architecture drift, and security issues. Fix critical/high immediately, defer medium/low.

**Architecture:** Three sequential sub-audits (runtime correctness → architecture alignment → security). Each produces a findings table. Sub-audit 1 fixes land before sub-audit 2 begins since fixes change the call graph.

**Tech Stack:** Python 3.10+, pytest, Core Lightning plugin framework, SQLite

---

## Task 0: Establish Test Baseline

**Files:**
- Check: `tests/` (all 38 test files)

- [ ] **Step 1: Run full test suite and identify failures**

Run: `python3 -m pytest tests/ --tb=short 2>&1 | grep -E 'FAILED|ERROR|passed|failed'`
Current known state: 793 passed, 2 failed, 1 skipped.

- [ ] **Step 2: Fix or delete failing tests**

Read each failing test to determine if it tests removed functionality (delete) or real code (fix).

- [ ] **Step 3: Verify clean baseline**

Run: `python3 -m pytest tests/ --tb=short 2>&1 | tail -3`
Expected: All pass (0 failures)

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "fix: establish clean test baseline for audit"
```

---

## Task 1: Database Method Call Audit

**Files:**
- Audit: all `modules/*.py` and `cl-hive.py`
- Fix: `modules/database.py`

- [ ] **Step 1: Extract all database method calls across the codebase**

```bash
grep -rn 'self\.db\.\|self\.database\.\|database\.' modules/*.py cl-hive.py \
  | grep -v __pycache__ | grep -v '\.pyc' \
  | sed 's/.*\(self\.db\.\|self\.database\.\|database\.\)\([a-z_]*\).*/\2/' \
  | sort -u > /tmp/db_calls.txt
```

- [ ] **Step 2: Extract all database method definitions**

```bash
grep -n 'def ' modules/database.py \
  | sed 's/.*def \([a-z_]*\).*/\1/' \
  | sort -u > /tmp/db_defs.txt
```

- [ ] **Step 3: Find missing methods**

```bash
comm -23 /tmp/db_calls.txt /tmp/db_defs.txt
```

Any output = methods called but not defined = runtime crash.

- [ ] **Step 4: For each missing method, add a stub or full implementation to database.py**

Follow the existing pattern: `self._get_connection()`, try/except, return safe default.

- [ ] **Step 5: Verify argument compatibility for critical methods**

For the 10 most-called database methods, cross-reference the caller's arguments against the method signature. Check for wrong keyword names, missing required args, or type mismatches.

```bash
# Example: find all callers of store_liquidity_need and compare to its def
grep -rn 'store_liquidity_need' modules/*.py
grep -A5 'def store_liquidity_need' modules/database.py
```

- [ ] **Step 6: Run tests**

Run: `python3 -m pytest tests/ --tb=short 2>&1 | tail -5`

- [ ] **Step 7: Commit**

```bash
git add modules/database.py && git commit -m "fix: restore missing database methods found in audit"
```

---

## Task 2: Dependency Injection Cross-Reference

**Files:**
- Audit: `cl-hive.py` (init dicts), `modules/protocol_handlers.py` (globals), `modules/background_loops.py` (globals)

- [ ] **Step 1: List all module-level None declarations in protocol_handlers.py**

```bash
grep -n '^[a-z_]* = None' modules/protocol_handlers.py
```

- [ ] **Step 2: List all keys in the init_protocol_handlers dict in cl-hive.py**

Read the dict around line 824 of cl-hive.py. Record every key.

- [ ] **Step 3: Find mismatches**

For each module-level None variable in protocol_handlers.py:
- Is it in the injection dict? If not → will be None at runtime → NameError when accessed
- Is it actually referenced in any function? If not → dead injection, remove

For each key in the injection dict:
- Does protocol_handlers.py declare it? If not → injection is silently lost

- [ ] **Step 4: Repeat for background_loops.py**

Same cross-reference for the background_loops injection dict (around line 864).

- [ ] **Step 5: Find all references to injected variables in protocol_handlers.py**

```bash
# For each declared global, check if it's actually used
for var in plugin database config shutdown_event our_pubkey handshake_mgr gossip_mgr state_manager intent_mgr contribution_mgr bridge relay_mgr fee_intel_mgr liquidity_coord peer_reputation_mgr yield_metrics_mgr rationalization_mgr strategic_positioning_mgr outbox_mgr traffic_intel_mgr outbox; do
  count=$(grep -c "\b${var}\b" modules/protocol_handlers.py)
  echo "$var: $count references"
done
```

- [ ] **Step 6: Remove unused injections and add missing ones**

- [ ] **Step 7: Run tests and commit**

```bash
python3 -m pytest tests/ --tb=short 2>&1 | tail -5
git add cl-hive.py modules/protocol_handlers.py modules/background_loops.py
git commit -m "fix: align dependency injection dicts with module globals"
```

---

## Task 3: Stale Variable and Import Audit

**Files:**
- Audit: all `modules/*.py` and `cl-hive.py`

- [ ] **Step 1: Find references to deleted modules**

```bash
grep -rn 'anticipatory_liquidity\|routing_intelligence\|phase6_ingest\|hive_bridge\|gossip_keeper\|realtime_surge' modules/*.py cl-hive.py \
  | grep -v __pycache__ | grep -v '\.pyc' | grep -v docs/ | grep -v plans/
```

Any matches = stale references to deleted modules.

- [ ] **Step 2: Find references to removed features**

```bash
grep -rn 'governance_mode\|GOVERNANCE_MODE\|advisor_mode\|failsafe_mode\|archon\|comms_active\|hive-comms\|CLBOSS\|trustedcoin' modules/*.py cl-hive.py \
  | grep -v __pycache__ | grep -v '\.pyc' | grep -v CHANGELOG | grep -v UPGRADE
```

- [ ] **Step 3: Check for undefined variable references in protocol_handlers.py**

Specifically look for variables used but not declared or injected:

```bash
# Find all bare name references that aren't function-local
grep -n 'initial_tier\|MEMBER_TIER\|BridgeStatus\|HiveMessageType' modules/protocol_handlers.py | head -20
```

Verify each is either imported or injected.

- [ ] **Step 4: Fix all stale references found**

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m pytest tests/ --tb=short 2>&1 | tail -5
git add -A && git commit -m "fix: remove stale references to deleted modules and features"
```

---

## Task 4: Dict Key Access Safety Audit

**Files:**
- Audit: `modules/protocol_handlers.py`, `modules/rpc_commands.py`, `cl-hive.py`

- [ ] **Step 1: Find all dict["key"] access patterns on external data**

```bash
grep -rn '\[\"[a-z_]*\"\]' modules/protocol_handlers.py modules/rpc_commands.py cl-hive.py \
  | grep -v __pycache__ | grep -v 'def \|import \|#' | head -50
```

Focus on data from: `payload.get`, `result.get`, RPC call returns, database query results.

- [ ] **Step 2: Classify each as safe or unsafe**

Safe: accessing a dict we just constructed locally
Unsafe: accessing RPC response, protocol payload, or database row with `dict["key"]` instead of `.get()`

- [ ] **Step 3: Fix unsafe accesses**

Change `data["key"]` to `data.get("key", default)` where the data source is external.

- [ ] **Step 4: Run tests and commit**

```bash
python3 -m pytest tests/ --tb=short 2>&1 | tail -5
git add -A && git commit -m "fix: use safe dict access for external data sources"
```

---

## Task 5: Background Loop Exception Audit

**Files:**
- Audit: `modules/background_loops.py`

- [ ] **Step 1: List all loop functions**

```bash
grep -n 'def .*_loop' modules/background_loops.py
```

- [ ] **Step 2: For each loop, verify the main body is wrapped in try/except**

Check that an unhandled exception in any single iteration cannot kill the thread. The pattern should be:

```python
while not shutdown_event.is_set():
    try:
        # ... loop body ...
    except Exception as e:
        plugin.log(f"Loop error: {e}", level='warn')
    shutdown_event.wait(interval)
```

- [ ] **Step 3: Identify loops with multiple RPC-blocking calls per iteration**

Flag loops that call multiple `plugin.rpc.*` or `bridge.safe_call()` in a single iteration — starvation risk if any blocks.

- [ ] **Step 4: Add missing exception guards. Document starvation risks.**

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m pytest tests/ --tb=short 2>&1 | tail -5
git add modules/background_loops.py && git commit -m "fix: harden background loop exception handling"
```

---

## Task 6: Architecture Alignment — Dead Code and Stale Docs

**Files:**
- Audit: all modules, CLAUDE.md, README.md, docs/, tests/

- [ ] **Step 1: Find dead functions (defined but never called)**

For each module, extract function defs and grep for call sites:

```bash
for mod in modules/*.py; do
  echo "=== $mod ==="
  grep -n 'def [a-z_]' "$mod" | while read line; do
    func=$(echo "$line" | sed 's/.*def \([a-z_]*\).*/\1/')
    count=$(grep -rn "\b${func}\b" modules/*.py cl-hive.py tests/*.py 2>/dev/null | grep -v "def ${func}" | grep -v __pycache__ | wc -l)
    if [ "$count" -eq 0 ]; then
      echo "  DEAD: $func (0 call sites)"
    fi
  done
done
```

- [ ] **Step 2: Update CLAUDE.md**

Fix:
- Module count (should match actual `ls modules/*.py | wc -l`)
- Module table (remove deleted modules, add any missing)
- Table counts (database tables)
- Remove stale pattern descriptions (ThreadSafeRpcProxy, RPC_LOCK if no longer used)
- Test file count

- [ ] **Step 3: Find stale RPC stubs**

```bash
grep -n 'no.op\|no_op\|stub\|removed\|simplification\|not.*needed' cl-hive.py | grep -i 'def\|method'
```

Identify RPCs that return hardcoded stubs for removed features. Delete them.

- [ ] **Step 4: Find unused imports across all modules**

```bash
for mod in modules/*.py; do
  python3 -c "
import ast, sys
with open('$mod') as f:
    tree = ast.parse(f.read())
imports = []
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        for alias in node.names:
            imports.append(alias.asname or alias.name)
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            imports.append(alias.asname or alias.name)
# This is a rough check - just flag for manual review
for name in imports:
    if name.startswith('_'):
        continue
    print(f'  import: {name}')
" 2>/dev/null
done
```

Manual review flagged imports for actual usage.

- [ ] **Step 5: Find stale test files**

```bash
# Test files that import deleted modules
grep -l 'anticipatory\|routing_intelligence\|phase6_ingest\|hive_bridge\|gossip_keeper' tests/*.py 2>/dev/null
```

Delete test files for removed modules.

- [ ] **Step 6: Commit all architecture fixes**

```bash
git add -A && git commit -m "refactor: remove dead code, stale docs, and unused imports"
```

---

## Task 7: Security Audit — Protocol and Membership

**Files:**
- Audit: `modules/protocol_handlers.py`, `modules/handshake.py`, `modules/protocol.py`, `cl-hive.py`

- [ ] **Step 1: Trace every protocol message handler for validation**

For each message type dispatched in cl-hive.py (search `msg_type == HiveMessageType.`):

| Message | Handler | Has signature check? | Has timestamp check? | Has membership check? | Has rate limit? |
|---------|---------|---------------------|---------------------|----------------------|----------------|

Fill in the table by reading each handler.

- [ ] **Step 2: Check membership flow completeness**

Trace the HELLO → CHALLENGE → ATTEST → WELCOME flow:
- Does HELLO require a channel? (should: yes)
- Does CHALLENGE require HELLO first? (should: yes, via pending request)
- Does ATTEST verify signature? (should: yes)
- Does WELCOME require outbound HELLO? (should: yes, via recent fix)
- Can any step be skipped or replayed?

- [ ] **Step 3: Check resource exhaustion vectors**

```bash
# Find unbounded dicts/lists that grow without cleanup
grep -rn 'Dict\[' modules/protocol_handlers.py modules/handshake.py | grep -v 'def \|import\|#'
grep -rn '_pending_\|_cache\|_rate_' modules/handshake.py modules/protocol_handlers.py
```

For each: is there a max size or TTL cleanup?

- [ ] **Step 4: Check relay amplification**

Read `_relay_message()` in protocol_handlers.py. Verify:
- TTL is decremented and checked
- Deduplication prevents loops
- A single message cannot cause unbounded relay

- [ ] **Step 5: Check ban bypass paths**

```bash
grep -rn 'is_banned' modules/protocol_handlers.py modules/handshake.py
```

For every handler that checks membership, verify it also checks ban status. A banned peer should not be able to:
- Send HELLO and get stored as pending
- Send GOSSIP/FULL_SYNC and have state merged
- Send any intelligence message and have it processed

- [ ] **Step 6: Document findings**

Create a security findings table with severity. Fix critical/high issues immediately.

- [ ] **Step 7: Commit security fixes**

```bash
git add -A && git commit -m "fix: address security audit findings"
```

---

## Task 8: Final Validation

- [ ] **Step 1: Run full test suite**

```bash
python3 -m pytest tests/ -v --tb=short 2>&1 | tail -10
```

Expected: All pass, 0 failures.

- [ ] **Step 2: Verify no remaining stale references**

```bash
grep -rn 'anticipatory\|routing_intelligence\|phase6_ingest\|GOVERNANCE_MODE\|comms_active\|CLBOSS\|trustedcoin' \
  modules/*.py cl-hive.py tests/*.py 2>/dev/null | grep -v __pycache__ | grep -v CHANGELOG | grep -v UPGRADE | grep -v plans/
```

Expected: No matches.

- [ ] **Step 3: Verify database method completeness**

```bash
comm -23 \
  <(grep -rn 'self\.db\.\|self\.database\.\|database\.' modules/*.py cl-hive.py | grep -v __pycache__ | sed 's/.*\(self\.db\.\|self\.database\.\|database\.\)\([a-z_]*\).*/\2/' | sort -u) \
  <(grep -n 'def ' modules/database.py | sed 's/.*def \([a-z_]*\).*/\1/' | sort -u)
```

Expected: No output (all called methods exist).

- [ ] **Step 4: Push all fixes**

```bash
git push origin main
```

- [ ] **Step 5: Compile final audit report**

Produce a summary with:
- Total findings by severity
- Fixes applied
- Deferred items
- Security posture assessment
