# Fee Coordination Transport Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Harden fee-coordination transport and ingestion paths so broadcast intelligence carries relay metadata, invalid traffic cannot burn dedupe state before validation, and msat parsing accepts CLN's structured/string forms in all routing-intelligence ingestion paths.

**Architecture:** Keep the existing relay and fee-intelligence subsystems, but fix the handoff points between them. The work stays scoped to `cl-hive.py` plus regression coverage, with tests written first for each transport bug and the minimal code changes applied afterward.

**Tech Stack:** Python, pytest, Core Lightning plugin RPC glue

---

### Task 1: Relay Metadata On Intelligence Broadcasts

**Files:**
- Modify: `cl-hive.py`
- Test: `tests/test_coordination_bugs.py`

**Step 1: Write the failing tests**

Add tests that prove origin broadcasts for stigmergic markers and pheromones attach `_relay` metadata before `sendcustommsg`, so downstream handlers can correctly classify relayed vs direct traffic.

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_coordination_bugs.py -q -p no:cacheprovider`
Expected: FAIL on the new relay-metadata regression tests

**Step 3: Write minimal implementation**

Update the broadcast helpers in `cl-hive.py` to prepare payloads with `_prepare_broadcast_payload()` before serialization so origin broadcasts include stable relay metadata.

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_coordination_bugs.py -q -p no:cacheprovider`
Expected: PASS

### Task 2: Validate Before Dedupe In Intelligence Handlers

**Files:**
- Modify: `cl-hive.py`
- Test: `tests/test_coordination_bugs.py`

**Step 1: Write the failing tests**

Add tests that prove invalid `STIGMERGIC_MARKER_BATCH` and `PHEROMONE_BATCH` messages do not consume dedupe state before sender, membership, freshness, and signature validation complete.

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_coordination_bugs.py -q -p no:cacheprovider`
Expected: FAIL on the new handler-ordering regression tests

**Step 3: Write minimal implementation**

Reorder the intelligence handlers so `_should_process_message()` executes only after the message passes membership, freshness, payload, and signature validation.

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_coordination_bugs.py -q -p no:cacheprovider`
Expected: PASS

### Task 3: Robust msat Parsing In Routing-Intelligence Ingestion

**Files:**
- Modify: `cl-hive.py`
- Test: `tests/test_fee_flow_bugs.py`

**Step 1: Write the failing tests**

Add tests that prove `_record_forward_for_fee_coordination()` and `hive_backfill_routing_intelligence()` accept CLN msat values when they arrive as strings like `"1234msat"` or nested dict forms.

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fee_flow_bugs.py -q -p no:cacheprovider`
Expected: FAIL on the new msat-parsing regression tests

**Step 3: Write minimal implementation**

Use `_parse_msat_value()` in the forward-event and backfill ingestion paths before computing ppm, revenue, and volume.

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fee_flow_bugs.py -q -p no:cacheprovider`
Expected: PASS

### Task 4: Final Verification

**Files:**
- Verify: `cl-hive.py`
- Verify: `tests/test_coordination_bugs.py`
- Verify: `tests/test_fee_flow_bugs.py`
- Verify: `tests/test_fee_coordination.py`
- Verify: `tests/test_fee_coordination_10_fixes.py`
- Verify: `tests/test_fee_coordination_polish.py`

**Step 1: Run targeted transport and ingestion tests**

Run: `python3 -m pytest tests/test_coordination_bugs.py tests/test_fee_flow_bugs.py -q -p no:cacheprovider`
Expected: PASS

**Step 2: Run the focused fee-coordination suite**

Run: `python3 -m pytest tests/test_fee_coordination.py tests/test_fee_coordination_10_fixes.py tests/test_fee_flow_bugs.py tests/test_fee_coordination_polish.py tests/test_coordination_bugs.py -q -p no:cacheprovider`
Expected: PASS

**Step 3: Review diff for scope**

Run: `git diff -- cl-hive.py tests/test_coordination_bugs.py tests/test_fee_flow_bugs.py docs/plans/2026-03-06-fee-coordination-transport-hardening.md`
Expected: only the planned files change for the intended fixes
