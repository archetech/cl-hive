# Hive Gossip Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Harden Hive gossip/state sync against the March 23, 2026 audit findings while remaining backward-compatible with already-deployed nodes.

**Architecture:** Keep the current `GOSSIP`, `STATE_HASH`, and `FULL_SYNC` message types and legacy signatures for compatibility, but add stronger v2 integrity fields that upgraded nodes verify. Legacy payloads remain parseable, but upgraded receivers sharply reduce what legacy messages can mutate: legacy `GOSSIP` may update only core state, legacy `STATE_HASH` treats `membership_hash` as advisory, and legacy `FULL_SYNC` may update at most the sender's own state row and must ignore membership rows. Persistence is hardened so remote timestamps survive restart and state-count limits are consistent across validation and processing.

**Tech Stack:** Python 3.12, Core Lightning plugin (`pyln-client`), SQLite, `pytest`, `unittest.mock`

---

### Task 1: Add v2 integrity helpers and shared sync limits

**Files:**
- Modify: `modules/protocol.py`
- Modify: `modules/gossip.py`
- Create: `tests/test_gossip_protocol_hardening.py`

**Step 1: Write the failing helper tests**

Add focused tests proving the new helpers must cover the audited fields:

```python
def test_gossip_signing_payload_v2_changes_when_addresses_change():
    payload = {..., "addresses": ["1.2.3.4:9735"]}
    tampered = dict(payload, addresses=["9.9.9.9:9735"])
    assert get_gossip_signing_payload_v2(payload) != get_gossip_signing_payload_v2(tampered)


def test_state_hash_signing_payload_v2_changes_when_membership_hash_changes():
    payload = {..., "membership_hash": "1" * 64}
    tampered = dict(payload, membership_hash="2" * 64)
    assert get_state_hash_signing_payload_v2(payload) != get_state_hash_signing_payload_v2(tampered)


def test_full_sync_states_hash_v2_changes_when_state_contents_change():
    assert compute_full_sync_states_hash_v2(states_a) != compute_full_sync_states_hash_v2(states_b)
```

**Step 2: Run the new helper tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip_protocol_hardening.py -k "payload_v2 or states_hash_v2" -v`

Expected: FAIL because the new helpers do not exist yet.

**Step 3: Implement the minimal v2 helper layer**

In `modules/protocol.py`:

- Add one shared network boundary constant, for example:

```python
MAX_FULL_SYNC_STATES = 500
```

- Add canonical v2 hashing/signing helpers:

```python
def compute_gossip_data_hash_v2(payload: Dict[str, Any]) -> str: ...
def get_gossip_signing_payload_v2(payload: Dict[str, Any]) -> str: ...
def get_state_hash_signing_payload_v2(payload: Dict[str, Any]) -> str: ...
def compute_full_sync_states_hash_v2(states: list) -> str: ...
def compute_full_sync_members_hash_v2(members: list) -> str: ...
def get_full_sync_signing_payload_v2(payload: Dict[str, Any]) -> str: ...
```

- Normalize lists before hashing where order is not semantically important:
  - `topology`
  - `addresses`
  - `capabilities`

In `modules/gossip.py`:

- Import and use the shared `MAX_FULL_SYNC_STATES` instead of the local hard-coded 2000.

**Step 4: Re-run the helper tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip_protocol_hardening.py -k "payload_v2 or states_hash_v2" -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add modules/protocol.py modules/gossip.py tests/test_gossip_protocol_hardening.py
git commit -m "fix: add gossip sync v2 integrity helpers"
```

### Task 2: Harden GOSSIP optional-field trust

**Files:**
- Modify: `modules/protocol_handlers.py`
- Modify: `modules/gossip.py`
- Modify: `tests/test_gossip_protocol_hardening.py`

**Step 1: Write the failing GOSSIP handler tests**

Add tests for both compatibility modes:

```python
def test_legacy_relayed_gossip_drops_addresses_and_skips_autoconnect(...):
    ...
    assert db.get_member(sender)["addresses"] is None
    plugin.rpc.connect.assert_not_called()


def test_v2_relayed_gossip_with_valid_signature_v2_persists_addresses(...):
    ...
    assert json.loads(db.get_member(sender)["addresses"]) == ["1.2.3.4:9735"]


def test_legacy_gossip_ignores_capabilities_and_budget_side_fields(...):
    ...
```

**Step 2: Run the GOSSIP hardening tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip_protocol_hardening.py -k "gossip and (legacy or signature_v2)" -v`

Expected: FAIL because the current handler still trusts legacy optional fields.

**Step 3: Implement legacy-safe and v2-trusted GOSSIP handling**

In `modules/protocol_handlers.py`:

- Add a helper that decides whether a GOSSIP payload has valid v2 proof.
- For legacy-only payloads, build a sanitized copy before calling `gossip_mgr.process_gossip()`:

```python
sanitized = dict(payload)
sanitized.pop("addresses", None)
sanitized.pop("capabilities", None)
sanitized.pop("budget_available_sats", None)
sanitized.pop("budget_reserved_until", None)
sanitized.pop("budget_last_update", None)
```

- Use the sanitized copy for persistence and side effects.
- Do not auto-connect from legacy relayed gossip.

In `modules/gossip.py`:

- Make sure `process_gossip()` still accepts core-state updates after optional fields are stripped.

**Step 4: Re-run the GOSSIP handler tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip_protocol_hardening.py -k "gossip and (legacy or signature_v2)" -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add modules/protocol_handlers.py modules/gossip.py tests/test_gossip_protocol_hardening.py
git commit -m "fix: harden legacy gossip optional fields"
```

