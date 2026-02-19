# Lightning Hive System Overview

**Status:** Living overview  
**Last Updated:** 2026-02-19

---

## 1. What This System Does

Lightning Hive is a protocol + plugin suite for operating Lightning nodes with:

- shared coordination between trusted fleet members
- advisor/client management for non-hive nodes
- verifiable identity and reputation
- conditional payments/escrow for managed actions
- marketplace discovery for advisor and liquidity services

In short: it turns Lightning node operations into a programmable, auditable, and market-driven system.

---

## 2. Why It Exists

The suite addresses three practical problems:

1. Node operations are hard to do consistently by hand (fees, rebalances, channel strategy, risk controls).
2. Trust is weak in ad-hoc remote management (who can execute what, under what limits, with what evidence).
3. Discovery and contracting are fragmented (finding reliable advisors/liquidity providers is manual and opaque).

Hive combines identity, policy, transport, and payments so remote management can be safer and repeatable.

---

## 3. Main Building Blocks

### Core runtime components

- `cl-hive`  
  Fleet coordination plugin for hive members: gossip, topology, economics, governance, settlements.

- `cl-hive-comms` (Phase 6 planned entry point)  
  Client-facing transport + policy + payment layer: Nostr/REST transport, schema execution, receipts, marketplace + liquidity client features.

- `cl-hive-archon` (Phase 6 planned optional add-on)  
  DID/Archon identity layer: DID provisioning/bindings, credential verification, dmail/vault/recovery integrations.

- `cl-revenue-ops`  
  Local profitability and fee-control companion. Integrates with hive for policy and execution flows.

### Economic/security primitives

- DID credentials + reputation claims
- management schemas + danger scoring
- Cashu escrow tickets (conditional execution/payment)
- settlement accounting and fair-share distribution
- policy engine constraints as operator last-line defense

---

## 4. Plugin Boundary Model (Current Plan)

Phase 6 planning currently defines a **3-plugin split**:

- `cl-hive-comms`: transport/payment/policy/marketplace/liquidity tables
- `cl-hive-archon`: DID/credential/Archon tables
- `cl-hive`: fleet coordination/economics/settlement tables

Marketplace functions are planned to stay inside `cl-hive-comms` at plugin boundary level, with feature flags for optional behavior (not a separate marketplace plugin at Phase 6 start).

Reference: [13-PHASE6-READINESS-GATED-PLAN.md](./13-PHASE6-READINESS-GATED-PLAN.md)

---

## 5. End-to-End Flow (Simplified)

1. A node receives a management intent (Nostr or REST/rune).
2. Credential + schema + policy checks run.
3. If payment conditions apply, escrow/payment path is prepared.
4. Command is translated to local node actions (CLN RPC, and swap/payment integrations as needed).
5. Result is logged in tamper-evident receipts.
6. Reputation and settlement/accounting paths consume outcomes over time.

---

## 6. Phases At A Glance

### Foundation and core

- Phase 1: DID credential foundation
- Phase 2: Management schemas + danger scoring
- Phase 3: Coordination and execution hardening
- Phase 4: Cashu escrow + extended settlements
- Phase 5: Nostr transport + marketplace/liquidity functionality

### Planned architectural split

- Phase 6: Runtime split into `cl-hive-comms` + `cl-hive-archon` + `cl-hive` with readiness gates

Reference plans: [11-IMPLEMENTATION-PLAN.md](./11-IMPLEMENTATION-PLAN.md), [12-IMPLEMENTATION-PLAN-PHASE4-6.md](./12-IMPLEMENTATION-PLAN-PHASE4-6.md), [13-PHASE6-READINESS-GATED-PLAN.md](./13-PHASE6-READINESS-GATED-PLAN.md)

---

## 7. How To Read The Planning Docs

For a quick orientation path:

1. This overview (`15`)
2. Client architecture (`08`)
3. Implementation plans (`11`, `12`, `13`)
4. Deep protocol specs (`01`–`07`, `09`, `10`) as needed

---

## 8. Operational Posture

- Phase 6 implementation is gated until earlier phases are production-ready.
- Repo scaffolding and architecture planning are allowed in advance.
- Rollout is intended to be staged with compatibility checks and rollback paths.

This is deliberate: stabilize core economics/control loops first, then extract runtime boundaries.
