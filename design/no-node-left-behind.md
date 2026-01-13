# No Node Left Behind (NNLB) - Design Document

## Overview

The NNLB system ensures every hive member can achieve profitability and maintain good network connectivity, regardless of their starting position or resources. The hive acts as a collective that actively helps weaker members while optimizing overall topology.

## Core Principles

1. **Collective Success**: The hive's strength is determined by its weakest member
2. **Resource Sharing**: Wealthy members help bootstrap newer members
3. **Intelligent Rebalancing**: Channels close/open strategically across members
4. **Budget Awareness**: Recommendations respect individual member budgets

---

## Feature 1: Member Health Scoring

Track each member's "health" to identify who needs help.

### Metrics Tracked
```python
@dataclass
class MemberHealth:
    peer_id: str
    # Capacity metrics
    total_channel_capacity_sats: int
    inbound_capacity_sats: int
    outbound_capacity_sats: int
    channel_count: int

    # Revenue metrics
    daily_forwards_count: int
    daily_forwards_sats: int
    daily_fees_earned_sats: int
    estimated_monthly_revenue_sats: int

    # Connectivity metrics
    unique_destinations_reachable: int
    avg_hops_to_major_nodes: float
    routing_centrality_score: float

    # Health scores (0-100)
    capacity_health: int
    revenue_health: int
    connectivity_health: int
    overall_health: int
```

### Health Thresholds
- **Critical** (< 25): Immediate intervention needed
- **Struggling** (25-50): Prioritize for channel opens
- **Healthy** (50-75): Normal operations
- **Thriving** (> 75): Can help others

### RPC: `hive-member-health`
```json
{
  "members": [
    {
      "peer_id": "031026...",
      "alias": "alice",
      "tier": "admin",
      "overall_health": 85,
      "capacity_health": 90,
      "revenue_health": 75,
      "connectivity_health": 88,
      "needs_help": false,
      "can_help_others": true
    },
    {
      "peer_id": "037254...",
      "alias": "carol",
      "tier": "member",
      "overall_health": 35,
      "capacity_health": 40,
      "revenue_health": 20,
      "connectivity_health": 45,
      "needs_help": true,
      "can_help_others": false,
      "recommendations": [
        "Needs inbound liquidity",
        "Low routing centrality",
        "Consider channel to ACINQ"
      ]
    }
  ]
}
```

---

## Feature 2: Intelligent Channel Closure Recommendations

Analyze cl-revenue-ops data to identify underperforming channels that should be closed.

### Closure Criteria
```python
@dataclass
class ChannelClosureCandidate:
    channel_id: str
    peer_id: str
    owner_member: str  # Which hive member owns this channel

    # Performance metrics
    capacity_sats: int
    utilization_pct: float  # How much capacity is being used
    forwards_30d: int
    fees_earned_30d_sats: int
    days_since_last_forward: int

    # Cost analysis
    locked_capital_sats: int
    opportunity_cost_monthly_sats: int

    # Recommendation
    recommendation: str  # "close", "reduce", "keep"
    closure_score: float  # 0-1, higher = should close
    reasons: List[str]

    # Reopen suggestion
    suggest_reopen_on: Optional[str]  # Another member's pubkey
    reopen_rationale: str
```

### Closure Decision Logic
```python
def should_close_channel(channel_stats, hive_topology):
    score = 0.0
    reasons = []

    # Low utilization (< 5% usage over 30 days)
    if channel_stats.utilization_pct < 0.05:
        score += 0.3
        reasons.append("Very low utilization (<5%)")

    # No forwards in 30+ days
    if channel_stats.days_since_last_forward > 30:
        score += 0.25
        reasons.append("No forwards in 30+ days")

    # Negative ROI (fees < opportunity cost)
    monthly_roi = channel_stats.fees_earned_30d_sats / max(1, channel_stats.locked_capital_sats)
    if monthly_roi < 0.001:  # < 0.1% monthly return
        score += 0.25
        reasons.append(f"Low ROI ({monthly_roi*100:.3f}%)")

    # Redundant routing path (hive already has better routes)
    if hive_has_better_route_to(channel_stats.peer_id, hive_topology):
        score += 0.2
        reasons.append("Redundant - hive has better routes")

    return ChannelClosureCandidate(
        ...,
        closure_score=score,
        recommendation="close" if score > 0.5 else "keep",
        reasons=reasons
    )
```

### RPC: `hive-closure-recommendations`
```json
{
  "analysis_period_days": 30,
  "total_channels_analyzed": 45,
  "closure_candidates": [
    {
      "owner": "alice",
      "channel_id": "850000x100x0",
      "peer_id": "02xyz...",
      "peer_alias": "low-traffic-node",
      "capacity_sats": 5000000,
      "utilization_pct": 2.1,
      "forwards_30d": 3,
      "fees_earned_30d": 45,
      "closure_score": 0.75,
      "recommendation": "close",
      "reasons": [
        "Very low utilization (<5%)",
        "Low ROI (0.027%)",
        "Redundant - bob has direct route"
      ],
      "suggest_reopen": {
        "on_member": "carol",
        "rationale": "Carol lacks connectivity to this network segment"
      }
    }
  ],
  "keep_channels": 40,
  "potential_capital_freed_sats": 15000000
}
```

---

## Feature 3: Channel Migration System

Coordinate moving channels from one member to another for better topology.

### Migration Flow
```
1. DETECT: Alice has underperforming channel to NodeX
2. ANALYZE: Carol needs connectivity to NodeX's network segment
3. PROPOSE: Create migration proposal
4. COORDINATE:
   - Carol reserves budget for new channel
   - Alice prepares to close old channel
5. EXECUTE:
   - Carol opens channel to NodeX
   - Once confirmed, Alice closes her channel
6. VERIFY: Check improved topology
```

