# Membership Simplification: Single-Role Trusted Fleet

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the remaining admin/member two-tier membership model to a single `member` role, remove all vestiges of the old invite-ticket/governance model, and ensure cl-hive ships as a lean trusted fleet coordination layer.

**Architecture:** All hive participants are equal members. New nodes join by opening a qualifying channel, sending HELLO, and being approved by any existing member. No admin role, no promotion pipeline, no quorum voting, no invite tickets. `cl-hive` coordinates and recommends; local tools execute.

**Tech Stack:** Python 3.10+, Core Lightning plugin (pyln-client), SQLite (WAL mode)

---

## File Structure

### Files to Modify

| File | Changes |
|------|---------|
| `modules/membership.py` | Remove `set_tier()`, remove "any tier" comment, rename section header |
| `modules/handshake.py` | Remove admin/invite references from docstrings and `generate_challenge()` |
| `modules/contribution.py` | Simplify tier checks to `is not None` |
| `modules/fee_coordination.py` | Simplify tier checks to `is not None` |
| `modules/protocol.py` | Clean stale comments (ticket, VOUCH/PROMOTION) |
| `modules/protocol_handlers.py` | Remove admin tier validation, quorum/promotion comments |
| `modules/rpc_commands.py` | Remove admin permission check, admin_count, governance mode references |
| `modules/planner.py` | Remove tier filtering, clean ticket references in comments |
| `modules/database.py` | Remove ORDER BY tier, simplify member queries |
| `cl-hive.py` | Remove all "Admin only" / "Member or Admin" permission language, clean docstrings |
| `README.md` | Rewrite Quick Start, remove invite/admin/voting language |
| `CLAUDE.md` | Update "Membership & Admin" → "Membership" |
| `docs/JOINING_THE_HIVE.md` | Already correct — minor polish only |
| `docker/README.md` | Remove invite examples |
| `docker/runbooks/emergency-shutdown.md` | "Hive Admin" → "Fleet operator" |
| `docker/runbooks/database-corruption.md` | Remove invite code reference |
| `tests/test_rpc.py` | Remove ticket/invite/founder references |
| `tests/test_membership.py` | Already correct — no changes needed |
| `tests/test_rebalancing_activity.py` | Change `tier: "admin"` → `tier: "member"` |
| `tests/test_crypto_integration.py` | Remove ticket verification references |
| `tests/test_protocol.py` | Remove ticket expiry test, clean serialize test |
| `tests/test_planner.py` | Remove "Ticket" and "Governance mode" comments |
| `tests/test_planner_simulation.py` | Remove "Ticket" comment |
| `tests/test_security.py` | Remove "Ticket S-01" comment |
| `tests/test_issue_59_60.py` | "promoted member" → "member with null addresses" |
| `tests/test_phase6_ingest.py` | Payload `ticket` key is generic test data — KEEP |

### Files to Delete

| File | Reason |
|------|--------|
| None | All remaining modules serve the reduced trusted fleet model |

### Files to Keep Unchanged

| File | Reason |
|------|--------|
| `modules/governance.py` | Already reduced to RecommendationLogger (83 LOC). Used by planner. |
| `modules/contribution.py` | Leech detection is valid fleet safety (after tier check cleanup). |
| `modules/phase6_ingest.py` | External transport parsing for cl-hive-comms. No old model refs. |
| `CHANGELOG.md` | Historical record. Old model references are factual changelog entries. |
| Audit files (`audits/`) | Historical security artifacts. Reference old model but are archival. |
| Design plan docs (`docs/plans/`) | Historical implementation plans. Already describe the simplification. |

---

## Task 1: Core Membership Module Cleanup

**Files:**
- Modify: `modules/membership.py`
- Modify: `modules/handshake.py`

### membership.py

- [ ] **Step 1: Remove "any tier" from is_member docstring**

```python
# Line 71: Change
def is_member(self, peer_id: str) -> bool:
    """Check if a peer is a hive member (any tier)."""
# To:
def is_member(self, peer_id: str) -> bool:
    """Check if a peer is a hive member."""
```

- [ ] **Step 2: Remove set_tier method complexity**

The `set_tier()` method accepts a tier parameter that should always be 'member'. Remove the tier parameter and hardcode.

```python
# Line 44-56: Change
def set_tier(self, peer_id: str, tier: str = MEMBER_TIER) -> bool:
    updated = self.db.update_member(peer_id, tier=tier)
# To:
def set_tier(self, peer_id: str) -> bool:
    """Update a peer's membership record (always member tier)."""
    updated = self.db.update_member(peer_id, tier=MEMBER_TIER)
```

