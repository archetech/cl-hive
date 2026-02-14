# DID Hive Client: Universal Lightning Node Management

**Status:** Proposal / Design Draft  
**Version:** 0.1.0  
**Author:** Hex (`did:cid:bagaaierajrr7k6izcrdfwqxpgtrobflsv5oibymfnthjazkkokaugszyh4ka`)  
**Date:** 2026-02-14  
**Feedback:** Open — file issues or comment in #singularity

---

## Abstract

This document specifies lightweight client software — a CLN plugin (`cl-hive-client`) and an LND companion daemon (`hive-lnd`) — that enables **any** Lightning node to contract for professional management services from advisors authenticated via Archon DIDs. The client implements the management interface defined in the [Fleet Management](./DID-L402-FLEET-MANAGEMENT.md) spec without requiring hive membership, bonds, gossip participation, or the full `cl-hive` plugin.

The result: every Lightning node operator — from a hobbyist running a Raspberry Pi to a business with a multi-BTC routing node — can hire AI-powered or human expert advisors for fee optimization, rebalancing, and channel management. The advisor authenticates with a DID credential, gets paid via Cashu escrow, and builds verifiable reputation. The client enforces local policy as the last line of defense against malicious or incompetent advisors. No trust required.

---

## Motivation

### The Total Addressable Market

The existing protocol suite assumes hive membership. Hive membership requires:
- Running the full `cl-hive` plugin
- Posting a bond (50,000–500,000 sats)
- Participating in gossip, settlement, and PKI protocols
- Maintaining ongoing obligations to other hive members

This is appropriate for sophisticated operators who want the full benefits of fleet coordination. But it limits the addressable market to operators willing to commit capital, infrastructure, and social participation.

The Lightning Network has **~15,000 publicly visible nodes** and an unknown number of private nodes. Most are unmanaged or self-managed with default settings. The operators fall into three categories:

| Category | Estimated Count | Current State | Willingness to Join a Hive |
|----------|----------------|---------------|---------------------------|
| Hobbyist operators | ~8,000 | Default fees, minimal optimization | Low (too complex, too much commitment) |
| Semi-professional | ~5,000 | Some manual tuning, basic monitoring | Medium (interested but barrier is high) |
| Professional routing nodes | ~2,000 | Active management, custom tooling | High (already sophisticated) |

The hive targets the professional tier (~2,000 nodes). The client targets **everyone** — lowering the barrier from "join a cooperative and post bonds" to "install a plugin and hire an advisor."

### The Value Proposition

**For node operators:**
- Professional management without learning routing optimization
- Pay-per-action or subscription pricing — no bond, no ongoing hive obligations
- Local policy engine ensures the advisor can never exceed operator-defined limits
- Try before you commit — trial periods with reduced scope
- Upgrade path to full hive membership if desired

**For advisors:**
- Access to the entire Lightning node market, not just hive members
- Build verifiable reputation across a larger client base
- Specialize and compete on merit
- No requirement to operate a Lightning node themselves (just need a DID and expertise)

**For the hive ecosystem:**
- Client nodes are the funnel for hive membership
- Advisors serving client nodes generate reputation that benefits the marketplace
- Revenue from client management fees funds hive development
- Network effects: more managed nodes → better routing intelligence → better management → more nodes

### Why Two Implementations

Lightning has two dominant implementations: CLN and LND. They share the Lightning protocol but differ in everything else — language, architecture, API surface, plugin model, configuration format. A single client implementation cannot serve both.

| Property | CLN | LND |
|----------|-----|-----|
| Language | C (core), Python (plugins) | Go |
| Plugin model | Dynamic plugins via JSON-RPC | Companion daemons via gRPC |
| Custom messages | `sendcustommsg` / `custommsg` hook | `SendCustomMessage` / `SubscribeCustomMessages` |
| Configuration | `config` file, command-line flags | `lnd.conf`, command-line flags |
| Extension convention | Python plugin, single file | Go binary, YAML/TOML config |

Building both `cl-hive-client` (Python, CLN plugin) and `hive-lnd` (Go, LND daemon) ensures the entire Lightning network can participate.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                         CLIENT NODE                                   │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │              cl-hive-client (CLN) / hive-lnd (LND)              │ │
│  │                                                                  │ │
│  │  ┌──────────┐ ┌────────────┐ ┌──────────┐ ┌──────────────────┐ │ │
│  │  │ Schema   │ │ Credential │ │ Escrow   │ │ Policy Engine    │ │ │
│  │  │ Handler  │ │ Verifier   │ │ Manager  │ │ (local overrides)│ │ │
│  │  └────┬─────┘ └─────┬──────┘ └────┬─────┘ └───────┬──────────┘ │ │
│  │       │              │              │               │            │ │
│  │  ┌────▼──────────────▼──────────────▼───────────────▼──────────┐ │ │
│  │  │                    Receipt Store                             │ │ │
│  │  │  (tamper-evident log of all management actions)             │ │ │
│  │  └─────────────────────────────────────────────────────────────┘ │ │
│  └──────────────────────────────┬──────────────────────────────────┘ │
│                                 │                                     │
│                    Custom Messages (49153/49155)                      │
│                                 │                                     │
│  ┌──────────────────────────────▼──────────────────────────────────┐ │
│  │                   Lightning Node (CLN / LND)                    │ │
│  └─────────────────────────────────────────────────────────────────┘ │
│                                                                       │
│  ┌────────────┐   ┌─────────────────┐                                │
│  │ Archon     │   │ Cashu Wallet    │                                │
│  │ Keymaster  │   │ (escrow tickets)│                                │
│  │ (DID)      │   │                 │                                │
│  └────────────┘   └─────────────────┘                                │
└──────────────────────────────────────────────────────────────────────┘

                              ▲
                              │ Bolt 8 Transport
                              │ (Custom TLV Messages)
                              ▼

┌──────────────────────────────────────────────────────────────────────┐
│                         ADVISOR                                       │
│                                                                       │
│  ┌────────────┐  ┌───────────────────┐  ┌────────────┐              │
│  │ Archon     │  │ Management Engine │  │ Lightning  │              │
│  │ Keymaster  │  │ (AI / human)      │  │ Wallet     │              │
│  │ (DID)      │  │                   │  │ (Cashu)    │              │
│  └────────────┘  └───────────────────┘  └────────────┘              │
└──────────────────────────────────────────────────────────────────────┘
```

### Comparison with Full Hive Membership

| Feature | Unmanaged | Client (`cl-hive-client` / `hive-lnd`) | Full Hive Member (`cl-hive`) |
|---------|-----------|----------------------------------------|------------------------------|
| Professional management | ✗ | ✓ | ✓ |
| Fee optimization | Manual | Via advisor | Via advisor + fleet intelligence |
| Rebalancing | Manual | Via advisor | Via advisor + fleet paths (97% cheaper) |
| Channel expansion | Manual | Via advisor proposal | Via advisor + hive coordination |
| Monitoring & alerts | DIY | Via advisor | Via advisor + hive health gossip |
| Gossip participation | ✗ | ✗ | ✓ |
| Settlement protocol | ✗ | ✗ (direct escrow only) | ✓ (netting, credit tiers) |
| Fleet rebalancing | ✗ | ✗ | ✓ (intra-hive paths) |
| Pheromone routing | ✗ | ✗ | ✓ |
| Intelligence market | ✗ | ✗ (buy from advisor directly) | ✓ (full market access) |
| Bond requirement | None | None | 50,000–500,000 sats |
| Infrastructure | Node only | Node + plugin/daemon + keymaster | Node + cl-hive + full PKI |
| Cost model | Free | Per-action or subscription | Bond + discounted per-action |

### Minimal Dependencies

The client has three dependencies:

1. **Lightning node** — CLN ≥ v24.08 or LND ≥ v0.18.0 (custom message support required)
2. **Archon Keymaster** — For DID identity. Lightweight: single binary or npm package. No full Archon node required.
3. **The client plugin/daemon itself** — Single file (CLN) or single binary (LND)

A built-in Cashu wallet handles escrow ticket creation and management. No external Cashu wallet software needed.

---

## CLN Plugin (`cl-hive-client`)

### Overview

A Python plugin following CLN's plugin architecture. Single file (`cl_hive_client.py`), no Docker, no complex setup. Registers custom message handlers for management schemas (types 49153/49155) and exposes RPC commands for operator interaction.

### Components

#### Schema Handler

Receives incoming management commands via custom message type 49153, validates the TLV payload structure per the [Fleet Management transport spec](./DID-L402-FLEET-MANAGEMENT.md#3-transport-layer-bolt-8--custom-messages), and dispatches to the appropriate CLN RPC.

```python
@plugin.hook("custommsg")
def on_custommsg(peer_id, payload, plugin, **kwargs):
    msg_type = int.from_bytes(payload[:2], 'big')
    if msg_type == 0xC001:  # 49153 — Hive Management Message
        return handle_management_message(peer_id, payload[2:])
    return {"result": "continue"}
