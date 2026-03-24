# Hive Gossip Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the unsafe legacy Hive state-sync contract with a strict v2-only `GOSSIP`/`STATE_HASH`/`FULL_SYNC` protocol that closes the March 23, 2026 audit findings.

**Architecture:** Upgraded nodes emit envelope version `2` for `GOSSIP`, `STATE_HASH`, and `FULL_SYNC`, and receivers immediately reject those messages unless the new v2-authenticated hashes and signatures are present and valid. There is no legacy fallback for these three message types. Alongside the protocol hardening, persistence is fixed so remote timestamps survive restart and all state-count boundaries use one shared limit.

**Tech Stack:** Python 3.12, Core Lightning plugin (`pyln-client`), SQLite, `pytest`, `unittest.mock`

---

### Task 1: Add v2 integrity helpers and strict protocol constants

**Files:**
- Modify: `modules/protocol.py`
- Modify: `tests/test_protocol.py`
- Create: `tests/test_gossip_protocol_hardening.py`

**Step 1: Write the failing helper tests**

Add focused tests covering the new contract:

```python
def test_gossip_signing_payload_v2_changes_when_addresses_change():
    ...


def test_state_hash_signing_payload_v2_changes_when_membership_hash_changes():
    ...


def test_full_sync_states_hash_v2_changes_when_state_contents_change():
    ...


def test_state_sync_messages_require_envelope_version_2():
    ...
```

**Step 2: Run the new helper tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_protocol.py tests/test_gossip_protocol_hardening.py -k "v2 or envelope_version_2" -v`

Expected: FAIL because the new helpers and strict version checks do not exist yet.

**Step 3: Implement the v2 helper layer**

In `modules/protocol.py`:

- Add one shared network boundary constant:

```python
MAX_FULL_SYNC_STATES = 500
```

- Add canonical v2 helpers:

```python
def compute_gossip_data_hash_v2(payload: Dict[str, Any]) -> str: ...
def get_gossip_signing_payload_v2(payload: Dict[str, Any]) -> str: ...
def get_state_hash_signing_payload_v2(payload: Dict[str, Any]) -> str: ...
def compute_full_sync_states_hash_v2(states: list) -> str: ...
def compute_full_sync_members_hash_v2(members: list) -> str: ...
def get_full_sync_signing_payload_v2(payload: Dict[str, Any]) -> str: ...
```

- Add strict helpers:

```python
STRICT_STATE_SYNC_VERSION = 2

def is_strict_state_sync_payload(payload: Dict[str, Any]) -> bool:
    return payload.get("_envelope_version") == STRICT_STATE_SYNC_VERSION
```

- Normalize before hashing:
  - `topology`
  - `addresses`
  - `capabilities`
  - member rows

**Step 4: Re-run the helper tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_protocol.py tests/test_gossip_protocol_hardening.py -k "v2 or envelope_version_2" -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add modules/protocol.py tests/test_protocol.py tests/test_gossip_protocol_hardening.py
git commit -m "fix: add strict v2 gossip sync helpers"
```

### Task 2: Reject legacy GOSSIP and require v2-authenticated optional fields

**Files:**
- Modify: `modules/protocol_handlers.py`
- Modify: `modules/gossip.py`
- Modify: `tests/test_gossip_protocol_hardening.py`

**Step 1: Write the failing GOSSIP handler tests**

Add:

```python
def test_handle_gossip_rejects_legacy_payload_without_signature_v2(...):
    ...
    plugin.rpc.checkmessage.assert_not_called()


def test_handle_gossip_rejects_envelope_v1(...):
    ...


def test_handle_gossip_accepts_v2_payload_and_persists_addresses(...):
    ...
    assert json.loads(db.get_member(sender)["addresses"]) == ["1.2.3.4:9735"]


def test_handle_gossip_accepts_v2_payload_and_autoconnects(...):
    ...
```

**Step 2: Run the GOSSIP tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip_protocol_hardening.py -k "handle_gossip" -v`

Expected: FAIL because the current handler still accepts legacy payloads.

**Step 3: Implement strict GOSSIP enforcement**

In `modules/protocol_handlers.py`:

- Add a helper to verify:
  - envelope version `2`
  - `signature_v2` exists
  - `get_gossip_signing_payload_v2(payload)` verifies against `sender_id`

- Reject before `gossip_mgr.process_gossip()` if any v2 requirement fails.
- Reject before persistence and auto-connect if `addresses` validation fails.
- Remove any legacy optional-field fallback logic for `GOSSIP`.

In `modules/gossip.py`:

- Keep core state processing unchanged once the handler has admitted a v2 payload.

**Step 4: Re-run the GOSSIP tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip_protocol_hardening.py -k "handle_gossip" -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add modules/protocol_handlers.py modules/gossip.py tests/test_gossip_protocol_hardening.py
git commit -m "fix: require v2 gossip authentication"
```

