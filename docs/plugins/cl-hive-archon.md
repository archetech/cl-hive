# cl-hive-archon: DID Identity Plugin

**Status:** Design Document  
**Version:** 0.1.0  
**Author:** Hex (`did:cid:bagaaierajrr7k6izcrdfwqxpgtrobflsv5oibymfnthjazkkokaugszyh4ka`)  
**Date:** 2026-02-15  
**Source Specs:** [DID-HIVE-CLIENT](../planning/DID-HIVE-CLIENT.md), [ARCHON-INTEGRATION](../planning/ARCHON-INTEGRATION.md), [DID-L402-FLEET-MANAGEMENT](../planning/DID-L402-FLEET-MANAGEMENT.md)

---

## Overview

`cl-hive-archon` is an **optional identity plugin** that adds Archon DID (Decentralized Identifier) capabilities to your Lightning node. It upgrades `cl-hive-comms` from Nostr-only verification to full DID-based identity — enabling cryptographic credential issuance, verifiable reputation, encrypted dmail transport, and vault-based backup with Shamir threshold recovery.

**Requires:** `cl-hive-comms`

**Core principle:** DIDs are plumbing, never user-facing. Operators "authorize advisors" and "verify identities" — they never see `did:cid:bagaaiera...` strings unless they ask for them with `--verbose`.

---

## Relationship to Other Plugins

| Plugin | Relationship |
|--------|-------------|
| **cl-hive-comms** | **Required.** cl-hive-archon registers with cl-hive-comms' transport abstraction (adding dmail) and upgrades the Credential Verifier from Nostr-only to full DID mode. |
| **cl-hive** | Optional. When both cl-hive-archon and cl-hive are installed, the node has full hive identity (Nostr + DID + hive PKI). |

### What cl-hive-archon Adds to cl-hive-comms

| Component | Without cl-hive-archon | With cl-hive-archon |
|-----------|----------------------|---------------------|
| Identity | Nostr keypair (auto-generated) | Nostr keypair + DID (auto-provisioned) |
| Credential verification | Nostr signature + scope + replay | Full DID resolution + VC signature + revocation check (fail-closed) |
| Credential issuance | Nostr-signed credentials | W3C Verifiable Credentials signed by DID |
| Transport | Nostr DM + REST/rune | + Archon Dmail (registered with cl-hive-comms) |
| Backup | Local only | Archon vault + optional Shamir threshold recovery |
| Alias resolution | Local aliases + profile names | + DID-based alias resolution |
| Marketplace verification | Nostr signature on events | + DID-Nostr binding proof (`did-nostr-proof` tag) |

---

## Archon Integration Tiers

The tier you operate at depends on **which plugins you install** and **how you configure them**:

| Tier | Plugins | Identity | DID Verification | Features |
|------|---------|----------|-----------------|----------|
| **None** (default) | `cl-hive-comms` only | Nostr keypair | None | Full transport + marketplace |
| **Lightweight** | `cl-hive-comms` + `cl-hive-archon` | DID via public Archon | ✓ (public gateway) | DID verification, credentials |
| **Full** | `cl-hive-comms` + `cl-hive-archon` (local node) | DID via local Archon | ✓ (local, sovereign) | + Dmail, vault, full sovereignty |
| **Hive Member** | All three plugins | Full hive identity | ✓ | + Gossip, topology, settlements |

---

## DID Auto-Provisioning

When `cl-hive-archon` is installed alongside `cl-hive-comms`:

1. Checks if a DID is configured
2. If not, **auto-provisions a DID** via the configured Archon gateway (zero user action)
3. **Automatically creates DID↔npub binding** with the Nostr key from cl-hive-comms
4. Logs: `"DID identity created and bound to Nostr key."`

```bash
# Just start the plugin — DID auto-provisioned
lightning-cli plugin start /path/to/cl_hive_archon.py
# → DID auto-provisioned via archon.technology
# → Bound to existing Nostr key from cl-hive-comms

# Or import existing identity
lightning-cli hive-archon-import-identity --file=/path/to/wallet.json
```

### Graceful Degradation

The client tries Archon endpoints in order:

1. **Local Archon node** (`http://localhost:4224`) — fastest, sovereign
2. **Public Archon gateway** (`https://archon.technology`) — no setup required
3. **Cached credentials** — if all gateways unreachable, honor existing cached creds
4. **Fail-closed** — if no cache, deny all commands from unverifiable credentials