```

The handler:
1. Deserializes the TLV payload (schema_type, schema_payload, credential, payment_proof, signature, nonce, timestamp)
2. Passes to Credential Verifier
3. Passes to Policy Engine
4. If both pass, executes the schema action via CLN RPC
5. Generates signed receipt
6. Sends response via custom message type 49155

#### Credential Verifier

Validates the Archon DID credential attached to each management command:

1. **DID resolution** — Resolves the agent's DID via local Archon Keymaster or remote Archon gateway
2. **Signature verification** — Verifies the credential's proof against the issuer's DID document
3. **Scope check** — Confirms the credential grants the required permission tier for the requested schema
4. **Constraint check** — Validates the command parameters against credential constraints (`max_fee_change_pct`, `max_rebalance_sats`, etc.)
5. **Revocation check** — Queries Archon revocation status. **Fail-closed**: if Archon is unreachable, deny. Cache with 1-hour TTL per the [Fleet Management spec](./DID-L402-FLEET-MANAGEMENT.md#credential-lifecycle).
6. **Replay protection** — Monotonic nonce check per agent DID. Timestamp within ±5 minutes.

#### Escrow Manager

Built-in Cashu wallet for escrow ticket handling. Manages the operator's side of the [Task Escrow protocol](./DID-CASHU-TASK-ESCROW.md):

- **Ticket creation** — Mints Cashu tokens with P2PK + HTLC + timelock conditions
- **Secret management** — Generates and stores HTLC secrets, reveals on task completion
- **Auto-replenishment** — When ticket balance drops below threshold, auto-mints new tokens (configurable)
- **Spending limits** — Enforces daily/weekly caps on escrow expenditure
- **Mint management** — Configurable trusted mints, multi-mint support
- **Receipt tracking** — Stores all completed task receipts locally

```python
# Example: auto-replenishment check
def check_escrow_balance(self):
    balance = self.cashu_wallet.get_balance()
    if balance < self.config['escrow_replenish_threshold']:
        amount = self.config['escrow_replenish_amount']
        self.cashu_wallet.mint(amount, mint_url=self.config['preferred_mint'])
        log.info(f"Auto-replenished escrow: +{amount} sats")
```

#### Policy Engine

The operator's last line of defense. Even with a valid credential and valid payment, the Policy Engine can reject any action based on local rules. See [Section 8: Local Policy Engine](#8-local-policy-engine) for full details.

#### Receipt Store

Append-only, hash-chained log of all management actions:

```json
{
  "receipt_id": 47,
  "prev_hash": "sha256:<hash_of_receipt_46>",
  "timestamp": "2026-02-14T12:34:56Z",
  "agent_did": "did:cid:<agent_did>",
  "schema": "hive:fee-policy/v1",
  "action": "set_anchor",
  "params": { "channel_id": "931770x2363x0", "target_fee_ppm": 150 },
  "result": "success",
  "state_hash_before": "sha256:<before>",
  "state_hash_after": "sha256:<after>",
  "agent_signature": "<sig>",
  "node_signature": "<sig>",
  "receipt_hash": "sha256:<hash_of_this_receipt>"
}
```

Tamper-evident: modifying any receipt breaks the hash chain. Receipts are stored in a local SQLite database with periodic merkle root computation for efficient auditing.

### RPC Commands

| Command | Description | Args |
|---------|-------------|------|
| `hive-client-status` | Show client status: active advisors, credential expiry, escrow balance, policy mode | None |
| `hive-client-authorize` | Issue a management credential to an advisor | `advisor_did`, `template` (or custom scope), `duration_days` |
| `hive-client-revoke` | Immediately revoke an advisor's credential | `advisor_did` or `credential_id` |
| `hive-client-receipts` | List management action receipts | `advisor_did` (optional), `since` (optional), `limit` (optional) |
| `hive-client-discover` | Find advisors via Archon/Nostr/direct | `capabilities` (optional), `max_results` (optional) |
| `hive-client-policy` | View or modify local policy | `preset` (optional), `rule` (optional) |
| `hive-client-escrow` | View escrow balance, mint status, spending history | `action` (`balance`/`mint`/`history`/`limits`) |
| `hive-client-trial` | Start or review a trial period | `advisor_did`, `duration_days`, `scope` |

### Configuration

```ini
# ~/.lightning/config (CLN config file)

# cl-hive-client configuration
hive-client-did=did:cid:bagaaiera...
hive-client-keymaster-path=/usr/local/bin/keymaster
hive-client-archon-gateway=https://archon.technology

# Escrow settings
hive-client-escrow-mint=https://mint.minibits.cash
hive-client-escrow-replenish-threshold=1000
hive-client-escrow-replenish-amount=5000
hive-client-escrow-daily-limit=50000
hive-client-escrow-weekly-limit=200000

# Policy preset (conservative | moderate | aggressive)
hive-client-policy-preset=moderate

# Credential defaults
hive-client-credential-duration=30
hive-client-credential-max-renewals=12

# Alert integration
hive-client-alert-webhook=https://hooks.example.com/hive
hive-client-alert-nostr-dm=npub1abc...
hive-client-alert-email=operator@example.com

# Discovery
hive-client-nostr-relays=wss://nos.lol,wss://relay.damus.io
```

### Installation

```bash
# 1. Download the plugin
curl -O https://github.com/lightning-goats/cl-hive-client/releases/latest/cl_hive_client.py

# 2. Make executable
chmod +x cl_hive_client.py

# 3. Add to CLN config
echo "plugin=/path/to/cl_hive_client.py" >> ~/.lightning/config

# 4. Install Archon Keymaster (if not already present)
npm install -g @didcid/keymaster

# 5. Create or import DID
npx @didcid/keymaster create-id --name my-node

# 6. Add DID to config
echo "hive-client-did=$(npx @didcid/keymaster show-id my-node)" >> ~/.lightning/config

# 7. Restart CLN (or load plugin dynamically)
lightning-cli plugin start /path/to/cl_hive_client.py
```

No Docker. No database setup. No complex dependencies. One plugin file, one config block, one DID.

### Relationship to Full `cl-hive`

`cl-hive-client` is a **strict subset** of `cl-hive`. If you're already running `cl-hive`, you don't need `cl-hive-client` — the full plugin includes all client functionality plus gossip, settlement, pheromone, and fleet coordination.

```
┌──────────────────────────────────────────────────────┐
│                      cl-hive (full)                   │
│                                                       │
│  ┌────────────────────────────────────────────────┐  │
│  │              cl-hive-client (subset)            │  │
│  │                                                 │  │
│  │  Schema Handler    Credential Verifier          │  │
│  │  Escrow Manager    Policy Engine                │  │
│  │  Receipt Store     RPC Commands                 │  │
│  └─────────────────────────────────────────────────┘  │
│                                                       │
│  Gossip Protocol        Settlement Protocol           │
│  Pheromone System       Bond Management               │
│  Fleet Coordination     Hive PKI                      │
│  Intelligence Market    Stigmergic Signals            │
└──────────────────────────────────────────────────────┘
```

**Migration path:** See [Section 11: Hive Membership Upgrade Path](#11-hive-membership-upgrade-path).

---

## LND Companion Daemon (`hive-lnd`)

### Overview

A Go daemon that connects to LND via gRPC and provides the same management interface as `cl-hive-client`. Runs as a standalone process alongside LND, similar to other LND companion tools (Loop, Pool, Faraday, Lightning Terminal).

### Architecture

```
┌──────────────────────────────────────────────────────┐
│                       hive-lnd                        │
│                                                       │
│  ┌──────────┐ ┌────────────┐ ┌──────────┐           │
│  │ Schema   │ │ Credential │ │ Escrow   │           │
│  │ Handler  │ │ Verifier   │ │ Manager  │           │
│  └────┬─────┘ └────────────┘ └──────────┘           │
│       │                                               │
│  ┌────▼──────────────────────────────────┐           │
│  │     Schema Translation Layer          │           │
│  │                                       │           │
│  │  hive:fee-policy → UpdateChannelPolicy│           │
│  │  hive:monitor    → GetInfo, ListChans │           │
│  │  hive:rebalance  → SendPaymentV2     │           │
│  │  hive:channel    → OpenChannel, Close │           │
│  │  ...                                  │           │
│  └────┬──────────────────────────────────┘           │
│       │                                               │
│  ┌────▼─────────────────────────┐                    │
│  │   LND gRPC Client           │                    │
│  │   (lnrpc, routerrpc, etc.)  │                    │
│  └──────────────────────────────┘                    │
│                                                       │
│  ┌──────────────────────────────┐                    │
│  │   Policy Engine + Receipt   │                    │
│  │   Store + Alert Manager     │                    │
│  └──────────────────────────────┘                    │
│                                                       │
│  ┌──────────────────────────────┐                    │
│  │   HiveClientService (gRPC)  │                    │
│  │   (local management API)    │                    │
│  └──────────────────────────────┘                    │
└──────────────────────────────────────────────────────┘
          │                    ▲
          │ gRPC               │ Custom Messages
          ▼                    │ (SubscribeCustomMessages)
     ┌─────────┐          ┌───┴───┐
     │  LND    │          │  LND  │
     │  (RPC)  │          │ (P2P) │
     └─────────┘          └───────┘