### Task 3: Reject legacy STATE_HASH and FULL_SYNC

**Files:**
- Modify: `modules/protocol_handlers.py`
- Modify: `modules/protocol.py`
- Modify: `tests/test_gossip_protocol_hardening.py`

**Step 1: Write the failing STATE_HASH and FULL_SYNC tests**

Add:

```python
def test_handle_state_hash_rejects_legacy_payload_without_signature_v2(...):
    ...


def test_handle_state_hash_accepts_v2_payload_with_membership_hash(...):
    ...


def test_handle_full_sync_rejects_legacy_payload_without_signature_v2(...):
    ...


def test_handle_full_sync_rejects_envelope_v1(...):
    ...
```

**Step 2: Run the strict rejection tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip_protocol_hardening.py -k "state_hash or full_sync" -v`

Expected: FAIL because the current handlers still accept legacy payloads.

**Step 3: Implement strict STATE_HASH/FULL_SYNC enforcement**

In `modules/protocol_handlers.py`:

- `handle_state_hash()` must require:
  - envelope version `2`
  - `signature_v2`
  - authenticated `membership_hash`

- `handle_full_sync()` must require:
  - envelope version `2`
  - `signature_v2`
  - `states_hash_v2`
  - `members_hash_v2`

- If any required v2 field is missing or invalid, reject immediately.
- Remove all legacy fallback behavior for these three message types.

**Step 4: Re-run the strict rejection tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip_protocol_hardening.py -k "state_hash or full_sync" -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add modules/protocol_handlers.py modules/protocol.py tests/test_gossip_protocol_hardening.py
git commit -m "fix: reject legacy hive state sync traffic"
```

### Task 4: Authenticate full state and membership rows in v2 FULL_SYNC

**Files:**
- Modify: `modules/protocol_handlers.py`
- Modify: `modules/protocol.py`
- Modify: `tests/test_gossip_protocol_hardening.py`

**Step 1: Write the failing v2 FULL_SYNC authority tests**

Add:

```python
def test_handle_full_sync_v2_applies_foreign_rows_when_hashes_verify(...):
    ...


def test_handle_full_sync_v2_rejects_state_hash_mismatch(...):
    ...


def test_handle_full_sync_v2_rejects_invalid_member_pubkey(...):
    ...


def test_handle_full_sync_v2_rejects_invalid_address_shape(...):
    ...
```

**Step 2: Run the v2 FULL_SYNC tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip_protocol_hardening.py -k "full_sync_v2 or hash_mismatch or invalid_member" -v`

Expected: FAIL because the current FULL_SYNC integrity contract is too weak.

**Step 3: Implement authenticated FULL_SYNC application**

In `modules/protocol_handlers.py`:

- Verify `states_hash_v2` against fully normalized state rows.
- Verify `members_hash_v2` against fully normalized member rows.
- Validate all state rows before applying any state mutation.
- Validate all member rows before calling `_apply_membership_sync()`.
- Reject the whole message on any v2 integrity or schema failure.

In `modules/protocol.py`:

- Make `validate_full_sync()` use the shared `MAX_FULL_SYNC_STATES`.
- Add reusable member-row and address validators if they keep handler code smaller and cleaner.

**Step 4: Re-run the v2 FULL_SYNC tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip_protocol_hardening.py -k "full_sync_v2 or hash_mismatch or invalid_member" -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add modules/protocol_handlers.py modules/protocol.py tests/test_gossip_protocol_hardening.py
git commit -m "fix: authenticate full sync rows in v2"
```

### Task 5: Stabilize remote-state persistence and unify limits

**Files:**
- Modify: `modules/state_manager.py`
- Modify: `modules/database.py`
- Modify: `modules/gossip.py`
- Modify: `tests/test_state.py`
- Modify: `tests/test_gossip_protocol_hardening.py`

**Step 1: Write the failing persistence and limit tests**

Add:

