# Membership Flow Hardening Design

**Goal:** Fix failure modes in the cl-hive membership handshake that cause one-sided membership, lost handshake state on restart, and silent failures that are impossible to diagnose.

**Context:** The 4-message handshake (HELLO → CHALLENGE → ATTEST → WELCOME) works on the happy path but breaks when: a peer is already in the member table (HELLO silently dropped), the plugin restarts mid-handshake (all in-memory state lost), or messages are rejected (logged at DEBUG, invisible to operators).

---

## Fix 1: Auto-WELCOME on HELLO from existing member

**File:** `modules/protocol_handlers.py` — `handle_hello()`

**Current behavior (lines 119-122):**
```python
existing_member = database.get_member(peer_id)
if existing_member:
    plugin.log(f"...already a member", level='debug')
    return {"result": "continue"}  # SILENT DROP
```

**New behavior:** When HELLO arrives from a peer already in our member table, construct and send a signed WELCOME immediately. This heals one-sided membership in a single message.

**Logic:**
1. Verify peer is not banned (already checked at line 114, before this block)
2. Look up hive_id from our own member metadata
3. Get member count and state hash
4. Sign the WELCOME fields
5. Send via sendcustommsg (catch exception, log WARN on failure, return)
6. Log at INFO: "auto-sending WELCOME to existing member"

**Error handling:** If sendcustommsg fails (peer disconnected), log at WARN and return. Do NOT crash. Match the pattern in handle_attest lines 325-333.

**No broadcast:** Do NOT call `_broadcast_full_sync_to_members()` from this path. No membership change occurred — the peer was already a member.

**Receiver side (Fix 1b):** The peer receiving this auto-WELCOME may not have sent a HELLO (they are the existing member in a one-sided state). Their `handle_welcome` currently rejects WELCOMEs without a prior outbound HELLO (line 416). Add a bypass: if the WELCOME sender is already in our member table, accept the WELCOME without requiring outbound HELLO tracking. This handles the reverse direction of the asymmetry — both sides can heal regardless of which side has the stale membership.

**Idempotency:** If the peer is already a member on their side, handle_welcome will see they're already a member and skip the INSERT (Fix 4 adds this guard explicitly). Metadata (hive_id) is still updated unconditionally.

**No in-memory state required.** No CHALLENGE/ATTEST round-trip. One message fixes the asymmetry.

---

## Fix 2: Persist handshake state to database

**Files:** `modules/database.py`, `modules/handshake.py`

**Problem:** Three critical dicts are in-memory only and lost on plugin restart:
- `_pending_requests` — pending join requests awaiting hive-approve
- `_outbound_hello_sent` — tracking that we sent HELLO (needed to accept CHALLENGE)
- `_pending_challenges` — nonces awaiting ATTEST response

**New table:**
```sql
CREATE TABLE IF NOT EXISTS handshake_state (
    peer_id TEXT NOT NULL,
    type TEXT NOT NULL,
    data TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (peer_id, type)
)
```

**Types, TTLs, and data column contents:**
- `pending_request` — TTL 86400s (24h). Data: `{"received_at": int, "channel_verified": true}`
- `outbound_hello` — TTL 86400s (24h). Data: `{"sent_at": int}`
- `pending_challenge` — TTL 300s (5min). Data: `{"nonce": str, "requirements": int, "initial_tier": str, "issued_at": int}`

**Write path:** Each store/record method writes to BOTH memory dict AND database (upsert via INSERT OR REPLACE).

**Read path:** Each lookup method checks memory first, falls back to database query. On DB hit, repopulate memory for subsequent fast reads. On read, check `expires_at` — if expired, delete from both memory and DB, return None.

**Delete path:** Each pop/clear method deletes from BOTH memory dict AND database:
- `pop_pending_request()` → DELETE WHERE peer_id=? AND type='pending_request'
- `clear_outbound_hello()` → DELETE WHERE peer_id=? AND type='outbound_hello'
- Challenge clearance (line 417) → DELETE WHERE peer_id=? AND type='pending_challenge'