```

### Custom Message Handling

LND exposes custom message handling via gRPC:

```go
// Subscribe to incoming custom messages
stream, err := client.SubscribeCustomMessages(ctx, &lnrpc.SubscribeCustomMessagesRequest{})
for {
    msg, err := stream.Recv()
    if msg.Type == 49153 { // Hive Management Message
        handleManagementMessage(msg.Peer, msg.Data)
    }
}

// Send custom message response
_, err = client.SendCustomMessage(ctx, &lnrpc.SendCustomMessageRequest{
    Peer: peerPubkey,
    Type: 49155, // Hive Management Response
    Data: responsePayload,
})
```

### Local gRPC Service

`hive-lnd` exposes a local gRPC service for operator interaction (equivalent to `cl-hive-client`'s RPC commands):

```protobuf
service HiveClientService {
  rpc Status(StatusRequest) returns (StatusResponse);
  rpc Authorize(AuthorizeRequest) returns (AuthorizeResponse);
  rpc Revoke(RevokeRequest) returns (RevokeResponse);
  rpc ListReceipts(ListReceiptsRequest) returns (ListReceiptsResponse);
  rpc Discover(DiscoverRequest) returns (DiscoverResponse);
  rpc GetPolicy(GetPolicyRequest) returns (PolicyResponse);
  rpc SetPolicy(SetPolicyRequest) returns (PolicyResponse);
  rpc EscrowInfo(EscrowInfoRequest) returns (EscrowInfoResponse);
  rpc StartTrial(StartTrialRequest) returns (TrialResponse);
}
```

### Configuration

```yaml
# hive-lnd.yaml

identity:
  did: "did:cid:bagaaiera..."
  keymaster_path: "/usr/local/bin/keymaster"
  archon_gateway: "https://archon.technology"

lnd:
  rpc_host: "localhost:10009"
  tls_cert: "/home/user/.lnd/tls.cert"
  macaroon: "/home/user/.lnd/data/chain/bitcoin/mainnet/admin.macaroon"

escrow:
  preferred_mint: "https://mint.minibits.cash"
  replenish_threshold: 1000
  replenish_amount: 5000
  daily_limit: 50000
  weekly_limit: 200000

policy:
  preset: "moderate"

credentials:
  default_duration_days: 30
  max_renewals: 12

alerts:
  webhook: "https://hooks.example.com/hive"
  nostr_dm: "npub1abc..."
  email: "operator@example.com"

discovery:
  nostr_relays:
    - "wss://nos.lol"
    - "wss://relay.damus.io"
```

### Installation

```bash
# 1. Download binary
curl -LO https://github.com/lightning-goats/hive-lnd/releases/latest/hive-lnd-linux-amd64
chmod +x hive-lnd-linux-amd64
mv hive-lnd-linux-amd64 /usr/local/bin/hive-lnd

# 2. Create config
hive-lnd init  # generates hive-lnd.yaml with defaults

# 3. Set up DID (if not already present)
npm install -g @didcid/keymaster
npx @didcid/keymaster create-id --name my-node

# 4. Edit config with DID and LND connection details
vim ~/.hive-lnd/hive-lnd.yaml

# 5. Run
hive-lnd --config ~/.hive-lnd/hive-lnd.yaml

