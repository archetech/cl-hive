# Member Broadcast Gateway Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace all ad hoc member-broadcast paths in `cl-hive.py` with one relay-aware gateway that enforces explicit transport policy and can use reliable/outbox delivery where appropriate.

**Architecture:** Add a single `_broadcast_member_message(...)` transport gateway in `cl-hive.py`, then migrate every member-broadcast caller onto it by policy bucket. Keep message-specific signing in callers, add `_relay` only in the gateway, and preserve legacy wrappers only as thin adapters during the migration.

**Tech Stack:** Python, pytest, Core Lightning plugin RPC glue, cl-hive reliable outbox

---

### Task 1: Add Gateway Regression Tests

**Files:**
- Create: `tests/test_member_broadcast_gateway.py`
- Modify: `cl-hive.py`

**Step 1: Write the failing test**

Create `tests/test_member_broadcast_gateway.py` with a lightweight `pyln.client` stub loader for `cl-hive.py`, then add four gateway-focused regressions:

```python
def test_gateway_adds_relay_metadata_for_payload_input():
    result = cl_hive._broadcast_member_message(
        msg_type=HiveMessageType.PHEROMONE_BATCH,
        payload={"reporter_id": our_pubkey, "timestamp": now, "signature": "sig", "pheromones": []},
        reliability="direct",
        failure_policy="best_effort",
        log_label="pheromone_batch",
    )
    sent_payload = decode_last_sendcustommsg()
    assert sent_payload["_relay"]["origin"] == our_pubkey


def test_gateway_normalizes_bytes_input_before_send():
    raw_msg = serialize(HiveMessageType.GOSSIP, {"sender_id": our_pubkey, "signature": "sig"})
    cl_hive._broadcast_member_message(
        message_bytes=raw_msg,
        reliability="direct",
        failure_policy="best_effort",
        log_label="gossip",
    )
    sent_payload = decode_last_sendcustommsg()
    assert "_relay" in sent_payload


def test_gateway_rejects_fail_closed_direct_policy():
    with pytest.raises(ValueError):
        cl_hive._broadcast_member_message(
            msg_type=HiveMessageType.GOSSIP,
            payload={"sender_id": our_pubkey},
            reliability="direct",
            failure_policy="fail_closed",
            log_label="gossip",
        )


def test_gateway_fail_closed_reliable_reports_partial_enqueue_failure():
    cl_hive.outbox_mgr.enqueue.return_value = 1
    result = cl_hive._broadcast_member_message(
        msg_type=HiveMessageType.FULL_SYNC,
        payload={"sender_id": our_pubkey, "signature": "sig"},
        reliability="reliable",
        failure_policy="fail_closed",
        log_label="full_sync",
    )
    assert result["ok"] is False
    assert result["failed"] == 1
```

**Step 2: Run test to verify it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_member_broadcast_gateway.py -q -p no:cacheprovider`
Expected: FAIL because `_broadcast_member_message(...)` does not exist yet

**Step 3: Write minimal implementation**

In `cl-hive.py`, add the gateway and a small normalization helper:

```python
def _normalize_member_broadcast_bytes(msg_type=None, payload=None, message_bytes=None, relay_ttl=3):
    if payload is not None:
        prepared = _prepare_broadcast_payload(dict(payload), ttl=relay_ttl)
        return serialize(msg_type, prepared)
    parsed_type, parsed_payload = deserialize(message_bytes)
    prepared = _prepare_broadcast_payload(dict(parsed_payload), ttl=relay_ttl)
    return serialize(parsed_type, prepared)


def _broadcast_member_message(msg_type=None, payload=None, message_bytes=None, *,
                              reliability="direct", failure_policy="best_effort",
                              targets=None, relay_ttl=3, log_label="member_broadcast"):
    if failure_policy == "fail_closed" and reliability != "reliable":
        raise ValueError("fail_closed broadcasts must use reliable delivery")
    ...
