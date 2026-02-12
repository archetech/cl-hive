# Archon Integration for Hive Governance Messaging

## Overview

Optional Archon DID integration for cl-hive enables cryptographically signed, verifiable governance messaging between hive members. Messages are delivered via Archon dmail (encrypted DID-to-DID communication).

## Configuration

### Node Configuration

Add to `config.json` or via `hive-config`:

```json
{
  "archon": {
    "enabled": false,
    "our_did": "did:cid:bagaaiera...",
    "gatekeeper_url": "https://archon.technology",
    "passphrase_env": "ARCHON_PASSPHRASE",
    "auto_notify": ["health_critical", "ban_proposal", "settlement_complete"],
    "message_retention_days": 90
  }
}
```

### Member Contact Registry

Each member can register their Archon DID for receiving governance messages:

```bash
lightning-cli hive-register-contact \
  peer_id="03796a3c5b18080d..." \
  alias="cypher" \
  archon_did="did:cid:bagaaiera..." \
  notify_preferences='["health", "governance", "settlement"]'
```

---

## Governance Message Categories

### 1. Membership Lifecycle

#### 1.1 New Member Joined
**Trigger:** `handle_join_complete()` / new member added to hive
**Recipients:** All existing members
**Template:**
```
Subject: [HIVE] New Member Joined: {alias}

A new member has joined the hive.

Member: {peer_id}
Alias: {alias}
Tier: {tier}
Joined: {timestamp}
Channels: {channel_count}
Capacity: {capacity_sats} sats

Welcome them to the fleet!

— Hive Governance System
Signed: {hive_admin_did}
```

#### 1.2 Welcome Message (to new member)
**Trigger:** Member successfully joins
**Recipients:** New member only
**Template:**
```
Subject: [HIVE] Welcome to {hive_name}

Welcome to the hive!

Your membership:
- Tier: neophyte (90-day probation)
- Voting rights: Limited until promotion
- Settlement: Eligible after first cycle

Getting Started:
1. Open channels to other fleet members (0 fee internally)
2. Participate in routing to build contribution score
3. Request promotion after demonstrating value

Fleet Members:
{member_list}

Questions? Contact: {admin_contact}

— Hive Governance System
```

#### 1.3 Member Left
**Trigger:** `handle_member_left()`
**Recipients:** All members
**Template:**
```
Subject: [HIVE] Member Departed: {alias}

A member has left the hive.

Member: {peer_id}
Alias: {alias}
Reason: {reason}  # voluntary, banned, inactive
Duration: {membership_duration}

{if reason == "voluntary"}
Their channels remain open but are no longer hive-internal.
Consider adjusting fees on channels to this peer.
{/if}

— Hive Governance System
```

---

### 2. Promotion Governance

#### 2.1 Promotion Proposed
**Trigger:** `hive-propose-promotion` called
**Recipients:** All voting members + the nominee
**Template:**
```
Subject: [HIVE] Promotion Proposal: {alias} → Member

A promotion has been proposed.

Nominee: {peer_id} ({alias})
Current Tier: neophyte
Proposed Tier: member
Proposer: {proposer_alias}

Nominee Stats:
- Membership Duration: {days} days
- Contribution Score: {score}
- Routing Volume: {volume_sats} sats
- Vouches: {vouch_count}

Vote Deadline: {deadline}
Quorum Required: {quorum_pct}% ({quorum_count} votes)

To vote:
  lightning-cli hive-vote-promotion {peer_id} approve="true"

— Hive Governance System
```

#### 2.2 Promotion Vote Cast
**Trigger:** `hive-vote-promotion` called
**Recipients:** Nominee + proposer
**Template:**
```
Subject: [HIVE] Vote Cast on Your Promotion

A vote has been cast on the promotion proposal.

Voter: {voter_alias}
Vote: {approve/reject}
Current Tally: {approve_count} approve / {reject_count} reject
Quorum: {current}/{required}

{if quorum_reached}
Quorum reached! Promotion will be executed.
{else}
{remaining} more votes needed.
{/if}

— Hive Governance System
```

#### 2.3 Promotion Executed
**Trigger:** Quorum reached and promotion applied
**Recipients:** All members
**Template:**
```
Subject: [HIVE] Promotion Complete: {alias} is now a Member

The promotion has been executed.

Member: {peer_id} ({alias})
New Tier: member
Effective: {timestamp}

New privileges:
- Full voting rights
- Settlement participation
- Can propose new members

Final Vote: {approve_count} approve / {reject_count} reject

Congratulations {alias}!

— Hive Governance System
```

---

### 3. Ban Governance