# Optional: systemd service
hive-lnd install-service  # creates and enables systemd unit
```

Single binary + config file. No Docker, no complex setup.

---

## 5. Schema Translation Layer

The management schemas defined in the [Fleet Management spec](./DID-L402-FLEET-MANAGEMENT.md#core-schemas) are implementation-agnostic. The client translates each schema action to the appropriate CLN RPC call or LND gRPC call. This section defines the full mapping for all 15 schema categories.

### Translation Table

| Schema | Action | CLN RPC | LND gRPC | Danger | Notes |
|--------|--------|---------|----------|--------|-------|
| **hive:monitor/v1** | | | | | |
| | `health_summary` | `getinfo` | `lnrpc.GetInfo` | 1 | |
| | `channel_list` | `listpeerchannels` | `lnrpc.ListChannels` | 1 | CLN uses `listpeerchannels` (v23.08+) |
| | `forward_history` | `listforwards` | `lnrpc.ForwardingHistory` | 1 | |
| | `peer_list` | `listpeers` | `lnrpc.ListPeers` | 1 | |
| | `invoice_list` | `listinvoices` | `lnrpc.ListInvoices` | 1 | |
| | `payment_list` | `listsendpays` | `lnrpc.ListPayments` | 1 | |
| | `htlc_snapshot` | `listpeerchannels` (htlcs field) | `lnrpc.ListChannels` (pending_htlcs) | 1 | |
| | `fee_report` | `listpeerchannels` (fee fields) | `lnrpc.FeeReport` | 1 | |
| | `onchain_balance` | `listfunds` | `lnrpc.WalletBalance` | 1 | |
| | `graph_query` | `listnodes` / `listchannels` | `lnrpc.DescribeGraph` | 1 | |
| | `log_stream` | `notifications` subscribe | `lnrpc.SubscribeInvoices` (partial) | 2 | LND lacks generic log streaming |
| | `plugin_status` | `plugin list` | N/A | 1 | LND: report `hive-lnd` version/status instead |
| | `backup_status` | Custom (check backup file timestamps) | `lnrpc.SubscribeChannelBackups` | 1 | |
| **hive:fee-policy/v1** | | | | | |
| | `set_anchor` (single) | `setchannel` | `lnrpc.UpdateChannelPolicy` | 2–3 | |
| | `set_anchor` (bulk) | `setchannel` (loop) | `lnrpc.UpdateChannelPolicy` (loop) | 4–5 | |
| | `set_htlc_limits` | `setchannel` (htlcmin/htlcmax) | `lnrpc.UpdateChannelPolicy` (min/max_htlc) | 2–5 | |
| | `set_zero_fee` | `setchannel` (0/0) | `lnrpc.UpdateChannelPolicy` (0/0) | 4 | |
| **hive:rebalance/v1** | | | | | |
| | `circular_rebalance` | `pay` (self-invoice) | `routerrpc.SendPaymentV2` (circular) | 3–5 | CLN: create invoice, self-pay via specific route |
| | `submarine_swap` | External (Loop/Boltz plugin) | `looprpc.LoopOut` / `LoopIn` | 5 | Requires Loop/Boltz integration |
| | `peer_rebalance` | Custom message to peer | Custom message to peer | 4 | Hive peers only; N/A for standalone client |
| **hive:config/v1** | | | | | |
| | `adjust` | `setconfig` (CLN ≥ v24.02) | `lnrpc.UpdateNodeAnnouncement` (limited) | 3–4 | LND: fewer runtime-adjustable params |
| | `set_alias` | `setconfig alias` | `lnrpc.UpdateNodeAnnouncement` | 1 | |
| | `disable_forwarding` (all) | `setchannel` (all, disabled) | `lnrpc.UpdateChannelPolicy` (all, disabled) | 6 | |
| **hive:expansion/v1** | | | | | |
| | `propose_channel_open` | Queued for operator approval | Queued for operator approval | 5–7 | Never auto-executed; always queued |
| **hive:channel/v1** | | | | | |
| | `open` | `fundchannel` | `lnrpc.OpenChannelSync` | 5–7 | |
| | `close_cooperative` | `close` | `lnrpc.CloseChannel` (cooperative) | 6 | |
| | `close_unilateral` | `close --unilateraltimeout=1` | `lnrpc.CloseChannel` (force=true) | 7 | |
| | `close_all` | `close` (loop, all) | `lnrpc.CloseChannel` (loop, all) | 10 | Nuclear. Always multi-sig. |
| **hive:splice/v1** | | | | | |
| | `splice_in` | `splice` (CLN ≥ v24.02) | N/A (experimental in LND) | 5–7 | LND: advertise as unsupported |
| | `splice_out` | `splice` | N/A | 6 | |
| **hive:peer/v1** | | | | | |
| | `connect` | `connect` | `lnrpc.ConnectPeer` | 2 | |
| | `disconnect` | `disconnect` | `lnrpc.DisconnectPeer` | 2–4 | |
| | `ban` | `dev-blacklist-peer` (if available) | Custom (blocklist file) | 5 | Implementation varies |
| **hive:payment/v1** | | | | | |
| | `create_invoice` | `invoice` | `lnrpc.AddInvoice` | 1 | |
| | `pay_invoice` | `pay` | `routerrpc.SendPaymentV2` | 4–6 | |
| | `keysend` | `keysend` | `routerrpc.SendPaymentV2` (keysend) | 4–6 | |
| **hive:wallet/v1** | | | | | |
| | `generate_address` | `newaddr` | `lnrpc.NewAddress` | 1 | |
| | `send_onchain` | `withdraw` | `lnrpc.SendCoins` | 6–9 | |
| | `utxo_management` | `fundpsbt` / `reserveinputs` | `walletrpc.FundPsbt` / `LeaseOutput` | 3–4 | |
| | `bump_fee` | `bumpfee` (via psbt) | `walletrpc.BumpFee` | 4 | |
| **hive:plugin/v1** | | | | | |
| | `list` | `plugin list` | N/A | 1 | LND: not applicable |
| | `start` | `plugin start` | N/A | 4–9 | LND: not applicable |
| | `stop` | `plugin stop` | N/A | 5 | LND: not applicable |
| **hive:backup/v1** | | | | | |
| | `trigger_backup` | `makesecret` + manual | `lnrpc.ExportAllChannelBackups` | 2 | |
| | `verify_backup` | Custom (hash check) | Custom (hash check) | 1 | |
| | `export_scb` | `staticbackup` | `lnrpc.ExportAllChannelBackups` | 3 | |
| | `restore` | N/A (requires restart) | `lnrpc.RestoreChannelBackups` | 10 | |
| **hive:emergency/v1** | | | | | |
| | `disable_forwarding` | `setchannel` (all, disabled) | `lnrpc.UpdateChannelPolicy` (all, disabled) | 6 | |
| | `fee_spike` | `setchannel` (all, max fee) | `lnrpc.UpdateChannelPolicy` (all, max fee) | 5 | |
| | `force_close` | `close --unilateraltimeout=1` | `lnrpc.CloseChannel` (force) | 8 | |
| | `force_close_all` | Loop `close` all | Loop `CloseChannel` all | 10 | |
| | `revoke_all_credentials` | Internal (revoke all via Archon) | Internal | 3 | |
| **hive:htlc/v1** | | | | | |
| | `list_stuck` | `listpeerchannels` (filter pending) | `lnrpc.ListChannels` (filter pending) | 2 | |
| | `inspect` | `listpeerchannels` (specific htlc) | `lnrpc.ListChannels` (specific htlc) | 2 | |
| | `fail_htlc` | `dev-fail-htlc` (dev mode) | `routerrpc.HtlcInterceptor` | 7 | CLN: requires `--developer`; LND: interceptor |
| | `settle_htlc` | `dev-resolve-htlc` (dev mode) | `routerrpc.HtlcInterceptor` | 7 | Same constraints |
| | `force_resolve_expired` | `dev-fail-htlc` (expired only) | `routerrpc.HtlcInterceptor` | 8 | Last resort |

### Semantic Differences

| Area | CLN Behavior | LND Behavior | Handling |
|------|-------------|-------------|----------|
| Fee unit | `fee_proportional_millionths` | `fee_rate_milli_msat` (ppm) | Translation layer normalizes to ppm |
| Channel ID | Short channel ID (`931770x2363x0`) | Channel point (`txid:index`) OR `chan_id` (uint64) | Both formats supported; translation layer converts |
| HTLC resolution | `dev-` commands (developer mode) | `routerrpc.HtlcInterceptor` stream | Capability advertised per implementation |
| Splicing | Native support (v24.02+) | Experimental / not available | Advertised as unsupported on LND |
| Plugin management | Full lifecycle | Not applicable | Schema returns `unsupported` on LND |
| Runtime config | `setconfig` (extensive) | Limited runtime changes | Advertised capabilities differ |

### Feature Capability Advertisement

On startup, the client determines which schemas it can support based on the underlying implementation and version:

```json
{
  "implementation": "CLN",
  "version": "24.08",
  "supported_schemas": [
    "hive:monitor/v1",
    "hive:fee-policy/v1",
    "hive:rebalance/v1",
    "hive:config/v1",
    "hive:expansion/v1",
    "hive:channel/v1",
    "hive:splice/v1",
    "hive:peer/v1",
    "hive:payment/v1",
    "hive:wallet/v1",
    "hive:plugin/v1",
    "hive:backup/v1",
    "hive:emergency/v1",
    "hive:htlc/v1"
  ],
  "unsupported_actions": [
    { "schema": "hive:htlc/v1", "action": "fail_htlc", "reason": "--developer not enabled" }
  ]
}
```

The advisor queries capabilities before sending commands. Commands for unsupported schemas return an error response with `status: 2` and a reason string.

**Danger score preservation:** Danger scores are identical regardless of implementation. A `hive:fee-policy/v1 set_anchor` is danger 3 whether on CLN or LND. The Policy Engine uses the same scoring table from the [Fleet Management spec](./DID-L402-FLEET-MANAGEMENT.md#task-taxonomy--danger-scoring).

---

## 6. Credential Management (Client Side)

### Issuing a Management Credential

The operator issues a `HiveManagementCredential` (per the [Fleet Management spec](./DID-L402-FLEET-MANAGEMENT.md#management-credentials)) to an advisor's DID:

```bash
# CLN
lightning-cli hive-client-authorize \
  --advisor-did="did:cid:bagaaiera..." \
  --template="fee_optimization" \
  --duration-days=30

# LND (via hive-lnd CLI)
hive-lnd authorize \
  --advisor-did="did:cid:bagaaiera..." \
  --template="fee_optimization" \
  --duration-days=30
