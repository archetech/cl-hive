# cl-hive Threat Model

This document describes the security assumptions, trust model, and potential attack vectors for the cl-hive plugin.

## Trust Model

### Hive Membership Trust

cl-hive operates under a **mutual trust model** among hive members. This is a fundamental design choice that enables the zero-fee routing and cooperative expansion features.

#### Core Assumptions

1. **Membership is Selective**: Nodes join the hive through an invitation process requiring admin approval
2. **Members Act Honestly**: Members are assumed to not intentionally sabotage the hive
3. **Compromise is Possible**: Individual members may be compromised or turn malicious
4. **Defense in Depth**: Multiple security layers protect against single points of failure

#### Trust Tiers

| Tier | Trust Level | Capabilities |
|------|-------------|--------------|
| Admin | High | Genesis, invite, ban, config changes |
| Member | Medium | Vouch, vote, expansion participation |
| Neophyte | Low | Discounted fees, observation only |
| External | None | Standard fee rates, no hive features |

### Message Authentication

All protocol messages are authenticated at multiple levels:

1. **Transport Layer**: Messages travel over encrypted Lightning Network gossip
2. **Membership Verification**: Sender must be a non-banned hive member
3. **Cryptographic Signatures**: Critical messages (nominations, elections) are signed

## Attack Vectors and Mitigations

### 1. Sybil Attacks

**Threat**: Attacker creates many fake nodes to dominate hive voting/elections.

**Mitigations**:
- Invitation-only membership requires admin approval
- Vouch system requires existing member endorsement
- Probation period (30 days default) before full membership
- `max_members` cap prevents unbounded growth

### 2. Gossip Flooding

**Threat**: Malicious member floods the network with `PEER_AVAILABLE` messages to cause denial of service.

**Mitigations**:
- Rate limiting (10 messages/minute per peer)
- Message validation rejects malformed payloads
- Membership check rejects messages from non-members

### 3. Election Spoofing

**Threat**: Attacker broadcasts fake `EXPANSION_ELECT` messages to manipulate channel opens.

**Mitigations**:
- Cryptographic signatures on all election messages
- Signature verification against claimed coordinator
- Coordinator must be a valid hive member

### 4. Nomination Spoofing

**Threat**: Attacker claims to be another member in nomination messages.

**Mitigations**:
- Cryptographic signatures on all nomination messages
- Signature verification confirms nominator identity
- Nominator pubkey must match signature

### 5. Quality Score Manipulation

**Threat**: Member reports inflated quality scores for certain peers to influence topology decisions.

**Mitigations**:
- Consistency scoring penalizes outliers (15% weight)
- Multiple reporters required for high confidence
- Historical data aggregation smooths manipulation

### 6. Budget Exhaustion

**Threat**: Attacker triggers many expansions to exhaust other members' on-chain funds.

**Mitigations**:
- Budget reserve percentage (default 20%)
- Daily budget cap (default 10M sats)
- Per-channel maximum (50% of daily budget)
- Pending action approval required in advisor mode

### 7. Fee Policy Attacks

**Threat**: Member manipulates fee settings to steal routing revenue.

**Mitigations**:
- Fee policy changes require bridge to cl-revenue-ops
- Hive strategy enforced for member channels
- Changes logged and auditable

### 8. State Desynchronization

**Threat**: Member maintains different state than rest of hive to exploit inconsistencies.

**Mitigations**:
- State hash comparison on heartbeat
- Full sync protocol on mismatch
- Gossip propagation ensures eventual consistency

### 9. Ban Evasion

**Threat**: Banned member rejoins with different identity.

**Mitigations**:
- Ban records stored persistently
- New members require existing member vouch
- Probation period allows observation

### 10. Replay Attacks

**Threat**: Attacker replays old valid messages to cause confusion.

**Mitigations**:
- Timestamps validated (must be recent)
- Round IDs are unique per expansion
- State versioning prevents stale updates

## Security Properties

### Guaranteed

1. **No Fund Loss**: cl-hive never has custody of funds; worst case is wasted on-chain fees
2. **No Unauthorized Channels**: Channel opens require explicit approval in advisor mode
3. **Audit Trail**: All significant actions logged for review
4. **Graceful Degradation**: Plugin failures don't affect core Lightning operation

### Not Guaranteed

1. **Perfect Coordination**: Network partitions may cause duplicate actions
2. **Fair Elections**: Malicious coordinator could bias elections (detectable via logs)
3. **Optimal Topology**: Quality scores can be manipulated within bounds

## Operational Security Recommendations

### For Hive Admins

1. **Vet new members** before issuing invitations
2. **Monitor logs** for unusual patterns
3. **Use advisor mode** until confident in autonomous operation
4. **Set conservative budgets** initially
5. **Review pending actions** regularly

### For Hive Members

1. **Protect node keys** - they sign all hive messages
2. **Keep software updated** for security patches
3. **Monitor channel opens** for unexpected activity
4. **Report suspicious behavior** to admins

### For Developers

1. **Validate all inputs** at protocol boundaries
2. **Use parameterized SQL** for all queries
3. **Sign critical messages** with node keys
4. **Rate limit** incoming messages
5. **Log security events** for forensics

## Incident Response

### Suspected Compromise

1. Ban the suspected member immediately via `hive-ban`
2. Review logs for unauthorized actions
3. Check pending actions queue for suspicious entries
4. Notify other admins via secure channel
5. Consider rotating hive genesis if admin compromised

### Protocol Vulnerability

1. Disable cooperative expansion (`planner_enable_expansions=false`)
2. Switch to advisor mode (`governance_mode=advisor`)
3. Apply patches as available
4. Monitor for exploitation attempts

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-01-13 | Initial threat model |
