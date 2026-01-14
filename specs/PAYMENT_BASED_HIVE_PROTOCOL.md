# Payment-Based Inter-Hive Protocol Specification

**Version:** 0.1.0-draft
**Status:** Proposal
**Authors:** cl-hive contributors
**Date:** 2025-01-14

## Abstract

This specification defines a Lightning payment-based protocol for inter-hive communication, discovery, and trust verification. All coordination uses actual Lightning payments as the transport and verification layer, ensuring that claims about network position, liquidity, and relationships are economically verified rather than trusted.

**Core Principle**: Payments don't lie. Use them to verify everything.

## Table of Contents

1. [Motivation](#1-motivation)
2. [Design Principles](#2-design-principles)
3. [Payment-Based Communication](#3-payment-based-communication)
4. [Hive Discovery Protocol](#4-hive-discovery-protocol)
5. [Hidden Hive Detection](#5-hidden-hive-detection)
6. [Reputation-Gated Messaging](#6-reputation-gated-messaging)
7. [Continuous Verification](#7-continuous-verification)
8. [Economic Security Model](#8-economic-security-model)
9. [Protocol Messages](#9-protocol-messages)
10. [Implementation Guidelines](#10-implementation-guidelines)

---

## 1. Motivation

### 1.1 The Problem with Message-Based Protocols

Traditional protocols rely on signed messages:
- Messages can claim anything ("I have 100 BTC capacity")
- Signatures prove identity, not capability
- No cost to lie (spam, false claims)
- Network position is self-reported

### 1.2 Payments as Proof

Lightning payments inherently prove:
- **Channel existence**: Payment fails if no path
- **Liquidity**: Payment fails if insufficient balance
- **Network position**: Route reveals actual topology
- **Bidirectional capability**: Can send AND receive
- **Economic commitment**: Real sats at stake

### 1.3 Trust Through Verification

Instead of:
```
"Trust me, I'm a friendly hive" → OK, you're trusted
```

We get:
```
"Trust me, I'm a friendly hive" → Prove it with payments → Verified or rejected
```

---

## 2. Design Principles

### 2.1 Payment as Authentication

Every claim must be backed by a payment that proves the claim:

| Claim | Payment Proof |
|-------|---------------|
| "I exist" | Receive my payment |
| "I can reach you" | Send you a payment |
| "I have liquidity" | Send large payment |
| "I'm part of Hive X" | Payment from Hive X admin |
| "I'm not hostile" | Stake payment in escrow |

### 2.2 Continuous Verification

Trust is not a state, it's a continuous stream of verified payments:

```
Initial verification → Periodic re-verification → Every interaction verified
         ↓                      ↓                         ↓
    Stake payment         Heartbeat payments        Message payments
```

### 2.3 Economic Deterrence

Make attacks expensive:
- Every message costs sats
- False claims forfeit stakes
- Reputation requires sustained payment history
- Detection costs less than evasion

### 2.4 Symmetry

If you can query me, I can query you. No asymmetric information advantages.

---

## 3. Payment-Based Communication

### 3.1 Message Payment Structure

All inter-hive messages are sent via keysend with custom TLV:

```
┌─────────────────────────────────────────────────────────────┐
│                    HIVE MESSAGE PAYMENT                      │
├─────────────────────────────────────────────────────────────┤
│  Amount: message_fee + optional_stake                        │
│                                                              │
│  TLV Records:                                                │
│    5482373484 (keysend preimage)                            │
│    48495645 ("HIVE" magic):                                  │
│      {                                                       │
│        "protocol": "hive_inter",                            │
│        "version": 1,                                         │
│        "msg_type": "query_hive_status",                     │
│        "payload": {...},                                     │
│        "reply_invoice": "lnbc...",                          │
│        "stake_hash": "abc123...",                           │
│        "sender_hive": "hive_xyz" | null                     │
│      }                                                       │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 Message Fee Schedule

| Message Type | Base Fee | Stake Required | Reply Expected |
|--------------|----------|----------------|----------------|
| ping | 10 sats | No | Yes (pong) |
| query_hive_status | 100 sats | No | Yes |
| hive_introduction | 1,000 sats | 10,000 sats | Yes |
| federation_request | 10,000 sats | 100,000 sats | Yes |
| intel_share | 500 sats | No | Optional |
| defense_alert | 0 sats | 50,000 sats | Yes |
| reputation_query | 100 sats | No | Yes |

### 3.3 Reply Mechanism

Messages include a `reply_invoice` for responses:

```python
def send_hive_message(self, target: str, msg_type: str, payload: dict) -> str:
    """Send payment-based hive message."""

    # Create invoice for reply
    reply_invoice = self.create_invoice(
        amount_msat=MESSAGE_FEES[msg_type] * 1000,
        description=f"hive_reply:{msg_type}",
        expiry=3600
    )

    # Calculate total amount
    amount = MESSAGE_FEES[msg_type]
    if msg_type in STAKE_REQUIRED:
        amount += STAKE_REQUIRED[msg_type]

    # Build TLV payload
    tlv_payload = {
        "protocol": "hive_inter",
        "version": 1,
        "msg_type": msg_type,
        "payload": payload,
        "reply_invoice": reply_invoice,
        "stake_hash": self.create_stake_hash() if msg_type in STAKE_REQUIRED else None,
        "sender_hive": self.our_hive_id
    }

    # Send keysend with TLV
    result = self.keysend(
        destination=target,
        amount_msat=amount * 1000,
        tlv_records={
            5482373484: os.urandom(32),  # keysend preimage
            48495645: json.dumps(tlv_payload).encode()
        }
    )

    return reply_invoice
```

### 3.4 Payment Verification

Every received message is verified:

```python
def verify_message_payment(self, payment: Payment) -> MessageVerification:
    """Verify incoming hive message payment."""

    # Extract TLV
    hive_tlv = payment.tlv_records.get(48495645)
    if not hive_tlv:
        return MessageVerification(valid=False, reason="no_hive_tlv")

    try:
        msg = json.loads(hive_tlv)
    except:
        return MessageVerification(valid=False, reason="invalid_json")

    # Verify protocol
    if msg.get("protocol") != "hive_inter":
        return MessageVerification(valid=False, reason="wrong_protocol")

    # Verify payment amount covers fee
    required_fee = MESSAGE_FEES.get(msg["msg_type"], 0)
    required_stake = STAKE_REQUIRED.get(msg["msg_type"], 0)

    if payment.amount_msat < (required_fee + required_stake) * 1000:
        return MessageVerification(valid=False, reason="insufficient_payment")

    # Verify sender can be reached (they included reply invoice)
    if msg.get("reply_invoice"):
        # Decode and verify invoice is from sender
        invoice = self.decode_invoice(msg["reply_invoice"])
        # Route hint should lead back to sender

    return MessageVerification(
        valid=True,
        msg_type=msg["msg_type"],
        payload=msg["payload"],
        sender=payment.sender,
        sender_hive=msg.get("sender_hive"),
        stake_amount=required_stake,
        reply_invoice=msg.get("reply_invoice")
    )
```

---

## 4. Hive Discovery Protocol

### 4.1 Direct Query: "Are You A Hive?"

Any node can query any other node:

```
┌─────────┐                              ┌─────────┐
│ Node A  │                              │ Node B  │
└────┬────┘                              └────┬────┘
     │                                        │
     │  Payment: 100 sats                     │
     │  TLV: query_hive_status                │
     │  reply_invoice: lnbc100n...            │
     │ ─────────────────────────────────────► │
     │                                        │
     │         Payment: 100 sats              │
     │         TLV: hive_status_response      │
     │ ◄───────────────────────────────────── │
     │                                        │
```

**Query Message:**
```json
{
  "msg_type": "query_hive_status",
  "payload": {
    "query_id": "q_abc123",
    "include_members": false,
    "include_federation": false
  }
}
```

**Response Options:**

1. **"Yes, I'm in a hive":**
```json
{
  "msg_type": "hive_status_response",
  "payload": {
    "query_id": "q_abc123",
    "is_hive_member": true,
    "hive_id": "hive_xyz789",
    "member_tier": "member",
    "hive_public": true,
    "verification_offer": {
      "type": "admin_voucher",
      "admin_node": "03admin...",
      "voucher_payment": 1000
    }
  }
}
```

2. **"No, I'm independent":**
```json
{
  "msg_type": "hive_status_response",
  "payload": {
    "query_id": "q_abc123",
    "is_hive_member": false,
    "open_to_joining": true,
    "requirements": ["min_capacity_10m", "min_channels_5"]
  }
}
```

3. **"None of your business"** (valid response):
```json
{
  "msg_type": "hive_status_response",
  "payload": {
    "query_id": "q_abc123",
    "declined": true,
    "reason": "private"
  }
}
```

### 4.2 Hive Membership Verification

Claims of hive membership must be verified:

```
┌─────────┐         ┌─────────┐         ┌─────────────┐
│ Querier │         │ Claimer │         │ Hive Admin  │
└────┬────┘         └────┬────┘         └──────┬──────┘
     │                   │                      │
     │  "Are you in      │                      │
     │   hive_xyz?"      │                      │
     │ ─────────────────►│                      │
     │                   │                      │
     │  "Yes, verify     │                      │
     │   with admin"     │                      │
     │ ◄─────────────────│                      │
     │                   │                      │
     │  Payment: 1000 sats                      │
     │  "Is 03claimer... in your hive?"        │
     │ ────────────────────────────────────────►│
     │                   │                      │
     │  Payment: 1000 sats                      │
     │  "Yes, member since <date>,             │
     │   tier: member, voucher: <sig>"         │
     │ ◄────────────────────────────────────────│
     │                   │                      │
```

**Admin Voucher:**
```json
{
  "msg_type": "membership_voucher",
  "payload": {
    "hive_id": "hive_xyz789",
    "member_node": "03claimer...",
    "member_since": 1700000000,
    "member_tier": "member",
    "voucher_expires": 1705234567,
    "voucher_signature": "admin_sig_of_above_fields"
  }
}
```

### 4.3 Hive Introduction Protocol

When hives want to establish contact:

```python
class HiveIntroduction:
    """Protocol for hive-to-hive introduction."""

    def initiate_introduction(self, target_hive_admin: str) -> IntroductionResult:
        """Initiate introduction to another hive."""

        # Step 1: Send introduction with stake
        intro_payment = self.send_hive_message(
            target=target_hive_admin,
            msg_type="hive_introduction",
            payload={
                "our_hive_id": self.hive_id,
                "our_admin_nodes": self.get_admin_nodes(),
                "our_member_count": self.get_member_count(),
                "our_capacity_tier": self.get_capacity_tier(),
                "introduction_stake": 10000,  # sats locked
                "proposed_relationship": "observer",
                "our_public_reputation": self.get_public_reputation()
            }
        )

        # Stake is locked until:
        # - They respond positively (stake returned)
        # - They respond negatively (stake returned minus fee)
        # - Timeout (stake returned)
        # - We misbehave (stake forfeited)

        return self.await_introduction_response(intro_payment)

    def handle_introduction(self, msg: HiveMessage) -> IntroductionResponse:
        """Handle incoming hive introduction."""

        # Verify stake was included
        if msg.stake_amount < 10000:
            return self.reject_introduction("insufficient_stake")

        # Verify their claims with payment probes
        verification = self.verify_hive_claims(msg.payload)

        if not verification.passed:
            # Return stake minus verification fee
            self.return_stake(msg, deduct=1000)
            return self.reject_introduction(verification.reason)

        # Check our policy toward unknown hives
        if not self.accept_new_introductions():
            self.return_stake(msg, deduct=0)
            return self.reject_introduction("not_accepting")

        # Accept introduction, return stake, begin observation
        self.return_stake(msg, deduct=0)
        self.create_hive_relationship(
            hive_id=msg.payload["our_hive_id"],
            status="observing",
            introduced_at=time.time()
        )

        return self.accept_introduction()
```

---

## 5. Hidden Hive Detection

### 5.1 The Challenge

Sophisticated hives may hide their coordination:
- Use non-zero internal fees (1-5 ppm)
- Stagger actions over days
- Avoid naming patterns
- Use diverse external peers

### 5.2 Payment-Based Probing

**Payments reveal what messages cannot:**

```python
class HiddenHiveDetector:
    """Detect hidden hives through payment probing."""

    def probe_suspected_cluster(self, nodes: List[str]) -> ClusterAnalysis:
        """Probe suspected hive cluster with payments."""

        results = {
            "internal_routing": {},
            "fee_consistency": {},
            "liquidity_correlation": {},
            "response_timing": {},
        }

        # Probe 1: Internal routing costs
        for src in nodes:
            for dst in nodes:
                if src != dst:
                    probe = self.probe_route(src, dst, amount=10000)
                    results["internal_routing"][(src, dst)] = {
                        "success": probe.success,
                        "fee_paid": probe.fee_msat,
                        "hops": probe.hop_count,
                        "time_ms": probe.time_ms
                    }

        # Probe 2: External routing comparison
        external_nodes = self.get_random_external_nodes(10)
        for src in nodes:
            for ext in external_nodes:
                probe = self.probe_route(src, ext, amount=10000)
                results["fee_consistency"][src] = self.compare_fees(
                    internal=results["internal_routing"],
                    external=probe
                )

        # Analysis
        return self.analyze_cluster_probes(results)

    def analyze_cluster_probes(self, results: dict) -> ClusterAnalysis:
        """Analyze probe results for hive indicators."""

        indicators = {}

        # Indicator 1: Internal fees significantly lower than external
        internal_fees = [r["fee_paid"] for r in results["internal_routing"].values() if r["success"]]
        external_fees = [r["fee_paid"] for r in results["fee_consistency"].values() if r.get("success")]

        if internal_fees and external_fees:
            fee_ratio = np.mean(internal_fees) / np.mean(external_fees)
            indicators["fee_ratio"] = fee_ratio
            indicators["fee_suspicious"] = fee_ratio < 0.3  # Internal 70%+ cheaper

        # Indicator 2: Unusually high internal routing success
        internal_success = sum(1 for r in results["internal_routing"].values() if r["success"])
        internal_total = len(results["internal_routing"])

        indicators["internal_success_rate"] = internal_success / internal_total if internal_total else 0
        indicators["success_suspicious"] = indicators["internal_success_rate"] > 0.95

        # Indicator 3: Correlated liquidity (simultaneous balance changes)
        # This requires multiple probes over time
        indicators["liquidity_correlation"] = self.calculate_liquidity_correlation(results)

        # Indicator 4: Response timing patterns
        # Hive nodes may have similar latency patterns
        indicators["timing_correlation"] = self.calculate_timing_correlation(results)

        # Overall assessment
        suspicion_score = sum([
            0.4 if indicators.get("fee_suspicious") else 0,
            0.3 if indicators.get("success_suspicious") else 0,
            0.2 if indicators.get("liquidity_correlation", 0) > 0.7 else 0,
            0.1 if indicators.get("timing_correlation", 0) > 0.8 else 0,
        ])

        return ClusterAnalysis(
            nodes=nodes,
            indicators=indicators,
            suspicion_score=suspicion_score,
            likely_hive=suspicion_score > 0.6,
            confidence=min(suspicion_score * 1.5, 1.0)
        )
```

### 5.3 Liquidity Correlation Analysis

Hidden hives often have correlated liquidity (rebalancing together):

```python
def detect_liquidity_correlation(self, nodes: List[str], days: int = 7) -> float:
    """Detect correlated liquidity changes through repeated probing."""

    # Probe each node periodically
    probe_schedule = []
    for _ in range(days * 24):  # Hourly probes
        for node in nodes:
            probe_schedule.append((time.time() + random.uniform(0, 3600), node))

    # Execute probes and record available liquidity
    liquidity_series = {node: [] for node in nodes}

    for probe_time, node in sorted(probe_schedule):
        time.sleep(max(0, probe_time - time.time()))

        # Probe max sendable to node
        max_sendable = self.probe_max_sendable(node)
        liquidity_series[node].append((probe_time, max_sendable))

    # Calculate pairwise correlation
    correlations = []
    for n1, n2 in combinations(nodes, 2):
        series1 = [v for _, v in liquidity_series[n1]]
        series2 = [v for _, v in liquidity_series[n2]]

        corr = np.corrcoef(series1, series2)[0, 1]
        correlations.append(corr)

    # High average correlation suggests coordinated liquidity management
    return np.mean(correlations) if correlations else 0.0
```

### 5.4 Fee Response Correlation

Probe how nodes respond to fee changes:

```python
def detect_fee_correlation(self, nodes: List[str]) -> float:
    """Detect if nodes change fees in correlation."""

    # Monitor fee changes over time
    fee_history = {node: [] for node in nodes}

    # Record initial fees
    for node in nodes:
        channels = self.get_node_channels(node)
        for chan in channels:
            fee_history[node].append({
                "time": time.time(),
                "channel": chan.scid,
                "fee_ppm": chan.fee_ppm
            })

    # Monitor for changes over 7 days
    # (In practice, subscribe to gossip updates)

    # Analyze: do fee changes cluster in time?
    all_changes = []
    for node, history in fee_history.items():
        for i in range(1, len(history)):
            if history[i]["fee_ppm"] != history[i-1]["fee_ppm"]:
                all_changes.append({
                    "node": node,
                    "time": history[i]["time"],
                    "change": history[i]["fee_ppm"] - history[i-1]["fee_ppm"]
                })

    # Calculate temporal clustering
    return self.calculate_temporal_clustering(all_changes)
```

### 5.5 Active Unmasking

If we suspect a hidden hive, we can try to unmask it:

```python
def attempt_unmask(self, suspected_nodes: List[str]) -> UnmaskResult:
    """Attempt to unmask a suspected hidden hive."""

    unmask_techniques = [
        self.probe_internal_routing,      # See if they have preferential internal routing
        self.stress_test_liquidity,       # See if one node's stress affects others
        self.fee_pressure_test,           # Raise fees and see if they coordinate response
        self.direct_query_all,            # Just ask each node directly
    ]

    evidence = []

    for technique in unmask_techniques:
        result = technique(suspected_nodes)
        if result.reveals_coordination:
            evidence.append(result)

    if len(evidence) >= 2:
        return UnmaskResult(
            unmasked=True,
            confidence=min(0.5 + len(evidence) * 0.15, 0.95),
            evidence=evidence,
            recommended_action="classify_as_hidden_hive"
        )

    return UnmaskResult(
        unmasked=False,
        confidence=0.3,
        evidence=evidence,
        recommended_action="continue_monitoring"
    )
```

---

## 6. Reputation-Gated Messaging

### 6.1 Core Principle

**No reputation = No communication (or very expensive communication)**

```python
class ReputationGate:
    """Gate all inter-hive communication by reputation."""

    # Fee multipliers by reputation tier
    FEE_MULTIPLIERS = {
        "unknown": 10.0,      # 10x fees for unknown senders
        "observed": 5.0,      # 5x for observed
        "neutral": 2.0,       # 2x for neutral
        "cooperative": 1.0,   # Standard for cooperative
        "federated": 0.5,     # Discount for federated
        "hostile": float('inf'),  # Blocked
        "parasitic": float('inf'),  # Blocked
    }

    def calculate_message_fee(
        self,
        sender: str,
        msg_type: str
    ) -> int:
        """Calculate fee for sender to send message type."""

        base_fee = MESSAGE_FEES[msg_type]

        # Get sender's hive and reputation
        sender_hive = self.get_hive_for_node(sender)

        if sender_hive is None:
            # Unknown independent node
            multiplier = self.FEE_MULTIPLIERS["unknown"]
        else:
            classification = sender_hive.classification
            multiplier = self.FEE_MULTIPLIERS.get(classification, 10.0)

        if multiplier == float('inf'):
            return -1  # Blocked, no fee will work

        return int(base_fee * multiplier)

    def should_accept_message(
        self,
        payment: Payment,
        msg: HiveMessage
    ) -> Tuple[bool, str]:
        """Determine if message should be accepted."""

        required_fee = self.calculate_message_fee(
            sender=payment.sender,
            msg_type=msg.msg_type
        )

        if required_fee == -1:
            return False, "sender_blocked"

        if payment.amount_msat < required_fee * 1000:
            return False, f"insufficient_fee_for_reputation"

        return True, "accepted"
```

### 6.2 Reputation Earning Through Payments

Reputation is earned through successful payment interactions:

```python
class PaymentReputation:
    """Build reputation through payment history."""

    def record_payment_interaction(
        self,
        counterparty: str,
        direction: str,  # "sent" or "received"
        amount_sats: int,
        success: bool,
        context: str  # "routing", "direct", "hive_message"
    ):
        """Record a payment interaction for reputation."""

        self.db.execute("""
            INSERT INTO payment_interactions
            (counterparty, direction, amount_sats, success, context, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (counterparty, direction, amount_sats, success, context, time.time()))

        # Update reputation score
        self.update_reputation(counterparty)

    def calculate_payment_reputation(self, node: str) -> PaymentReputationScore:
        """Calculate reputation from payment history."""

        interactions = self.get_interactions(node, days=90)

        if len(interactions) < 10:
            return PaymentReputationScore(
                score=0.0,
                confidence=0.1,
                reason="insufficient_history"
            )

        # Metrics
        total_volume = sum(i.amount_sats for i in interactions)
        success_rate = sum(1 for i in interactions if i.success) / len(interactions)

        # Directional balance (should be roughly equal)
        sent = sum(i.amount_sats for i in interactions if i.direction == "sent")
        received = sum(i.amount_sats for i in interactions if i.direction == "received")
        balance_ratio = min(sent, received) / max(sent, received, 1)

        # Consistency (regular interactions better than sporadic)
        consistency = self.calculate_interaction_consistency(interactions)

        # Calculate score
        score = (
            0.3 * success_rate +
            0.3 * min(total_volume / 10_000_000, 1.0) +  # Cap at 10M sats
            0.2 * balance_ratio +
            0.2 * consistency
        )

        confidence = min(len(interactions) / 100, 1.0)

        return PaymentReputationScore(
            score=score,
            confidence=confidence,
            total_volume=total_volume,
            success_rate=success_rate,
            balance_ratio=balance_ratio,
            interaction_count=len(interactions)
        )
```

### 6.3 Reputation Verification Challenges

Periodically challenge counterparties to verify reputation:

```python
class ReputationChallenge:
    """Challenge counterparties to verify their reputation."""

    def issue_challenge(self, target: str, stake: int = 10000) -> Challenge:
        """Issue a reputation verification challenge."""

        # Create a challenge that requires them to:
        # 1. Receive a payment from us
        # 2. Send a payment back within time limit
        # 3. Route a payment for us

        challenge = Challenge(
            challenge_id=generate_id(),
            target=target,
            stake=stake,
            created_at=time.time(),
            expires_at=time.time() + 3600,  # 1 hour
            tasks=[
                {"type": "receive", "amount": 1000, "status": "pending"},
                {"type": "send_back", "amount": 900, "status": "pending"},
                {"type": "route", "amount": 5000, "status": "pending"},
            ]
        )

        # Send initial challenge payment
        self.send_challenge_payment(target, challenge)

        return challenge

    def verify_challenge_completion(self, challenge: Challenge) -> ChallengeResult:
        """Verify if challenge was completed."""

        completed_tasks = sum(1 for t in challenge.tasks if t["status"] == "completed")
        total_tasks = len(challenge.tasks)

        if completed_tasks == total_tasks:
            # Full completion - reputation boost
            return ChallengeResult(
                passed=True,
                reputation_delta=0.1,
                stake_returned=True
            )
        elif completed_tasks > 0:
            # Partial completion
            return ChallengeResult(
                passed=False,
                reputation_delta=-0.05,
                stake_returned=True,
                note="partial_completion"
            )
        else:
            # No completion - forfeit stake
            return ChallengeResult(
                passed=False,
                reputation_delta=-0.2,
                stake_returned=False,
                note="challenge_failed"
            )
```

---

## 7. Continuous Verification

### 7.1 Trust Decay Without Verification

Even federated hives must continuously prove trustworthiness:

```python
class ContinuousVerification:
    """Continuously verify all hive relationships."""

    # Required verification frequency by relationship level
    VERIFICATION_INTERVALS = {
        "unknown": 3600,        # Every hour
        "observed": 14400,      # Every 4 hours
        "neutral": 86400,       # Daily
        "cooperative": 259200,  # Every 3 days
        "federated": 604800,    # Weekly
    }

    def run_verification_loop(self):
        """Continuous verification loop."""

        while not self.shutdown_event.is_set():
            for hive in self.get_all_known_hives():
                interval = self.VERIFICATION_INTERVALS.get(
                    hive.classification, 3600
                )

                if time.time() - hive.last_verified > interval:
                    self.verify_hive(hive)

            self.shutdown_event.wait(60)  # Check every minute

    def verify_hive(self, hive: DetectedHive) -> VerificationResult:
        """Verify a hive is still trustworthy."""

        verifications = []

        # 1. Verify members are still reachable via payment
        for member in hive.members[:5]:  # Sample 5 members
            probe = self.send_verification_payment(member, amount=100)
            verifications.append({
                "type": "reachability",
                "node": member,
                "passed": probe.success
            })

        # 2. Verify behavior hasn't changed
        recent_behavior = self.analyze_recent_behavior(hive.hive_id, days=7)
        verifications.append({
            "type": "behavior",
            "passed": recent_behavior.consistent_with_classification
        })

        # 3. Verify economic relationship is balanced
        economic = self.analyze_economic_relationship(hive.hive_id)
        verifications.append({
            "type": "economic",
            "passed": economic.is_balanced
        })

        # 4. For federated: verify they're honoring agreements
        if hive.classification == "federated":
            federation = self.get_federation(hive.hive_id)
            compliance = self.verify_federation_compliance(federation)
            verifications.append({
                "type": "federation_compliance",
                "passed": compliance.is_compliant
            })

        # Calculate result
        passed_count = sum(1 for v in verifications if v["passed"])
        total_count = len(verifications)

        if passed_count == total_count:
            status = "verified"
            action = "maintain_classification"
        elif passed_count >= total_count * 0.7:
            status = "partial"
            action = "increase_monitoring"
        else:
            status = "failed"
            action = "downgrade_classification"

        # Update verification timestamp
        self.update_hive_verification(hive.hive_id, time.time(), status)

        return VerificationResult(
            hive_id=hive.hive_id,
            verifications=verifications,
            status=status,
            action=action
        )
```

### 7.2 Federation Heartbeat Payments

Federated hives exchange regular heartbeat payments:

```python
class FederationHeartbeat:
    """Exchange heartbeat payments with federated hives."""

    HEARTBEAT_AMOUNT = 1000  # sats
    HEARTBEAT_INTERVAL = 86400  # Daily

    def send_heartbeat(self, federation_id: str) -> HeartbeatResult:
        """Send heartbeat payment to federated hive."""

        federation = self.get_federation(federation_id)
        their_admin = federation.their_admin_node

        # Include current status in heartbeat
        heartbeat_payload = {
            "heartbeat_id": generate_id(),
            "our_status": {
                "member_count": self.get_member_count(),
                "health": self.get_health_summary(),
                "active_alerts": self.get_active_alert_count()
            },
            "federation_status": {
                "our_compliance": True,
                "issues_detected": [],
                "next_review": federation.next_review_timestamp
            }
        }

        # Send heartbeat as payment with TLV
        result = self.send_hive_message(
            target=their_admin,
            msg_type="federation_heartbeat",
            payload=heartbeat_payload
        )

        if result.success:
            self.record_heartbeat_sent(federation_id)
        else:
            self.record_heartbeat_failure(federation_id, result.error)

            # Multiple failures = verification concern
            failures = self.count_recent_heartbeat_failures(federation_id)
            if failures >= 3:
                self.flag_federation_for_review(federation_id)

        return result

    def handle_heartbeat(self, msg: HiveMessage) -> HeartbeatResponse:
        """Handle incoming heartbeat from federated hive."""

        federation = self.get_federation_by_sender(msg.sender)

        if federation is None:
            return HeartbeatResponse(
                accepted=False,
                reason="not_federated"
            )

        # Verify heartbeat payment was sufficient
        if msg.payment_amount < self.HEARTBEAT_AMOUNT:
            return HeartbeatResponse(
                accepted=False,
                reason="insufficient_heartbeat_payment"
            )

        # Record received heartbeat
        self.record_heartbeat_received(federation.federation_id, msg.payload)

        # Send response heartbeat
        self.schedule_heartbeat_response(federation.federation_id)

        return HeartbeatResponse(
            accepted=True,
            our_status=self.get_status_summary()
        )
```

### 7.3 Verification Failure Consequences

```python
def handle_verification_failure(
    self,
    hive_id: str,
    failure_type: str,
    severity: str
) -> List[str]:
    """Handle verification failure."""

    actions = []
    hive = self.get_hive(hive_id)

    if severity == "critical":
        # Immediate downgrade
        if hive.classification == "federated":
            self.suspend_federation(hive_id)
            self.reclassify_hive(hive_id, "observed")
            actions.append("federation_suspended")
            actions.append("downgraded_to_observed")
        else:
            new_class = self.downgrade_classification(hive.classification)
            self.reclassify_hive(hive_id, new_class)
            actions.append(f"downgraded_to_{new_class}")

    elif severity == "warning":
        # Increase monitoring, potential downgrade
        self.increase_monitoring(hive_id)
        self.record_warning(hive_id, failure_type)
        actions.append("increased_monitoring")

        # Check for pattern of warnings
        warnings = self.count_recent_warnings(hive_id, days=30)
        if warnings >= 3:
            self.schedule_classification_review(hive_id)
            actions.append("review_scheduled")

    # Notify federated hives of verification failure
    if hive.classification in ["cooperative", "federated"]:
        self.notify_federates_of_issue(hive_id, failure_type, severity)
        actions.append("federates_notified")

    return actions
```

---

## 8. Economic Security Model

### 8.1 Attack Cost Analysis

| Attack | Without Payment Protocol | With Payment Protocol |
|--------|-------------------------|----------------------|
| Fake hive creation | Free | Cost of real channels + liquidity |
| False hive membership claim | Free | Must receive voucher payment from admin |
| Federation request spam | Free | 10,000 sats + 100,000 stake per request |
| Hidden hive operation | Free | Detectable via payment probing |
| Reputation fraud | Easy | Requires sustained payment history |
| Intelligence gathering | Free | Must pay for every query |
| Long con infiltration | Time only | Time + significant locked capital |

### 8.2 Stake Requirements

```python
STAKE_SCHEDULE = {
    # Relationship establishment
    "hive_introduction": 10_000,           # 10k sats
    "federation_request_level_1": 100_000,  # 100k sats
    "federation_request_level_2": 1_000_000,  # 1M sats
    "federation_request_level_3": 10_000_000,  # 10M sats
    "federation_request_level_4": 50_000_000,  # 50M sats

    # Message stakes (for high-trust messages)
    "defense_alert": 50_000,               # Must have skin in game for alerts
    "intel_share_high_value": 100_000,     # Stake behind valuable intel

    # Verification stakes
    "reputation_challenge": 10_000,         # Challenge stake
    "membership_voucher_request": 5_000,    # Verify membership
}

STAKE_VESTING = {
    # How long until stake is returned
    "federation_level_1": 180,   # 6 months
    "federation_level_2": 365,   # 1 year
    "federation_level_3": 730,   # 2 years
    "federation_level_4": 1095,  # 3 years
}

STAKE_FORFEIT_TRIGGERS = [
    "hostile_action_detected",
    "federation_terms_violation",
    "false_intel_provided",
    "false_membership_claim",
    "false_defense_alert",
    "verification_fraud",
]
```

### 8.3 Payment Flow Tracking

Track all payment flows for economic analysis:

```sql
CREATE TABLE hive_payment_flows (
    id INTEGER PRIMARY KEY,
    counterparty_node TEXT NOT NULL,
    counterparty_hive TEXT,
    direction TEXT NOT NULL,          -- 'inbound', 'outbound'
    amount_sats INTEGER NOT NULL,
    fee_paid_sats INTEGER,
    purpose TEXT NOT NULL,            -- 'routing', 'message', 'stake', 'heartbeat'
    success BOOLEAN NOT NULL,
    timestamp INTEGER NOT NULL,

    -- For routing payments
    was_routing BOOLEAN DEFAULT FALSE,
    route_source TEXT,
    route_destination TEXT,

    -- For hive messages
    message_type TEXT,
    message_id TEXT
);

CREATE INDEX idx_payment_flows_counterparty ON hive_payment_flows(counterparty_node, timestamp);
CREATE INDEX idx_payment_flows_hive ON hive_payment_flows(counterparty_hive, timestamp);
```

### 8.4 Economic Anomaly Detection

```python
class EconomicAnomalyDetector:
    """Detect economic anomalies in hive relationships."""

    def detect_anomalies(self, hive_id: str) -> List[EconomicAnomaly]:
        """Detect economic anomalies with a hive."""

        anomalies = []
        flows = self.get_payment_flows(hive_id, days=30)

        # Anomaly 1: Sudden volume spike (potential attack setup)
        recent_volume = sum(f.amount_sats for f in flows if f.timestamp > time.time() - 86400)
        historical_avg = self.get_historical_daily_volume(hive_id)

        if recent_volume > historical_avg * 5:
            anomalies.append(EconomicAnomaly(
                type="volume_spike",
                severity="warning",
                details=f"24h volume {recent_volume} vs avg {historical_avg}"
            ))

        # Anomaly 2: Asymmetric flow (potential extraction)
        inbound = sum(f.amount_sats for f in flows if f.direction == "inbound")
        outbound = sum(f.amount_sats for f in flows if f.direction == "outbound")

        if outbound > 0 and inbound / outbound < 0.2:
            anomalies.append(EconomicAnomaly(
                type="asymmetric_extraction",
                severity="critical",
                details=f"Inbound/outbound ratio: {inbound/outbound:.2f}"
            ))

        # Anomaly 3: Message payment without routing relationship
        message_payments = [f for f in flows if f.purpose == "message"]
        routing_payments = [f for f in flows if f.purpose == "routing"]

        if len(message_payments) > 10 and len(routing_payments) == 0:
            anomalies.append(EconomicAnomaly(
                type="message_only_relationship",
                severity="warning",
                details="Many messages but no routing - possible reconnaissance"
            ))

        # Anomaly 4: Stake without follow-through
        stakes = [f for f in flows if f.purpose == "stake"]
        introductions = self.get_introduction_completions(hive_id)

        if len(stakes) > 3 and len(introductions) == 0:
            anomalies.append(EconomicAnomaly(
                type="repeated_abandoned_stakes",
                severity="warning",
                details="Multiple stakes placed but introductions abandoned"
            ))

        return anomalies
```

---

## 9. Protocol Messages

### 9.1 Message Type Registry

| Type ID | Name | Fee | Stake | Description |
|---------|------|-----|-------|-------------|
| 1 | ping | 10 | - | Basic connectivity test |
| 2 | pong | 10 | - | Ping response |
| 10 | query_hive_status | 100 | - | Ask if node is in hive |
| 11 | hive_status_response | 100 | - | Response to status query |
| 20 | hive_introduction | 1,000 | 10,000 | Introduce our hive |
| 21 | introduction_response | 1,000 | - | Response to introduction |
| 30 | membership_voucher_request | 500 | 5,000 | Request membership proof |
| 31 | membership_voucher | 500 | - | Membership proof from admin |
| 40 | federation_request | 10,000 | varies | Request federation |
| 41 | federation_response | 10,000 | - | Federation decision |
| 50 | federation_heartbeat | 1,000 | - | Regular federation check-in |
| 51 | heartbeat_response | 1,000 | - | Heartbeat acknowledgment |
| 60 | reputation_query | 100 | - | Query reputation |
| 61 | reputation_response | 100 | - | Reputation data |
| 70 | reputation_challenge | 500 | 10,000 | Issue reputation challenge |
| 71 | challenge_response | 500 | - | Challenge completion |
| 80 | intel_share | 500 | varies | Share intelligence |
| 81 | intel_acknowledgment | 100 | - | Acknowledge intel receipt |
| 90 | defense_alert | 0 | 50,000 | Alert about threat |
| 91 | defense_response | 0 | - | Response to alert |
| 100 | verification_probe | 100 | - | Verification payment |
| 101 | verification_response | 100 | - | Verification acknowledgment |

### 9.2 Message Schemas

See Appendix A for full JSON schemas for each message type.

---

## 10. Implementation Guidelines

### 10.1 Prerequisites

| Requirement | Status | Notes |
|-------------|--------|-------|
| cl-hive | Required | Base coordination |
| Keysend support | Required | For payment-based messages |
| Custom TLV support | Required | For message payloads |
| Invoice creation | Required | For reply routing |
| Route probing | Required | For hidden hive detection |

### 10.2 New RPC Commands

| Command | Description |
|---------|-------------|
| `hive-query <node>` | Query if node is in a hive |
| `hive-introduce <admin>` | Introduce our hive to another |
| `hive-verify-membership <node> <hive>` | Verify membership claim |
| `hive-probe-cluster <nodes...>` | Probe for hidden hive |
| `hive-challenge <node>` | Issue reputation challenge |
| `hive-payment-reputation <node>` | Get payment-based reputation |
| `hive-economic-analysis <hive>` | Analyze economic relationship |

### 10.3 Database Schema Additions

```sql
-- Payment-based reputation
CREATE TABLE payment_reputation (
    node_id TEXT PRIMARY KEY,
    total_volume_sats INTEGER DEFAULT 0,
    success_rate REAL DEFAULT 0,
    balance_ratio REAL DEFAULT 0,
    interaction_count INTEGER DEFAULT 0,
    last_interaction INTEGER,
    reputation_score REAL DEFAULT 0,
    confidence REAL DEFAULT 0
);

-- Hive message log
CREATE TABLE hive_messages (
    id INTEGER PRIMARY KEY,
    direction TEXT NOT NULL,          -- 'sent', 'received'
    counterparty TEXT NOT NULL,
    counterparty_hive TEXT,
    msg_type INTEGER NOT NULL,
    payment_amount_sats INTEGER,
    stake_amount_sats INTEGER,
    payload TEXT,                      -- JSON
    reply_invoice TEXT,
    status TEXT,                       -- 'sent', 'delivered', 'replied', 'failed'
    timestamp INTEGER NOT NULL
);

-- Verification history
CREATE TABLE verification_history (
    id INTEGER PRIMARY KEY,
    hive_id TEXT NOT NULL,
    verification_type TEXT NOT NULL,
    result TEXT NOT NULL,              -- 'passed', 'partial', 'failed'
    details TEXT,                      -- JSON
    timestamp INTEGER NOT NULL
);

-- Stakes and bonds
CREATE TABLE active_stakes (
    stake_id TEXT PRIMARY KEY,
    counterparty_hive TEXT NOT NULL,
    purpose TEXT NOT NULL,
    amount_sats INTEGER NOT NULL,
    locked_at INTEGER NOT NULL,
    vests_at INTEGER,
    status TEXT DEFAULT 'locked',      -- 'locked', 'vesting', 'returned', 'forfeited'
    forfeit_reason TEXT
);
```

---

## Appendix A: Full Message Schemas

### A.1 query_hive_status

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "required": ["msg_type", "payload"],
  "properties": {
    "msg_type": {"const": "query_hive_status"},
    "payload": {
      "type": "object",
      "required": ["query_id"],
      "properties": {
        "query_id": {"type": "string"},
        "include_members": {"type": "boolean", "default": false},
        "include_federation": {"type": "boolean", "default": false},
        "our_hive_id": {"type": "string"}
      }
    },
    "reply_invoice": {"type": "string"}
  }
}
```

### A.2 hive_introduction

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "required": ["msg_type", "payload", "stake_hash"],
  "properties": {
    "msg_type": {"const": "hive_introduction"},
    "payload": {
      "type": "object",
      "required": ["our_hive_id", "our_admin_nodes", "introduction_stake"],
      "properties": {
        "our_hive_id": {"type": "string"},
        "our_admin_nodes": {
          "type": "array",
          "items": {"type": "string"},
          "minItems": 1
        },
        "our_member_count": {"type": "integer", "minimum": 1},
        "our_capacity_tier": {
          "type": "string",
          "enum": ["small", "medium", "large", "xlarge"]
        },
        "introduction_stake": {"type": "integer", "minimum": 10000},
        "proposed_relationship": {
          "type": "string",
          "enum": ["observer", "partner", "allied"]
        },
        "our_public_reputation": {"type": "number", "minimum": 0, "maximum": 1}
      }
    },
    "stake_hash": {"type": "string"},
    "reply_invoice": {"type": "string"}
  }
}
```

---

## Changelog

- **0.1.0-draft** (2025-01-14): Initial specification draft
