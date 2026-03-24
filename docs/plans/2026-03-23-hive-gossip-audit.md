# Hive Gossip Audit Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Produce a full security and correctness audit of the Hive gossip surface, including reproducible findings, negative-path coverage, and a written audit report.

**Architecture:** Treat gossip as one pipeline: outbound payload creation in `modules/gossip.py`, wire/schema/signature rules in `modules/protocol.py`, inbound enforcement in `modules/protocol_handlers.py`, relay/dedup in `modules/relay.py`, and merge/persistence in `modules/state_manager.py` plus `modules/database.py`. Re-verify older red-team issues first, then probe the currently under-covered edges: unsigned optional fields, relay trust boundaries, replay/rate-limit behavior, resource bounds, and state/persistence consistency.

**Tech Stack:** Python 3.12, Core Lightning plugin (`pyln-client`), SQLite, pytest, ripgrep

---

### Task 1: Establish audit scope, baseline, and output artifacts

**Files:**
- Create: `audits/2026-03-23_HIVE_GOSSIP_AUDIT.md`
- Inspect: `modules/gossip.py`
- Inspect: `modules/protocol.py`
- Inspect: `modules/protocol_handlers.py`
- Inspect: `modules/state_manager.py`
- Inspect: `modules/relay.py`
- Inspect: `tests/test_gossip.py`
- Inspect: `tests/test_state.py`
- Inspect: `tests/test_state_planner_bugs.py`
- Inspect: `tests/test_security.py`
- Inspect: `tests/test_cl_hive_fixes.py`
- Inspect: `audits/2026-01-08_RED_TEAM_FINDINGS_V2.md`

**Step 1: Create the audit report skeleton**

Create `audits/2026-03-23_HIVE_GOSSIP_AUDIT.md` with this structure:

```markdown
# Hive Gossip Audit

**Date:** 2026-03-23
**Scope:** Gossip, STATE_HASH, FULL_SYNC, relay, and state persistence

## Surface Inventory
- outbound payload creation
- schema/signature validation
- inbound handler gating
- relay/dedup path
- state merge and persistence

## Existing Coverage

## Findings

## Reproductions

## Test Gaps
```

**Step 2: Run the existing gossip/state/security baseline**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip.py tests/test_state.py tests/test_state_planner_bugs.py tests/test_security.py tests/test_cl_hive_fixes.py -v`

Expected: PASS. Record the exact pass count in the audit report so later failures are attributable to new audit probes, not unrelated breakage.

**Step 3: Capture the gossip surface map**

Run:
`rg -n "handle_gossip|handle_state_hash|handle_full_sync|process_gossip|process_state_hash|process_full_sync|create_gossip_payload|create_state_hash_payload|create_full_sync_payload|RelayManager" modules cl-hive.py tests`

Record the exact file/function inventory in `Surface Inventory`.

**Step 4: Record prior findings that were already supposed to be fixed**

From `audits/2026-01-08_RED_TEAM_FINDINGS_V2.md`, copy only the gossip-relevant historical items into `Existing Coverage`, especially:
- FULL_SYNC state poisoning
- gossip freshness checks
- FULL_SYNC cooldown/rate limiting

**Step 5: Commit the audit scaffold**

```bash
git add audits/2026-03-23_HIVE_GOSSIP_AUDIT.md
git commit -m "docs: start hive gossip audit"
```

### Task 2: Audit signed-field coverage and schema completeness

**Files:**
- Inspect: `modules/gossip.py:209-292`
- Inspect: `modules/protocol.py:396-700`
- Create: `tests/test_gossip_audit.py`

**Step 1: Write failing hypothesis tests for unsigned or weakly-validated fields**

Create targeted tests in `tests/test_gossip_audit.py`:

```python
def test_gossip_signing_payload_changes_when_addresses_change():
    payload = {
        "sender_id": "02" + "a" * 64,
        "timestamp": 100,
        "version": 1,
        "fleet_hash": "f" * 64,
        "capacity_sats": 1000,
        "available_sats": 500,
        "fee_policy": {},
        "topology": [],
        "addresses": ["1.2.3.4:9735"],
    }
    tampered = dict(payload, addresses=["9.9.9.9:9735"])

    assert get_gossip_signing_payload(payload) != get_gossip_signing_payload(tampered)