- [ ] **Step 3: Rename QUORUM section header**

```python
# Line 186-187: Change
# ACTIVE MEMBERS & QUORUM
# To:
# ACTIVE MEMBERS
```

### handshake.py

- [ ] **Step 4: Clean generate_challenge docstring**

```python
# Lines 304-312: Remove invite ticket and admin references from docstring
# Change:
    """
    Generate a challenge nonce for a peer.

    Args:
        peer_id: Peer's public key
        requirements: Bitmask requirements from the invite ticket
        initial_tier: Starting tier for new member ('admin' or 'member')
# To:
    """
    Generate a challenge nonce for a peer.

    Args:
        peer_id: Peer's public key
        requirements: Bitmask of required capabilities
        initial_tier: Starting tier for new member (always 'member')
```

- [ ] **Step 5: Run tests**

Run: `cd /home/sat/bin/cl-hive/.worktrees/trusted-fleet-simplification && python3 -m pytest tests/test_membership.py tests/test_rpc.py -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add modules/membership.py modules/handshake.py
git commit -m "refactor: remove admin tier references from membership and handshake modules"
```

---

## Task 2: Tier Check Simplification Across Modules

**Files:**
- Modify: `modules/contribution.py`
- Modify: `modules/fee_coordination.py`
- Modify: `modules/planner.py`

These three modules check `member.get("tier") in ("admin", "member")` which should be simplified since there's only one tier now.

### contribution.py

- [ ] **Step 1: Simplify tier checks in handle_forward_event**

```python
# Line 205: Change
if member and member.get("tier") in ("admin", "member"):
# To:
if member:

# Line 212: Change
if member and member.get("tier") in ("admin", "member"):
# To:
if member:
```

### fee_coordination.py

- [ ] **Step 2: Simplify tier checks**

```python
# Line 525: Change
return bool(member and member.get("tier") in ("admin", "member"))
# To:
return bool(member)

# Line 799: Change
if member and member.get("tier") in ("admin", "member"):
# To:
if member:
```

### planner.py

- [ ] **Step 3: Simplify tier filtering and clean comments**

```python
# Line 1169: Change docstring
"""Get list of Hive member pubkeys (admin + member tiers)."""
# To:
"""Get list of Hive member pubkeys."""

# Line 1173: Change
return [m['peer_id'] for m in members if m.get('tier') in ('admin', 'member')]
# To:
return [m['peer_id'] for m in members]

# Line 14-15: Change
# This ticket (6-01) implements ONLY saturation detection and guard mechanism.
# Expansion logic will be added in later tickets.
# To:
# Implements saturation detection, guard mechanism, and expansion logic.

# Line 1498: Change
# EXPANSION LOGIC (Ticket 6-02)
# To:
# EXPANSION LOGIC
```

- [ ] **Step 4: Run tests**

Run: `cd /home/sat/bin/cl-hive/.worktrees/trusted-fleet-simplification && python3 -m pytest tests/test_planner.py tests/test_fee_coordination.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add modules/contribution.py modules/fee_coordination.py modules/planner.py
git commit -m "refactor: simplify tier checks to single-role member model"
```

---

## Task 3: Protocol and Handler Cleanup

**Files:**
- Modify: `modules/protocol.py`
- Modify: `modules/protocol_handlers.py`

### protocol.py

- [ ] **Step 1: Clean stale comments**

```python
# Line 67: Change
#     GOSSIP (Phase 2), INTENT (Phase 3), VOUCH/BAN/PROMOTION (Phase 5)
# To:
#     GOSSIP (Phase 2), INTENT (Phase 3), BAN/MEMBERSHIP (Phase 5)

# Line 70: Change
HELLO = 32769       # Ticket presentation
# To:
HELLO = 32769       # Join request presentation

# Line 264: Change the serialize example
>>> data = serialize(HiveMessageType.HELLO, {"ticket": "abc123..."})
# To:
>>> data = serialize(HiveMessageType.HELLO, {"pubkey": "02abc123..."})

# Line 771: Change
Channel existence serves as proof of stake - no ticket needed.
# To:
Channel existence serves as proof of stake.
```

### protocol_handlers.py

- [ ] **Step 2: Remove admin tier validation**

```python
# Line 399: Change
# Start as member — admin promotion is done via RPC by the fleet operator.
# To:
# Start as member — single-role model, all members have equal privileges.

# Lines 734-736: Change
# Validate tier value (admin/member system)
if tier not in ("admin", "member"):
    tier = "member"
# To:
# Validate tier value (single-role model)
if tier != "member":
    tier = "member"
```