This means the node never silently downgrades security. New credential issuance and revocation checks fail-closed if Archon is unreachable.

---

## DID Abstraction Layer

### Principle: DIDs Are Plumbing

Operators never interact with DIDs directly. The abstraction layer ensures:

- **Auto-provisioning** — DID created on first run, no user action
- **Human-readable names** — Advisors shown by `displayName`, not DID strings
- **Alias system** — `advisor_name → DID` mapping, used in all CLI commands
- **Transparent credential management** — "Authorize this advisor" not "issue VC"
- **Technical details hidden by default** — Only visible with `--verbose` or `--technical`

### Alias Resolution

Every DID gets a human-readable alias:

| Internal | User Sees |
|----------|-----------|
| `did:cid:bagaaierajrr7k...` | `"Hex Fleet Advisor"` |
| `did:cid:bagaaierawhtw...` | `"RoutingBot Pro"` |
| `did:cid:bagaaierabnbx...` | `"my-node"` (auto-assigned) |

Sources (priority order):
1. **Local aliases** — Operator assigns names
2. **Profile display names** — From advisor's `HiveServiceProfile.displayName`
3. **Auto-generated** — `"advisor-1"`, `"advisor-2"`

---

## Credential Issuance & Verification

### Full DID Mode (cl-hive-archon installed)

Verification chain for each management command:

1. **DID resolution** — Resolve agent's DID via Archon Keymaster or gateway
2. **Signature verification** — Verify VC proof against issuer's DID document
3. **Scope check** — Credential grants required permission tier
4. **Constraint check** — Parameters within credential constraints
5. **Revocation check** — Query Archon revocation status. Cache with 1-hour TTL. **Fail-closed**: deny if unreachable.
6. **Replay protection** — Monotonic nonce per agent DID. Timestamp within ±5 minutes.

### Credential Format

Management credentials are W3C Verifiable Credentials:

```json
{
  "@context": ["https://www.w3.org/ns/credentials/v2", "https://hive.lightning/management/v1"],
  "type": ["VerifiableCredential", "HiveManagementCredential"],
  "issuer": "did:cid:<node_operator_did>",
  "credentialSubject": {
    "id": "did:cid:<agent_did>",
    "nodeId": "03abcdef...",
    "permissions": {
      "monitor": true,
      "fee_policy": true,
      "rebalance": false
    },
    "constraints": {
      "max_fee_change_pct": 50,
      "max_rebalance_sats": 1000000,
      "max_daily_actions": 100,
      "allowed_schemas": ["hive:fee-policy/*", "hive:monitor/*"]
    }
  },
  "validFrom": "2026-02-14T00:00:00Z",
  "validUntil": "2026-03-14T00:00:00Z"
}
```

### DID-Nostr Binding

Automatically created when cl-hive-archon is installed alongside cl-hive-comms. Links the DID to the Nostr pubkey via an Archon attestation credential. This:

- Prevents impersonation on Nostr marketplace events
- Enables `did-nostr-proof` tags on published events
- Allows anyone to verify that a Nostr profile belongs to a specific DID

---

## Dmail Transport

When installed, cl-hive-archon registers **Archon Dmail** as an additional transport with cl-hive-comms:

```python
# cl-hive-archon registers dmail transport on startup
comms.register_transport("dmail", DmailTransport(archon_gateway))
```

**Dmail properties:**
- DID-to-DID encrypted messaging
- Higher security than Nostr DM (end-to-end with DID keys)
- Stored on Archon network (persistent, not relay-dependent)
- Best for high-value communications (contract formation, dispute evidence)

**Transport selection:** cl-hive-comms automatically selects the best transport for each message. Dmail is preferred for sensitive operations when available; Nostr DM remains the primary general-purpose transport.

---

## Backup & Recovery System

### What Gets Backed Up

| Data | Priority | Notes |
|------|----------|-------|
| DID wallet (identity + keys) | **Critical** | Without this, the node loses its identity |
| Credential store | **Critical** | Active advisor authorizations |
| Receipt chain (hash-linked log) | High | Tamper-evident audit trail |
| Nostr keypair | High | Transport identity; regenerable but loses continuity |
| Cashu escrow tokens | High | Unspent tokens = real sats |
| Policy configuration | Medium | Recreatable but tedious |
| Alias registry | Low | Convenience only |

### Vault Architecture

Backups use Archon's group vault primitive — a DID-addressed container:

