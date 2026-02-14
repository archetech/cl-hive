# Marketplace Spec Audit Report — Final

**Date:** 2026-02-14  
**Auditor:** Hex (subagent)  
**Iterations:** 2 (initial audit + self-audit pass)  
**Result:** PASS — all identified issues resolved

---

## Summary of Changes

### DID-HIVE-MARKETPLACE.md (8 changes)

1. **CRITICAL — VC 2.0 proof structure**: Replaced non-standard `counterSignature` field in contract credential with a proper `proof` array containing two entries (operator + advisor). VC 2.0 supports multiple proofs as an array; a custom field name breaks interoperability with VC libraries.

2. **CRITICAL — Reputation credential VC compliance**: Added `@context`, `type` array, and `validFrom` to both reputation credential examples in Section 9 (node-rates-advisor, advisor-rates-node). Previously these were bare fragments missing required VC 2.0 fields.

3. **CRITICAL — `hive:client` profile separation**: Changed advisor-rates-node credential from `domain: "hive:node"` to `domain: "hive:client"`. The metrics (`payment_timeliness`, `sla_reasonableness`, `communication_quality`, `infrastructure_reliability`) are marketplace-specific and don't belong in the `hive:node` profile. Updated accompanying note to reference the new profile and the Defining New Profiles process.

4. **IMPORTANT — Sealed-bid auction reveal phase**: Expanded the 5-step sealed-bid mechanism with explicit nonce reveal step, third-party auditability, and enumeration of attack vectors prevented (bid sharing, post-deadline insertion, bid suppression).

5. **IMPORTANT — Anti-trial-cycling protection**: Added new subsection in Section 5 with concrete protections: concurrent trial limit (2), sequential cooldown (14 days), trial history transparency, graduated pricing (2×/3× for repeat trials), and advisor opt-out rights.

6. **MINOR — Referral reputation snippet**: Clarified that the `hive:referrer` JSON is a `credentialSubject` excerpt within a full `DIDReputationCredential`, not a standalone structure.

7. **MINOR — Cross-reference update**: Updated "Using the `hive:node` profile" text to "Using the `hive:client` profile" with link to the new profile section.

8. **MINOR — Proof description update**: Updated text describing dual signatures to reference VC 2.0 proof arrays.

### DID-L402-FLEET-MANAGEMENT.md (1 change)

9. **CRITICAL — Bond amount alignment**: Fixed Permission Tier ↔ Settlement Privilege mapping table. Previous values (10k/50k/100k sats) contradicted the authoritative bond sizes in the Settlements spec (50k/150k/300k sats). Updated to match:
   - `standard` → Basic routing: 50,000 sats (was 10,000)
   - `advanced` → Full member: 150,000 sats (was 50,000)
   - `admin` → Liquidity provider: 300,000 sats (was 100,000)

### DID-REPUTATION-SCHEMA.md (1 change)

10. **IMPORTANT — New `hive:client` profile**: Added `hive:client` profile definition with 5 metrics (`payment_timeliness`, `sla_reasonableness`, `communication_quality`, `infrastructure_reliability`, `trial_count_90d`). This ensures the marketplace's advisor-rates-node credentials reference a real, defined profile rather than ad-hoc metrics on `hive:node`.

### DID-CASHU-TASK-ESCROW.md — No changes needed

### DID-HIVE-SETTLEMENTS.md — No changes needed

---

## Cross-Spec Consistency Verification

| Check | Status |
|-------|--------|
| All cross-reference anchors resolve | ✅ Verified |
| Tier names consistent (monitor/standard/advanced/admin) | ✅ |
| Bond amounts consistent across Fleet Mgmt ↔ Settlements | ✅ Fixed |
| VC 2.0 context URLs consistent | ✅ |
| Reputation profile domains match between specs | ✅ Fixed (hive:client) |
| Settlement type references (Type 7, Type 9) match | ✅ |
| Danger score references align | ✅ |
| Implementation roadmap dependencies coherent | ✅ |
| No contradictions between specs | ✅ |

---

## Final Assessment

The marketplace spec is now internally consistent and aligned with all four companion specs. The main structural improvements were:

1. Proper VC 2.0 compliance in all credential examples
2. Clean separation of marketplace-specific reputation metrics into a dedicated `hive:client` profile
3. Hardened sealed-bid auction with cryptographic reveal
4. Anti-gaming protections for trial period exploitation
5. Bond amount consistency across the spec suite

## Remaining Concerns Needing Real-World Validation

These are flagged in open questions across the specs and are design unknowns, not spec defects:

1. **Bond amount calibration** — 50k–500k sats range is theoretical; needs market testing
2. **Trial-cycling graduated pricing** — 2×/3× multipliers are reasonable but untested
3. **Sealed-bid auction adoption** — Whether advisors will participate in sealed bids vs. preferring open negotiation
4. **Multi-advisor conflict thresholds** — Cross-advisor conflict detection engine sensitivity needs tuning with real workloads
5. **Intelligence sharing base/bonus split** — 70/30 ratio and 10% improvement threshold need data
6. **Cross-hive reputation portability** — How reputation earned in one hive transfers to another is deferred to governance

---

*— Hex ⬡*
