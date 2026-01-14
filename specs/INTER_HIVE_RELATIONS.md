# Inter-Hive Relations Protocol Specification

**Version:** 0.1.0-draft
**Status:** Proposal
**Authors:** cl-hive contributors
**Date:** 2025-01-14

## Abstract

This specification defines protocols for detecting, classifying, and managing relationships with other Lightning Network node fleets ("hives"). It establishes reputation systems, policy frameworks, and federation mechanisms while maintaining security against hostile actors.

## Table of Contents

1. [Motivation](#1-motivation)
2. [Design Principles](#2-design-principles)
3. [Hive Detection](#3-hive-detection)
4. [Hive Classification](#4-hive-classification)
5. [Reputation System](#5-reputation-system)
6. [Policy Framework](#6-policy-framework)
7. [Federation Protocol](#7-federation-protocol)
8. [Security Considerations](#8-security-considerations)
9. [Implementation Guidelines](#9-implementation-guidelines)

---

## 1. Motivation

### 1.1 The Multi-Hive Future

As coordinated node management becomes more common, the Lightning Network will contain multiple independent hives:
- Commercial routing operations
- Community cooperatives
- Geographic clusters
- Protocol-specific fleets (LSPs, exchanges)

### 1.2 Strategic Necessity

Without inter-hive awareness:
- We can't distinguish coordinated competitors from random nodes
- We miss opportunities for mutually beneficial cooperation
- We're vulnerable to predatory fleet behavior
- We can't form defensive alliances

### 1.3 Trust Challenges

Other hives may be:
- **Cooperative**: Potential allies for mutual benefit
- **Competitive**: Fair market rivals
- **Hostile**: Actively harmful actors
- **Deceptive**: Appearing friendly while extracting value

**Core Principle**: Don't trust. Verify.

---

## 2. Design Principles

### 2.1 Verify Everything

Never trust self-reported data. All classifications based on:
- Observed behavior over time
- Verifiable on-chain actions
- Third-party corroboration
- Economic incentive analysis

### 2.2 Assume Hostility by Default

New hives start at `unknown` with restricted policies. Trust is earned through consistent positive interactions, never granted.

### 2.3 Gradual Trust Building

```
unknown → observed → neutral → cooperative → federated
    ↓         ↓          ↓           ↓
  hostile  predatory  competitive  [revoked]
```

Trust increases slowly, decreases quickly.

### 2.4 Reciprocity Required

Relationships must be mutually beneficial. One-sided value extraction triggers automatic downgrade.

### 2.5 Isolation by Default

Hive internal information is never shared with external hives unless explicitly federated and verified.

---

## 3. Hive Detection

### 3.1 Detection Methods

#### 3.1.1 Channel Pattern Analysis

Identify node clusters with coordinated characteristics:

```python
class HiveDetector:
    def analyze_cluster(self, nodes: List[str]) -> HiveSignature:
        signals = {
            "internal_zero_fee": self.check_internal_fees(nodes),
            "coordinated_opens": self.check_open_timing(nodes),
            "fee_synchronization": self.check_fee_patterns(nodes),
            "capacity_distribution": self.check_capacity_patterns(nodes),
            "common_peers": self.check_peer_overlap(nodes),
            "naming_patterns": self.check_alias_patterns(nodes),
        }
        return HiveSignature(nodes=nodes, signals=signals)
```

**Detection Signals**:

| Signal | Weight | Description |
|--------|--------|-------------|
| Internal zero-fee | 0.9 | Channels between suspected members have 0 ppm |
| Coordinated opens | 0.7 | Multiple nodes open to same target within hours |
| Fee synchronization | 0.6 | Fee changes occur simultaneously |
| Shared peer set | 0.5 | Unusually high overlap in channel partners |
| Naming patterns | 0.3 | Similar aliases (e.g., "HiveX-1", "HiveX-2") |
| Geographic clustering | 0.4 | Nodes in same IP ranges or regions |

**Confidence Threshold**: Σ(signals × weights) > 2.0 → likely hive

#### 3.1.2 Behavioral Analysis

Track coordinated actions over time:

```python
def detect_coordinated_behavior(self, timeframe_hours=168):
    """Detect hives through behavioral correlation."""
    events = self.get_network_events(timeframe_hours)

    correlations = {}
    for event in events:
        # Find nodes that acted within 1 hour of each other
        correlated = self.find_correlated_actors(event, window_hours=1)
        for pair in combinations(correlated, 2):
            correlations[pair] = correlations.get(pair, 0) + 1

    # Cluster highly correlated nodes
    return self.cluster_correlated_nodes(correlations, threshold=5)
```

#### 3.1.3 Self-Identification

Some hives may announce themselves via:
- Custom TLV in channel announcements
- Public registry (future)
- Direct introduction protocol

**Trust Level**: Self-identification alone = 0. Must be verified by behavior.

#### 3.1.4 Intelligence Sharing (Federated Hives Only)

Trusted federated hives may share hive detection intelligence:

```json
{
  "type": "hive_intel_share",
  "from_hive": "hive_abc123",
  "detected_hive": {
    "suspected_members": ["02xyz...", "03abc..."],
    "confidence": 0.75,
    "classification": "competitive",
    "evidence_summary": ["coordinated_fees", "shared_peers"],
    "first_detected": 1705234567
  },
  "attestation": {...}
}
```

### 3.2 Hive Signature

```python
@dataclass
class HiveSignature:
    hive_id: str                    # Generated hash of member set
    suspected_members: List[str]    # Node pubkeys
    confidence: float               # 0.0 - 1.0
    detection_method: str           # "pattern", "behavior", "self_id", "intel"
    first_detected: int             # Unix timestamp
    last_confirmed: int             # Last behavioral confirmation
    signals: Dict[str, float]       # Detection signals and scores

    def stable_id(self) -> str:
        """Generate stable ID from sorted member list."""
        return hashlib.sha256(
            ",".join(sorted(self.suspected_members)).encode()
        ).hexdigest()[:16]
```

### 3.3 Hive Registry

```sql
CREATE TABLE detected_hives (
    hive_id TEXT PRIMARY KEY,
    members TEXT NOT NULL,          -- JSON array of pubkeys
    confidence REAL NOT NULL,
    classification TEXT DEFAULT 'unknown',
    reputation_score REAL DEFAULT 0.0,
    first_detected INTEGER NOT NULL,
    last_updated INTEGER NOT NULL,
    detection_evidence TEXT,        -- JSON
    policy_id INTEGER REFERENCES hive_policies(id)
);

CREATE TABLE hive_members (
    node_id TEXT PRIMARY KEY,
    hive_id TEXT REFERENCES detected_hives(hive_id),
    confidence REAL NOT NULL,
    first_seen INTEGER NOT NULL,
    last_confirmed INTEGER NOT NULL
);
```

---

## 4. Hive Classification

### 4.1 Classification Categories

| Category | Description | Default Policy |
|----------|-------------|----------------|
| `unknown` | Newly detected, insufficient data | Restricted |
| `observed` | Under active monitoring | Cautious |
| `neutral` | No positive or negative relationship | Standard |
| `competitive` | Competing for same corridors, fair play | Standard |
| `cooperative` | Mutually beneficial interactions | Favorable |
| `federated` | Formal alliance with verified trust | Allied |
| `hostile` | Actively harmful behavior detected | Defensive |
| `predatory` | Extracting value without reciprocity | Restricted |
| `parasitic` | Free-riding on infrastructure | Blocked |

### 4.2 Classification Criteria

#### 4.2.1 Behavioral Indicators

**Positive Indicators** (toward cooperative):
- Reciprocal channel opens
- Fair fee pricing (not undercutting)
- Route reliability (low failure rate)
- Timely HTLC resolution
- Balanced liquidity flow

**Negative Indicators** (toward hostile):
- Coordinated fee undercutting
- Channel jamming patterns
- Probe attacks from multiple members
- Forced closure campaigns
- Liquidity drain without reciprocity

```python
class BehaviorAnalyzer:
    POSITIVE_SIGNALS = {
        "reciprocal_opens": 2.0,
        "fair_pricing": 1.5,
        "route_reliability": 1.0,
        "balanced_flow": 1.0,
        "timely_htlc": 0.5,
    }

    NEGATIVE_SIGNALS = {
        "fee_undercutting": -2.0,
        "channel_jamming": -3.0,
        "probe_attacks": -2.5,
        "forced_closures": -3.0,
        "liquidity_drain": -2.0,
        "sybil_behavior": -4.0,
    }

    def calculate_behavior_score(self, hive_id: str, days: int = 30) -> float:
        events = self.get_hive_events(hive_id, days)
        score = 0.0
        for event in events:
            if event.type in self.POSITIVE_SIGNALS:
                score += self.POSITIVE_SIGNALS[event.type]
            elif event.type in self.NEGATIVE_SIGNALS:
                score += self.NEGATIVE_SIGNALS[event.type]
        return score
```

#### 4.2.2 Economic Analysis

```python
def analyze_economic_relationship(self, hive_id: str) -> EconomicProfile:
    """Analyze value exchange with another hive."""

    # Revenue we earn from routing their payments
    revenue_from = self.calculate_revenue_from_hive(hive_id)

    # Revenue they earn from routing our payments
    revenue_to = self.calculate_revenue_to_hive(hive_id)

    # Channel capacity we provide to them
    capacity_to = self.calculate_capacity_provided(hive_id)

    # Channel capacity they provide to us
    capacity_from = self.calculate_capacity_received(hive_id)

    # Calculate balance
    revenue_ratio = revenue_from / max(revenue_to, 1)
    capacity_ratio = capacity_from / max(capacity_to, 1)

    return EconomicProfile(
        revenue_balance=revenue_ratio,
        capacity_balance=capacity_ratio,
        is_parasitic=revenue_ratio < 0.2 and capacity_ratio < 0.3,
        is_predatory=revenue_ratio < 0.1 and capacity_to > 0,
        is_mutual=0.5 < revenue_ratio < 2.0 and 0.5 < capacity_ratio < 2.0
    )
```

### 4.3 Classification State Machine

```
                    ┌─────────────────────────────────────┐
                    │                                     │
                    ▼                                     │
┌─────────┐    ┌──────────┐    ┌─────────┐    ┌────────────────┐
│ unknown │───▶│ observed │───▶│ neutral │───▶│  cooperative   │
└─────────┘    └──────────┘    └─────────┘    └────────────────┘
     │              │               │                  │
     │              │               │                  │
     │              ▼               ▼                  ▼
     │         ┌─────────┐    ┌───────────┐    ┌────────────┐
     │         │ hostile │    │competitive│    │ federated  │
     │         └─────────┘    └───────────┘    └────────────┘
     │              │
     │              ▼
     │         ┌──────────┐    ┌───────────┐
     └────────▶│predatory │───▶│ parasitic │
               └──────────┘    └───────────┘
```

**Transition Rules**:

| From | To | Trigger |
|------|-----|---------|
| unknown | observed | Detection confidence > 0.7 |
| observed | neutral | 30 days observation, no negative signals |
| observed | hostile | Negative behavior score < -5.0 |
| neutral | cooperative | Positive score > 10.0 over 60 days |
| neutral | competitive | Competing for same targets, fair play |
| cooperative | federated | Formal handshake + 90 days trust |
| any | hostile | Confirmed attack or malicious behavior |
| any | predatory | Economic analysis shows extraction |
| predatory | parasitic | Continued extraction after warning |

### 4.4 Classification Confidence

```python
def calculate_classification_confidence(
    self,
    hive_id: str,
    classification: str
) -> float:
    """Calculate confidence in current classification."""

    factors = {
        "observation_days": min(self.days_observed(hive_id) / 90, 1.0),
        "interaction_count": min(self.interaction_count(hive_id) / 100, 1.0),
        "behavior_consistency": self.behavior_consistency(hive_id),
        "economic_data_quality": self.economic_data_quality(hive_id),
        "corroboration": self.external_corroboration(hive_id),
    }

    weights = {
        "observation_days": 0.2,
        "interaction_count": 0.2,
        "behavior_consistency": 0.3,
        "economic_data_quality": 0.2,
        "corroboration": 0.1,
    }

    return sum(factors[k] * weights[k] for k in factors)
```

---

## 5. Reputation System

### 5.1 Multi-Dimensional Reputation

Reputation is not a single score but multiple dimensions:

```python
@dataclass
class HiveReputation:
    hive_id: str

    # Core dimensions (0.0 - 1.0 scale)
    reliability: float      # Route success, uptime
    fairness: float         # Pricing, not predatory
    reciprocity: float      # Balanced value exchange
    security: float         # No attacks, clean behavior
    responsiveness: float   # Timely actions, communication

    # Metadata
    sample_size: int        # Number of data points
    last_updated: int       # Unix timestamp
    confidence: float       # Overall confidence in scores

    def overall_score(self) -> float:
        """Weighted overall reputation."""
        weights = {
            "reliability": 0.25,
            "fairness": 0.20,
            "reciprocity": 0.25,
            "security": 0.20,
            "responsiveness": 0.10,
        }
        return sum(
            getattr(self, dim) * weight
            for dim, weight in weights.items()
        )
```

### 5.2 Reputation Calculation

#### 5.2.1 Reliability

```python
def calculate_reliability(self, hive_id: str, days: int = 30) -> float:
    """Calculate reliability based on routing performance."""

    members = self.get_hive_members(hive_id)

    metrics = {
        "route_success_rate": self.avg_route_success(members, days),
        "htlc_resolution_time": self.normalize_htlc_time(members, days),
        "channel_uptime": self.avg_channel_uptime(members, days),
        "forced_closure_rate": 1.0 - self.forced_closure_rate(members, days),
    }

    weights = [0.35, 0.25, 0.25, 0.15]
    return sum(m * w for m, w in zip(metrics.values(), weights))
```

#### 5.2.2 Fairness

```python
def calculate_fairness(self, hive_id: str) -> float:
    """Calculate fairness based on pricing and behavior."""

    factors = {
        # Are their fees reasonable vs network average?
        "fee_reasonableness": self.compare_fees_to_network(hive_id),

        # Do they undercut specifically to steal routes?
        "no_predatory_pricing": 1.0 - self.detect_predatory_pricing(hive_id),

        # Do they honor informal agreements?
        "agreement_adherence": self.agreement_adherence_rate(hive_id),

        # Equal treatment (no discrimination)?
        "equal_treatment": self.equal_treatment_score(hive_id),
    }

    return sum(factors.values()) / len(factors)
```

#### 5.2.3 Reciprocity

```python
def calculate_reciprocity(self, hive_id: str) -> float:
    """Calculate reciprocity in relationship."""

    economic = self.analyze_economic_relationship(hive_id)

    # Ideal ratio is 1.0 (balanced)
    revenue_score = 1.0 - min(abs(1.0 - economic.revenue_balance), 1.0)
    capacity_score = 1.0 - min(abs(1.0 - economic.capacity_balance), 1.0)

    # Check for reciprocal actions
    action_reciprocity = self.action_reciprocity_score(hive_id)

    return (revenue_score * 0.4 + capacity_score * 0.3 + action_reciprocity * 0.3)
```

#### 5.2.4 Security

```python
def calculate_security(self, hive_id: str) -> float:
    """Calculate security score (absence of malicious behavior)."""

    incidents = {
        "probe_attacks": self.count_probe_attacks(hive_id),
        "jamming_attempts": self.count_jamming_attempts(hive_id),
        "sybil_indicators": self.sybil_indicator_count(hive_id),
        "forced_closures_initiated": self.forced_closures_against_us(hive_id),
        "suspicious_htlc_patterns": self.suspicious_htlc_count(hive_id),
    }

    # Each incident type reduces score
    penalties = {
        "probe_attacks": 0.1,
        "jamming_attempts": 0.2,
        "sybil_indicators": 0.3,
        "forced_closures_initiated": 0.15,
        "suspicious_htlc_patterns": 0.1,
    }

    score = 1.0
    for incident_type, count in incidents.items():
        score -= min(count * penalties[incident_type], 0.5)

    return max(score, 0.0)
```

### 5.3 Reputation Decay

Reputation should decay over time without new data:

```python
def apply_reputation_decay(self, reputation: HiveReputation) -> HiveReputation:
    """Apply time-based decay to reputation scores."""

    days_since_update = (time.time() - reputation.last_updated) / 86400

    # Decay factor: lose 10% per 30 days of no data
    decay_factor = 0.9 ** (days_since_update / 30)

    # Pull scores toward neutral (0.5) with decay
    def decay_toward_neutral(score: float) -> float:
        neutral = 0.5
        return neutral + (score - neutral) * decay_factor

    return HiveReputation(
        hive_id=reputation.hive_id,
        reliability=decay_toward_neutral(reputation.reliability),
        fairness=decay_toward_neutral(reputation.fairness),
        reciprocity=decay_toward_neutral(reputation.reciprocity),
        security=decay_toward_neutral(reputation.security),
        responsiveness=decay_toward_neutral(reputation.responsiveness),
        sample_size=reputation.sample_size,
        last_updated=reputation.last_updated,
        confidence=reputation.confidence * decay_factor,
    )
```

### 5.4 Reputation Events

```sql
CREATE TABLE reputation_events (
    id INTEGER PRIMARY KEY,
    hive_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    dimension TEXT NOT NULL,        -- reliability, fairness, etc.
    impact REAL NOT NULL,           -- Positive or negative
    evidence TEXT,                  -- JSON proof
    timestamp INTEGER NOT NULL,
    expires INTEGER,                -- When this event stops affecting score

    FOREIGN KEY (hive_id) REFERENCES detected_hives(hive_id)
);

CREATE INDEX idx_reputation_events_hive ON reputation_events(hive_id, timestamp);
```

---

## 6. Policy Framework

### 6.1 Policy Templates

```python
@dataclass
class HivePolicy:
    policy_id: str
    name: str
    classification: str

    # Fee policies
    fee_multiplier: float           # 1.0 = standard, 0.5 = discount, 2.0 = premium
    min_fee_ppm: int
    max_fee_ppm: int

    # Channel policies
    accept_channel_opens: bool
    initiate_channel_opens: bool
    max_channels_per_member: int
    min_channel_size_sats: int
    max_channel_size_sats: int

    # Routing policies
    route_through: bool             # Allow routing via their nodes
    route_to: bool                  # Allow payments to their nodes
    max_htlc_exposure_sats: int

    # Information sharing
    share_fee_intelligence: bool
    share_hive_detection: bool
    share_reputation_data: bool

    # Monitoring
    enhanced_monitoring: bool
    log_all_interactions: bool
```

### 6.2 Default Policies by Classification

```python
DEFAULT_POLICIES = {
    "unknown": HivePolicy(
        name="Unknown Hive - Restricted",
        classification="unknown",
        fee_multiplier=1.5,
        min_fee_ppm=50,
        max_fee_ppm=2000,
        accept_channel_opens=False,
        initiate_channel_opens=False,
        max_channels_per_member=0,
        min_channel_size_sats=0,
        max_channel_size_sats=0,
        route_through=True,
        route_to=True,
        max_htlc_exposure_sats=1_000_000,
        share_fee_intelligence=False,
        share_hive_detection=False,
        share_reputation_data=False,
        enhanced_monitoring=True,
        log_all_interactions=True,
    ),

    "observed": HivePolicy(
        name="Observed Hive - Cautious",
        classification="observed",
        fee_multiplier=1.2,
        min_fee_ppm=25,
        max_fee_ppm=3000,
        accept_channel_opens=True,
        initiate_channel_opens=False,
        max_channels_per_member=1,
        min_channel_size_sats=1_000_000,
        max_channel_size_sats=10_000_000,
        route_through=True,
        route_to=True,
        max_htlc_exposure_sats=5_000_000,
        share_fee_intelligence=False,
        share_hive_detection=False,
        share_reputation_data=False,
        enhanced_monitoring=True,
        log_all_interactions=True,
    ),

    "neutral": HivePolicy(
        name="Neutral Hive - Standard",
        classification="neutral",
        fee_multiplier=1.0,
        min_fee_ppm=10,
        max_fee_ppm=5000,
        accept_channel_opens=True,
        initiate_channel_opens=True,
        max_channels_per_member=2,
        min_channel_size_sats=500_000,
        max_channel_size_sats=50_000_000,
        route_through=True,
        route_to=True,
        max_htlc_exposure_sats=10_000_000,
        share_fee_intelligence=False,
        share_hive_detection=False,
        share_reputation_data=False,
        enhanced_monitoring=False,
        log_all_interactions=False,
    ),

    "cooperative": HivePolicy(
        name="Cooperative Hive - Favorable",
        classification="cooperative",
        fee_multiplier=0.8,
        min_fee_ppm=5,
        max_fee_ppm=5000,
        accept_channel_opens=True,
        initiate_channel_opens=True,
        max_channels_per_member=5,
        min_channel_size_sats=100_000,
        max_channel_size_sats=100_000_000,
        route_through=True,
        route_to=True,
        max_htlc_exposure_sats=50_000_000,
        share_fee_intelligence=True,
        share_hive_detection=True,
        share_reputation_data=False,
        enhanced_monitoring=False,
        log_all_interactions=False,
    ),

    "federated": HivePolicy(
        name="Federated Hive - Allied",
        classification="federated",
        fee_multiplier=0.5,
        min_fee_ppm=0,
        max_fee_ppm=5000,
        accept_channel_opens=True,
        initiate_channel_opens=True,
        max_channels_per_member=10,
        min_channel_size_sats=100_000,
        max_channel_size_sats=500_000_000,
        route_through=True,
        route_to=True,
        max_htlc_exposure_sats=100_000_000,
        share_fee_intelligence=True,
        share_hive_detection=True,
        share_reputation_data=True,
        enhanced_monitoring=False,
        log_all_interactions=False,
    ),

    "hostile": HivePolicy(
        name="Hostile Hive - Defensive",
        classification="hostile",
        fee_multiplier=3.0,
        min_fee_ppm=500,
        max_fee_ppm=5000,
        accept_channel_opens=False,
        initiate_channel_opens=False,
        max_channels_per_member=0,
        min_channel_size_sats=0,
        max_channel_size_sats=0,
        route_through=True,           # Still route (earn fees)
        route_to=True,
        max_htlc_exposure_sats=500_000,
        share_fee_intelligence=False,
        share_hive_detection=False,
        share_reputation_data=False,
        enhanced_monitoring=True,
        log_all_interactions=True,
    ),

    "predatory": HivePolicy(
        name="Predatory Hive - Restricted",
        classification="predatory",
        fee_multiplier=2.0,
        min_fee_ppm=200,
        max_fee_ppm=5000,
        accept_channel_opens=False,
        initiate_channel_opens=False,
        max_channels_per_member=0,
        min_channel_size_sats=0,
        max_channel_size_sats=0,
        route_through=True,
        route_to=True,
        max_htlc_exposure_sats=1_000_000,
        share_fee_intelligence=False,
        share_hive_detection=False,
        share_reputation_data=False,
        enhanced_monitoring=True,
        log_all_interactions=True,
    ),

    "parasitic": HivePolicy(
        name="Parasitic Hive - Blocked",
        classification="parasitic",
        fee_multiplier=5.0,
        min_fee_ppm=1000,
        max_fee_ppm=5000,
        accept_channel_opens=False,
        initiate_channel_opens=False,
        max_channels_per_member=0,
        min_channel_size_sats=0,
        max_channel_size_sats=0,
        route_through=False,          # Block routing
        route_to=False,
        max_htlc_exposure_sats=0,
        share_fee_intelligence=False,
        share_hive_detection=False,
        share_reputation_data=False,
        enhanced_monitoring=True,
        log_all_interactions=True,
    ),
}
```

### 6.3 Policy Application

```python
class HivePolicyEngine:
    def get_policy_for_node(self, node_id: str) -> HivePolicy:
        """Get effective policy for a node."""

        # Check if node belongs to detected hive
        hive = self.get_hive_for_node(node_id)

        if hive is None:
            return DEFAULT_POLICIES["neutral"]  # Non-hive node

        # Get hive classification
        classification = hive.classification

        # Check for policy override
        override = self.get_policy_override(hive.hive_id)
        if override:
            return override

        return DEFAULT_POLICIES.get(classification, DEFAULT_POLICIES["unknown"])

    def should_accept_channel(self, node_id: str, amount_sats: int) -> Tuple[bool, str]:
        """Determine if we should accept a channel open."""
        policy = self.get_policy_for_node(node_id)

        if not policy.accept_channel_opens:
            return False, f"Policy blocks opens from {policy.classification} hives"

        if amount_sats < policy.min_channel_size_sats:
            return False, f"Channel too small for {policy.classification} policy"

        if amount_sats > policy.max_channel_size_sats:
            return False, f"Channel too large for {policy.classification} policy"

        # Check existing channel count
        existing = self.count_channels_with_hive(node_id)
        if existing >= policy.max_channels_per_member:
            return False, f"Max channels reached for this hive member"

        return True, "Accepted"

    def get_fee_for_node(self, node_id: str, base_fee: int) -> int:
        """Calculate fee for routing to/through a node."""
        policy = self.get_policy_for_node(node_id)
        return int(base_fee * policy.fee_multiplier)
```

### 6.4 Policy Override Commands

```
hive-relation-policy set <hive_id> <policy_name>
hive-relation-policy override <hive_id> fee_multiplier=0.5
hive-relation-policy reset <hive_id>
hive-relation-policy list
```

---

## 7. Federation Protocol

### 7.1 Federation Levels

| Level | Trust | Shared Data | Joint Actions |
|-------|-------|-------------|---------------|
| 0: None | Zero | Nothing | None |
| 1: Observer | Low | Public data only | None |
| 2: Partner | Medium | Fee intel, hive detection | Coordinated defense |
| 3: Allied | High | Reputation, strategies | Joint expansion |
| 4: Integrated | Full | Full transparency | Full coordination |

### 7.2 Federation Handshake

#### 7.2.1 Introduction

```json
{
  "type": "federation_introduce",
  "version": 1,
  "from_hive": {
    "hive_id": "hive_abc123",
    "member_count": 5,
    "total_capacity_tier": "large",
    "established_timestamp": 1700000000,
    "admin_contact_node": "03xyz..."
  },
  "proposal": {
    "requested_level": 2,
    "offered_benefits": ["fee_intel_sharing", "coordinated_defense"],
    "requested_benefits": ["fee_intel_sharing", "hive_detection_sharing"],
    "trial_period_days": 30
  },
  "credentials": {
    "attestation": {...},
    "references": []           # Other federated hives that vouch
  },
  "signature": "..."
}
```

#### 7.2.2 Verification Period

Before accepting federation:
1. Observe behavior for `trial_period_days`
2. Verify claimed member count matches detection
3. Check references with existing federated hives
4. Analyze economic relationship potential

```python
def evaluate_federation_proposal(self, proposal: FederationProposal) -> FederationEvaluation:
    """Evaluate a federation proposal."""

    checks = {
        "member_count_verified": self.verify_member_count(proposal),
        "behavior_acceptable": self.check_behavior_history(proposal.from_hive),
        "economic_potential": self.analyze_economic_potential(proposal.from_hive),
        "references_valid": self.verify_references(proposal.credentials.references),
        "no_hostile_history": self.check_hostile_history(proposal.from_hive),
    }

    all_passed = all(checks.values())

    return FederationEvaluation(
        proposal_id=proposal.id,
        checks=checks,
        recommendation="accept" if all_passed else "reject",
        suggested_level=min(proposal.requested_level, 2) if all_passed else 0,
        notes=self.generate_evaluation_notes(checks),
    )
```

#### 7.2.3 Acceptance

```json
{
  "type": "federation_accept",
  "version": 1,
  "proposal_id": "prop_xyz789",
  "from_hive": "hive_def456",
  "to_hive": "hive_abc123",

  "agreement": {
    "level": 2,
    "effective_timestamp": 1705234567,
    "review_timestamp": 1707826567,    // 30 days
    "terms": {
      "share_fee_intel": true,
      "share_hive_detection": true,
      "share_reputation": false,
      "coordinated_defense": true,
      "joint_expansion": false
    },
    "termination_notice_days": 7
  },

  "signatures": {
    "from_hive": "...",
    "to_hive": "..."
  }
}
```

### 7.3 Federation Data Exchange

#### 7.3.1 Fee Intelligence Sharing

```json
{
  "type": "federation_fee_intel",
  "from_hive": "hive_abc123",
  "to_hive": "hive_def456",
  "timestamp": 1705234567,

  "intel": {
    "corridor_fees": [
      {
        "corridor": "exchanges_to_retail",
        "avg_fee_ppm": 150,
        "trend": "increasing",
        "sample_size": 500
      }
    ],
    "competitor_analysis": [
      {
        "hive_id": "hive_hostile1",
        "classification": "predatory",
        "observed_tactics": ["undercutting", "jamming"]
      }
    ]
  },

  "attestation": {...}
}
```

#### 7.3.2 Coordinated Defense

```json
{
  "type": "federation_defense_alert",
  "from_hive": "hive_abc123",
  "timestamp": 1705234567,
  "priority": "high",

  "threat": {
    "threat_type": "coordinated_attack",
    "attacker_hive": "hive_hostile1",
    "attack_vector": "channel_jamming",
    "affected_corridors": ["us_to_eu"],
    "evidence": [...]
  },

  "requested_response": {
    "action": "increase_fees_to_attacker",
    "parameters": {"fee_multiplier": 3.0},
    "duration_hours": 24
  },

  "attestation": {...}
}
```

### 7.4 Federation Management

```sql
CREATE TABLE federations (
    federation_id TEXT PRIMARY KEY,
    our_hive_id TEXT NOT NULL,
    their_hive_id TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending, active, suspended, terminated
    established_timestamp INTEGER,
    last_review_timestamp INTEGER,
    next_review_timestamp INTEGER,
    terms TEXT,                                -- JSON agreement terms
    trust_score REAL DEFAULT 0.5,

    UNIQUE(our_hive_id, their_hive_id)
);

CREATE TABLE federation_events (
    id INTEGER PRIMARY KEY,
    federation_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    data TEXT,                                 -- JSON
    timestamp INTEGER NOT NULL,

    FOREIGN KEY (federation_id) REFERENCES federations(federation_id)
);
```

### 7.5 Federation Trust Verification

```python
class FederationVerifier:
    """Continuously verify federated hive behavior matches agreements."""

    def verify_federation(self, federation_id: str) -> VerificationResult:
        federation = self.get_federation(federation_id)
        their_hive = federation.their_hive_id

        violations = []

        # Check for terms violations
        if federation.terms.get("no_undercutting"):
            if self.detect_undercutting(their_hive):
                violations.append("undercutting_detected")

        # Check for hostile actions despite federation
        if self.detect_hostile_actions(their_hive):
            violations.append("hostile_action_detected")

        # Check reciprocity
        if federation.level >= 2:
            intel_received = self.count_intel_received(their_hive)
            intel_sent = self.count_intel_sent(their_hive)
            if intel_received < intel_sent * 0.5:
                violations.append("insufficient_reciprocity")

        # Calculate trust adjustment
        trust_delta = -0.1 * len(violations) if violations else 0.02
        new_trust = max(0, min(1, federation.trust_score + trust_delta))

        return VerificationResult(
            federation_id=federation_id,
            violations=violations,
            trust_score=new_trust,
            recommendation=self.get_recommendation(violations, new_trust),
        )

    def get_recommendation(self, violations: List[str], trust: float) -> str:
        if "hostile_action_detected" in violations:
            return "terminate_immediately"
        if trust < 0.3:
            return "suspend_and_review"
        if violations:
            return "warn_and_monitor"
        return "continue"
```

---

## 8. Security Considerations

### 8.1 Sybil Attacks

**Threat**: Attacker creates fake "friendly" hive to gain trust and intelligence.

**Mitigations**:
- Long observation periods before trust upgrade
- Economic analysis (fake hives have low real activity)
- Cross-reference with federated hives
- Channel history verification (new nodes are suspicious)

```python
def detect_sybil_hive(self, hive_id: str) -> SybilRisk:
    """Detect potential sybil hive."""

    members = self.get_hive_members(hive_id)

    risk_factors = {
        # New nodes are suspicious
        "avg_node_age_days": self.avg_node_age(members),

        # Low real routing activity
        "routing_volume": self.total_routing_volume(members),

        # Few external relationships
        "external_channel_ratio": self.external_channel_ratio(members),

        # Concentrated funding sources
        "funding_concentration": self.funding_source_concentration(members),

        # Suspiciously perfect behavior
        "behavior_variance": self.behavior_variance(members),
    }

    # Score each factor
    sybil_score = 0.0
    if risk_factors["avg_node_age_days"] < 90:
        sybil_score += 0.3
    if risk_factors["routing_volume"] < 1_000_000:
        sybil_score += 0.2
    if risk_factors["external_channel_ratio"] < 0.3:
        sybil_score += 0.2
    if risk_factors["funding_concentration"] > 0.8:
        sybil_score += 0.2
    if risk_factors["behavior_variance"] < 0.1:
        sybil_score += 0.1  # Too perfect = suspicious

    return SybilRisk(
        hive_id=hive_id,
        risk_score=sybil_score,
        risk_factors=risk_factors,
        recommendation="high_scrutiny" if sybil_score > 0.5 else "normal",
    )
```

### 8.2 Intelligence Gathering

**Threat**: Hostile hive poses as friendly to gather intelligence.

**Mitigations**:
- Tiered information sharing (more trust = more data)
- Sensitive data only at federation level 3+
- Monitor for data leakage to third parties
- Time-delayed sharing of strategic information

### 8.3 Infiltration

**Threat**: Hostile actor joins our hive to gather intelligence or sabotage.

**Mitigations**:
- Standard hive membership vetting applies
- Cross-reference new member with known hostile hive members
- Monitor member behavior for coordination with external hives

```python
def check_infiltration_risk(self, new_member: str) -> InfiltrationRisk:
    """Check if new member might be infiltrator."""

    # Check if node appears in any detected hostile hive
    hostile_hives = self.get_hives_by_classification(["hostile", "predatory", "parasitic"])

    for hive in hostile_hives:
        if new_member in hive.suspected_members:
            return InfiltrationRisk(
                node_id=new_member,
                risk_level="critical",
                reason=f"Node is member of {hive.classification} hive {hive.hive_id}",
                recommendation="reject",
            )

        # Check channel relationships with hostile hive
        overlap = self.channel_overlap(new_member, hive.suspected_members)
        if overlap > 0.5:
            return InfiltrationRisk(
                node_id=new_member,
                risk_level="high",
                reason=f"High channel overlap ({overlap:.0%}) with {hive.classification} hive",
                recommendation="reject_or_extended_probation",
            )

    return InfiltrationRisk(
        node_id=new_member,
        risk_level="low",
        reason="No hostile hive association detected",
        recommendation="standard_vetting",
    )
```

### 8.4 Federation Betrayal

**Threat**: Federated hive turns hostile or leaks shared intelligence.

**Mitigations**:
- Continuous verification of federated hive behavior
- Automatic suspension on trust score drop
- Limited blast radius (tiered information sharing)
- Federation termination protocol

```python
def handle_federation_breach(self, federation_id: str, breach_type: str):
    """Handle detected federation breach."""

    federation = self.get_federation(federation_id)
    their_hive = federation.their_hive_id

    # Immediate actions
    actions = []

    if breach_type == "hostile_action":
        # Immediate termination
        self.terminate_federation(federation_id, reason=breach_type)
        self.reclassify_hive(their_hive, "hostile")
        actions.append("federation_terminated")
        actions.append("hive_reclassified_hostile")

    elif breach_type == "intelligence_leak":
        # Suspend and investigate
        self.suspend_federation(federation_id)
        self.increase_monitoring(their_hive)
        actions.append("federation_suspended")
        actions.append("enhanced_monitoring_enabled")

    elif breach_type == "terms_violation":
        # Warn and reduce trust
        self.warn_federation(federation_id, breach_type)
        self.reduce_federation_level(federation_id)
        actions.append("warning_issued")
        actions.append("federation_level_reduced")

    # Alert federated hives
    self.broadcast_to_federated(
        type="federation_breach_alert",
        breaching_hive=their_hive,
        breach_type=breach_type,
        our_response=actions,
    )

    return actions
```

### 8.5 Coordinated Attack Defense

```python
class CoordinatedDefense:
    """Coordinate defense with federated hives."""

    def request_coordinated_defense(
        self,
        attacker_hive: str,
        attack_type: str,
        evidence: List[Dict],
    ) -> DefenseCoordination:
        """Request coordinated defense from federated hives."""

        # Determine appropriate response
        response_plan = self.create_response_plan(attacker_hive, attack_type)

        # Request participation from federated hives
        participants = []
        for federation in self.get_active_federations(min_level=2):
            response = self.request_defense_participation(
                federation.their_hive_id,
                attacker_hive=attacker_hive,
                response_plan=response_plan,
                evidence=evidence,
            )
            if response.will_participate:
                participants.append(federation.their_hive_id)

        # Execute coordinated response
        if len(participants) >= response_plan.min_participants:
            self.execute_coordinated_response(response_plan, participants)

        return DefenseCoordination(
            attacker=attacker_hive,
            response_plan=response_plan,
            participants=participants,
            status="active" if participants else "solo_defense",
        )
```

---

## 9. Implementation Guidelines

### 9.1 Prerequisites

| Requirement | Status | Notes |
|-------------|--------|-------|
| cl-hive | Required | Base coordination |
| cl-revenue-ops | Required | Fee execution |
| Gossip analysis module | Required | For detection |
| Graph analysis capability | Required | For pattern detection |

### 9.2 Phased Rollout

**Phase 1: Detection Only**
- Implement hive detection algorithms
- Build hive registry
- Manual classification only
- No automated policies

**Phase 2: Classification & Reputation**
- Automated classification based on behavior
- Multi-dimensional reputation system
- Basic policy framework
- Human approval for classification changes

**Phase 3: Policy Automation**
- Automated policy application
- Real-time fee adjustments
- Channel decision automation
- Human override capability

**Phase 4: Federation**
- Federation handshake protocol
- Intelligence sharing
- Coordinated defense
- Multi-hive operations

### 9.3 RPC Commands

| Command | Description |
|---------|-------------|
| `hive-relation-detect` | Trigger hive detection scan |
| `hive-relation-list` | List detected hives |
| `hive-relation-info <hive_id>` | Get details on a hive |
| `hive-relation-classify <hive_id> <class>` | Manually classify a hive |
| `hive-relation-reputation <hive_id>` | Get reputation details |
| `hive-relation-policy <hive_id>` | Get effective policy |
| `hive-relation-federate <hive_id>` | Initiate federation |
| `hive-relation-unfederate <hive_id>` | Terminate federation |
| `hive-relation-federations` | List federations |

### 9.4 Database Schema Summary

```sql
-- Core tables
detected_hives          -- Detected hive registry
hive_members           -- Node to hive mappings
hive_reputation        -- Multi-dimensional reputation
reputation_events      -- Reputation change log
hive_policies          -- Policy configurations
federations            -- Federation agreements
federation_events      -- Federation activity log
hive_interactions      -- Interaction history for analysis
```

---

## Appendix A: Detection Signal Weights

| Signal | Weight | Threshold | Notes |
|--------|--------|-----------|-------|
| Internal zero-fee | 0.9 | 3+ channels | Strong indicator |
| Coordinated opens | 0.7 | 3+ opens in 24h | Time correlation |
| Fee synchronization | 0.6 | 90% correlation | Statistical analysis |
| Shared peer set | 0.5 | >60% overlap | Jaccard similarity |
| Naming patterns | 0.3 | Regex match | Weak signal alone |
| Geographic clustering | 0.4 | Same /24 subnet | IP analysis |
| Funding source | 0.5 | >80% same source | On-chain analysis |

---

## Appendix B: Reputation Score Interpretation

| Overall Score | Interpretation | Recommended Policy |
|--------------|----------------|-------------------|
| 0.9 - 1.0 | Excellent | Federation candidate |
| 0.7 - 0.9 | Good | Cooperative |
| 0.5 - 0.7 | Neutral | Standard |
| 0.3 - 0.5 | Concerning | Enhanced monitoring |
| 0.1 - 0.3 | Poor | Restricted |
| 0.0 - 0.1 | Hostile | Blocked |

---

## Changelog

- **0.1.0-draft** (2025-01-14): Initial specification draft
