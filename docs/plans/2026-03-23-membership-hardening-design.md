# Membership Hardening Design

**Date:** 2026-03-23

**Goal:** Fix the audited membership security and convergence failures without breaking interoperability with already-deployed cl-hive nodes.

## Context

The current membership surface has five concrete failures:

1. `BAN` can remove members without authenticating the sender.
2. `hive-ban` and `hive-remove-member` do not converge across the fleet.
3. `CHALLENGE` can turn the node into a signing oracle because it does not require an outbound join attempt.
4. `MEMBER_LEFT` accepts signed-but-stale leave events and can evict rejoined members.
5. `ATTEST` commits the member locally before `WELCOME` delivery succeeds.

The fix must remain backward compatible. That means upgraded nodes must continue to interoperate with existing nodes, and any new fields or message types must degrade safely when an older node ignores them.

## Compatibility Model

The hardening strategy keeps the current HELLO/CHALLENGE/ATTEST/WELCOME, BAN, MEMBER_LEFT, FULL_SYNC, and RPC surfaces intact.

- Existing message types stay valid.
- New fields are additive and optional.
- New message types use odd experimental IDs so older nodes ignore them safely.
- Existing FULL_SYNC signing stays unchanged so upgraded receivers still accept FULL_SYNC from older senders.

Mixed fleets stay operational. Full convergence for the new manual-removal flow requires upgraded peers, but bans and voluntary leaves continue to propagate to older peers through the existing message types.

## Design Decisions

### 1. Gate `CHALLENGE` on outbound join state

`handle_challenge()` should only produce an `ATTEST` if we previously sent a HELLO to that peer and the outbound HELLO is still fresh. The necessary tracking already exists in `HandshakeManager`.

Result:

- unsolicited `CHALLENGE` no longer triggers signing work
- normal manual join and auto-join continue to work

### 2. Make `ATTEST` activation commit-on-send

The join path should not add the candidate to `hive_members` until the local node has successfully handed the `WELCOME` frame to CLN for delivery.

Implementation rule:

- validate ATTEST
- build and send WELCOME
- if local send fails, keep the challenge pending and do not add the member
- if local send succeeds, add the member, persist capabilities/addresses/presence, clear the challenge, and broadcast sync

This is not a perfect end-to-end ack, but it removes the local inconsistency that currently creates a ghost member on local send failure.

### 3. Harden BAN without breaking legacy peers

`BAN` gets a backward-compatible authenticated envelope:

- `sender_id`
- `timestamp`
- `signature`
- `event_id`

Upgraded nodes verify these fields when present. Legacy BAN payloads remain accepted only from a directly connected, current, non-banned hive member. Non-member BAN senders are always rejected.

`hive-ban` must also start broadcasting the BAN it creates locally. That fixes live ban propagation for both upgraded and older peers because older peers already understand the BAN message type and will ignore the extra fields.

### 4. Add freshness and rejoin protection to `MEMBER_LEFT`

`handle_member_left()` should apply the same basic event hygiene used elsewhere:

- reject future-skewed timestamps
- reject very old leave events
- reject leave events whose timestamp is older than the target member's current `joined_at`

The `joined_at` comparison is the key replay defense for rejoin scenarios. A valid old leave signature must not evict a member who has since rejoined.

Received leave events should also use the same removal helper as manual removals and bans so state-manager cleanup stays consistent.

### 5. Persist membership tombstones

Membership deletions need durable state, not just transient logs. Add a dedicated `membership_tombstones` table with enough information to replay removals safely:

- `event_id`
- `peer_id`
- `event`
- `actor_peer_id`
- `reason`
- `timestamp`
- `joined_at_cutoff`

The tombstone store is the source of truth for:

- replay-safe application of deletes
- offline catch-up for upgraded peers
- suppressing stale re-adds during sync

### 6. Propagate manual removals with a new message, and catch them up through FULL_SYNC

Ban and leave already have message types. Manual remove does not, so add `MEMBER_REMOVED` as a new signed event for upgraded peers.

Live propagation:

- `hive-remove-member` emits `MEMBER_REMOVED`
- upgraded peers verify and apply it immediately
- older peers ignore it safely

Catch-up path:

- FULL_SYNC keeps its current signed payload
- upgraded senders append optional `membership_events` from the tombstone table
- upgraded receivers apply those events before the normal add-only member merge
- older receivers ignore the extra field

This keeps FULL_SYNC backward compatible while still giving upgraded peers deterministic deletion convergence after restart or downtime. The `membership_events` list is transport-authenticated by the direct BOLT 8 peer channel, but not included in the legacy FULL_SYNC application-layer signing payload. That trade-off is deliberate to preserve mixed-fleet compatibility.

## Data Flow

### Join

1. Candidate sends HELLO.
2. Member approves and sends CHALLENGE.
3. Candidate accepts CHALLENGE only if an outbound HELLO to that peer is pending.
4. Candidate sends ATTEST.
5. Approver validates ATTEST, sends WELCOME, then commits membership locally.
6. Approver broadcasts updated FULL_SYNC to current members.

### Ban

1. Operator runs `hive-ban`.
2. Local node persists ban + tombstone and removes the member locally.
3. Local node broadcasts BAN with optional authenticated envelope.
4. Peers reject BAN from non-members, verify upgraded envelope when present, and apply idempotently.

### Manual Removal

1. Operator runs `hive-remove-member`.
2. Local node persists a removal tombstone and removes the member locally.
3. Local node broadcasts `MEMBER_REMOVED` to upgraded peers.
4. Offline upgraded peers catch up through FULL_SYNC `membership_events`.

### Voluntary Leave

1. Leaving node broadcasts MEMBER_LEFT.
2. Receivers enforce freshness and `joined_at` replay protection.
3. Receivers record a tombstone and remove the member through the shared helper.

## Testing Strategy

Add direct handler-level tests for the failure modes that escaped the current suite:

- BAN from a non-member
- legacy BAN from a direct current member
- unsolicited CHALLENGE
- ATTEST with local WELCOME send failure
- stale MEMBER_LEFT
- pre-rejoin MEMBER_LEFT replay
- MEMBER_REMOVED propagation and validation
- FULL_SYNC catch-up applying tombstones before add-only membership merge

Also keep the current regression suite green:

- `tests/test_membership.py`
- `tests/test_rpc.py`
- `tests/test_security.py`
- `tests/test_state.py`

## Rollout Notes

- Older nodes keep participating in the fleet.
- BAN propagation becomes better immediately because old nodes already process BAN.
- Manual remove convergence becomes immediate only between upgraded nodes.
- Offline upgraded peers catch up via FULL_SYNC `membership_events`.
- Once the fleet is fully upgraded, the manual-removal convergence gap disappears.
