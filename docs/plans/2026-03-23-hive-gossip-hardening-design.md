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

The fix must remain backward-compatible with already-deployed nodes.

## Constraints

- Keep existing message types and current signatures working during a rolling upgrade.
- Do not preserve the audited exploit paths just to keep feature parity with old nodes.
- Prefer additive fields that older nodes ignore cleanly.
- Mixed fleets may degrade in catch-up quality temporarily, but upgraded nodes must not keep trusting unsafe legacy data.

## Approaches Considered

### 1. Strict v2-only protocol cutover

Require all upgraded nodes to reject legacy `GOSSIP`, `STATE_HASH`, and `FULL_SYNC` formats unless they carry the new stronger signatures.

This closes the issues cleanly, but it partitions mixed fleets and does not satisfy the compatibility requirement.

### 2. Additive integrity layer on top of the current protocol

Keep the existing message shapes and legacy signature checks, then add optional v2 integrity fields covering the missing content. Upgraded receivers prefer v2 proofs when present and sharply reduce what legacy payloads are allowed to mutate.

This keeps rolling upgrades possible while removing the high-severity trust failures. This is the selected approach.

### 3. Immediate legacy lock-down with no richer v2 path

Stop trusting legacy `FULL_SYNC` and relayed optional gossip fields immediately, but do not add stronger replacement proofs yet.

This is safe, but it unnecessarily degrades convergence because upgraded peers would have no authenticated full-snapshot path during the rollout.

## Selected Design

### 1. Additive v2 integrity fields

Upgraded senders continue emitting the existing legacy signature so old nodes remain interoperable. They also add stronger v2 proof fields that old nodes ignore.

Planned additions:

- `GOSSIP`
  - Add a canonical v2 content hash that covers all trusted fields: core state, budget fields, `addresses`, and `capabilities`.
  - Add `signature_v2` over `{sender_id, timestamp, version, fleet_hash/state_hash, content_hash_v2}`.
- `STATE_HASH`
  - Add `signature_v2` that covers `{sender_id, fleet_hash, membership_hash, peer_count, timestamp}`.
- `FULL_SYNC`
  - Add `states_hash_v2` over fully normalized state rows.
  - Add `members_hash_v2` over fully normalized member rows, including `addresses`.
  - Add `signature_v2` over `{sender_id, timestamp, states_hash_v2, members_hash_v2}`.

The legacy signature stays unchanged during rollout.

### 2. Receiver trust policy

Upgraded receivers split behavior into trusted v2 and reduced-trust legacy handling.

`GOSSIP`:

- If v2 proof is present and valid, trust the full payload.
- If only the legacy proof is present, still accept core state from the sender, but strip unsigned optional fields before persistence and side effects.
- Specifically: do not persist `addresses`, do not auto-connect from relayed legacy gossip, and do not trust legacy `capabilities` or budget fields for decision-making.

`STATE_HASH`:

- If v2 proof is present and valid, trust `membership_hash`.
- If only the legacy proof is present, treat `membership_hash` as advisory and do not take membership actions based solely on it.

`FULL_SYNC`:

- If v2 proof is present and valid, accept full state rows and member rows after schema validation.
- If only the legacy proof is present, do not allow the sender to mutate foreign peer state or membership.
- Safe compatibility rule: legacy `FULL_SYNC` may update at most the sender's own state row and must ignore the `members` array entirely.

This preserves wire compatibility but intentionally removes unsafe legacy authority.

### 3. Validation and normalization

Schema validation must match the fields that receivers later trust.

Required hardening:

- Validate member `peer_id` as an actual node pubkey before syncing membership.
- Validate `addresses` as bounded lists of strings before persistence or auto-connect.
- Normalize `topology`, `addresses`, and `capabilities` deterministically before hashing.
- Centralize one network `MAX_FULL_SYNC_STATES` value used by both validation and processing.

### 4. Persistence and restart stability

Remote state must preserve the sender-authored timestamp across restarts.

Changes:

- Store the remote payload timestamp for remote state instead of local receipt time.
- Thread that timestamp through `StateManager.update_peer_state()`, `StateManager.apply_full_sync()`, and `HiveDatabase.update_hive_state()`.
- Remove the silent `LIMIT 1000` reload truncation from `get_all_hive_states()`.
- Keep version guards, but do not rewrite `last_update` semantics during persistence.

### 5. Testing strategy

The rollout should be test-driven and mixed-fleet aware.

Required test coverage:

- legacy vs v2 `GOSSIP` handling for relayed addresses
- legacy vs v2 `STATE_HASH` handling for `membership_hash`
- legacy vs v2 `FULL_SYNC` handling for foreign state rows and membership rows
- invalid member/address rejection in membership sync
- restart hash stability for remote state
- unified FULL_SYNC state-count limits

## Rollout Notes

- This design is backward-compatible at the wire level.
- It is not legacy-feature-compatible in every case: upgraded nodes will intentionally trust less data from legacy peers until those peers also emit v2 proofs.
- That is the correct tradeoff for the audited high-severity issues.