#### 3.1 Ban Proposed
**Trigger:** `handle_ban_proposal()` or gaming detected
**Recipients:** All voting members + accused (optional)
**Template:**
```
Subject: [HIVE] ⚠️ Ban Proposal: {alias}

A ban has been proposed against a hive member.

Accused: {peer_id} ({alias})
Proposer: {proposer_alias}
Reason: {reason}

Evidence:
{evidence_details}

Vote Deadline: {deadline}
Quorum Required: {quorum_pct}% to ban

To vote:
  lightning-cli hive-vote-ban {peer_id} {proposal_id} approve="true|false"

NOTE: Non-votes count as implicit approval after deadline.

— Hive Governance System
```

#### 3.2 Ban Vote Cast
**Trigger:** Ban vote received
**Recipients:** Proposer + accused
**Template:**
```
Subject: [HIVE] Ban Vote Update: {alias}

A vote has been cast on the ban proposal.

Voter: {voter_alias}
Vote: {approve_ban/reject_ban}
Current Tally: {approve_count} ban / {reject_count} keep
Rejection Threshold: {threshold} (to prevent ban)

{if ban_prevented}
Ban has been rejected. Member remains in good standing.
{/if}

— Hive Governance System
```

#### 3.3 Ban Executed
**Trigger:** Ban quorum reached
**Recipients:** All members + banned member
**Template:**
```
Subject: [HIVE] 🚫 Member Banned: {alias}

A member has been banned from the hive.

Banned: {peer_id} ({alias})
Reason: {reason}
Effective: {timestamp}
Duration: {permanent/until_date}

Final Vote: {approve_count} ban / {reject_count} keep
Implicit approvals: {implicit_count}

Actions taken:
- Removed from member list
- Settlement distributions suspended
- Peer ID added to ban list

{if channels_remain}
Note: {channel_count} channels remain open. Consider closing.
{/if}

— Hive Governance System
```

---

### 4. Settlement Governance

#### 4.1 Settlement Cycle Starting
**Trigger:** `settlement_loop()` initiates new cycle
**Recipients:** All members
**Template:**
```
Subject: [HIVE] Settlement Cycle {period} Starting

A new settlement cycle is beginning.

Period: {period_id}
Start: {start_timestamp}
End: {end_timestamp}

Current Pool:
- Total Revenue: {total_revenue_sats} sats
- Eligible Members: {member_count}
- Your Contribution: {your_contribution_pct}%

Ensure your BOLT12 offer is registered:
  lightning-cli hive-register-settlement-offer {your_bolt12}

— Hive Governance System
```

#### 4.2 Settlement Ready to Execute
**Trigger:** All members confirmed ready
**Recipients:** All participating members
**Template:**
```
Subject: [HIVE] Settlement {period} Ready for Execution

Settlement is ready to execute.

Period: {period_id}
Total Pool: {total_sats} sats

Distribution Preview:
{for each member}
  {alias}: {amount_sats} sats ({contribution_pct}%)
{/for}

Execution will begin in {countdown}.
Payments via BOLT12 offers.

— Hive Governance System
```

#### 4.3 Settlement Complete
**Trigger:** `handle_settlement_executed()`
**Recipients:** All participating members
**Template:**
```
Subject: [HIVE] ✅ Settlement {period} Complete

Settlement has been executed successfully.

Period: {period_id}
Total Distributed: {total_sats} sats

Your Receipt:
- Amount Received: {your_amount_sats} sats
- Contribution Score: {your_score}
- Payment Hash: {payment_hash}

Full Distribution:
{for each member}
  {alias}: {amount_sats} sats ✓
{/for}

This message serves as a cryptographic receipt.

— Hive Governance System
Signed: {settlement_coordinator_did}
```

#### 4.4 Settlement Gaming Detected
**Trigger:** `_check_settlement_gaming_and_propose_bans()`
**Recipients:** All members + accused
**Template:**
```
Subject: [HIVE] ⚠️ Settlement Gaming Detected

Potential settlement gaming has been detected.

Accused: {peer_id} ({alias})
Violation: {violation_type}

Evidence:
- Metric: {metric_name}
- Your Value: {member_value}
- Fleet Median: {median_value}
- Z-Score: {z_score} (threshold: {threshold})

{if auto_ban_proposed}
A ban proposal has been automatically created.
Proposal ID: {proposal_id}
{/if}

— Hive Governance System
```

---

### 5. Health & Alerts

#### 5.1 Member Health Critical
**Trigger:** NNLB health score < threshold
**Recipients:** Affected member + fleet coordinator
**Template:**
```
Subject: [HIVE] 🔴 Health Critical: {alias} ({health_score}/100)

Your node health has dropped to critical levels.

Node: {peer_id} ({alias})
Health Score: {health_score}/100
Tier: {health_tier}  # critical, struggling, stable, thriving

Issues Detected:
{for each issue}
  - {issue_description}
{/for}

Recommended Actions:
1. {recommendation_1}
2. {recommendation_2}
3. {recommendation_3}

The fleet may offer assistance via NNLB rebalancing.
Contact {coordinator_alias} if you need help.

— Hive Health Monitor
```

