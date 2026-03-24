# Membership Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate the audited membership security and convergence failures while keeping upgraded cl-hive nodes backward compatible with already-deployed peers.

**Architecture:** The join flow keeps the current HELLO/CHALLENGE/ATTEST/WELCOME protocol but only answers CHALLENGE for peers we actually tried to join, and only commits new members after local WELCOME send succeeds. Membership deletions become durable tombstones, propagate through BAN/MEMBER_LEFT plus a new MEMBER_REMOVED event, and are replayed during FULL_SYNC through optional `membership_events` data that upgraded nodes understand and older nodes ignore.

**Tech Stack:** Python 3.10+, Core Lightning plugin (`pyln-client`), SQLite, pytest

---

### Task 1: Build the membership protocol regression harness

**Files:**
- Create: `tests/test_membership_protocol_handlers.py`
- Modify: `tests/test_rpc.py`

**Step 1: Write the failing tests**

Create direct handler tests that exercise the exact audit regressions.

```python
def test_handle_challenge_requires_pending_outbound_hello(mock_plugin):
    ph.handshake_mgr = MagicMock()
    ph.handshake_mgr.has_pending_outbound_hello.return_value = False

    result = ph.handle_challenge(PEER_B, {"nonce": "n" * 64, "hive_id": "h"}, mock_plugin)

    assert result == {"result": "continue"}
    mock_plugin.rpc.call.assert_not_called()


def test_handle_attest_does_not_activate_member_when_welcome_send_fails(db, mock_plugin):
    mock_plugin.rpc.call.side_effect = Exception("send failed")
    ph.handshake_mgr.get_pending_challenge.return_value = {
        "nonce": "n" * 64,
        "issued_at": int(time.time()),
        "requirements": 0,
        "initial_tier": "member",
    }
    ph.handshake_mgr.verify_manifest.return_value = (True, "")
    ph.handshake_mgr.check_requirements.return_value = (True, [])

    result = ph.handle_attest(PEER_B, attest_payload_for(PEER_B), mock_plugin)

    assert result == {"result": "continue"}
    assert db.get_member(PEER_B) is None
    ph.handshake_mgr.clear_challenge.assert_not_called()
```

Add one RPC-level test in `tests/test_rpc.py` that proves `hive-ban` now broadcasts a BAN payload instead of only mutating local DB state.

**Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_membership_protocol_handlers.py::test_handle_challenge_requires_pending_outbound_hello tests/test_membership_protocol_handlers.py::test_handle_attest_does_not_activate_member_when_welcome_send_fails tests/test_rpc.py::TestBanRPC::test_hive_ban_broadcasts_signed_payload -v`

Expected: FAIL because `handle_challenge()` still sends ATTEST without outbound HELLO state, `handle_attest()` still commits before local WELCOME delivery succeeds, and `hive-ban` does not broadcast anything.

**Step 3: Write the minimal implementation**

Do not implement all fixes here. Only add the test harness, shared fixtures, and helper builders needed by later tasks:

```python
def make_temp_db(tmp_path, plugin):
    db = HiveDatabase(str(tmp_path / "membership_protocol.db"), plugin)
    db.initialize()
    return db


def attest_payload_for(pubkey: str) -> dict:
    return {
        "manifest": {
            "pubkey": pubkey,
            "version": "cl-hive v2.2.6",
            "features": ["proto-v2"],
            "timestamp": int(time.time()),
            "nonce": "n" * 64,
        },
        "pubkey": pubkey,
        "version": "cl-hive v2.2.6",
        "features": ["proto-v2"],
        "nonce_signature": "nonce_sig",
        "manifest_signature": "manifest_sig",
    }
```

**Step 4: Run the tests again**

Run: `pytest tests/test_membership_protocol_handlers.py tests/test_rpc.py -v`

Expected: The new tests still fail, but they fail deterministically and isolate the audited gaps.

**Step 5: Commit**

```bash
git add tests/test_membership_protocol_handlers.py tests/test_rpc.py
git commit -m "test: add membership protocol regression coverage"
```

### Task 2: Harden CHALLENGE and ATTEST without breaking the join flow

**Files:**
- Modify: `modules/protocol_handlers.py:140-344`
- Modify: `modules/handshake.py:222-236`
- Test: `tests/test_membership_protocol_handlers.py`

**Step 1: Write the failing tests**

Expand the new test file with the join-flow assertions:

```python
def test_handle_challenge_accepts_tracked_outbound_join(mock_plugin):
    ph.handshake_mgr.has_pending_outbound_hello.return_value = True
    ph.handshake_mgr.create_manifest.return_value = valid_manifest_bundle(PEER_A)

    result = ph.handle_challenge(PEER_B, {"nonce": "n" * 64, "hive_id": "h"}, mock_plugin)

    assert result == {"result": "continue"}
    mock_plugin.rpc.call.assert_called_once()