- [ ] **Step 3: Clean promotion/quorum references**

```python
# Line 1364: Change
# PHASE 5: PROMOTION PROTOCOL HANDLERS
# To:
# PHASE 5: MEMBERSHIP PROTOCOL HELPERS

# Line 1864: Delete the line
# - Neophyte: dynamic strategy (normal fee behavior)

# Lines 2061-2062: Change
# BAN is broadcast by the node that first reaches quorum in _check_ban_quorum.
# Most nodes will have already executed the ban independently when they tallied
# enough BAN_VOTEs.  This handler acts as a catch-up mechanism: if this node
# missed some votes and hasn't banned the target yet, we enforce it now.
# To:
# BAN is broadcast by the banning member to notify the fleet.
# This handler is idempotent — if we've already banned the target, it's a no-op.

# Line 2073: Change
reason = payload.get("reason", "quorum_ban")
# To:
reason = payload.get("reason", "member_ban")
```

- [ ] **Step 4: Run tests**

Run: `cd /home/sat/bin/cl-hive/.worktrees/trusted-fleet-simplification && python3 -m pytest tests/test_protocol.py tests/test_protocol_versioning.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add modules/protocol.py modules/protocol_handlers.py
git commit -m "refactor: clean protocol comments and remove admin tier validation"
```

---

## Task 4: RPC Commands and Permission System

**Files:**
- Modify: `modules/rpc_commands.py`
- Modify: `cl-hive.py`

### rpc_commands.py

- [ ] **Step 1: Remove admin permission check**

```python
# Lines 215-234: Replace entire check_permission function
def check_permission(ctx: HiveContext, required_tier: str) -> Optional[Dict[str, Any]]:
    ...
# To:
def check_permission(ctx: HiveContext, required_tier: str = 'member') -> Optional[Dict[str, Any]]:
    """
    Check if the local node is a hive member.

    All members have equal privileges in the single-role model.

    Returns:
        None if permission granted, or error dict if denied.
    """
    if not ctx.our_pubkey or not ctx.database:
        return {"error": "Not initialized"}

    member = ctx.database.get_member(ctx.our_pubkey)
    if not member:
        return {"error": "Not a Hive member", "required_tier": required_tier}

    return None  # Permission granted
```

- [ ] **Step 2: Remove admin_count from status**

```python
# Line 252: Change
#     Dict with hive state, member count, governance mode, etc.
# To:
#     Dict with hive state and member count.

# Line 258: Delete
admin_count = len([m for m in members if m['tier'] == 'admin'])

# Line 288: Change
"admin": admin_count,
# To: (delete this line entirely)
```

### cl-hive.py

- [ ] **Step 3: Clean all permission docstrings**

Replace every occurrence of these patterns:
- `Permission: Admin only` → `Permission: Any member`
- `Permission: Member or Admin` → `Permission: Any member`
- `# Permission check: Admin only (test commands)` → `# Permission check: member`
- `# Permission check: Member or Admin` → `# Permission check: member`

- [ ] **Step 4: Clean stale docstrings in cl-hive.py**

```python
# Line 1574: Change
#     Dict with hive state, member count, governance mode, etc.
# To:
#     Dict with hive state and member count.

# Line 1623: Change
# Permission: Admin only
# To:
# Permission: Any member

# Line 3681: Change
# Remove a member from the hive (admin maintenance).
# To:
# Remove a member from the hive (fleet maintenance).

# Line 1629: Change
# List all Hive members with their tier and stats.
# To:
# List all Hive members with their stats.
```

- [ ] **Step 5: Run tests**

Run: `cd /home/sat/bin/cl-hive/.worktrees/trusted-fleet-simplification && python3 -m pytest tests/test_rpc.py tests/test_rpc_commands_audit.py -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add modules/rpc_commands.py cl-hive.py
git commit -m "refactor: remove admin permission tier, all members have equal privileges"
```

---

## Task 5: Database Schema Cleanup

**Files:**
- Modify: `modules/database.py`

- [ ] **Step 1: Clean tier references in queries and docstrings**

```python
# Line 31: Change
# - Member registry (peer_id, tier, contribution, uptime)
# To:
# - Member registry (peer_id, contribution, uptime)

# Line 585: Change
# tier: Membership tier (always 'member')
# To: (delete this line)

# Line 616: Change
"SELECT * FROM hive_members ORDER BY tier, joined_at LIMIT 1000"
# To:
"SELECT * FROM hive_members ORDER BY joined_at LIMIT 1000"

# Line 624-626: Change
# Includes peer_id and tier for each member, sorted by peer_id.
# FULL_SYNC when tiers differ.
# To:
# Includes peer_id and tier for each member, sorted by peer_id.
```