```
Node DID: did:cid:bagaaiera...
  └── Vault: hive-backup-<node-short-id>
       ├── Member: node DID (owner)
       ├── Member: operator DID (recovery)
       ├── Member: trusted-peer DID (optional)
       │
       ├── Item: wallet-backup-<timestamp>.enc
       ├── Item: credentials-<timestamp>.enc
       ├── Item: receipts-<timestamp>.enc
       ├── Item: escrow-tokens-<timestamp>.enc
       └── Item: config-<timestamp>.enc
```

### Backup Schedule & Triggers

Backups are triggered:
1. **On schedule** — default: daily at 3 AM local
2. **On critical state change** — new credential issued, credential revoked, escrow token created
3. **On demand** — `lightning-cli hive-archon-backup`

### Shamir Threshold Recovery

For distributed trust, the DID wallet encryption key can be split into `n` shares with threshold `k`:

```ini
hive-archon-threshold-enabled=true
hive-archon-threshold-k=2              # shares needed to recover
hive-archon-threshold-n=3              # total shares distributed
hive-archon-threshold-holders=did:cid:operator,did:cid:peer1,did:cid:peer2
```

**How it works:**

1. Wallet backup encrypted with random symmetric key
2. Symmetric key split into `n` Shamir shares
3. Each share encrypted to a specific holder's DID
4. Shares stored as separate vault items
5. Recovery requires `k` holders to contribute their shares

```
Vault: hive-backup-<node>
  ├── wallet-backup-<ts>.enc          ← encrypted with random key K
  ├── share-1-<operator-did>.enc      ← Shamir share 1, encrypted to operator
  ├── share-2-<peer1-did>.enc         ← Shamir share 2, encrypted to peer 1
  └── share-3-<peer2-did>.enc         ← Shamir share 3, encrypted to peer 2
```

### Recovery Scenarios

#### Scenario 1: Routine Backup Restore (Single Operator)

**Situation:** Node disk failed. New machine with CLN installed. Operator has their Archon wallet.

```bash
lightning-cli plugin start cl_hive_comms.py
lightning-cli plugin start cl_hive_archon.py
lightning-cli hive-archon-import-identity --file=/path/to/operator-wallet.json
lightning-cli hive-archon-restore
# → Restores DID wallet, credentials, receipts, escrow tokens, config
```

**Time to recovery:** ~5 minutes (excluding CLN sync).

#### Scenario 2: Single-Operator Recovery (No Threshold)

**Situation:** Lost node AND local wallet backup, but DID still valid on Archon network.

```bash
npx @didcid/keymaster recover-id --seed="..."
# Then same steps as Scenario 1
```

#### Scenario 3: Threshold Recovery (k-of-n Shamir)

**Situation:** Cannot access vault alone. Need `k` share holders.

```bash
lightning-cli hive-archon-restore --threshold
# → Sends recovery request via Nostr DM to all share holders
# → Each holder decrypts and returns their share
# → Once k shares collected, vault decrypted and restored

# Alternative: manual share collection (offline)
lightning-cli hive-archon-restore --threshold --manual
# → Prompts operator to paste k shares (base64-encoded)
```

#### Scenario 4: Lost DID Recovery

**Situation:** Lost DID entirely — no wallet, no seed, no passphrase.

```bash
# 1. Auto-provision new DID
lightning-cli plugin start cl_hive_archon.py

# 2. If threshold configured: recover using new identity
lightning-cli hive-archon-restore --threshold --new-identity

# 3. Otherwise: contact advisors to re-issue credentials to new DID
# 4. Publish DID rotation notice
lightning-cli hive-archon-rotate-did --old="did:cid:old..." --new="did:cid:new..."
```

#### Scenario 5: Contested Recovery

**Situation:** Recovery request suspected unauthorized.

**Protections:**
1. Share holders can refuse independently
2. Verification challenge (out-of-band identity proof)
3. Configurable mandatory delay (`hive-archon-threshold-delay=24h`)
4. All holders notified when any recovery starts
5. Real operator can revoke DID immediately to block unauthorized recovery

#### Scenario 6: Partial Recovery (Degraded State)

**Situation:** Backup incomplete or corrupted.

| Component | If Missing | Impact | Mitigation |
|-----------|-----------|--------|------------|
| DID wallet | Identity lost | → Scenario 4 | Keep offline backup |
| Credentials | Advisors can't verify | Re-issue from advisors | Advisors retain copies |
| Receipt chain | Audit trail broken | New chain starts | Partial chain still valuable |
| Nostr keypair | Transport identity lost | Regenerate | Publish key rotation |
| Cashu tokens | Escrowed sats lost | Negotiate with advisors | Small balances |
| Policy config | Manual reconfiguration | Apply preset | Export separately |