```python
def test_remote_state_hash_survives_restart_reload(...):
    ...
    assert hash_before == hash_after


def test_get_all_hive_states_does_not_truncate_after_1000_rows(...):
    ...
    assert len(reloaded.get_all_peer_states()) == 1105


def test_full_sync_processing_uses_protocol_limit(...):
    ...
```

**Step 2: Run the persistence tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_state.py tests/test_gossip_protocol_hardening.py -k "restart or truncate or protocol_limit" -v`

Expected: FAIL because the current DB path rewrites timestamps and truncates reload.

**Step 3: Implement the persistence fix**

In `modules/database.py`:

- Extend `update_hive_state()` with an optional logical timestamp parameter:

```python
def update_hive_state(..., version: Optional[int] = None, last_update_ts: Optional[int] = None) -> None:
    stored_ts = last_update_ts if last_update_ts is not None else int(time.time())
```

- Use `stored_ts` instead of unconditional local time.
- Remove `LIMIT 1000` from `get_all_hive_states()`.

In `modules/state_manager.py`:

- Pass remote timestamps through `update_peer_state()` and `apply_full_sync()`.
- Keep local-state writes using current time.

In `modules/gossip.py`:

- Import the shared `MAX_FULL_SYNC_STATES` from `modules.protocol`.

**Step 4: Re-run the persistence tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_state.py tests/test_gossip_protocol_hardening.py -k "restart or truncate or protocol_limit" -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add modules/state_manager.py modules/database.py modules/gossip.py tests/test_state.py tests/test_gossip_protocol_hardening.py
git commit -m "fix: stabilize v2 gossip persistence"
```

### Task 6: Update send paths and final verification

**Files:**
- Modify: `modules/protocol_handlers.py`
- Modify: `modules/gossip.py`
- Modify: `tests/test_gossip_protocol_hardening.py`
- Modify: `tests/test_state.py`

**Step 1: Write the failing outbound-message tests**

Add:

```python
def test_create_signed_gossip_msg_emits_envelope_v2_and_signature_v2(...):
    ...


def test_create_signed_state_hash_msg_emits_envelope_v2_and_signature_v2(...):
    ...


def test_create_signed_full_sync_msg_emits_envelope_v2_hashes_and_signature_v2(...):
    ...
```

**Step 2: Run the outbound tests**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_gossip_protocol_hardening.py -k "create_signed" -v`

Expected: FAIL because the send paths still build the legacy contract.

**Step 3: Implement outbound v2 emission**

In `modules/protocol_handlers.py`:

- Update `_create_signed_gossip_msg()`, `_create_signed_state_hash_msg()`, and `_create_signed_full_sync_msg()` to populate the new v2 hashes/signatures.
- Ensure serialized envelopes use version `2` for these message types.

In `modules/gossip.py`:

- Keep payload construction deterministic for the new hashes.

**Step 4: Run the full verification sweep**

Run:
`/home/sat/bin/cl-hive/.venv/bin/python -m pytest tests/test_protocol.py tests/test_gossip.py tests/test_state.py tests/test_security.py tests/test_cl_hive_fixes.py tests/test_gossip_protocol_hardening.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add modules/protocol_handlers.py modules/gossip.py tests/test_gossip_protocol_hardening.py tests/test_state.py tests/test_protocol.py
git commit -m "fix: cut over hive state sync to strict v2"
```

### Task 7: Review and operator handoff

**Files:**
- Finalize: `docs/plans/2026-03-23-hive-gossip-hardening-design.md`
- Finalize: `docs/plans/2026-03-23-hive-gossip-hardening.md`

**Step 1: Request review**

- Request code review once implementation is complete.

**Step 2: Prepare rollout notes**

Document:

- upgraded nodes reject legacy `GOSSIP`, `STATE_HASH`, and `FULL_SYNC`
- mixed fleets will not anti-entropy sync correctly until upgraded
- operators must coordinate rollout before expecting normal convergence

**Step 3: Verify clean branch state**

Run:
`git status --short`

Expected: clean working tree.

## Final Rollout Notes

- Upgraded nodes now reject legacy `GOSSIP`, `STATE_HASH`, and `FULL_SYNC` immediately.
- Mixed fleets will not anti-entropy sync correctly until all participating peers are upgraded.
- Operators must coordinate rollout before expecting normal convergence.
- Implementation status:
  - branch: `codex/hive-gossip-hardening-20260323`
  - verification sweep: `166 passed`