**Thread safety:**
- `_pending_challenges`: DB write MUST happen inside existing `_challenge_lock` to maintain atomicity with memory dict.
- `_pending_requests` and `_outbound_hello_sent`: No existing lock. SQLite WAL mode is thread-safe per connection. The memory dict operations are simple assignments (not read-modify-write), so no additional lock is needed. If a race occurs, the DB is the source of truth on restart.

**Expiry cleanup:** `expire_pending_requests()` (already called periodically) extended to also run `DELETE FROM handshake_state WHERE expires_at < ?`. No index needed — max ~3000 rows.

**Migration:** Table created in `database.initialize()` with IF NOT EXISTS. No migration needed for existing installs.

---

## Fix 3: Upgrade silent drops to INFO logging

**File:** `modules/protocol_handlers.py`

Four message rejection points currently log at DEBUG. Change to INFO with actionable context. (Two other points — handle_attest "no pending challenge" and handle_welcome "no outbound HELLO" — already log at WARN and need no change.)

| Location | Current (DEBUG) | New (INFO) |
|---|---|---|
| handle_hello: not our member (line 110) | "but we're not a member" | "HELLO from X rejected: we are not a hive member" |
| handle_hello: no channel (line 133) | "no channel (proof of stake)" | "HELLO from X rejected: no channel with peer" |
| handle_hello: existing member (line 121) | "already a member" | Becomes success path (Fix 1): "HELLO from X (existing member) -- auto-sending WELCOME" |
| handle_challenge: no pending HELLO (line 165) | "no pending outbound HELLO" | "CHALLENGE from X rejected: no outbound HELLO recorded (plugin may have restarted)" |

**Principle:** Any message rejection that an operator might need to diagnose should be at INFO. Security rejections (banned peer, signature failure) already log at WARN and are unchanged.

---

## Fix 4+5: Idempotent membership creation in handle_welcome

**File:** `modules/protocol_handlers.py` — `handle_welcome()`

**Current behavior (lines 433-442):** Calls `add_member()` unconditionally. IntegrityError is caught and returns False, so duplicates don't crash. But the intent is unclear and metadata update (hive_id) is skipped if the member already exists.

**New behavior:**
```python
if not database.get_member(our_pubkey):
    database.add_member(our_pubkey, ...)
# Always update metadata (hive_id may have changed)
database.update_member(our_pubkey, metadata=json.dumps({"hive_id": hive_id}))

if not database.get_member(peer_id):
    database.add_member(peer_id, ...)
```

This makes WELCOME processing idempotent — receiving it twice has no side effects. Metadata is always refreshed regardless of whether the member existed.

---

## Files Changed

| File | Changes |
|---|---|
| `modules/protocol_handlers.py` | Fix 1+1b (auto-WELCOME + receiver bypass), Fix 3 (logging), Fix 4+5 (idempotency) |
| `modules/handshake.py` | Fix 2 (persist to DB on write, fallback on read, delete on pop/clear) |
| `modules/database.py` | Fix 2 (new handshake_state table, upsert/query/delete/cleanup methods) |

## Testing

- Verify auto-WELCOME: mock a HELLO from an existing member, confirm WELCOME is sent
- Verify auto-WELCOME receiver: mock receiving WELCOME from existing member without prior HELLO, confirm accepted
- Verify sendcustommsg failure in auto-WELCOME: confirm WARN log, no crash
- Verify persistence: write pending request, clear memory dict, verify DB fallback recovers it
- Verify expiry: write with short TTL, verify cleanup removes from both memory and DB
- Verify delete paths: pop_pending_request, clear_outbound_hello, challenge clearance all remove from DB
- Verify thread safety: generate_challenge DB write happens inside _challenge_lock
- Verify idempotency: send duplicate WELCOME, verify no crash, no duplicate members, metadata updated
- Verify logging: confirm rejections appear at INFO level with actionable messages