```

Return a dict with `ok`, `attempted`, `queued`, `sent`, `failed`, `mode`, and `policy`.

**Step 4: Run test to verify it passes**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_member_broadcast_gateway.py -q -p no:cacheprovider`
Expected: PASS

**Step 5: Commit**

```bash
git add cl-hive.py tests/test_member_broadcast_gateway.py
git commit -m "feat: add member broadcast gateway"
```

### Task 2: Rebase Legacy Broadcast Helpers On The Gateway

**Files:**
- Modify: `cl-hive.py`
- Modify: `tests/test_member_broadcast_gateway.py`

**Step 1: Write the failing test**

Add regressions proving the legacy helpers now delegate into the gateway instead of owning transport logic themselves:

```python
def test_broadcast_to_members_delegates_to_gateway():
    with patch.object(cl_hive, "_broadcast_member_message", return_value={"sent": 2, "ok": True}) as gateway:
        sent = cl_hive._broadcast_to_members(b"abc")
    gateway.assert_called_once()
    assert sent == 2


def test_reliable_broadcast_delegates_to_gateway():
    with patch.object(cl_hive, "_broadcast_member_message", return_value={"queued": 3, "ok": True}) as gateway:
        cl_hive._reliable_broadcast(HiveMessageType.BAN_VOTE, {"voter_id": "x"})
    assert gateway.call_args.kwargs["reliability"] == "reliable"
    assert gateway.call_args.kwargs["failure_policy"] == "fail_closed"
```

**Step 2: Run test to verify it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_member_broadcast_gateway.py -q -p no:cacheprovider`
Expected: FAIL because the wrappers still own their old send paths

**Step 3: Write minimal implementation**

Convert the compatibility helpers in `cl-hive.py` into thin adapters:

```python
def _broadcast_to_members(message_bytes: bytes) -> int:
    result = _broadcast_member_message(
        message_bytes=message_bytes,
        reliability="direct",
        failure_policy="best_effort",
        log_label="legacy_broadcast",
    )
    return result["sent"]


def _reliable_broadcast(msg_type: HiveMessageType, payload: Dict, msg_id: Optional[str] = None) -> None:
    _broadcast_member_message(
        msg_type=msg_type,
        payload=payload,
        reliability="reliable",
        failure_policy="fail_closed",
        log_label=msg_type.name.lower(),
    )
```

Preserve current return contracts for callers that expect integer counts or `None`.

**Step 4: Run test to verify it passes**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_member_broadcast_gateway.py -q -p no:cacheprovider`
Expected: PASS

**Step 5: Commit**

```bash
git add cl-hive.py tests/test_member_broadcast_gateway.py
git commit -m "refactor: route legacy broadcast helpers through gateway"
```

### Task 3: Migrate Fail-Closed Reliable Member Broadcasts

**Files:**
- Modify: `cl-hive.py`
- Modify: `tests/test_member_broadcast_gateway.py`
- Verify: `docs/plans/2026-03-06-member-broadcast-gateway-design.md`

**Step 1: Write the failing test**

Add representative regressions for correctness-critical member broadcasts. Cover at least these call sites:
- `_broadcast_full_sync_to_members()`
- `_broadcast_expansion_nomination()`
- `_broadcast_mcf_solution()`

```python
def test_full_sync_uses_reliable_fail_closed_gateway():
    with patch.object(cl_hive, "_broadcast_member_message", return_value={"ok": True, "queued": 2}):
        cl_hive._broadcast_full_sync_to_members(plugin)
    assert gateway.call_args.kwargs["reliability"] == "reliable"
    assert gateway.call_args.kwargs["failure_policy"] == "fail_closed"


def test_expansion_nomination_uses_reliable_fail_closed_gateway():
    ...


def test_mcf_solution_uses_reliable_fail_closed_gateway():
    ...
```