```bash
# Restore specific components
lightning-cli hive-archon-restore --components=wallet,credentials
lightning-cli hive-archon-restore --skip=receipts
```

### Backup Design Principles

1. **Automatic** — No operator action after initial setup
2. **Interactive restore** — Always prompts for confirmation
3. **Threshold optional** — Single-operator vault is default
4. **Archon stores encrypted blobs** — Never sees plaintext state
5. **Fail-safe** — Partial recovery always attempted

---

## RPC Commands

| Command | Description |
|---------|-------------|
| `hive-archon-status` | Show DID identity, gateway health, vault status |
| `hive-archon-import-identity` | Import existing Archon wallet |
| `hive-archon-backup` | Trigger immediate backup to vault |
| `hive-archon-backup-status` | Last backup time, vault health, share holders |
| `hive-archon-restore` | Restore from vault (interactive) |
| `hive-archon-rotate-shares` | Re-split and redistribute Shamir shares |
| `hive-archon-export` | Export backup locally (offline/cold storage) |
| `hive-archon-rotate-did` | Publish DID rotation notice |
| `hive-archon-verify-contact` | Challenge-response DID verification for a peer |

---

## Configuration Reference

```ini
# ~/.lightning/config

# === Archon Gateway ===
# Lightweight tier (public gateway, no local node needed):
hive-archon-gateway=https://archon.technology

# Full tier (local Archon node — maximum sovereignty):
# hive-archon-gateway=http://localhost:4224

# === Backup ===
hive-archon-backup-interval=daily         # daily | hourly | manual
hive-archon-backup-retention=30           # days to keep old backups
hive-archon-backup-vault=auto             # auto-create vault on first run

# === Shamir Threshold Recovery (optional) ===
# hive-archon-threshold-enabled=false
# hive-archon-threshold-k=2
# hive-archon-threshold-n=3
# hive-archon-threshold-holders=did:cid:op,did:cid:peer1,did:cid:peer2
# hive-archon-threshold-delay=24h         # mandatory wait before share submission
# hive-archon-threshold-notify=all        # notify all holders on recovery request
```

---

## Installation

```bash
# Requires cl-hive-comms to be running
lightning-cli plugin start /path/to/cl_hive_archon.py
# → DID auto-provisioned via configured gateway
# → Bound to existing Nostr key from cl-hive-comms
# → Credential Verifier upgraded to full DID mode
# → Dmail transport registered
# → Vault auto-created for backup
```

For permanent installation:

```ini
plugin=/path/to/cl_hive_comms.py
plugin=/path/to/cl_hive_archon.py
```

### Requirements

- **cl-hive-comms** running
- Network access to an Archon gateway (public or local)
- Optional: local Archon node for full sovereignty

---

## Implementation Roadmap

| Phase | Scope | Timeline |
|-------|-------|----------|
| 1 | DID auto-provisioning, DID↔npub binding, Archon gateway integration | 2–3 weeks |
| 2 | Full DID credential verification (upgrade from Nostr-only) | 2–3 weeks |
| 3 | Dmail transport registration with cl-hive-comms | 1–2 weeks |
| 4 | Vault backup (auto + on-demand + on-state-change) | 2–3 weeks |
| 5 | Shamir threshold recovery | 2–3 weeks |
| 6 | DID rotation, partial restore, contested recovery | 2 weeks |

---

## References

- [DID Hive Client](../planning/DID-HIVE-CLIENT.md) — Plugin architecture, Archon integration tiers, backup system (Section 12a)
- [DID + L402 Fleet Management](../planning/DID-L402-FLEET-MANAGEMENT.md) — Credential format, DID verification
- [Archon Integration](../planning/ARCHON-INTEGRATION.md) — Governance messaging, DID verification flow
- [Archon: Decentralized Identity for AI Agents](https://github.com/archetech/archon)
- [W3C DID Core 1.0](https://www.w3.org/TR/did-core/)
- [W3C Verifiable Credentials Data Model 2.0](https://www.w3.org/TR/vc-data-model-2.0/)

---

*Feedback welcome. File issues on [cl-hive](https://github.com/lightning-goats/cl-hive) or discuss in #singularity.*

*— Hex ⬡*