### Task 3: Harden FULL_SYNC state and membership authority

**Files:**
- Modify: `modules/protocol_handlers.py`
- Modify: `modules/protocol.py`
- Modify: `tests/test_gossip_protocol_hardening.py`

**Step 1: Write the failing FULL_SYNC tests**

Add explicit mixed-fleet and v2 tests:

```python
def test_legacy_full_sync_only_applies_sender_row(...):
    ...
    assert state_manager.get_peer_state(victim) is None


def test_legacy_full_sync_ignores_members_array(...):
    ...
    assert db.get_member("not-a-pubkey") is None


def test_v2_full_sync_applies_foreign_rows_when_hashes_verify(...):
    ...
    assert state_manager.get_peer_state(victim).capacity_sats == 999999


def test_v2_full_sync_rejects_invalid_member_pubkey(...):
    ...
```

**Step 2: Run the FULL_SYNC tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip_protocol_hardening.py -k "full_sync or members_array" -v`

Expected: FAIL because legacy FULL_SYNC still mutates foreign rows and membership.

**Step 3: Implement the receiver policy**

In `modules/protocol_handlers.py`:

- Add a helper to verify FULL_SYNC `signature_v2`, `states_hash_v2`, and `members_hash_v2`.
- For legacy-only payloads:
  - keep `sender_id == peer_id` and member checks
  - filter `states` down to rows where `row["peer_id"] == sender_id`
  - ignore `members` entirely
- For v2 payloads:
  - validate each state row before applying
  - validate each member row before syncing
  - reject invalid member pubkeys and malformed addresses

Suggested structure:

```python
if has_valid_full_sync_v2(payload):
    trusted_states = validate_v2_states(payload["states"])
    trusted_members = validate_v2_members(payload.get("members", []))
else:
    trusted_states = [row for row in payload["states"] if row.get("peer_id") == sender_id]
    trusted_members = []
```

**Step 4: Re-run the FULL_SYNC tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip_protocol_hardening.py -k "full_sync or members_array" -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add modules/protocol_handlers.py modules/protocol.py tests/test_gossip_protocol_hardening.py
git commit -m "fix: restrict legacy full sync authority"
```

### Task 4: Stabilize remote-state persistence and remove truncation

**Files:**
- Modify: `modules/state_manager.py`
- Modify: `modules/database.py`
- Modify: `modules/gossip.py`
- Modify: `tests/test_state.py`
- Modify: `tests/test_gossip_protocol_hardening.py`

**Step 1: Write the failing persistence tests**

Add:

```python
def test_remote_state_hash_survives_restart_reload(...):
    ...
    assert hash_before == hash_after


def test_get_all_hive_states_does_not_truncate_after_1000_rows(...):
    ...
    assert len(reloaded.get_all_peer_states()) == 1105


def test_full_sync_processing_uses_shared_limit(...):
    ...
```

**Step 2: Run the persistence tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_state.py tests/test_gossip_protocol_hardening.py -k "restart or truncate or shared_limit" -v`

Expected: FAIL because the current DB path rewrites timestamps and truncates reload.

**Step 3: Implement the persistence fix**

In `modules/database.py`:

- Extend `update_hive_state()` with an optional logical timestamp parameter:

```python
def update_hive_state(..., version: Optional[int] = None, last_update_ts: Optional[int] = None) -> None:
    stored_ts = last_update_ts if last_update_ts is not None else int(time.time())
```

- Use `stored_ts` instead of unconditional `now`.
- Remove the `LIMIT 1000` from `get_all_hive_states()`.

In `modules/state_manager.py`:

- Pass remote timestamps through on `update_peer_state()` and `apply_full_sync()`.
- Keep local-state writes using current time.

In `modules/gossip.py`:

- Keep using the shared `MAX_FULL_SYNC_STATES` from Task 1.

**Step 4: Re-run the persistence tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_state.py tests/test_gossip_protocol_hardening.py -k "restart or truncate or shared_limit" -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add modules/state_manager.py modules/database.py modules/gossip.py tests/test_state.py tests/test_gossip_protocol_hardening.py
git commit -m "fix: stabilize gossip persistence across restart"
```

### Task 5: Add mixed-fleet verification and finalize

**Files:**
- Modify: `tests/test_gossip_protocol_hardening.py`
- Modify: `tests/test_state.py`

**Step 1: Add end-to-end compatibility tests**

Add one focused test for each compatibility promise:

```python
def test_legacy_gossip_still_updates_core_state(...):
    ...


def test_legacy_state_hash_still_detects_fleet_hash_mismatch(...):
    ...


def test_v2_full_sync_allows_authenticated_fleet_catchup(...):
    ...
```

**Step 2: Run the focused mixed-fleet slice**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip_protocol_hardening.py -k "legacy or v2" -v`

Expected: PASS.

**Step 3: Run the full gossip/state/security verification sweep**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip.py tests/test_state.py tests/test_security.py tests/test_cl_hive_fixes.py tests/test_gossip_protocol_hardening.py -v`

Expected: PASS.

**Step 4: Commit**

```bash
git add tests/test_gossip_protocol_hardening.py tests/test_state.py
git commit -m "test: cover mixed-fleet gossip hardening"
```

**Step 5: Request review and summarize rollout risk**

- Request code review before merge.
- In the handoff summary, call out the intentional compatibility tradeoff:
  - legacy peers still interoperate
  - legacy peers lose authority to mutate fleet-wide sync state until upgraded