def test_handle_attest_clears_challenge_only_after_successful_welcome_send(db, mock_plugin):
    mock_plugin.rpc.call.return_value = None
    ph.handshake_mgr.get_pending_challenge.return_value = valid_pending_challenge()
    ph.handshake_mgr.verify_manifest.return_value = (True, "")
    ph.handshake_mgr.check_requirements.return_value = (True, [])

    ph.handle_attest(PEER_B, attest_payload_for(PEER_B), mock_plugin)

    assert db.get_member(PEER_B) is not None
    ph.handshake_mgr.clear_challenge.assert_called_once_with(PEER_B)
```

**Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_membership_protocol_handlers.py::test_handle_challenge_accepts_tracked_outbound_join tests/test_membership_protocol_handlers.py::test_handle_attest_clears_challenge_only_after_successful_welcome_send -v`

Expected: FAIL until the handlers enforce outbound HELLO state and reorder the ATTEST commit.

**Step 3: Write the minimal implementation**

In `handle_challenge()` add the outbound-HELLO gate before any signing work:

```python
if not handshake_mgr or not handshake_mgr.has_pending_outbound_hello(peer_id):
    plugin.log(
        f"cl-hive: CHALLENGE rejected from {peer_id[:16]}... no pending outbound HELLO",
        level="warn",
    )
    return {"result": "continue"}
```

In `handle_attest()` move local membership activation after the local WELCOME send succeeds:

```python
try:
    plugin.rpc.call("sendcustommsg", {"node_id": peer_id, "msg": welcome_msg.hex()})
except Exception as e:
    plugin.log(f"cl-hive: Failed to send WELCOME: {e}", level="warn")
    return {"result": "continue"}

database.add_member(peer_id=peer_id, tier=MEMBER_TIER, joined_at=int(time.time()))
database.log_membership_event("joined", peer_id)
database.save_peer_capabilities(peer_id, manifest_features)
database.update_presence(peer_id, is_online=True, now_ts=int(time.time()), window_seconds=30 * 86400)
handshake_mgr.clear_challenge(peer_id)
```

Keep the existing pending challenge intact when local send fails so the peer can retry within the same TTL window.

**Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_membership_protocol_handlers.py::test_handle_challenge_requires_pending_outbound_hello tests/test_membership_protocol_handlers.py::test_handle_challenge_accepts_tracked_outbound_join tests/test_membership_protocol_handlers.py::test_handle_attest_does_not_activate_member_when_welcome_send_fails tests/test_membership_protocol_handlers.py::test_handle_attest_clears_challenge_only_after_successful_welcome_send -v`

Expected: PASS

**Step 5: Commit**

```bash
git add modules/protocol_handlers.py modules/handshake.py tests/test_membership_protocol_handlers.py
git commit -m "fix: harden challenge and attest join flow"
```

### Task 3: Authenticate BAN and replay-harden MEMBER_LEFT

**Files:**
- Modify: `modules/protocol.py:340-380`
- Modify: `modules/protocol_handlers.py:1806-1927`
- Modify: `cl-hive.py:3094-3226`
- Test: `tests/test_membership_protocol_handlers.py`

**Step 1: Write the failing tests**

Add the negative-path tests that match the audit:

```python
def test_handle_ban_rejects_non_member_sender(db, mock_plugin):
    db.add_member(PEER_B, tier="member", joined_at=1)
    ph.database = db

    result = ph.handle_ban(PEER_A, {"peer_id": PEER_B, "reason": "spoofed"}, mock_plugin)

    assert result["status"] == "ignored"
    assert db.is_banned(PEER_B) is False
    assert db.get_member(PEER_B) is not None


def test_handle_member_left_rejects_pre_rejoin_event(db, mock_plugin):
    db.add_member(PEER_B, tier="member", joined_at=200)
    mock_plugin.rpc.checkmessage.return_value = {"verified": True, "pubkey": PEER_B}

    result = ph.handle_member_left(PEER_B, {
        "peer_id": PEER_B,
        "timestamp": 100,
        "reason": "old-leave",
        "signature": "sig",
    }, mock_plugin)

    assert result == {"result": "continue"}
    assert db.get_member(PEER_B) is not None
