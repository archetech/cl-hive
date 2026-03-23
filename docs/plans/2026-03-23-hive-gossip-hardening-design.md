# Hive Gossip Hardening Design

**Date:** 2026-03-23
**Scope:** `GOSSIP`, `STATE_HASH`, `FULL_SYNC`, relay trust, and state persistence

## Problem Summary

The 2026-03-23 gossip audit confirmed five protocol and correctness failures in the current mainline implementation:

1. `FULL_SYNC` only authenticates minimal state tuples, so any current member can forge other peers' state.
2. `FULL_SYNC` membership sync trusts sender-supplied member rows and can inject synthetic members.
3. Relayed `GOSSIP` can tamper `addresses` because they are outside the signing contract and later trusted for persistence and auto-connect.
4. Remote state is persisted with local receipt time instead of the signed remote timestamp, so restart changes fleet hashes.
5. State-count limits disagree across validation, processing, and reload paths.

This design assumes upgraded nodes immediately reject all legacy `GOSSIP`, `STATE_HASH`, and `FULL_SYNC` traffic.

## Constraints

- This is an intentional protocol break for the state-sync surface.
- `HELLO`, `CHALLENGE`, `ATTEST`, `WELCOME`, and other unrelated message types do not need to change as part of this design.
- Upgraded nodes must not preserve any audited exploit path for compatibility.
- Mixed fleets are expected to partition at the gossip/state-sync layer until all participating peers are upgraded.

## Approaches Considered

### 1. Strict v2-only protocol cutover

Require all upgraded nodes to send and accept only a stronger v2 contract for `GOSSIP`, `STATE_HASH`, and `FULL_SYNC`.

This cleanly closes the audited trust failures, makes operator expectations explicit, and is the selected approach.

### 2. Additive integrity layer with legacy fallback

Keep legacy messages working, add optional v2 proofs, and continue to accept reduced-trust legacy payloads during rollout.

This helps rolling upgrades, but it intentionally preserves a compatibility surface around messages that were already shown to be unsafe. The user explicitly rejected this tradeoff.

### 3. Partial strictness

Require v2 only for `FULL_SYNC`, but keep accepting legacy `GOSSIP` and `STATE_HASH`.

This reduces some of the blast radius, but it leaves the relayed-address issue and unsigned `membership_hash` semantics in place. It is not sufficient.

## Selected Design

### 1. New required v2 contract for state-sync traffic

Upgraded senders emit only the stronger v2-authenticated form of these messages. Upgraded receivers reject the message before any state mutation if the v2 contract is missing or invalid.

Required fields:

- `GOSSIP`
  - `signature_v2`
  - canonical content hash covering:
    - `capacity_sats`
    - `available_sats`
    - `fee_policy`
    - `topology`
    - budget fields
    - `addresses`
    - `capabilities`
- `STATE_HASH`
  - `signature_v2`
  - canonical signing payload covering:
    - `sender_id`
    - `fleet_hash`
    - `membership_hash`
    - `peer_count`
    - `timestamp`
- `FULL_SYNC`
  - `states_hash_v2` over fully normalized state rows
  - `members_hash_v2` over fully normalized member rows
  - `signature_v2` over:
    - `sender_id`
    - `timestamp`
    - `states_hash_v2`
    - `members_hash_v2`

The existing legacy signatures may remain in payloads temporarily for debugging or transitional observability, but upgraded receivers ignore them for trust decisions.

### 2. Immediate receiver policy

`GOSSIP`

- Reject if `signature_v2` is missing.
- Reject if the v2 signing payload does not verify.
- Only after v2 verification:
  - persist `addresses`
  - trust `capabilities`
  - trust budget fields
  - allow auto-connect side effects

`STATE_HASH`

- Reject if `signature_v2` is missing.
- Reject if `membership_hash` is missing or the v2 signing payload does not verify.
- Treat `membership_hash` as authenticated input only in v2.

`FULL_SYNC`

- Reject if `signature_v2`, `states_hash_v2`, or `members_hash_v2` is missing.
- Reject if either v2 hash mismatches the provided rows.
- Reject invalid state rows and invalid member rows before applying any mutation.
- No legacy fallback path: legacy `FULL_SYNC` does not update sender state, foreign state, or membership.

### 3. Version signaling

The simplest cutover is to make these three message families explicitly v2-only on upgraded nodes:

- send them with envelope version `2`
- require envelope version `2` when handling them

That gives operators a clear network signal during rollout and prevents ambiguity about which contract is in force.

### 4. Validation and normalization

Schema validation must match the fields that receivers later trust.

Required hardening:

- Validate member `peer_id` as an actual node pubkey before syncing membership.
- Validate `addresses` as bounded lists of strings before persistence or auto-connect.
- Normalize `topology`, `addresses`, `capabilities`, and member rows deterministically before hashing.
- Centralize one network `MAX_FULL_SYNC_STATES` value used by both validation and processing.

### 5. Persistence and restart stability

Remote state must preserve the sender-authored timestamp across restarts.

Changes:

- Store the remote payload timestamp for remote state instead of local receipt time.
- Thread that timestamp through `StateManager.update_peer_state()`, `StateManager.apply_full_sync()`, and `HiveDatabase.update_hive_state()`.
- Remove the silent `LIMIT 1000` reload truncation from `get_all_hive_states()`.
- Keep version guards, but do not rewrite the logical update timestamp during persistence.

### 6. Testing strategy

The rollout should be test-driven and explicit about the intentional break.

Required test coverage:

- legacy `GOSSIP` is rejected
- legacy `STATE_HASH` is rejected
- legacy `FULL_SYNC` is rejected
- valid v2 `GOSSIP` persists `addresses` and permits auto-connect
- valid v2 `STATE_HASH` authenticates `membership_hash`
- valid v2 `FULL_SYNC` applies authenticated foreign state and authenticated member rows
- invalid v2 member/address rows are rejected
- restart hash stability for remote state
- unified FULL_SYNC state-count limits

## Rollout Notes

- This design is not backward-compatible for the state-sync surface.
- Upgraded nodes will refuse legacy `GOSSIP`, `STATE_HASH`, and `FULL_SYNC` immediately.
- Operators must coordinate upgrades across the fleet before expecting normal anti-entropy and membership convergence.
- That operational cost is justified by the audited high-severity trust failures.