### RPC: `hive-propose-migration`
```json
{
  "proposal_id": "mig_abc123",
  "type": "channel_migration",
  "from_member": "alice",
  "to_member": "carol",
  "target_peer": "02xyz...",
  "current_capacity_sats": 5000000,
  "proposed_capacity_sats": 3000000,
  "rationale": {
    "from_member_benefit": "Frees 5M sats, low-performing channel",
    "to_member_benefit": "Gains connectivity to 15 new nodes",
    "hive_benefit": "Better distributed topology, helps struggling member"
  },
  "cost_analysis": {
    "alice_onchain_cost": 2500,
    "carol_onchain_cost": 2500,
    "carol_budget_available": 7500000,
    "carol_budget_sufficient": true
  },
  "approval_required": true,
  "status": "pending"
}
```

---

## Feature 4: Automatic Liquidity Assistance

Wealthy members can automatically provide liquidity assistance to struggling members.

### Assistance Types

1. **Dual-Funded Channel**: Open balanced channel with struggling member
2. **Liquidity Swap**: Push liquidity to struggling member via circular route
3. **Channel Lease**: Wealthy member opens to target, leases to struggler

### Configuration
```python
# New config options
assistance_enabled: bool = True
assistance_max_per_member_sats: int = 10_000_000  # Max 10M per member
assistance_min_health_to_give: int = 70  # Must be healthy to give
assistance_max_health_to_receive: int = 40  # Must be struggling to receive
```

### RPC: `hive-assistance-status`
```json
{
  "my_status": {
    "can_provide_assistance": true,
    "health_score": 85,
    "available_for_assistance_sats": 25000000
  },
  "members_needing_help": [
    {
      "peer_id": "037254...",
      "alias": "carol",
      "health_score": 35,
      "primary_need": "inbound_liquidity",
      "suggested_assistance": [
        {
          "type": "dual_funded_channel",
          "amount_sats": 5000000,
          "estimated_benefit": "+15 health points"
        }
      ]
    }
  ],
  "recent_assistance_given": [
    {
      "to": "carol",
      "type": "channel_open",
      "amount_sats": 2000000,
      "timestamp": 1768300000
    }
  ]
}
```

---

## Feature 5: New Member Onboarding

Automatically help new members get established.

### Onboarding Checklist
```python
@dataclass
class OnboardingProgress:
    member_id: str
    joined_at: int
    days_in_hive: int

    # Checklist items
    has_channel_from_hive: bool      # At least one hive member opened to them
    has_channel_to_external: bool    # They opened to at least one external node
    has_forwarded_payment: bool      # Successfully routed at least one payment
    has_earned_fees: bool            # Earned at least 1 sat in fees
    has_received_vouch: bool         # Received a vouch from existing member

    # Metrics
    total_capacity_sats: int
    inbound_from_hive_sats: int

    # Recommendations
    next_steps: List[str]
```

### Auto-Bootstrap for New Members
```python
def bootstrap_new_member(new_member_id: str):
    """
    Automatically help bootstrap a new hive member.

    Actions:
    1. Admins auto-vouch for the new member
    2. Healthiest member opens a dual-funded channel
    3. Suggest 3 optimal external channels to open
    4. Monitor progress for 30 days
    """
    # Find healthiest member with budget
    helper = find_healthiest_member_with_budget(min_budget=5_000_000)

    if helper:
        # Propose dual-funded channel
        propose_assistance_channel(
            from_member=helper,
            to_member=new_member_id,
            amount=5_000_000,
            dual_funded=True
        )

    # Generate recommendations
    external_targets = find_best_channels_for_member(
        member_id=new_member_id,
        count=3,
        budget=member_budget(new_member_id)
    )

    return OnboardingPlan(
        member_id=new_member_id,
        helper_member=helper,
        recommended_channels=external_targets
    )
```

---

## Implementation Priority

### Phase 1 (Immediate)
1. Member Health Scoring
2. Basic onboarding notifications

### Phase 2 (Short-term)
3. Channel closure recommendations
4. Integration with cl-revenue-ops metrics

### Phase 3 (Medium-term)
5. Channel migration proposals
6. Automatic assistance for struggling members

### Phase 4 (Long-term)
7. Fully automated rebalancing
8. Cross-hive liquidity networks

---

## Database Schema Extensions

```sql
-- Member health tracking
CREATE TABLE member_health_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_id TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    overall_health INTEGER,
    capacity_health INTEGER,
    revenue_health INTEGER,
    connectivity_health INTEGER,
    metrics_json TEXT
);

-- Channel migration proposals
CREATE TABLE migration_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT UNIQUE NOT NULL,
    from_member TEXT NOT NULL,
    to_member TEXT NOT NULL,
    target_peer TEXT NOT NULL,
    current_capacity_sats INTEGER,
    proposed_capacity_sats INTEGER,
    status TEXT DEFAULT 'pending',
    created_at INTEGER,
    executed_at INTEGER,
    rationale_json TEXT
);

-- Assistance tracking
CREATE TABLE assistance_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    assistance_type TEXT NOT NULL,
    amount_sats INTEGER,
    timestamp INTEGER,
    outcome TEXT
);
```

---

## Success Metrics

1. **Member Health Distribution**: Track improvement in health scores for struggling members
2. **Onboarding Success Rate**: % of new members reaching "healthy" status within 30 days
3. **Topology Efficiency**: Measure routing centrality and redundancy improvements
4. **Revenue Equality**: Gini coefficient of member revenues should decrease over time