def test_gossip_signing_payload_changes_when_capabilities_change():
    payload = {
        "sender_id": "02" + "a" * 64,
        "timestamp": 100,
        "version": 1,
        "fleet_hash": "f" * 64,
        "capacity_sats": 1000,
        "available_sats": 500,
        "fee_policy": {},
        "topology": [],
        "capabilities": ["mcf"],
    }
    tampered = dict(payload, capabilities=["fake-cap"])

    assert get_gossip_signing_payload(payload) != get_gossip_signing_payload(tampered)


def test_state_hash_signing_payload_changes_when_membership_hash_changes():
    payload = {
        "sender_id": "02" + "a" * 64,
        "fleet_hash": "f" * 64,
        "membership_hash": "1" * 64,
        "peer_count": 2,
        "timestamp": 100,
    }
    tampered = dict(payload, membership_hash="2" * 64)

    assert get_state_hash_signing_payload(payload) != get_state_hash_signing_payload(tampered)
```

**Step 2: Run the hypothesis tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip_audit.py -v`

Expected: At least the address/capability/membership-hash signing tests fail on current code if those fields are truly outside the signature contract.

**Step 3: Inspect schema validators for optional-field blind spots**

Check whether `validate_gossip()` and `validate_full_sync()` enforce bounds and type checks for:
- `addresses`
- `capabilities`
- `membership_hash`
- `members`
- future additive fields that handlers later trust

Record confirmed blind spots in `Findings` or `Test Gaps`.

**Step 4: Add one direct handler proof for any confirmed unsigned field**

If Task 2 proves a field is unsigned and later trusted, add a direct handler test like:

```python
def test_handle_gossip_does_not_accept_tampered_addresses_with_valid_signature(...):
    ...
```

Only keep the test if it demonstrates a real trust-boundary defect, not just an abstract signing mismatch.

**Step 5: Commit the audit probes**

```bash
git add tests/test_gossip_audit.py audits/2026-03-23_HIVE_GOSSIP_AUDIT.md
git commit -m "test: probe hive gossip signed-field coverage"
```

### Task 3: Audit handler trust boundaries and relay semantics

**Files:**
- Inspect: `modules/protocol_handlers.py:429-724`
- Inspect: `modules/relay.py:161-417`
- Modify: `tests/test_gossip_audit.py`

**Step 1: Write targeted relay and membership-gating tests**

Add tests like:

```python
def test_handle_gossip_rejects_relayed_message_from_non_member_relay(...):
    ...


def test_handle_gossip_rejects_banned_relay_peer(...):
    ...


def test_handle_full_sync_rejects_sender_id_peer_id_mismatch(...):
    ...


def test_handle_state_hash_rejects_non_member_before_process_state_hash(...):
    ...
```

Use `MagicMock()` for `gossip_mgr` so each test proves the handler never calls `process_gossip()`, `process_state_hash()`, or `process_full_sync()` when the trust gate should fail.

**Step 2: Run only the new trust-boundary tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip_audit.py -k "relay or non_member or sender_id" -v`

Expected: PASS for existing hardened paths, FAIL only where relay or sender validation still has gaps.

**Step 3: Verify relay metadata cannot silently drift from signed sender identity**

Inspect:
- `_validate_relay_sender()`
- `RelayManager.prepare_for_broadcast()`
- `RelayManager.prepare_for_relay()`
- `RelayManager.should_process()`

Explicitly answer in the audit report:
- Is `origin` trusted?
- Is `origin` signed or only metadata?
- Can a relayed payload alter `sender_id`-adjacent semantics without invalidating the message signature?

**Step 4: Add a manual reproduction snippet for any confirmed defect**

For any real issue, add a short repro block under `Reproductions` showing:
- crafted payload
- expected handler return
- actual side effect or missing rejection

**Step 5: Commit the relay audit updates**

```bash
git add tests/test_gossip_audit.py audits/2026-03-23_HIVE_GOSSIP_AUDIT.md
git commit -m "test: audit hive gossip relay trust boundaries"
```

### Task 4: Audit replay, freshness, and rate-limit behavior

**Files:**
- Inspect: `modules/gossip.py:303-520`
- Inspect: `modules/protocol_handlers.py:429-724`
- Modify: `tests/test_gossip_audit.py`
- Inspect: `tests/test_state_planner_bugs.py`
- Inspect: `tests/test_cl_hive_fixes.py`

**Step 1: Write focused edge-case tests**

Add:

```python
def test_handle_gossip_rejects_stale_payload_before_signature_check(...):
    ...


