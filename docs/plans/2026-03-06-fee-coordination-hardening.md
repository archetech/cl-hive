# Fee Coordination Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Tighten fee coordination correctness and safety by fixing defense ordering, stale corridor ownership, signal duplication, and the adaptive learned-fee path.

**Architecture:** Keep the existing blended recommendation pipeline, but harden the invariants around it. The work stays local to `fee_coordination.py` and its direct ingestion points in `cl-hive.py`, with regression coverage added first for each behavior change.

**Tech Stack:** Python, pytest, Core Lightning plugin RPC glue

---

### Task 1: Defense Floor And Salience Bypass

**Files:**
- Modify: `modules/fee_coordination.py`
- Test: `tests/test_fee_coordination.py`

**Step 1: Write the failing tests**

Add tests that prove:
- active defense cannot be reduced by later time/centrality adjustments
- defense-critical changes bypass the normal salience revert/cooldown path

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fee_coordination.py -q -p no:cacheprovider`
Expected: FAIL on the new defense regression tests

**Step 3: Write minimal implementation**

Update `FeeCoordinationManager.get_fee_recommendation()` to:
- compute a defended floor immediately after applying defense
- preserve that minimum through later adjustments and bounds
- bypass non-salient revert when defense is active and the defended fee differs from current

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fee_coordination.py -q -p no:cacheprovider`
Expected: PASS

### Task 2: Corridor Refresh On Lookup

**Files:**
- Modify: `modules/fee_coordination.py`
- Test: `tests/test_fee_coordination_10_fixes.py`

**Step 1: Write the failing test**

Add a regression test proving `get_fee_recommendation()` refreshes expired corridor assignments before reading `get_fee_for_member()`.

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fee_coordination_10_fixes.py -q -p no:cacheprovider`
Expected: FAIL on the stale-corridor test

**Step 3: Write minimal implementation**

Make `FlowCorridorManager.get_fee_for_member()` TTL-aware by refreshing through `get_assignments()` when the snapshot is stale.

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fee_coordination_10_fixes.py -q -p no:cacheprovider`
Expected: PASS

### Task 3: Marker Volume Wiring And Gossip Dedupe

**Files:**
- Modify: `modules/fee_coordination.py`
- Modify: `cl-hive.py`
- Test: `tests/test_fee_flow_bugs.py`
- Test: `tests/test_fee_coordination_10_fixes.py`

**Step 1: Write the failing tests**

Add tests that prove:
- routing outcomes create markers using forwarded volume, not fee revenue
- repeated marker/pheromone gossip from the same reporter does not stack duplicate evidence

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fee_flow_bugs.py tests/test_fee_coordination_10_fixes.py -q -p no:cacheprovider`
Expected: FAIL on the new marker and dedupe tests

**Step 3: Write minimal implementation**

Update the forward ingestion and fee coordination APIs to:
- pass `volume_sats` separately from `revenue_sats`
- derive local marker strength from actual forwarded volume
- dedupe remote pheromones per `(peer_id, reporter_id, fee_ppm)` and dedupe markers per route/reporter/event fingerprint

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fee_flow_bugs.py tests/test_fee_coordination_10_fixes.py -q -p no:cacheprovider`
Expected: PASS

### Task 4: Learned Fee Usage In Adaptive Recommendation

**Files:**
- Modify: `modules/fee_coordination.py`
- Test: `tests/test_fee_coordination.py`
- Test: `tests/test_fee_coordination_10_fixes.py`

**Step 1: Write the failing tests**

Add tests that prove strong pheromone state pulls recommendations toward `_pheromone_fee`, not just the current fee.

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fee_coordination.py tests/test_fee_coordination_10_fixes.py -q -p no:cacheprovider`
Expected: FAIL on the new adaptive learned-fee regression tests

**Step 3: Write minimal implementation**

Change `AdaptiveFeeController.suggest_fee()` to use the stored learned fee when pheromone is strong, while preserving existing floor/ceiling bounds.

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fee_coordination.py tests/test_fee_coordination_10_fixes.py -q -p no:cacheprovider`
Expected: PASS

### Task 5: Final Verification

**Files:**
- Verify: `modules/fee_coordination.py`
- Verify: `cl-hive.py`
- Verify: `tests/test_fee_coordination.py`
- Verify: `tests/test_fee_coordination_10_fixes.py`
- Verify: `tests/test_fee_flow_bugs.py`
- Verify: `tests/test_fee_coordination_polish.py`
- Verify: `tests/test_coordination_bugs.py`

**Step 1: Run the focused fee-coordination suite**

Run: `python3 -m pytest tests/test_fee_coordination.py tests/test_fee_coordination_10_fixes.py tests/test_fee_flow_bugs.py tests/test_fee_coordination_polish.py tests/test_coordination_bugs.py -q -p no:cacheprovider`
Expected: PASS

**Step 2: Review diff for scope**

Run: `git diff -- modules/fee_coordination.py cl-hive.py tests/test_fee_coordination.py tests/test_fee_coordination_10_fixes.py tests/test_fee_flow_bugs.py`
Expected: only the planned files change for the intended fixes
