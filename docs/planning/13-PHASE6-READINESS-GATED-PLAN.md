# Phase 6 Readiness-Gated Plan

**Status:** Planning-only (implementation deferred)  
**Last Updated:** 2026-02-17  
**Scope:** Phase 6 split into `cl-hive-comms`, `cl-hive-archon`, and `cl-hive` repos and plugins

---

## 1. Decision

Phase 6 is approved for detailed planning and repo scaffolding, but not for feature implementation until Phases 1-5 are production ready.

This means:
- Allowed now: architecture docs, rollout docs, repo scaffolds, CI/release planning, test plans.
- Blocked now: production code extraction/refactor of runtime behavior into new plugins.

---

## 2. Repo Topology (Lightning-Goats)

Target GitHub repos:
- `lightning-goats/cl-hive` (existing, coordination plugin)
- `lightning-goats/cl-hive-comms` (new, transport/payment/policy entry-point)
- `lightning-goats/cl-hive-archon` (new, DID/Archon identity layer)

Expected local workspace layout:
- `~/bin/cl-hive`
- `~/bin/cl_revenue_ops`
- `~/bin/cl-hive-comms`
- `~/bin/cl-hive-archon`

Notes:
- New repos can be created now as empty/skeleton repos.
- Runtime plugin extraction is deferred until gates in Section 4 pass.

---

## 3. Ownership Boundaries (Planned)

`cl-hive-comms` owns:
- Transport abstraction and Nostr connectivity
- Marketplace client and liquidity marketplace client
- Payment routing (Bolt11/Bolt12/L402/Cashu hooks)
- Policy engine and client-oriented RPC surface
- Tables: `nostr_state`, `management_receipts`, `marketplace_*`, `liquidity_*`

`cl-hive-archon` owns:
- Archon DID provisioning and DID bindings
- Credential verification upgrade path and revocation checks
- Dmail transport registration
- Vault/backup/recovery integrations
- Tables: `did_credentials`, `did_reputation_cache`, `archon_*`

`cl-hive` owns:
- Gossip, topology, settlements, governance, fleet coordination
- Existing hive membership/economics/state management
- Tables: existing hive tables plus `settlement_*`, `escrow_*`

---

## 4. Implementation Unblock Gates

All gates must pass before any Phase 6 code extraction starts.

### Gate A: Reliability
- `python3 -m pytest tests -q` green on release branch.
- No open high-priority defects in active Phases 1-5.
- No new Sev1/Sev2 incidents during soak window (recommended: 14 days).

### Gate B: Operational Readiness
- Docker rollout and rollback runbooks complete and validated.
- Manual non-docker install/upgrade/rollback guide validated.
- Database backup/restore workflow verified against current production schema.

### Gate C: Security & Audit
- High/medium audit findings for active Phase 1-5 paths resolved or explicitly accepted with compensating controls.
- RPC allowlist and MCP method surface reviewed for split architecture.

### Gate D: Compatibility
- Plugin dependency matrix documented and validated:
  - `cl-hive-comms` standalone
  - `cl-hive-comms + cl-hive-archon`
  - `cl-hive-comms + cl-hive`
  - full 3-plugin stack
- Backward compatibility path for existing monolith deployments documented.

---

## 5. Pre-Implementation Deliverables (Allowed Now)

1. Repo scaffolding
- Create local repos under `~/bin`.
- Create GitHub repos in `lightning-goats` when approved.
- Add branch protection and CI placeholders.

2. Design freeze docs
- API boundaries and ownership map.
- Table ownership and cross-plugin read-only policy.
- Plugin startup order and failure modes.

3. Deployment docs
- Docker integration plan for optional plugin enablement.
- Manual install/upgrade guide for existing non-docker members.

4. Test strategy
- Define integration test matrix and acceptance criteria.
- Define migration/no-migration verification checks.

---

## 6. Planned Rollout Sequence (After Gates Pass)

1. `cl-hive-comms` alpha release (standalone mode, no `cl-hive` dependency)
2. `cl-hive-archon` alpha release (requires `cl-hive-comms`)
3. `cl-hive` compatibility release with sibling plugin detection
4. Canary deployment on one node
5. Staged rollout to remaining nodes
6. Default-enable policy only after stability window completes

---

## 7. Acceptance Criteria for Phase 6 Start

Phase 6 implementation may begin only when:
- All gates in Section 4 are green.
- Maintainers explicitly mark this plan as "Execution Approved".
- A release tag for the final Phase 5 production baseline is cut.

