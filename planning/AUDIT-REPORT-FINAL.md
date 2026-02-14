# Final Audit Report — Protocol Specs Hardening

**Date:** 2026-02-14  
**Auditor:** Hex (subagent: spec-hardening)  
**Scope:** All four protocol specs in `/docs/planning/`  
**Iterations:** 2 (fix + self-audit + fix)

---

## Summary of Changes

### DID-L402-FLEET-MANAGEMENT.md

| # | Finding | Change |
|---|---------|--------|
| 1 | Duplicate reference | Removed duplicate "DID Reputation Schema" from References |
| 6 | No mapping between permission tiers and settlement privileges | Added "Permission Tier ↔ Settlement Privilege Mapping" table with bond requirements |
| 7 | Agent tier "New" collides with node tier naming | Renamed agent tier to "Novice" (agents: Novice/Established/Proven; nodes: Newcomer/Recognized/Trusted/Senior/Founding) |
| 11/22 | VC 1.1 context URL and field names | Updated all `@context` to `https://www.w3.org/ns/credentials/v2`, `issuanceDate`→`validFrom`, `expirationDate`→`validUntil` |
| 20 | Even message type 49152 would disconnect non-hive peers | Changed to odd types: 49153 (request), 49155 (response). Added BOLT 1 rationale. |
| 21 | Internal TLV keys undocumented | Added note clarifying internal TLV keys vs BOLT-level TLVs. Changed to odd key numbers. |
| 29 | 8 referenced schemas never defined | Added stub definitions with example JSON for all: `hive:channel/v1`, `hive:splice/v1`, `hive:peer/v1`, `hive:payment/v1`, `hive:wallet/v1`, `hive:plugin/v1`, `hive:backup/v1`, `hive:emergency/v1` |
| 32 | Revocation check strategy unspecified | Added: cache with 1-hour TTL, fail-closed if Archon unreachable, websocket subscription |
| 40 | Performance baseline manipulation | Specified baseline must precede credential issuance |
| 41 | Operator trust modifier based on self-reported disputes | Changed to require arbitrated disputes only |
| 45 | No cross-spec implementation roadmap | Added "Cross-Spec Critical Path" with week-by-week dependency chain |
| 47 | Proven agent could auto-execute nuclear ops | Added `max()` floor to approval formula; hard-coded danger 9-10 as always multi-sig |
| 49 | Taxonomy length | Kept in-document (extracting would break too many cross-refs) |
| 52 | No version number | Added `Version: 0.1.0` |

### DID-REPUTATION-SCHEMA.md

| # | Finding | Change |
|---|---------|--------|
| 8 | Score thresholds only in Settlements, not Reputation | Added "Score Threshold Interpretation" section with reference thresholds and note about consumer-specific interpretation |
| 11/22 | VC 1.1 context and fields | Updated all context URLs to v2, field names to `validFrom`/`validUntil`, updated W3C VC section |
| 51 | "Why issue reputation?" left as open question | Promoted to full "Issuance Incentives" section covering: automated issuance, protocol requirement, reciprocity, negative reputation as defense |
| 52 | No version number | Added `Version: 0.1.0` |

### DID-CASHU-TASK-ESCROW.md

| # | Finding | Change |
|---|---------|--------|
| 15 | NUT-10/11/14 descriptions conflated | Complete rewrite: NUT-10 = structured secret format (container), NUT-11 = P2PK signature conditions, NUT-14 = HTLC composition. Relabeled the JSON example as "NUT-14 HTLC Secret Structure (using NUT-10 format)" |
| 16 | Hash tag format included extraneous "SHA256" | Fixed to `["hash", "<hex>"]` per NUT-14 spec. Added implementation note. |
| 17 | Multi-refund possibility not noted | Added note about refund tag accepting a list of pubkeys |
| 18 | Mint compatibility not addressed | Added "Mint Requirements" section: NUT-10, NUT-11, NUT-14, NUT-07 required. Added capability verification via NUT-06. |
| 19 | Wrong endpoint name `/v1/check` | Fixed to `POST /v1/checkstate` (NUT-07) |
| 24 | Operator→Node secret generation unspecified | Added "Secret Generation Protocol" section with 3 models: operator-generated, node API, credential-delegated. Includes bash example. |
| 25 | Performance ticket trust assumption buried | Added prominent warning box. Specified baseline integrity requirements (must precede credential). |
| 33 | Multi-node task guidance missing | Resolved open question: destination node generates secret (mirrors Lightning receiver-generates pattern). Added `verifier_node_id` metadata field. |
| 40 | Baseline manipulation | Added baseline integrity rules: measurement before credential validFrom, signed by node, rolling 7-day average |
| 52 | No version number | Added `Version: 0.1.0` |

### DID-HIVE-SETTLEMENTS.md