Note: The `tier` column itself stays in the schema — it defaults to 'member' and removing it would require a migration. The column is harmless and used in FULL_SYNC wire format.

- [ ] **Step 2: Run database tests**

Run: `cd /home/sat/bin/cl-hive/.worktrees/trusted-fleet-simplification && python3 -m pytest tests/test_database_audit.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add modules/database.py
git commit -m "refactor: clean tier references from database queries and docstrings"
```

---

## Task 6: Test File Cleanup

**Files:**
- Modify: `tests/test_rpc.py`
- Modify: `tests/test_rebalancing_activity.py`
- Modify: `tests/test_crypto_integration.py`
- Modify: `tests/test_protocol.py`
- Modify: `tests/test_planner.py`
- Modify: `tests/test_planner_simulation.py`
- Modify: `tests/test_security.py`
- Modify: `tests/test_issue_59_60.py`

- [ ] **Step 1: Fix test_rpc.py**

```python
# Line 6: Change
# - Invite/Join Test: Generate ticket -> verify ticket structure
# To:
# - Approval Flow Test: Pending request -> approve -> verify membership

# Line 93: Change
founder = database.get_member(result['member_pubkey'])
# To:
member = database.get_member(result['member_pubkey'])

# Line 94-95: Change
assert founder is not None
assert founder['tier'] == 'member'
# To:
assert member is not None
assert member['tier'] == 'member'
```

- [ ] **Step 2: Fix test_rebalancing_activity.py**

```python
# Line 93: Change
OUR_PUBKEY: {"peer_id": OUR_PUBKEY, "tier": "admin"}
# To:
OUR_PUBKEY: {"peer_id": OUR_PUBKEY, "tier": "member"}

# Line 181: Change
self.db.members = {OUR_PUBKEY: {"peer_id": OUR_PUBKEY, "tier": "admin"}}
# To:
self.db.members = {OUR_PUBKEY: {"peer_id": OUR_PUBKEY, "tier": "member"}}
```

- [ ] **Step 3: Fix test_crypto_integration.py**

```python
# Line 9: Change
# - Full ticket verification flow (Genesis → Invite → Join)
# To:
# - Full handshake verification flow (Genesis → HELLO → Approve → Join)

# Line 339: Change
"""Test signing and verifying JSON-structured messages (like tickets)."""
# To:
"""Test signing and verifying JSON-structured messages."""

# Lines 340-341: Change
ticket_data = {
    "admin_pubkey": node_a_pubkey,
# To:
message_data = {
    "member_pubkey": node_a_pubkey,

# Line 349: Change
message = json.dumps(ticket_data, sort_keys=True, separators=(',', ':'))
# To:
message = json.dumps(message_data, sort_keys=True, separators=(',', ':'))
```

- [ ] **Step 4: Fix test_protocol.py**

```python
# Line 8: Change
# 4. Ticket Expiry - Expired tickets are rejected
# To:
# 4. Serialization round-trip tests

# Line 90: Change
original_payload = {"ticket": "base64encodedticket", "protocol_version": 1}
# To:
original_payload = {"pubkey": "02abcdef1234", "protocol_version": 1}

# Line 96: Change
assert payload['ticket'] == original_payload['ticket']
# To:
assert payload['pubkey'] == original_payload['pubkey']
```

- [ ] **Step 5: Fix remaining test comments**

```python
# tests/test_planner.py line 2: Change
# Tests for Phase 6: Planner Module (Ticket 6-01)
# To:
# Tests for Phase 6: Planner Module

# tests/test_planner.py line 8: Delete
# - Governance mode behavior

# tests/test_planner.py line 598: Change
# EXPANSION LOGIC TESTS (Ticket 6-02)
# To:
# EXPANSION LOGIC TESTS

# tests/test_planner_simulation.py line 2: Change
# Simulation & Game Theory Tests for the Planner (Ticket 6-05)
# To:
# Simulation & Game Theory Tests for the Planner

# tests/test_security.py line 2: Change
# Tests for Ticket S-01: Critical Security Hardening
# To:
# Tests for Critical Security Hardening

# tests/test_issue_59_60.py line 6: Change
# Issue #60: A promoted member has null addresses.
# To:
# Issue #60: A member has null addresses.
```

- [ ] **Step 6: Run full test suite**

Run: `cd /home/sat/bin/cl-hive/.worktrees/trusted-fleet-simplification && python3 -m pytest tests/ -v`
Expected: All pass (or known-skipped tests only)