#### 5.2 Fleet-Wide Alert
**Trigger:** Admin or automated detection
**Recipients:** All members
**Template:**
```
Subject: [HIVE] 📢 Fleet Alert: {alert_title}

An important alert for all fleet members.

Alert Type: {alert_type}
Severity: {severity}
Time: {timestamp}

Details:
{alert_body}

Required Action: {action_required}
Deadline: {deadline}

— Hive Governance System
```

---

### 6. Channel Coordination

#### 6.1 Channel Open Suggestion
**Trigger:** Expansion recommendations or MCF optimization
**Recipients:** Specific member
**Template:**
```
Subject: [HIVE] Channel Suggestion: Open to {target_alias}

The fleet coordinator suggests opening a channel.

Target: {target_peer_id} ({target_alias})
Suggested Size: {size_sats} sats
Reason: {reason}

Benefits:
- {benefit_1}
- {benefit_2}

To proceed:
  lightning-cli fundchannel {target_peer_id} {size_sats}

This is a suggestion, not a requirement.

— Fleet Coordinator
```

#### 6.2 Channel Close Recommendation
**Trigger:** Rationalization analysis
**Recipients:** Channel owner
**Template:**
```
Subject: [HIVE] Channel Review: Consider Closing {channel_id}

A channel has been flagged for potential closure.

Channel: {short_channel_id}
Peer: {peer_alias}
Reason: {reason}

Analysis:
- Age: {age_days} days
- Your Routing Activity: {your_routing_pct}%
- Owner's Routing Activity: {owner_routing_pct}%
- Recommendation: {close/keep/monitor}

{if close_recommended}
This peer is better served by {owner_alias} who routes {owner_pct}% of traffic.
Closing would free {capacity_sats} sats for better positioning.
{/if}

— Fleet Rationalization System
```

#### 6.3 Splice Coordination
**Trigger:** `hive-splice` initiated
**Recipients:** Splice counterparty
**Template:**
```
Subject: [HIVE] Splice Request: {channel_id}

A splice operation has been proposed for your channel.

Channel: {short_channel_id}
Initiator: {initiator_alias}
Operation: {add/remove} {amount_sats} sats

Current State:
- Capacity: {current_capacity} sats
- Your Balance: {your_balance} sats

Proposed State:
- New Capacity: {new_capacity} sats
- Your New Balance: {new_balance} sats

To accept:
  lightning-cli hive-splice-accept {splice_id}

To reject:
  lightning-cli hive-splice-reject {splice_id}

Expires: {expiry_timestamp}

— Hive Splice Coordinator
```

---

### 7. Positioning & Strategy

#### 7.1 Positioning Proposal
**Trigger:** Physarum/positioning analysis
**Recipients:** Relevant members
**Template:**
```
Subject: [HIVE] Positioning Proposal: {corridor_name}

A strategic positioning opportunity has been identified.

Corridor: {source} → {destination}
Value Score: {corridor_score}
Current Coverage: {coverage_pct}%

Proposal:
{proposal_details}

Assigned Member: {assigned_alias}
Reason: {assignment_reason}

Expected Impact:
- Revenue Increase: ~{revenue_estimate} sats/month
- Network Position: {position_improvement}

— Fleet Strategist
```

#### 7.2 MCF Assignment
**Trigger:** MCF optimizer assigns rebalance task
**Recipients:** Assigned member
**Template:**
```
Subject: [HIVE] MCF Assignment: Rebalance {from_channel} → {to_channel}

You've been assigned a rebalance task by the MCF optimizer.

Assignment ID: {assignment_id}
From Channel: {from_channel} ({from_balance}% local)
To Channel: {to_channel} ({to_balance}% local)
Amount: {amount_sats} sats
Max Fee: {max_fee_sats} sats

Deadline: {deadline}
Priority: {priority}

To claim and execute:
  lightning-cli hive-claim-mcf-assignment {assignment_id}

If you cannot complete this, it will be reassigned.

— MCF Optimizer
```

---

## Database Schema