def test_handle_full_sync_rejects_future_timestamp_before_signature_check(...):
    ...


def test_full_sync_rate_limit_prunes_old_sender_entries(...):
    ...


def test_relay_dedup_window_covers_gossip_freshness_window(...):
    ...
```

The first two tests should assert `plugin.rpc.checkmessage.assert_not_called()` so the audit proves stale payloads are rejected before expensive signature work.

**Step 2: Run the replay/rate-limit slice**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip_audit.py -k "stale or future or rate_limit or dedup" -v`

Expected: PASS where replay/rate-limit logic is coherent; FAIL if cooldown, dedup, or freshness ordering is inconsistent.

**Step 3: Manually compare timing constants**

Record whether these windows are intentionally aligned or drifting:
- `MAX_GOSSIP_AGE_SECONDS`
- `MAX_STATE_HASH_AGE_SECONDS`
- `FULL_SYNC_COOLDOWN`
- `DEDUP_EXPIRY_SECONDS`

Call out any mismatch that creates:
- duplicate reprocessing
- needless full-sync churn
- stale relay acceptance

**Step 4: Add findings and impact notes**

For each confirmed issue, document:
- exploit path
- whether it is CPU, DB, memory, or convergence risk
- whether it requires member credentials or only connectivity

**Step 5: Commit the replay/rate-limit audit**

```bash
git add tests/test_gossip_audit.py audits/2026-03-23_HIVE_GOSSIP_AUDIT.md
git commit -m "test: audit hive gossip replay and rate limits"
```

### Task 5: Audit state-merge correctness and persistence boundaries

**Files:**
- Inspect: `modules/state_manager.py:282-650`
- Inspect: `modules/database.py`
- Modify: `tests/test_gossip_audit.py`
- Modify: `tests/test_state.py`

**Step 1: Write merge-integrity hypothesis tests**

Add:

```python
def test_full_sync_validate_limit_matches_process_limit():
    payload = {
        "sender_id": "02" + "a" * 64,
        "fleet_hash": "",
        "timestamp": 100,
        "signature": "signedpayload",
        "states": [{}] * 501,
    }

    assert validate_full_sync(payload) is False


def test_state_hash_payload_signature_contract_covers_all_divergence_inputs(...):
    ...


def test_handle_full_sync_does_not_partially_persist_invalid_state_batch(...):
    ...
```

The goal here is to prove whether the handler, validator, and `StateManager.apply_full_sync()` all enforce the same limits and whether rejected batches can still partially mutate local or persisted state.

**Step 2: Run the merge-integrity slice**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip_audit.py tests/test_state.py -k "full_sync or state_hash or persist" -v`

Expected: PASS if merge and persistence boundaries are consistent. Any partial-write or inconsistent-limit behavior should be recorded as a finding.

**Step 3: Inspect DB write ordering**

Trace:
- `GossipManager.process_gossip()`
- `StateManager.update_peer_state()`
- `StateManager.apply_full_sync()`
- `HiveDatabase.update_hive_state()`

Answer in the audit report:
- Are stale states rejected before DB writes?
- Can invalid optional fields still reach DB or auto-connect side effects?
- Does restart reload preserve the same invariants as live gossip?

**Step 4: Add at least one restart/persistence check**

If no existing test covers it, add:

```python
def test_rejected_gossip_does_not_survive_restart_reload(...):
    ...