```

Also add:

- `test_handle_member_left_rejects_stale_timestamp`
- `test_handle_ban_accepts_legacy_direct_member_sender`
- `test_hive_ban_broadcasts_signed_payload`

**Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_membership_protocol_handlers.py::test_handle_ban_rejects_non_member_sender tests/test_membership_protocol_handlers.py::test_handle_member_left_rejects_stale_timestamp tests/test_membership_protocol_handlers.py::test_handle_member_left_rejects_pre_rejoin_event tests/test_rpc.py::TestBanRPC::test_hive_ban_broadcasts_signed_payload -v`

Expected: FAIL because BAN still trusts any sender, MEMBER_LEFT has no freshness/joined_at guard, and `hive-ban` still does not broadcast.

**Step 3: Write the minimal implementation**

Add a BAN envelope builder in `hive-ban`:

```python
timestamp = int(time.time())
ban_payload = {
    "peer_id": peer_id,
    "reason": reason,
    "sender_id": our_pubkey,
    "timestamp": timestamp,
    "event_id": generate_event_id("BAN", {
        "peer_id": peer_id,
        "reason": reason,
        "sender_id": our_pubkey,
        "timestamp": timestamp,
    }, our_pubkey),
}
ban_payload["signature"] = plugin.rpc.signmessage(
    f"hive:ban:{our_pubkey}:{peer_id}:{timestamp}:{reason}"
)["zbase"]
protocol_handlers._reliable_broadcast(HiveMessageType.BAN, ban_payload)
```

In `handle_ban()`:

- reject if `peer_id` is not a current non-banned member
- if `sender_id`/`timestamp`/`signature` exist, verify freshness and signature
- if those fields are absent, accept only the legacy case where the direct sender is itself a current member

In `handle_member_left()`:

```python
MAX_MEMBERSHIP_EVENT_AGE_SECONDS = 30 * 86400

if not _check_timestamp_freshness(payload, MAX_MEMBERSHIP_EVENT_AGE_SECONDS, "MEMBER_LEFT"):
    return {"result": "continue"}

member = database.get_member(leaving_peer_id)
if member and timestamp < int(member.get("joined_at") or 0):
    plugin.log(f"cl-hive: MEMBER_LEFT stale for rejoined peer {leaving_peer_id[:16]}...", level="warn")
    return {"result": "continue"}
```

Route successful leaves through the shared removal helper instead of raw `database.remove_member(...)`.

**Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_membership_protocol_handlers.py::test_handle_ban_rejects_non_member_sender tests/test_membership_protocol_handlers.py::test_handle_ban_accepts_legacy_direct_member_sender tests/test_membership_protocol_handlers.py::test_handle_member_left_rejects_stale_timestamp tests/test_membership_protocol_handlers.py::test_handle_member_left_rejects_pre_rejoin_event tests/test_rpc.py::TestBanRPC::test_hive_ban_broadcasts_signed_payload -v`

Expected: PASS

**Step 5: Commit**

```bash
git add modules/protocol.py modules/protocol_handlers.py cl-hive.py tests/test_membership_protocol_handlers.py tests/test_rpc.py
git commit -m "fix: authenticate bans and replay-harden member leave"
```

### Task 4: Persist membership tombstones in the database

**Files:**
- Modify: `modules/database.py:146-250`
- Modify: `modules/database.py:1230-1465`
- Create: `tests/test_membership_tombstones.py`

**Step 1: Write the failing tests**

Create a focused DB regression file:

```python
def test_record_membership_tombstone_is_idempotent(database):
    ok1 = database.record_membership_tombstone(
        event_id="evt-1",
        peer_id=PEER_B,
        event="removed",
        actor_peer_id=PEER_A,
        reason="maintenance",
        timestamp=123,
        joined_at_cutoff=100,
    )
    ok2 = database.record_membership_tombstone(
        event_id="evt-1",
        peer_id=PEER_B,
        event="removed",
        actor_peer_id=PEER_A,
        reason="maintenance",
        timestamp=123,
        joined_at_cutoff=100,
    )

    assert ok1 is True
    assert ok2 is False


def test_get_membership_tombstones_returns_newest_first(database):
    database.record_membership_tombstone("evt-1", PEER_A, "left", None, "voluntary", 100, 90)
    database.record_membership_tombstone("evt-2", PEER_B, "banned", PEER_A, "spam", 200, 150)

    rows = database.get_membership_tombstones(limit=10)

    assert [row["event_id"] for row in rows] == ["evt-2", "evt-1"]