- [ ] **Step 7: Commit**

```bash
git add tests/
git commit -m "test: remove admin/invite/ticket references from test files"
```

---

## Task 7: Documentation Rewrite

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `docker/README.md`
- Modify: `docker/runbooks/emergency-shutdown.md`
- Modify: `docker/runbooks/database-corruption.md`

- [ ] **Step 1: Rewrite README.md Quick Start and Coordination section**

Replace invite/join section (lines 42-53):
```markdown
### Add a Member

A new node joins by opening a channel to any existing member:

```bash
# On the new node: open a channel to a fleet member
lightning-cli connect <member-pubkey>@<host>:<port>
lightning-cli fundchannel <member-pubkey> 1000000
```

cl-hive sends a HELLO automatically. An existing member approves:

```bash
# On any existing member's node:
lightning-cli hive-pending
lightning-cli hive-approve <new-node-pubkey>
```
```

Replace coordination section (lines 74-79):
```markdown
### Coordination (consensus across fleet)

- Membership management (any member can approve/remove/ban)
- Intent Lock protocol for conflict-free channel opens
- Gossip-based state synchronization with anti-entropy
```

Replace RPC table (lines 88-89):
```markdown
| `hive-approve <pubkey>` | Approve a pending join request |
| `hive-pending` | List pending join requests |
```

- [ ] **Step 2: Fix CLAUDE.md**

```markdown
# Line 160: Change
**Membership & Admin**:
# To:
**Membership**:
```

- [ ] **Step 3: Fix docker/README.md invite section**

Replace invite/join examples (lines 389-398) with approve flow or delete the section if it's a stale subsection.

- [ ] **Step 4: Fix docker runbooks**

```markdown
# docker/runbooks/emergency-shutdown.md line 79: Change
- **Hive Admin**: [Configure in your deployment]
# To:
- **Fleet Operator**: [Configure in your deployment]

# docker/runbooks/database-corruption.md line 107: Change
docker-compose exec cln lightning-cli hive-join "YOUR_INVITE_CODE"
# To:
# (After restoring, rejoin by having an existing member run hive-approve)
```

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md docker/
git commit -m "docs: rewrite documentation for single-role membership model"
```

---

## Task 8: Final Validation

- [ ] **Step 1: Grep for remaining old-model language**

Run: `cd /home/sat/bin/cl-hive/.worktrees/trusted-fleet-simplification && grep -rn --include='*.py' --include='*.md' -i 'admin\|invite\|ticket\|neophyte\|quorum\|vouch\|promote' --exclude-dir=audits --exclude-dir=docs/plans --exclude=CHANGELOG.md | grep -v '__pycache__' | grep -v '.pyc'`

Review each remaining hit. Acceptable hits:
- `NET_ADMIN` (Linux capability, not hive admin)
- `administrator` in docker networking context
- Generic uses of "ticket" (Jira ticket, etc.) that aren't invite tickets
- `admin` as an RPC override flag in cl-revenue-ops (not cl-hive)

- [ ] **Step 2: Run full test suite**

Run: `cd /home/sat/bin/cl-hive/.worktrees/trusted-fleet-simplification && python3 -m pytest tests/ -v --tb=short 2>&1 | tail -30`
Expected: All tests pass

- [ ] **Step 3: Verify no broken imports**

Run: `cd /home/sat/bin/cl-hive/.worktrees/trusted-fleet-simplification && python3 -c "import sys; sys.path.insert(0,'.'); exec(open('cl-hive.py').read())" 2>&1 | head -5 || echo "Import check requires CLN runtime - verify manually"`

- [ ] **Step 4: Final commit (if any fixups needed)**

```bash
git add -A
git commit -m "fix: resolve remaining old-model references found in validation"
```

---

## Summary Checklist

After all tasks, verify:

- [ ] No `admin` role in membership logic
- [ ] No `MembershipTier` enum
- [ ] No invite-ticket system
- [ ] No quorum/voting/vouch logic
- [ ] No promotion pipeline
- [ ] `check_permission` treats all members equally
- [ ] `hive-status` reports only `member` count (no `admin` count)
- [ ] All docstrings say "Any member" not "Admin only" or "Member or Admin"
- [ ] README describes channel+approve join flow
- [ ] JOINING_THE_HIVE.md is correct (already done)
- [ ] All tests pass
- [ ] `governance.py` retained as minimal RecommendationLogger
- [ ] `contribution.py` retained for anti-leech safety
- [ ] `phase6_ingest.py` retained for cl-hive-comms transport
- [ ] No new modules created
- [ ] No compatibility layers added