**Step 2: Run test to verify it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_member_broadcast_gateway.py -q -p no:cacheprovider`
Expected: FAIL because these callers still send directly or via old wrappers

**Step 3: Write minimal implementation**

Change each representative caller to invoke `_broadcast_member_message(...)` directly with explicit policy:

```python
result = _broadcast_member_message(
    message_bytes=full_sync_msg,
    reliability="reliable",
    failure_policy="fail_closed",
    log_label="full_sync",
)
```

Use payload input instead of `message_bytes` when the function already has the signed payload in dict form. Keep signing logic in the caller.

**Step 4: Run test to verify it passes**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_member_broadcast_gateway.py -q -p no:cacheprovider`
Expected: PASS

**Step 5: Commit**

```bash
git add cl-hive.py tests/test_member_broadcast_gateway.py
git commit -m "refactor: migrate critical broadcasts to reliable gateway"
```

### Task 4: Migrate Best-Effort Reliable Member Broadcasts

**Files:**
- Modify: `cl-hive.py`
- Modify: `tests/test_member_broadcast_gateway.py`

**Step 1: Write the failing test**

Add representative regressions for event-driven but non-fatal broadcasts. Cover at least these call sites:
- `_broadcast_circular_flow_alerts()`
- `_broadcast_our_positioning_proposals()`
- `_broadcast_our_close_proposals()`

```python
def test_positioning_proposals_use_best_effort_reliable_gateway():
    with patch.object(cl_hive, "_broadcast_member_message", return_value={"ok": True, "queued": 3, "failed": 1}) as gateway:
        cl_hive._broadcast_our_positioning_proposals()
    assert gateway.call_args.kwargs["reliability"] == "reliable"
    assert gateway.call_args.kwargs["failure_policy"] == "best_effort"
```

**Step 2: Run test to verify it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_member_broadcast_gateway.py -q -p no:cacheprovider`
Expected: FAIL because these callers still own raw member loops

**Step 3: Write minimal implementation**

Convert these broadcasters to explicit best-effort reliable dispatch:

```python
_broadcast_member_message(
    message_bytes=msg,
    reliability="reliable",
    failure_policy="best_effort",
    log_label="positioning_proposal",
)
```

Preserve current loop semantics for one-message-per-proposal broadcasters by calling the gateway once per proposal.

**Step 4: Run test to verify it passes**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_member_broadcast_gateway.py -q -p no:cacheprovider`
Expected: PASS

**Step 5: Commit**

```bash
git add cl-hive.py tests/test_member_broadcast_gateway.py
git commit -m "refactor: migrate advisory broadcasts to reliable gateway"
```

### Task 5: Migrate Best-Effort Direct Member Broadcasts

**Files:**
- Modify: `cl-hive.py`
- Modify: `tests/test_member_broadcast_gateway.py`
- Verify: `tests/test_coordination_bugs.py`
- Verify: `tests/test_fee_flow_bugs.py`

**Step 1: Write the failing test**

Add representative regressions for high-frequency telemetry and learning broadcasts. Cover at least these call sites:
- `_broadcast_our_fee_intelligence()`
- `_broadcast_our_stigmergic_markers()`
- `_broadcast_our_pheromones()`
- `_broadcast_our_yield_metrics()`
- `_broadcast_our_temporal_patterns()`
- `_broadcast_our_corridor_values()`
- `_broadcast_our_coverage_analysis()`
- `_broadcast_health_report()`
- `_broadcast_liquidity_needs()`
- gossip loop member broadcast block

```python
def test_fee_intelligence_uses_best_effort_direct_gateway():
    ...


def test_health_report_uses_best_effort_direct_gateway():
    ...


def test_gossip_broadcast_uses_best_effort_direct_gateway():
    ...
```

**Step 2: Run test to verify it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_member_broadcast_gateway.py tests/test_coordination_bugs.py tests/test_fee_flow_bugs.py -q -p no:cacheprovider`
Expected: FAIL because the remaining broadcast loops have not been migrated

**Step 3: Write minimal implementation**

Replace the remaining raw `_get_broadcast_targets()` loops with gateway calls:

```python
_broadcast_member_message(
    message_bytes=msg,
    reliability="direct",
    failure_policy="best_effort",
    log_label="fee_intelligence_snapshot",
)
```

For payload-first callers, use `payload=` instead of `message_bytes=`. Keep current logging text, but derive send/queue counts from the gateway result.

**Step 4: Run test to verify it passes**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_member_broadcast_gateway.py tests/test_coordination_bugs.py tests/test_fee_flow_bugs.py -q -p no:cacheprovider`
Expected: PASS

