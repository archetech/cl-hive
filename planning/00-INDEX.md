# Lightning Hive Protocol Suite — Planning Documents

**Status:** Phase 1 Implemented
**Last Updated:** 2026-02-19
**Author:** Hex (`did:cid:bagaaierajrr7k6izcrdfwqxpgtrobflsv5oibymfnthjazkkokaugszyh4ka`)

---

## Document Index

Documents are numbered by dependency order: foundational specs first, implementation plans last.

| # | Document | Status | Description |
|---|----------|--------|-------------|
| 01 | [Reputation Schema](./01-REPUTATION-SCHEMA.md) | **Phase 1 Implemented** | `DIDReputationCredential` — W3C VC schema for agent/node/service reputation. Domain-specific profiles for Lightning metrics. Foundation for trust across all protocols. **Core implemented in `modules/did_credentials.py`** (commit cd4c60a). |
| 02 | [Fleet Management](./02-FLEET-MANAGEMENT.md) | Draft | DID + L402 remote fleet management protocol. Authenticated, paid commands via Nostr DM (primary) and REST/rune (secondary). Advisor↔node interaction model. |
| 03 | [Cashu Task Escrow](./03-CASHU-TASK-ESCROW.md) | Draft | Conditional Cashu ecash tokens as escrow for agent task execution. NUT-10/11/14 (P2PK + HTLC + timelock). Atomic task completion ↔ payment release. |
| 04 | [Hive Marketplace](./04-HIVE-MARKETPLACE.md) | Draft | Decentralized marketplace for advisor management services. Service discovery, negotiation, contract formation. DID-authenticated, reputation-ranked, Cashu-escrowed. |
| 05 | [Nostr Marketplace](./05-NOSTR-MARKETPLACE.md) | Draft | Public marketplace layer on Nostr. Unified event kinds, relay strategy, service advertising. Any Nostr client can browse services without hive membership. Supersedes Nostr sections in 04 and 07. |
| 06 | [Hive Settlements](./06-HIVE-SETTLEMENTS.md) | Draft | Trustless settlement protocol — revenue shares, rebalancing costs, liquidity leases, penalties. Obligation tracking, netting, Cashu escrow settlement. |
| 07 | [Hive Liquidity](./07-HIVE-LIQUIDITY.md) | Draft | Liquidity-as-a-Service marketplace. 9 service types, 6 pricing models. Channel leases, JIT, swaps, pools, insurance. Turns liquidity into a commodity. |
| 08 | [Hive Client](./08-HIVE-CLIENT.md) | Draft | Client-side architecture — 3 independently installable CLN plugins: `cl-hive-comms` (Nostr + REST transport), `cl-hive-archon` (DID + VC), `cl-hive` (coordination). One plugin → all services. |
| 09 | [Archon Integration](./09-ARCHON-INTEGRATION.md) | Draft | Optional Archon DID integration for governance messaging. Tiered participation: Basic (routing, no DID) → Governance (voting, proposals, verified identity). |
| 10 | [Node Provisioning](./10-NODE-PROVISIONING.md) | Draft | Autonomous VPS lifecycle — provision, operate, and decommission self-sustaining Lightning nodes. Paid with Lightning. Revenue ≥ costs or graceful death. Capital allocation: 6.55M–19.46M sats. |
| 11 | [Implementation Plan (Phase 1–3)](./11-IMPLEMENTATION-PLAN.md) | **Phase 2 Complete** | Phased implementation roadmap. Dependency order: Reputation → Fleet Mgmt → Escrow → Marketplace → Settlements → Liquidity → Client. Python-first with Archon wired in later. Phase 1 (DID Credential Foundation) and Phase 2 (Management Schemas + Danger Scoring) implemented. |
| 12 | [Implementation Plan (Phase 4–6)](./12-IMPLEMENTATION-PLAN-PHASE4-6.md) | Draft | Later implementation phases. |
| 13 | [Phase 6 Readiness-Gated Plan](./13-PHASE6-READINESS-GATED-PLAN.md) | Planning-only | Detailed execution and rollout plan for the 3-plugin split (`cl-hive-comms`, `cl-hive-archon`, `cl-hive`) with explicit production-readiness gates, plus plugin-boundary decision notes (marketplace stays in `cl-hive-comms` with feature flags). Repos may be scaffolded in advance, but implementation remains gated. |
| 15 | [Hive System Overview](./15-HIVE-SYSTEM-OVERVIEW.md) | Living overview | High-level explanation of what the Lightning Hive suite does, plugin boundaries, core flows, and how the planning documents fit together. |

---

## Dependency Graph

```
                    ┌─────────────────┐
                    │ 01 Reputation   │ ← Foundation: trust scoring
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ 02 Fleet Mgmt   │ ← Core: advisor↔node protocol
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
     ┌────────▼───────┐ ┌───▼────────┐ ┌──▼──────────────┐
     │ 03 Task Escrow │ │ 09 Archon  │ │ 04 Marketplace  │
     └────────┬───────┘ └────────────┘ └──┬──────────────┘
              │                            │
              │                   ┌────────▼────────┐
              │                   │ 05 Nostr Mktpl  │
              │                   └────────┬────────┘
              │                            │
     ┌────────▼────────────────────────────▼──┐
     │           06 Settlements               │
     └────────────────┬───────────────────────┘
                      │
             ┌────────▼────────┐
             │ 07 Liquidity    │
             └────────┬────────┘
                      │
             ┌────────▼────────┐
             │ 08 Hive Client  │ ← User-facing: 3-plugin architecture
             └────────┬────────┘
                      │
             ┌────────▼────────┐
             │ 10 Provisioning │ ← Operational: autonomous node lifecycle
             └─────────────────┘
```

---

## Other Files

| File | Description |
|------|-------------|
| [TODO-route-history.md](./TODO-route-history.md) | Route history tracking implementation notes (internal) |

---

## How to Read

- **Operators** wanting to understand what the Hive offers: Start with **08 (Client)**, then **07 (Liquidity)** and **04 (Marketplace)**.
- **Developers** building the stack: Follow the dependency order **01 → 12**, or start with **11 (Implementation Plan)**.
- **Fleet members** joining the Hive: Read **09 (Archon)** for identity, **06 (Settlements)** for economics, **10 (Provisioning)** for node setup.
- **Economists** evaluating the model: Focus on **06 (Settlements)**, **03 (Escrow)**, **10 (Provisioning §8: Survival Economics)**.