```sql
-- Member contact registry for Archon messaging
CREATE TABLE member_archon_contacts (
    peer_id TEXT PRIMARY KEY,
    alias TEXT,
    archon_did TEXT,                    -- did:cid:bagaaiera...
    notify_preferences TEXT,            -- JSON: ["health", "governance", "settlement"]
    registered_at INTEGER,
    verified_at INTEGER,                -- When DID ownership was verified
    last_message_at INTEGER
);

-- Outbound message queue
CREATE TABLE archon_message_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_type TEXT NOT NULL,         -- 'promotion_proposed', 'settlement_complete', etc.
    recipient_did TEXT NOT NULL,
    recipient_peer_id TEXT,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    priority TEXT DEFAULT 'normal',     -- 'low', 'normal', 'high', 'critical'
    created_at INTEGER NOT NULL,
    scheduled_for INTEGER,              -- For delayed delivery
    sent_at INTEGER,
    delivery_status TEXT DEFAULT 'pending',  -- 'pending', 'sent', 'failed', 'delivered'
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    message_cid TEXT                    -- IPFS CID after sending
);

-- Inbound message tracking
CREATE TABLE archon_message_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_cid TEXT UNIQUE,
    sender_did TEXT NOT NULL,
    sender_peer_id TEXT,
    subject TEXT,
    body TEXT,
    received_at INTEGER NOT NULL,
    read_at INTEGER,
    message_type TEXT,                  -- Parsed from subject/body
    archived INTEGER DEFAULT 0
);

-- Message templates (customizable)
CREATE TABLE archon_message_templates (
    template_id TEXT PRIMARY KEY,
    subject_template TEXT NOT NULL,
    body_template TEXT NOT NULL,
    variables TEXT,                     -- JSON list of required variables
    updated_at INTEGER
);

CREATE INDEX idx_message_queue_status ON archon_message_queue(delivery_status, created_at);
CREATE INDEX idx_message_inbox_sender ON archon_message_inbox(sender_did, received_at);
```

---

## Implementation Plan

### Phase 1: Core Infrastructure
1. Add `archon` config section to hive config schema
2. Create database tables for contacts, queue, inbox, templates
3. Implement `HiveArchonBridge` class for Keymaster integration
4. Add basic send/receive RPC methods
5. Error handling and retry logic for failed deliveries

### Phase 2: Docker Setup Wizard Integration
1. Add optional Archon DID prompt to `cl-hive-setup.sh` wizard
2. Prompt: "Enable Archon governance messaging? (y/n)"
3. If yes:
   - Check if `npx @didcid/keymaster` is available
   - Prompt for existing DID or create new one
   - Securely store passphrase in Docker secrets or env file
   - Configure gatekeeper URL (public vs local node)
   - Set default notification preferences
4. Generate `archon` config block in node config
5. Document setup in container README

### Phase 3: Contact Registry
1. `hive-register-contact` RPC — Map peer_id → Archon DID
2. `hive-list-contacts` RPC
3. `hive-verify-contact` — Optional challenge-response DID verification
4. Contact import/export (JSON format)
5. Auto-discovery: Parse DID from member metadata if provided

### Phase 4: Message Templates
1. Define all governance message templates (20+ types)
2. Template variable substitution engine (Jinja2-style)
3. Admin template customization via RPC
4. i18n support for multi-language templates (future)

### Phase 5: Event Integration
1. Hook into governance events:
   - Membership: join, leave, promotion, ban
   - Settlement: cycle start, ready, complete, gaming detected
   - Health: NNLB critical alerts
2. Hook into channel coordination:
   - Expansion recommendations
   - Close recommendations
   - Splice requests
3. Configurable `auto_notify` rules per event type
4. Rate limiting to prevent spam

### Phase 6: Inbox & History
1. Periodic inbox polling (configurable interval)
2. `hive-dmail-inbox` RPC for message history
3. Read receipts (optional, via Archon acknowledgment)
4. Message archival and retention policy
5. Search/filter inbox by sender, type, date

### Phase 7: Advisor Integration
1. Advisor can send dmails on behalf of fleet
2. Health alerts trigger auto-dmail to affected operator
3. Settlement receipts auto-sent on completion
4. Configurable escalation: critical alerts → multiple recipients

---

## RPC Methods

```python
# Contact management
hive-register-contact(peer_id, alias, archon_did, notify_preferences)
hive-update-contact(peer_id, ...)
hive-remove-contact(peer_id)
hive-list-contacts()
hive-verify-contact(peer_id)  # Challenge-response DID verification

# Messaging
hive-dmail-send(recipient, subject, body, priority)
hive-dmail-broadcast(tier, subject, body)  # Send to all members of tier
hive-dmail-check()  # Poll for new messages
hive-dmail-inbox(limit, offset, unread_only)
hive-dmail-read(message_id)
hive-dmail-queue-status()

# Templates
hive-dmail-templates()
hive-dmail-template-preview(template_id, variables)
hive-dmail-template-update(template_id, subject, body)
```

---

## Security Considerations

1. **Passphrase handling**: Never log or expose `ARCHON_PASSPHRASE`
2. **DID verification**: Optionally verify member owns claimed DID via challenge
3. **Rate limiting**: Prevent message spam
4. **Encryption**: All dmails are E2E encrypted by Archon
5. **Non-repudiation**: All messages signed by sender DID
6. **Retention policy**: Auto-delete old messages per config