```

The credential is signed by the operator's DID and delivered to the advisor via Bolt 8 custom message, Archon Dmail, or Nostr DM.

### Credential Templates

Pre-configured permission sets for common scenarios. Operators can use templates or define custom scopes.

| Template | Permissions | Schemas | Constraints | Use Case |
|----------|-----------|---------|-------------|----------|
| `monitor_only` | `monitor` | `hive:monitor/*` | Read-only, no state changes | Dashboard, alerting, reporting |
| `fee_optimization` | `monitor`, `fee_policy` | `hive:monitor/*`, `hive:fee-policy/*`, `hive:config/fee_*` | `max_fee_change_pct: 50`, `max_daily_actions: 50` | Automated fee management |
| `full_routing` | `monitor`, `fee_policy`, `rebalance`, `config_tune` | `hive:monitor/*`, `hive:fee-policy/*`, `hive:rebalance/*`, `hive:config/*` | `max_rebalance_sats: 1000000`, `max_daily_actions: 100` | Full routing optimization |
| `complete_management` | All except `channel_close` | All except `hive:channel/close_*`, `hive:emergency/force_close_*` | `max_daily_actions: 200` | Full management minus nuclear options |

#### Custom Scope

```bash
lightning-cli hive-client-authorize \
  --advisor-did="did:cid:bagaaiera..." \
  --permissions='{"monitor":true,"fee_policy":true,"rebalance":true}' \
  --schemas='["hive:monitor/*","hive:fee-policy/*","hive:rebalance/circular_*"]' \
  --constraints='{"max_fee_change_pct":25,"max_rebalance_sats":500000}' \
  --duration-days=14
```

### Credential Lifecycle

```
Issue ──► Active ──┬──► Renew ──► Active (extended)
                   │
                   ├──► Expire (natural end)
                   │
                   └──► Revoke (operator-initiated, immediate)
```

1. **Issue** — Operator creates and signs credential. Delivered to advisor.
2. **Active** — Advisor presents credential with each management command. Node validates.
3. **Renew** — Before expiry, operator issues a new credential with updated terms. Old credential superseded.
4. **Expire** — Credential's `validUntil` date passes. All commands rejected. No cleanup needed.
5. **Revoke** — Operator calls `hive-client-revoke`. Credential marked as revoked in Archon. All pending commands from this credential are rejected immediately.

### Multi-Advisor Support

Operators can issue credentials to multiple advisors with non-overlapping scopes:

```bash
# Advisor A: fee expert
lightning-cli hive-client-authorize --advisor-did="did:cid:A..." --template="fee_optimization"

# Advisor B: rebalance specialist
lightning-cli hive-client-authorize --advisor-did="did:cid:B..." \
  --permissions='{"monitor":true,"rebalance":true}' \
  --schemas='["hive:monitor/*","hive:rebalance/*"]'

# Advisor C: monitoring only (dashboard provider)
lightning-cli hive-client-authorize --advisor-did="did:cid:C..." --template="monitor_only"
```

The Policy Engine enforces scope isolation — Advisor A cannot send `hive:rebalance/*` commands even if their credential somehow includes that scope, because the operator configured them for fee optimization only.

For multi-advisor coordination details (conflict detection, shared state, action cooldowns), see the [Marketplace spec, Section 6](./DID-HIVE-MARKETPLACE.md#6-multi-advisor-coordination).

### Emergency Revocation

```bash
# Immediate revocation — all pending commands rejected
lightning-cli hive-client-revoke --advisor-did="did:cid:badactor..."

# Revoke ALL advisors (emergency lockdown)
lightning-cli hive-client-revoke --all
```

Revocation:
1. Marks credential as revoked locally (takes effect immediately for all pending/future commands)
2. Publishes revocation to Archon network (propagates to advisor and any verifier)
3. Logs the revocation event with reason in the Receipt Store
4. Sends alert via configured channels (webhook, Nostr DM, email)

The advisor's pending legitimate compensation (escrow tickets for completed work where the preimage was already revealed) is honored — the advisor can still redeem those tokens. Revocation only affects future commands.

---

## 7. Escrow Management (Client Side)

### Built-in Cashu Wallet

The client includes a lightweight Cashu wallet implementing NUT-10 (structured secrets), NUT-11 (P2PK), NUT-14 (HTLCs), and NUT-07 (token state checks). This wallet handles all escrow operations without requiring external wallet software.

### Ticket Creation Workflow

```
Operator                     Client Plugin               Cashu Mint
    │                             │                          │
    │  1. Advisor requests task   │                          │
    │  ◄──────────────────────    │                          │
    │                             │                          │
    │  2. Client auto-creates     │                          │
    │     escrow ticket:          │                          │
    │     - Generates HTLC secret │                          │
    │     - Computes H(secret)    │                          │
    │     - Mints Cashu token     │                          │
    │                      ───────────────────────────────►  │
    │                             │                          │
    │     - Token received        │                          │
    │                      ◄───────────────────────────────  │
    │                             │                          │
    │  3. Ticket sent to advisor  │                          │
    │     via Bolt 8              │                          │
    │  ──────────────────────►    │                          │
    │                             │                          │
```

For low-danger actions (score 1–2), the operator can configure **direct payment** (simple Cashu token, no HTLC escrow) to reduce overhead. For danger score 3+, full escrow is always used per the [Task Escrow spec](./DID-CASHU-TASK-ESCROW.md#danger-score-integration).

### Auto-Replenishment

```yaml
escrow:
  replenish_threshold: 1000    # sats — trigger replenishment when balance drops below
  replenish_amount: 5000       # sats — amount to mint on replenishment
  replenish_source: "onchain"  # "onchain" (from node wallet) or "lightning" (via invoice)
  auto_replenish: true         # enable automatic replenishment
```

When auto-replenishment triggers:
1. Client checks node's on-chain wallet balance (or creates a Lightning invoice)
2. If sufficient funds, mints new Cashu tokens at the preferred mint
3. New tokens added to the escrow wallet
4. Operator notified via alert channel

**Safety:** Auto-replenishment respects `daily_limit` and `weekly_limit`. If the limit would be exceeded, replenishment is blocked and the operator is alerted.

### Spending Limits

| Limit | Default | Configurable | Enforcement |
|-------|---------|-------------|-------------|
| Per-action cap | None (uses danger-score pricing) | Yes | Hard reject if exceeded |
| Daily cap | 50,000 sats | Yes | No new escrow tickets minted beyond cap |
| Weekly cap | 200,000 sats | Yes | No new escrow tickets minted beyond cap |
| Per-advisor daily cap | 25,000 sats | Yes | Per-advisor enforcement |

When a limit is reached, the client stops minting new escrow tickets and alerts the operator. The advisor receives a `budget_exhausted` error on their next command attempt.

### Mint Selection

```yaml
escrow:
  preferred_mint: "https://mint.minibits.cash"
  backup_mints:
    - "https://mint2.example.com"
  mint_health_check_interval: 3600  # seconds
```

The client periodically checks mint health (`GET /v1/info`) and switches to backup mints if the preferred mint is unreachable. Mint capabilities (NUT-10, NUT-11, NUT-14 support) are verified at startup.

### Receipt Tracking

All completed tasks generate receipts stored in the local Receipt Store:

```bash
# View recent receipts
lightning-cli hive-client-receipts --limit=10

# View receipts for a specific advisor
lightning-cli hive-client-receipts --advisor-did="did:cid:A..."

# Export receipts for auditing
lightning-cli hive-client-receipts --since="2026-02-01" --format=json > receipts.json
```

Each receipt links to the escrow ticket, the task command, the execution result, and the HTLC preimage (for completed tasks). This creates a complete audit trail of all management activity and its cost.

---

## 8. Local Policy Engine

### Purpose

The Policy Engine is the operator's **last line of defense**. Even if an advisor presents a valid credential, a valid payment, and a well-formed command, the Policy Engine can reject the action based on locally-defined rules. This is critical because:

- Credentials can be too permissive (operator granted broader access than intended)
- Advisors can make mistakes (valid action, bad judgment)
- Advisors can be adversarial (valid credential, malicious intent)

The Policy Engine enforces the operator's risk tolerance independent of the credential system.

### Default Policy Presets

| Preset | Philosophy | Max Fee Change/24h | Max Rebalance | Forbidden Actions | Confirmation Required |
|--------|-----------|-------------------|--------------|-------------------|----------------------|
| `conservative` | Safety first | ±15% per channel | 100k sats | Channel close, force close, wallet send, plugin start | Danger ≥ 5 |
| `moderate` | Balanced | ±30% per channel | 500k sats | Force close, wallet sweep, plugin start (unapproved) | Danger ≥ 7 |
| `aggressive` | Maximum advisor autonomy | ±50% per channel | 2M sats | Wallet sweep, force close all | Danger ≥ 9 |

### Custom Policy Rules

Operators can define granular rules beyond the presets:

```json
{
  "policy_version": 1,
  "preset": "moderate",
  "overrides": {
    "max_fee_change_per_24h_pct": 25,
    "max_rebalance_sats": 300000,
    "max_rebalance_fee_ppm": 500,
    "forbidden_peers": ["03badpeer..."],
    "protected_channels": ["931770x2363x0"],
    "required_confirmation": {
      "danger_gte": 6,
      "channel_close": "always",
      "onchain_send_gte_sats": 50000
    },
    "rate_limits": {
      "fee_changes_per_hour": 10,
      "rebalances_per_day": 20,
      "total_actions_per_day": 100
    },
    "time_restrictions": {
      "quiet_hours": { "start": "23:00", "end": "07:00", "timezone": "UTC" },
      "quiet_hour_max_danger": 2
    }
  }
}
```

#### Protected Channels

Channels in the `protected_channels` list cannot be modified by any advisor. Fee changes, disabling, closing — all rejected. This is useful for critical channels with important peers.

#### Forbidden Peers

Advisors cannot open channels to, connect to, or route through nodes in the `forbidden_peers` list. Protects against advisors routing through known malicious nodes or competitors.

#### Quiet Hours

During quiet hours, only low-danger actions (monitoring, read-only) are permitted. This prevents advisors from making significant changes while the operator is sleeping.

### Confirmation Flow

When the Policy Engine requires confirmation (based on danger score or rule):

```
Advisor ──► Client Plugin ──► Policy Engine
                                    │
                              Requires confirmation
                                    │
                         ┌──────────▼──────────┐
                         │   Alert Operator     │
                         │   (webhook/Nostr/    │
                         │    email)            │
                         └──────────┬──────────┘
                                    │
                              Operator reviews
                                    │
                         ┌──────────▼──────────┐
                         │  Approve / Reject    │
                         │  (via RPC command)   │
                         └──────────┬──────────┘
                                    │
                              ┌─────┴─────┐
                              │           │
                           Approve     Reject
                              │           │
                           Execute    Reject + notify advisor
```

Pending confirmations expire after a configurable timeout (default: 24 hours for danger 5–6, 4 hours for danger 7–8). Expired confirmations are rejected.

```bash
# View pending confirmations
lightning-cli hive-client-status --pending

# Approve a pending action
lightning-cli hive-client-approve --action-id=47

# Reject a pending action
lightning-cli hive-client-approve --action-id=47 --reject --reason="Too aggressive"
```

### Alert Integration

The Policy Engine sends alerts for all advisor actions above a configurable threshold:

| Alert Level | Trigger | Channels |
|------------|---------|----------|
| **info** | Any action executed (danger 1–2) | Digest (daily summary) |
| **notice** | Standard actions (danger 3–4) | Real-time: webhook |
| **warning** | Elevated actions (danger 5–6) | Real-time: webhook + Nostr DM |
| **critical** | High/critical actions (danger 7+) | Real-time: webhook + Nostr DM + email |
| **confirmation** | Action requires approval | All channels + push notification |

Alert channels:

```yaml
alerts:
  webhook: "https://hooks.example.com/hive"
  nostr_dm: "npub1abc..."
  email: "operator@example.com"
  # Future: Telegram, Signal, SMS
```

### Policy Overrides

Operators can temporarily tighten or loosen policy:

```bash
# Temporarily tighten (e.g., during maintenance window)
lightning-cli hive-client-policy --override='{"max_danger": 2}' --duration="4h"

# Temporarily loosen (e.g., for a specific operation)
lightning-cli hive-client-policy --override='{"max_rebalance_sats": 2000000}' --duration="1h"

# Remove override (return to base policy)
lightning-cli hive-client-policy --clear-override
```

Overrides auto-expire after the specified duration. This prevents "forgot to undo the loose policy" scenarios.

---

## 9. Discovery for Non-Hive Nodes

Non-hive nodes cannot use hive gossip for advisor discovery. Four alternative mechanisms are supported, ordered by decentralization:

### Archon Network Discovery

Query the Archon network for `HiveServiceProfile` credentials:

```bash
lightning-cli hive-client-discover --source=archon --capabilities="fee-optimization"
```

Under the hood:
1. Client queries the Archon gateway for credentials of type `HiveServiceProfile`
2. Filters by requested capabilities, pricing, availability
3. Fetches linked reputation credentials
4. Ranks results using the [Marketplace ranking algorithm](./DID-HIVE-MARKETPLACE.md#filtering--ranking-algorithm)
5. Returns sorted advisor list

**Trust level:** High — profiles are signed VCs, reputation is verifiable, DID resolution is cryptographic.

### Nostr Discovery

Advisors publish service profiles to Nostr (as defined in the [Marketplace spec](./DID-HIVE-MARKETPLACE.md#advertising-via-nostr-optional)):

```bash
lightning-cli hive-client-discover --source=nostr --capabilities="rebalancing"
```

The client subscribes to Nostr events with kind `38383` and tag `t:hive-advisor`, filters by capability tags, and verifies the embedded `HiveServiceProfile` credential signature.

**Trust level:** Medium — Nostr events are signed by Nostr keys, but the DID-to-Nostr binding must be verified via the advisor's attestation credential.

### Directory Discovery

Optional curated directories — web services that aggregate and vet advisor profiles:

```bash
lightning-cli hive-client-discover --source=directory --url="https://hive-advisors.example.com"
```

Directories are not trusted — they're convenience tools. The client always verifies the underlying DID credentials independently.

**Trust level:** Low for the directory itself (could be biased); high for the verified credentials it surfaces.

### Direct Connection

The operator already has the advisor's DID (e.g., from a personal recommendation, a website, or a conference):

```bash
lightning-cli hive-client-authorize --advisor-did="did:cid:bagaaiera..." --template="fee_optimization"
```

No discovery needed. The operator directly issues a credential.

### Referral Discovery

An existing client refers an advisor via a signed referral credential (per the [Marketplace spec, Section 8](./DID-HIVE-MARKETPLACE.md#8-referral--affiliate-system)):

```bash
# Advisor A refers Advisor B to the operator
# Operator receives referral credential and reviews
lightning-cli hive-client-discover --source=referral --referral-cred="did:cid:referral..."
```

**Trust level:** Proportional to the referrer's reputation.

---

## 10. Onboarding Flow

Step-by-step process for a new node operator to start using professional management:

### Step 1: Install Plugin/Daemon

```bash
# CLN
curl -O https://github.com/lightning-goats/cl-hive-client/releases/latest/cl_hive_client.py
lightning-cli plugin start /path/to/cl_hive_client.py

# LND
curl -LO https://github.com/lightning-goats/hive-lnd/releases/latest/hive-lnd-linux-amd64
hive-lnd init && hive-lnd --config ~/.hive-lnd/hive-lnd.yaml
```

### Step 2: Create or Import DID

```bash
npm install -g @didcid/keymaster
npx @didcid/keymaster create-id --name my-node
# Add DID to config
```

If the operator already has an Archon DID, import it instead.

### Step 3: Discover Advisors

```bash
lightning-cli hive-client-discover --capabilities="fee-optimization,rebalancing"
```

Returns a ranked list of advisors with reputation scores, pricing, and availability.

### Step 4: Review Advisor Reputation

```bash
# View detailed advisor profile and reputation
lightning-cli hive-client-discover --advisor-did="did:cid:advisor..." --detail
```

Review:
- Number of nodes managed and average tenure
- Revenue improvement metrics across clients
- Escrow history (completed tickets, timeouts, disputes)
- Trial period success rate

### Step 5: Select Advisor and Configure Credential

```bash
# Start with a trial period
lightning-cli hive-client-trial \
  --advisor-did="did:cid:advisor..." \
  --duration-days=14 \
  --scope="monitor,fee-policy"
```

### Step 6: Fund Escrow Wallet

```bash
# Check current balance
lightning-cli hive-client-escrow balance

# Mint initial escrow tokens
lightning-cli hive-client-escrow mint --amount=10000
```

### Step 7: Trial Period (7–14 Days)

During the trial:
- Advisor operates with reduced scope (monitor + fee-policy only)
- Flat-fee compensation (no performance bonus)
- Client measures baseline metrics
- Both parties evaluate fit

### Step 8: Review Trial Results

```bash
# View trial metrics
lightning-cli hive-client-trial --review

# Output: actions taken, revenue delta, uptime, response time
```

### Step 9: Full Contract or Terminate

```bash
# If satisfied: upgrade to full credential
lightning-cli hive-client-authorize \
  --advisor-did="did:cid:advisor..." \
  --template="full_routing" \
  --duration-days=90

# If not: terminate trial (no penalty)
lightning-cli hive-client-revoke --advisor-did="did:cid:advisor..."
```

### Step 10: Ongoing Management

With the full credential active:
- Advisor manages the node per contracted scope
- Escrow auto-replenishes
- Policy Engine enforces local rules
- Operator receives alerts for significant actions
- Receipts accumulate for auditing
- At contract end, both parties issue mutual reputation credentials

---

## 11. Hive Membership Upgrade Path

Client-only nodes can upgrade to full hive membership when they want the benefits of fleet coordination.

### What Changes

| Aspect | Client | Full Hive Member |
|--------|--------|-----------------|
| Software | `cl-hive-client` | `cl-hive` (full plugin) |
| Bond | None | 50,000–500,000 sats (per [Settlements spec](./DID-HIVE-SETTLEMENTS.md#bond-sizing)) |
| Gossip | No participation | Full gossip network access |
| Settlement | Direct escrow only | Netting, credit tiers, bilateral/multilateral |
| Fleet rebalancing | N/A | Intra-hive paths (97% fee savings) |
| Pheromone routing | N/A | Full stigmergic signal access |
| Intelligence market | Buy from advisor directly | Full market access (buy/sell) |
| Management fees | Per-action / subscription | Discounted (fleet paths reduce advisor costs) |

### What Stays the Same

- Same management interface (schemas, custom messages, receipt format)
- Same credential system (management credentials work identically)
- Same escrow mechanism (Cashu tickets, same mints)
- Same advisor relationships (existing credentials remain valid)
- Same reputation history (reputation credentials are portable across membership levels)

### Migration Process

```bash
# 1. Install full cl-hive (replaces cl-hive-client)
lightning-cli plugin stop cl_hive_client.py
lightning-cli plugin start cl_hive.py

# 2. Join hive PKI
lightning-cli hive-join --hive-id="<hive_identifier>"

# 3. Post bond
lightning-cli hive-bond --amount=50000 --mint="https://mint.hive.lightning"

# 4. Wait for hive acceptance (bond verification + existing reputation review)
lightning-cli hive-status

# 5. Existing advisor relationships continue unchanged
```

### Incentives to Upgrade

| Benefit | Impact |
|---------|--------|
| Fleet rebalancing paths | 97% cheaper than public routing (per cl-hive pheromone system) |
| Intelligence market access | Buy/sell routing intelligence with other hive members |
| Discounted management | Advisors pass on cost savings from fleet paths |
| Settlement netting | Bilateral/multilateral netting reduces escrow overhead |
| Credit tiers | Long-tenure members get credit lines, reducing pre-payment requirements |
| Governance participation | Vote on hive parameters, schema governance |

---

## 12. Security Considerations

### Attack Surface

The client plugin/daemon introduces a new attack surface on the node:

| Attack Vector | Risk | Mitigation |
|--------------|------|-----------|
| Malicious custom messages from non-advisors | Low — messages from unauthorized DIDs are rejected at credential check | Credential Verifier is the first check; messages without valid credentials never reach the Schema Handler |
| Compromised advisor credential | Medium — advisor could execute damaging actions within credential scope | Policy Engine limits blast radius; credential scope is narrow; revocation is instant |
| Compromised Archon Keymaster | High — attacker could issue credentials | Keymaster passphrase protection; key material never leaves the operator's machine |
| Malicious mint | Medium — escrow tokens could be stolen | Multi-mint strategy; operator controls which mints are trusted; pre-flight token verification |
| DID resolution poisoning | Low — attacker provides false DID documents | Multiple Archon gateways for verification; local cache with TTL |
| Policy Engine bypass | Critical if possible — but code is local, operator-controlled | Open-source auditable code; policy is enforced locally, not by the advisor |

### Malicious Advisor Protections

Assume the worst: the advisor is adversarial. Defense layers, from outermost to innermost:

1. **Credential scope** — The blast radius is limited to the schemas and constraints in the credential. A `fee_optimization` credential cannot close channels.

2. **Policy Engine** — Even within credential scope, the Policy Engine enforces operator-defined limits. Max fee change per period, max rebalance amount, forbidden peers, quiet hours.

3. **Spending limits** — Escrow expenditure is capped daily and weekly. An adversarial advisor cannot drain the operator's escrow wallet.

4. **Confirmation requirements** — High-danger actions require explicit operator approval. The advisor cannot auto-execute anything above the configured danger threshold.

5. **Rate limiting** — Actions are rate-limited per hour and per day. An advisor cannot flood the node with rapid-fire commands.

6. **Audit trail** — Every action is logged in the tamper-evident Receipt Store. The operator can review what the advisor did and when.

7. **Instant revocation** — One command (`hive-client-revoke`) immediately invalidates the advisor's credential. Fail-closed: if Archon is unreachable for revocation check, all commands are denied.

### What Advisors Can Never Do

Regardless of credential scope or Policy Engine configuration:

- **Access private keys** — The client never exposes node private keys, seed phrases, or HSM secrets to advisors
- **Modify the client software** — Advisors interact via the schema interface only; they cannot change plugin code or configuration
- **Bypass the Policy Engine** — Policy is enforced locally; the advisor has no mechanism to disable it
- **Access other advisors' credentials** — Multi-advisor isolation is enforced by the client
- **Persist access after revocation** — Revocation is instant and fail-closed

### Audit Log

The Receipt Store serves as a tamper-evident audit log:

- **Hash chaining** — Each receipt includes the hash of the previous receipt. Modifying any receipt breaks the chain.
- **Dual signatures** — Both the agent's DID and the node sign each receipt. Neither party can forge a receipt alone.
- **Periodic merkle roots** — Hourly/daily merkle roots are computed and optionally published (e.g., to Archon or Nostr) for external timestamping.
- **Export** — Receipts can be exported for independent audit at any time.

### Network-Level Security

- **Bolt 8 encryption** — All management traffic uses Noise_XK with forward secrecy. Management commands are invisible to network observers.
- **No cleartext management traffic** — The client never sends management commands over unencrypted channels.
- **Custom message types are odd** (49153, 49155) — Per BOLT 1, non-hive peers simply ignore these messages. No information leakage to uninvolved peers.

---

## 13. Comparison: Client vs Hive Member vs Unmanaged

### Feature Comparison

| Feature | Unmanaged | Client | Hive Member |
|---------|-----------|--------|-------------|
| Fee optimization | Manual | ✓ (advisor) | ✓ (advisor + fleet intel) |
| Rebalancing | Manual | ✓ (advisor) | ✓ (advisor + 97% cheaper paths) |
| Channel expansion | Manual | ✓ (advisor proposals) | ✓ (advisor + hive coordination) |
| Monitoring | DIY tools | ✓ (advisor + client alerts) | ✓ (advisor + hive health) |
| HTLC resolution | Manual | ✓ (advisor, if admin tier) | ✓ (advisor + fleet coordination) |
| Pheromone routing | ✗ | ✗ | ✓ |
| Intelligence market | ✗ | ✗ (advisor provides) | ✓ (full market) |
| Settlement netting | ✗ | ✗ | ✓ |
| Credit tiers | ✗ | ✗ | ✓ |
| Governance | ✗ | ✗ | ✓ |
| Reputation earned | ✗ | ✓ (`hive:client`) | ✓ (`hive:node`) |
| DID identity | Optional | Required | Required |
| Local policy engine | ✗ | ✓ | ✓ |
| Audit trail | ✗ | ✓ | ✓ |

### Cost Comparison

| Model | Upfront | Ongoing | Revenue Impact |
|-------|---------|---------|----------------|
| **Unmanaged** | 0 sats | 0 sats | Baseline (leaving 50–200% revenue on table) |
| **Client** | 0 sats | 2,000–50,000 sats/month (per advisor pricing) | +50–300% revenue improvement (varies by advisor quality) |
| **Hive Member** | 50,000–500,000 sats (bond) | 1,000–30,000 sats/month (discounted via fleet) | +100–500% revenue improvement (fleet intelligence + cheaper rebalancing) |

Bond is recoverable (minus any slashing) on hive exit.

### Risk Comparison

| Risk | Unmanaged | Client | Hive Member |
|------|-----------|--------|-------------|
| Adversarial advisor | N/A | Policy Engine + credential scope + escrow limits | Same + bond forfeiture for hive-attested advisors |
| Fund loss from mismanagement | Self-inflicted | Limited by Policy Engine constraints | Same + fleet cross-checks |
| Privacy | Full control | Advisor sees channel data (within credential scope) | Hive sees aggregate data; advisor sees detail |
| Lock-in | None | None (switch advisors anytime) | Bond lock-up (6-month default) |
| Dependency | None | Advisor uptime (mitigated by monitoring fallback) | Advisor + hive infrastructure |

### When to Use Each Model

| Scenario | Recommendation |
|----------|---------------|
| Hobbyist, < 5 channels, no revenue goal | Unmanaged |
| Small-medium node, wants optimization, low commitment | **Client** with `fee_optimization` template |
| Medium node, wants full management, growing fleet | **Client** with `full_routing` template |
| Large routing node, wants fleet benefits, willing to post bond | **Hive Member** |
| Professional routing business, multiple nodes | **Hive Member** (founding/full) |

---

## 14. Implementation Roadmap

Phased delivery, aligned with the other specs' roadmaps. The client is designed to be useful early — even Phase 1 provides value.

### Phase 1: Core Client (4–6 weeks)
*Prerequisites: Fleet Management Phase 1–2 (schemas + DID auth)*

- `cl-hive-client` Python plugin with Schema Handler and Credential Verifier
- Custom message handling (types 49153/49155)
- Basic Policy Engine (presets only)
- Receipt Store (SQLite, hash-chained)
- RPC commands: `hive-client-status`, `hive-client-authorize`, `hive-client-revoke`, `hive-client-receipts`
- CLN schema translation for categories 1–4 (monitor, fee-policy, HTLC policy, forwarding)

### Phase 2: Escrow Integration (3–4 weeks)
*Prerequisites: Task Escrow Phase 1 (single tickets)*

- Built-in Cashu wallet (NUT-10/11/14)
- Escrow ticket creation and management
- Auto-replenishment
- Spending limits
- `hive-client-escrow` RPC command

### Phase 3: Full Schema Coverage (3–4 weeks)
*Prerequisites: Phase 1*

- Schema translation for categories 5–15 (rebalancing through emergency)
- Feature capability advertisement
- Danger score integration with Policy Engine

### Phase 4: LND Daemon (4–6 weeks)
*Prerequisites: Phase 1–3 (proven design from CLN)*

- `hive-lnd` Go daemon with all components
- LND gRPC integration for all schema categories
- Schema translation layer (CLN → LND equivalents)
- `HiveClientService` gRPC API
- CLI tool and systemd integration

### Phase 5: Discovery & Onboarding (3–4 weeks)
*Prerequisites: Marketplace Phase 1 (service profiles)*

- `hive-client-discover` with Archon, Nostr, and directory sources
- `hive-client-trial` for trial period management
- Onboarding wizard (interactive CLI)
- Referral discovery support

### Phase 6: Advanced Policy & Alerts (2–3 weeks)
*Prerequisites: Phase 1*

- Custom policy rules (beyond presets)
- Confirmation flow for high-danger actions
- Alert integration (webhook, Nostr DM, email)
- Quiet hours, protected channels, forbidden peers
- Policy overrides with auto-expiry

### Phase 7: Multi-Advisor & Upgrade Path (2–3 weeks)
*Prerequisites: Phase 1, Marketplace Phase 4 (multi-advisor)*

- Multi-advisor scope isolation
- Conflict detection
- Hive membership upgrade flow
- Migration tooling (client → full member)

### Cross-Spec Integration

```
Fleet Mgmt Phase 1-2  ──────────►  Client Phase 1 (core client)
                                         │
Task Escrow Phase 1    ──────────►  Client Phase 2 (escrow)
                                         │
Fleet Mgmt Phase 3     ──────────►  Client Phase 3 (full schemas)
                                         │
Client Phase 1-3       ──────────►  Client Phase 4 (LND daemon)
                                         │
Marketplace Phase 1    ──────────►  Client Phase 5 (discovery)
```

---

## 15. Open Questions

1. **Keymaster packaging:** Should the Archon Keymaster be bundled with the client plugin/daemon, or remain a separate dependency? Bundling reduces friction but increases maintenance burden.

2. **Auto-replenishment funding source:** Should auto-replenishment draw from the node's on-chain wallet (simple, requires on-chain funds) or via Lightning invoice (more complex, uses existing liquidity)? Both have tradeoffs.

3. **LND HTLC management:** LND lacks `dev-fail-htlc`-style commands. The `HtlcInterceptor` API provides similar functionality but requires the daemon to intercept all HTLCs, which has performance implications. Is this acceptable for production use?

4. **Policy Engine complexity:** How many custom rules are too many? A complex policy is harder to audit and may have unexpected interactions between rules. Should we limit the number of custom rules or provide rule conflict detection?

5. **Multi-implementation testing:** The Schema Translation Layer assumes specific RPC behavior from CLN and LND. How do we test correctness across both implementations, especially for edge cases (concurrent operations, error handling)?

6. **Advisor-side client library:** This spec focuses on the node operator's client. Should there be a corresponding advisor-side library/SDK that simplifies building advisors? Or is the schema spec sufficient?

7. **Offline operation:** If the Archon gateway is unreachable, the client denies all commands (fail-closed). This is safe but could deny service during Archon outages. Should there be a cached-credential mode for short outages, with degraded trust?

8. **Cross-implementation credentials:** A credential issued for a CLN node should work if the operator migrates to LND (same DID, same node pubkey). Are there edge cases where implementation-specific credential constraints break?

9. **Client-to-client communication:** Could client nodes discover and communicate with each other (e.g., for referral-based reputation, cooperative rebalancing) without full hive membership? This would create a "light hive" network.

10. **Tiered client product:** Should there be a free tier (monitor-only, limited discovery) and a paid tier (full management, priority discovery)? Or should the client software be fully open and free, with advisors as the only revenue source?

---

## 16. References

- [DID + L402 Remote Fleet Management](./DID-L402-FLEET-MANAGEMENT.md) — Schema definitions, credential format, transport protocol, danger scoring
- [DID + Cashu Task Escrow Protocol](./DID-CASHU-TASK-ESCROW.md) — Escrow ticket format, HTLC conditions, ticket types
- [DID Hive Marketplace Protocol](./DID-HIVE-MARKETPLACE.md) — Service profiles, discovery, negotiation, contracting, multi-advisor coordination
- [DID + Cashu Hive Settlements Protocol](./DID-HIVE-SETTLEMENTS.md) — Bond system, settlement types, credit tiers
- [DID Reputation Schema](./DID-REPUTATION-SCHEMA.md) — Reputation credential format, `hive:advisor` and `hive:client` profiles
- [CLN Plugin Documentation](https://docs.corelightning.org/docs/plugin-development)
- [CLN Custom Messages](https://docs.corelightning.org/reference/lightning-sendcustommsg)
- [CLN `setchannel` RPC](https://docs.corelightning.org/reference/lightning-setchannel)
- [CLN `listpeerchannels` RPC](https://docs.corelightning.org/reference/lightning-listpeerchannels)
- [LND gRPC API Reference](https://api.lightning.community/)
- [LND `lnrpc.UpdateChannelPolicy`](https://api.lightning.community/#updatechannelpolicy)
- [LND `routerrpc.SendPaymentV2`](https://api.lightning.community/#sendpaymentv2)
- [LND Custom Messages](https://api.lightning.community/#sendcustommessage)
- [Cashu NUT-10: Spending Conditions](https://github.com/cashubtc/nuts/blob/main/10.md)
- [Cashu NUT-11: Pay-to-Public-Key](https://github.com/cashubtc/nuts/blob/main/11.md)
- [Cashu NUT-14: Hashed Timelock Contracts](https://github.com/cashubtc/nuts/blob/main/14.md)
- [W3C DID Core 1.0](https://www.w3.org/TR/did-core/)
- [W3C Verifiable Credentials Data Model 2.0](https://www.w3.org/TR/vc-data-model-2.0/)
- [Archon: Decentralized Identity for AI Agents](https://github.com/archetech/archon)
- [BOLT 1: Base Protocol](https://github.com/lightning/bolts/blob/master/01-messaging.md) — Custom message type rules (odd = optional)
- [BOLT 8: Encrypted and Authenticated Transport](https://github.com/lightning/bolts/blob/master/08-transport.md)
- [Lightning Hive: Swarm Intelligence for Lightning](https://github.com/lightning-goats/cl-hive)

---

*Feedback welcome. File issues on [cl-hive](https://github.com/lightning-goats/cl-hive) or discuss in #singularity.*

*— Hex ⬡*