| # | Finding | Change |
|---|---------|--------|
| 7 | Node tier "Established" collides with agent tier | Renamed to "Recognized" throughout (tier progression, credit table, pheromone metadata) |
| 26 | Bond multisig construction unspecified | Added complete NUT-11 multisig example: 3-of-5 with `pubkeys`, `n_sigs` tags. Specified async signature collection with 72-hour window. |
| 27 | Intelligence sharing pretends to be trustless | Added prominent trust model warning. Restructured to base payment (non-escrowed) + performance bonus (escrowed). |
| 28 | Pheromone path node requirements | Added explicit note: path nodes must run cl-hive settlement plugin |
| 30 | Arbitration panel size and randomness unspecified | Specified 7-member panel, stake-weighted selection via `SHA256(dispute_id \|\| block_hash)`, eligibility requirements (tier ≥ Recognized, bond ≥ 50k), arbitrator bonds (5k sats), 5-of-7 majority, 72-hour voting window |
| 31 | Multilateral netting offline node behavior | Added 2-hour timeout, fallback to bilateral, heartbeat penalty for repeated non-response |
| 34 | Emergency exit undefined | Added complete "Emergency Exit Protocol" section: intent-to-leave broadcast, 4-hour settlement window, 7-day bond hold, involuntary exit with 48-hour grace period |
| 37 | Minimum bond exploit | Increased all bond minimums (Basic: 10k→50k, Full: 50k→150k, LP: 100k→300k, Founding: 250k→500k). Added dynamic bond floor (50% of median). Added time-weighted staking. Gated intelligence behind Full member tier. |
| 38 | Sybil arbitration capture | Stake-weighted panel selection, tenure requirements, arbitrator bonds, node pubkey linking to prevent DID recycling, 2× bond multiplier for re-joining after slash |
| 39 | Heartbeat penalties too low for large leases | Changed to `500 + (leased_capacity_sats × 0.001)` per missed window |
| 42 | Opportunity cost impossible to compute | Replaced with configurable `liquidity_rate_ppm` flat rate per sat-hour |
| 43 | Credit lines in msat too low | Converted to sats, increased 10-100×: Recognized 10k sats, Trusted 50k, Senior 200k, Founding 1M |
| 46 | Settlement vs task escrow confusion | Added note explaining semantic difference (acknowledgment vs completion) |
| 50 | Types 6 & 7 thin | Fleshed out pheromone (path node requirements) and intelligence (split payment model, trust warning) |
| 52 | No version number | Added `Version: 0.1.0` |

---

## Self-Audit Findings (Iteration 2)

After the initial fix pass, a complete re-read found:

1. **Pheromone metadata still said "established"** → Fixed to "recognized"
2. **"New (0.5)" in approval table** → Fixed to "Novice (0.5)"  
3. **Escrow doc still had "New (no history)"** → Fixed to "Novice (no history)"
4. **Fleet Mgmt reputation credential type was changed to "HiveReputationCredential"** → Reverted to "DIDReputationCredential" (the base schema type; domain field distinguishes instances)
5. **Reputation Schema W3C section still referenced issuanceDate** → Fixed to validFrom
6. **Reputation Schema issuance incentives referenced "HiveReputationCredential"** → Fixed to "DIDReputationCredential (with domain: hive:advisor)"

All found issues were fixed in the same pass.

---

## Final Assessment

### DID-L402-FLEET-MANAGEMENT.md — ✅ Ready for Implementation

Complete protocol spec covering identity, payment, transport, and schema layers. All 14 categories of node operations catalogued with danger scores. All referenced schemas now have stub definitions. Cross-spec dependencies documented.

### DID-REPUTATION-SCHEMA.md — ✅ Ready for Implementation

Universal reputation credential schema with domain profiles, aggregation algorithm, and sybil resistance strategies. Score threshold interpretation documented. Issuance incentive question resolved. VC 2.0 compliant.

### DID-CASHU-TASK-ESCROW.md — ✅ Ready for Implementation

Conditional escrow protocol with accurate NUT-10/11/14 descriptions. Secret generation protocol specified. Mint requirements documented. Trust assumptions explicitly flagged for performance tickets.

### DID-HIVE-SETTLEMENTS.md — ✅ Ready for Implementation

Comprehensive settlement protocol with hardened bond economics, sybil-resistant arbitration, emergency exit procedures, and specified timeout behaviors. Game theory now accounts for rational adversaries with proper penalty calibration.

### Areas Requiring Real-World Validation

1. **Bond amounts** — The increased minimums (50k-500k sats) need market testing. Too high = barriers to entry; too low = sybil vulnerability. Governance should adjust based on hive size and market conditions.
2. **Arbitration panel dynamics** — The 7-member stake-weighted panel is theoretically sound but untested. Edge cases with small hives (< 15 members) may require fallback to smaller panels.
3. **Intelligence market pricing** — The base+bonus split for intelligence is a design choice. Real-world data quality correlation needs validation.
4. **Performance baseline integrity** — The "baseline must precede credential" rule works but creates a chicken-and-egg problem for first-time advisor-operator relationships. A trial period mechanism may be needed.
5. **Cross-mint escrow** — Multi-mint ticket redemption atomicity remains an open design challenge. Partial payment on single-mint failure is accepted but not ideal.

---

---

## Post-Audit Update: Archon Schema Adoption

**Date:** 2026-02-14

The `DIDReputationCredential` and `DIDReputationProfile` JSON schemas defined in `DID-REPUTATION-SCHEMA.md` have been upstreamed to the Archon project. The canonical schema files are now maintained at [archetech/schemas/credentials/reputation/v1](https://github.com/archetech/schemas/tree/main/credentials/reputation/v1). All specs have been updated to reference the canonical Archon schema location and include the `https://schemas.archetech.com/credentials/reputation/v1` context URL in credential examples.

---

*Generated by spec-hardening subagent, 2026-02-14*