```

Use a real temporary `HiveDatabase`, not a mock, so the audit proves persistence behavior.

**Step 5: Commit the merge/persistence audit**

```bash
git add tests/test_gossip_audit.py tests/test_state.py audits/2026-03-23_HIVE_GOSSIP_AUDIT.md
git commit -m "test: audit hive gossip merge and persistence behavior"
```

### Task 6: Audit resource-exhaustion and payload-bound defenses

**Files:**
- Inspect: `modules/protocol.py`
- Inspect: `modules/gossip.py`
- Inspect: `modules/state_manager.py`
- Modify: `tests/test_gossip_audit.py`

**Step 1: Write bounds tests for oversized-but-schema-adjacent payloads**

Add:

```python
def test_validate_gossip_rejects_excessive_capabilities_array():
    ...


def test_validate_gossip_rejects_oversized_addresses():
    ...


def test_handle_gossip_does_not_auto_connect_from_untrusted_optional_field(...):
    ...


def test_full_sync_large_state_batch_rejected_at_handler_boundary(...):
    ...
```

Use explicit array lengths and string sizes, not vague “large input.”

**Step 2: Run the resource-bounds slice**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip_audit.py -k "oversized or large or capabilities or addresses" -v`

Expected: PASS if bounds are enforced. FAIL if optional fields bypass validation or handler-side size limits.

**Step 3: Reconcile validator limits**

Document mismatches such as:
- `validate_full_sync()` max `states` length
- `GossipManager.process_full_sync()` max `states` length
- `StateManager._validate_state_entry()` field limits

Any mismatch should be treated as an audit finding or at minimum a hardening recommendation.

**Step 4: Update the audit report with exploitability**

For each confirmed bound issue, mark whether it is:
- pre-auth
- member-only
- relay-assisted
- persistence-amplified

**Step 5: Commit the resource audit**

```bash
git add tests/test_gossip_audit.py audits/2026-03-23_HIVE_GOSSIP_AUDIT.md
git commit -m "test: audit hive gossip resource bounds"
```

### Task 7: Synthesize findings and verify the final audit package

**Files:**
- Finalize: `audits/2026-03-23_HIVE_GOSSIP_AUDIT.md`
- Finalize: `tests/test_gossip_audit.py`

**Step 1: Run the full gossip audit suite**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip.py tests/test_gossip_audit.py tests/test_state.py tests/test_state_planner_bugs.py tests/test_security.py tests/test_cl_hive_fixes.py -v`

Expected: PASS for confirmed invariants. If a hypothesis test is intentionally documenting a real defect, convert it to a reproduction snippet and do not leave the branch with a knowingly failing suite.

**Step 2: Finalize the audit report**

The report must include:
- executive summary
- confirmed findings ordered by severity
- exact file/line references
- exploit sketches
- whether the issue is already covered by tests
- recommended next action: ignore, harden, or fix immediately

**Step 3: Verify no speculative findings remain**

Every reported issue must have one of:
- a passing invariant test proving safety
- a failing/then-reproduced hypothesis
- a direct code-path proof with exact source references

Delete any audit note that is still only a suspicion.

**Step 4: Commit the finished audit**

```bash
git add audits/2026-03-23_HIVE_GOSSIP_AUDIT.md tests/test_gossip_audit.py
git commit -m "docs: complete hive gossip audit"
```

**Step 5: Prepare handoff summary**

Summarize:
- number of confirmed findings
- highest-severity finding
- tests added
- whether follow-up remediation work should be a separate plan

## Notes

- Keep this audit focused on gossip/state-sync/relay behavior. Do not drift into membership, intent, or planner issues unless they are directly reachable from the gossip surface.
- Re-check historical red-team gossip findings first so the audit distinguishes regressions from already-closed items.
- Pay special attention to fields that are transmitted and later trusted but not obviously included in the signing payload:
  - `addresses`
  - `capabilities`
  - budget fields
  - `membership_hash`
- Treat validator/manager limit mismatches as real audit targets, not style issues.
- If the audit proves a concrete defect, prefer a minimal regression test plus written reproduction over speculative prose.