**Step 5: Commit**

```bash
git add cl-hive.py tests/test_member_broadcast_gateway.py tests/test_coordination_bugs.py tests/test_fee_flow_bugs.py
git commit -m "refactor: migrate telemetry broadcasts to gateway"
```

### Task 6: Remove Raw Member-Broadcast Loops And Verify Scope

**Files:**
- Modify: `cl-hive.py`
- Verify: `tests/test_member_broadcast_gateway.py`
- Verify: `tests/test_fee_coordination.py`
- Verify: `tests/test_fee_coordination_10_fixes.py`
- Verify: `tests/test_fee_flow_bugs.py`
- Verify: `tests/test_fee_coordination_polish.py`
- Verify: `tests/test_coordination_bugs.py`

**Step 1: Write the failing test**

Add a guardrail regression or structural assertion for the migration endpoint:

```python
def test_no_raw_member_loops_remain_outside_gateway():
    content = Path("cl-hive.py").read_text()
    assert "for member in _get_broadcast_targets():" not in content_without_gateway_helpers
```

If that structural test is too brittle, replace it with a command-based verification in Step 4 and keep the test file focused on behavior.

**Step 2: Run test to verify it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_member_broadcast_gateway.py -q -p no:cacheprovider`
Expected: FAIL or remain pending until the final caller-side loops are removed

**Step 3: Write minimal implementation**

Finish the migration by removing any remaining caller-side `_get_broadcast_targets()` loops and routing them through `_broadcast_member_message(...)`. Leave `_get_broadcast_targets()` itself intact as the target selector used by the gateway.

**Step 4: Run test to verify it passes**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_member_broadcast_gateway.py -q -p no:cacheprovider`
Expected: PASS

Run: `rg -n "for member in _get_broadcast_targets\(" cl-hive.py`
Expected: only the gateway helper uses `_get_broadcast_targets()` for dispatch

Run: `rg -n "_broadcast_to_members\(" cl-hive.py`
Expected: helper definition only, or no remaining caller-side uses

**Step 5: Commit**

```bash
git add cl-hive.py tests/test_member_broadcast_gateway.py
git commit -m "refactor: remove duplicate member broadcast loops"
```

### Task 7: Final Verification

**Files:**
- Verify: `cl-hive.py`
- Verify: `tests/test_member_broadcast_gateway.py`
- Verify: `tests/test_coordination_bugs.py`
- Verify: `tests/test_fee_flow_bugs.py`
- Verify: `tests/test_fee_coordination.py`
- Verify: `tests/test_fee_coordination_10_fixes.py`
- Verify: `tests/test_fee_coordination_polish.py`
- Verify: `docs/plans/2026-03-06-member-broadcast-gateway-design.md`

**Step 1: Run the gateway-focused test slice**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_member_broadcast_gateway.py tests/test_coordination_bugs.py tests/test_fee_flow_bugs.py -q -p no:cacheprovider`
Expected: PASS

**Step 2: Run the focused fee-coordination suite**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_fee_coordination.py tests/test_fee_coordination_10_fixes.py tests/test_fee_flow_bugs.py tests/test_fee_coordination_polish.py tests/test_coordination_bugs.py tests/test_member_broadcast_gateway.py -q -p no:cacheprovider`
Expected: PASS

**Step 3: Review diff for scope**

Run: `git diff -- cl-hive.py tests/test_member_broadcast_gateway.py tests/test_coordination_bugs.py tests/test_fee_flow_bugs.py docs/plans/2026-03-06-member-broadcast-gateway-design.md docs/plans/2026-03-06-member-broadcast-gateway.md`
Expected: only the planned files change for the intended migration