```

**Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_membership_tombstones.py -v`

Expected: FAIL because the table and helpers do not exist yet.

**Step 3: Write the minimal implementation**

Add a dedicated table and helpers:

```python
CREATE TABLE IF NOT EXISTS membership_tombstones (
    event_id TEXT PRIMARY KEY,
    peer_id TEXT NOT NULL,
    event TEXT NOT NULL,
    actor_peer_id TEXT,
    reason TEXT,
    timestamp INTEGER NOT NULL,
    joined_at_cutoff INTEGER NOT NULL
)
```

```python
def record_membership_tombstone(...):
    try:
        conn.execute(
            \"\"\"INSERT INTO membership_tombstones
               (event_id, peer_id, event, actor_peer_id, reason, timestamp, joined_at_cutoff)
               VALUES (?, ?, ?, ?, ?, ?, ?)\"\"\",
            (...),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def get_membership_tombstones(self, limit: int = 500) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM membership_tombstones ORDER BY timestamp DESC, event_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]
```

Keep the existing audit log. The new table is for convergence and replay safety, not human-readable history.

**Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_membership_tombstones.py -v`

Expected: PASS

**Step 5: Commit**

```bash
git add modules/database.py tests/test_membership_tombstones.py
git commit -m "feat: persist membership tombstones for convergence"
```

### Task 5: Add live manual-removal propagation for upgraded peers

**Files:**
- Modify: `modules/protocol.py:56-89`
- Modify: `modules/protocol_handlers.py:1767-1935`
- Modify: `cl-hive.py:1053-1074`
- Modify: `cl-hive.py:3229-3320`
- Test: `tests/test_membership_protocol_handlers.py`

**Step 1: Write the failing tests**

Add new protocol tests:

```python
def test_hive_remove_member_broadcasts_member_removed(mock_plugin, member_context):
    result = hive_remove_member(mock_plugin, peer_id=PEER_B, reason="maintenance", force=True)

    assert result["status"] == "removed"
    protocol_handlers._reliable_broadcast.assert_called_once()


def test_handle_member_removed_rejects_non_member_sender(db, mock_plugin):
    payload = signed_member_removed_payload(actor=PEER_A, target=PEER_B, timestamp=200)

    result = ph.handle_member_removed(PEER_A, payload, mock_plugin)

    assert result["result"] == "continue"
    assert db.get_member(PEER_B) is not None
```

Also add `test_handle_member_removed_rejects_pre_rejoin_event` and `test_handle_member_removed_clears_state_and_records_tombstone`.

**Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_membership_protocol_handlers.py::test_hive_remove_member_broadcasts_member_removed tests/test_membership_protocol_handlers.py::test_handle_member_removed_rejects_non_member_sender -v`

Expected: FAIL because `MEMBER_REMOVED` does not exist yet.

**Step 3: Write the minimal implementation**

Add the new message type and dispatch path:

```python
class HiveMessageType(IntEnum):
    BAN = 32791
    MEMBER_REMOVED = 32793
    MEMBER_LEFT = 32797
```

In `hive-remove-member()`:

```python
timestamp = int(time.time())
payload = {
    "peer_id": peer_id,
    "actor_peer_id": our_pubkey,
    "reason": reason,
    "timestamp": timestamp,
    "event_id": generate_event_id("MEMBER_REMOVED", {...}, our_pubkey),
    "signature": plugin.rpc.signmessage(
        f"hive:remove:{our_pubkey}:{peer_id}:{timestamp}:{reason}"
    )["zbase"],
}
protocol_handlers._reliable_broadcast(HiveMessageType.MEMBER_REMOVED, payload)
```

In `handle_member_removed()`:

- require direct sender membership
- verify freshness and signature
- reject events older than the target member's current `joined_at`
- record a tombstone
- call the shared removal helper

**Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_membership_protocol_handlers.py::test_hive_remove_member_broadcasts_member_removed tests/test_membership_protocol_handlers.py::test_handle_member_removed_rejects_non_member_sender tests/test_membership_protocol_handlers.py::test_handle_member_removed_clears_state_and_records_tombstone -v`

Expected: PASS

**Step 5: Commit**

```bash
git add modules/protocol.py modules/protocol_handlers.py cl-hive.py tests/test_membership_protocol_handlers.py
git commit -m "feat: propagate manual member removals to upgraded peers"
```

### Task 6: Replay tombstones during FULL_SYNC for offline upgraded peers

**Files:**
- Modify: `modules/protocol_handlers.py:704-840`
- Modify: `modules/protocol_handlers.py:1699-1804`
- Test: `tests/test_membership_protocol_handlers.py`
- Test: `tests/test_state.py`

**Step 1: Write the failing tests**

Add catch-up tests:

```python
def test_apply_membership_sync_applies_membership_events_before_add_only_merge(db, mock_plugin):
    db.add_member(PEER_A, tier="member", joined_at=100)
    db.add_member(PEER_B, tier="member", joined_at=200)

    events = [{
        "event_id": "evt-1",
        "peer_id": PEER_B,
        "event": "removed",
        "actor_peer_id": PEER_A,
        "reason": "maintenance",
        "timestamp": 250,
        "joined_at_cutoff": 200,
    }]
    members = [{"peer_id": PEER_A, "tier": "member", "joined_at": 100}]

    changed = ph._apply_membership_sync(members, PEER_A, mock_plugin, membership_events=events)

    assert changed == 1
    assert db.get_member(PEER_B) is None
```

Add one FULL_SYNC integration test showing that an upgraded node receiving `membership_events` removes the stale peer during sync, and that a stale event older than `joined_at` is ignored.

**Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_membership_protocol_handlers.py::test_apply_membership_sync_applies_membership_events_before_add_only_merge tests/test_state.py -v`

Expected: FAIL because `_apply_membership_sync()` is currently add-only.

**Step 3: Write the minimal implementation**

Append tombstones to outgoing FULL_SYNC without changing the legacy signature payload:

```python
full_sync_payload = gossip_mgr.create_full_sync_payload()
full_sync_payload["members"] = _create_membership_payload()
full_sync_payload["membership_events"] = database.get_membership_tombstones(limit=500)
```

Apply events before the normal add-only member merge:

```python
def _apply_membership_events(events: list, sender_id: str, plugin: Plugin) -> int:
    changed = 0
    for event in events:
        peer_id = event.get("peer_id")
        cutoff = int(event.get("joined_at_cutoff") or 0)
        member = database.get_member(peer_id)
        if member and int(member.get("joined_at") or 0) <= cutoff:
            _execute_member_removal(peer_id, reason=event.get("event", "removed"))
            changed += 1
        database.record_membership_tombstone(...)
    return changed
```

Update the sync entry point so it accepts an optional `membership_events` parameter but keeps the old add-only behavior when that field is absent.

**Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_membership_protocol_handlers.py::test_apply_membership_sync_applies_membership_events_before_add_only_merge tests/test_membership_protocol_handlers.py::test_apply_membership_sync_ignores_stale_event_for_rejoined_member tests/test_state.py -v`

Expected: PASS

**Step 5: Commit**

```bash
git add modules/protocol_handlers.py tests/test_membership_protocol_handlers.py tests/test_state.py
git commit -m "fix: replay membership tombstones during full sync"
```

### Task 7: Run the full verification sweep

**Files:**
- Modify: `tests/test_membership_protocol_handlers.py`
- Modify: `tests/test_membership_tombstones.py`

**Step 1: Run the new focused tests**

Run: `pytest tests/test_membership_protocol_handlers.py tests/test_membership_tombstones.py -v`

Expected: PASS

**Step 2: Run the existing membership/security regression suite**

Run: `pytest tests/test_membership.py tests/test_rpc.py tests/test_security.py tests/test_state.py -v`

Expected: PASS

**Step 3: Run the combined quick suite**

Run: `pytest -q tests/test_membership_protocol_handlers.py tests/test_membership_tombstones.py tests/test_membership.py tests/test_rpc.py tests/test_security.py tests/test_state.py`

Expected: PASS with no unexpected skips or xfails.

**Step 4: Review operational behavior**

Confirm these manual checks in logs or focused tests:

- unsolicited `CHALLENGE` logs a rejection and sends no ATTEST
- `WELCOME` send failure leaves no local ghost member
- BAN from a non-member is ignored
- stale/pre-rejoin `MEMBER_LEFT` is ignored
- manual `hive-remove-member` emits `MEMBER_REMOVED`
- upgraded nodes replay `membership_events` after restart

**Step 5: Commit**

```bash
git add tests/test_membership_protocol_handlers.py tests/test_membership_tombstones.py modules/protocol.py modules/protocol_handlers.py modules/database.py cl-hive.py
git commit -m "fix: harden membership protocol and convergence"
```
