# NNLB-Aware Rebalancing, Liquidity & Splice Integration Plan

## Executive Summary

Integrate cl-hive's distributed intelligence (NNLB health scores, liquidity state awareness, topology data) with cl-revenue-ops' EVRebalancer and future splice support. This creates a system where nodes share *information* to make better *independent* decisions about their own channels.

**Critical Principle: Node balances remain completely separate.** Nodes never transfer sats to each other. Coordination is purely informational:
- Share health status so the fleet knows who is struggling
- Share liquidity needs so others can adjust fees to influence flow
- Coordinate timing to avoid conflicting rebalances
- Check splice safety to maintain fleet connectivity

## Three-Phase Roadmap

| Phase | Name | Description |
|-------|------|-------------|
| 1 | NNLB-Aware Rebalancing | EVRebalancer uses hive health scores to prioritize own operations |
| 2 | Liquidity Intelligence Sharing | Share liquidity state to enable coordinated fee/rebalance decisions |
| 3 | Splice Coordination | Safety checks to prevent connectivity gaps during splice-out |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           HIVE FLEET                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                              │
│  │ Node A   │  │ Node B   │  │ Node C   │   ... (hive members)         │
│  │ cl-hive  │  │ cl-hive  │  │ cl-hive  │                              │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘                              │
│       │             │             │                                     │
│       └─────────────┼─────────────┘                                     │
│                     │ GOSSIP (HEALTH_STATUS, LIQUIDITY_STATE, SPLICE_CHECK)│
│                     ▼                                                   │
│  ┌──────────────────────────────────────┐                              │
│  │     cl-hive Coordination Layer       │                              │
│  │  - Information aggregation only      │                              │
│  │  - No fund movement between nodes    │                              │
│  │  - Advisory recommendations          │                              │
│  └──────────────────┬───────────────────┘                              │
└─────────────────────┼───────────────────────────────────────────────────┘
                      │
                      │ INFORMATION ONLY (never sats)
                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      cl-revenue-ops                                      │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │  Each node makes INDEPENDENT decisions about:                      │ │
│  │  - Its own rebalancing (using hive intelligence)                   │ │
│  │  - Its own fee adjustments (considering fleet state)               │ │
│  │  - Its own splice operations (with safety coordination)            │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
```

---

# Phase 1: NNLB-Aware Rebalancing

## Goal
EVRebalancer uses hive NNLB health scores to adjust *its own* rebalancing priorities and budgets. Struggling nodes prioritize their own recovery; healthy nodes can be more selective.

## Concept: Health-Tier Budget Multipliers

Each node adjusts *its own* rebalancing budget based on its health tier:

```
┌────────────────────────────────────────────────────────────────┐
│                    NNLB Health Tiers                            │
├─────────────┬───────────────┬──────────────────────────────────┤
│ Tier        │ Health Score  │ Own Budget Multiplier            │
├─────────────┼───────────────┼──────────────────────────────────┤
│ Struggling  │ 0-30          │ 2.0x (prioritize own recovery)   │
│ Vulnerable  │ 31-50         │ 1.5x (elevated self-care)        │
│ Stable      │ 51-70         │ 1.0x (normal operation)          │
│ Thriving    │ 71-100        │ 0.75x (be selective, save fees)  │
└─────────────┴───────────────┴──────────────────────────────────┘
```

**Logic:**
- Struggling nodes accept higher rebalance costs to recover their own channels faster
- Thriving nodes are more selective (only high-EV rebalances) to conserve routing fees
- Each node optimizes *itself* - no fund transfers between nodes

## How Fleet Awareness Helps (Without Transferring Sats)

Knowing fleet health enables smarter *individual* decisions:

1. **Fee Coordination**: If Node A knows Node B is struggling with Peer X, Node A can:
   - Lower fees toward Peer X to attract flow that might help B indirectly
   - Avoid competing for the same rebalance routes

2. **Rebalance Conflict Avoidance**: If Node A knows Node B is rebalancing via Peer X, Node A can:
   - Delay its own rebalance through that route
   - Choose alternate paths to avoid fee competition

3. **Topology Intelligence**: Knowing who needs what helps the planner:
   - Prioritize channel opens to peers that help struggling members
   - Avoid creating redundant capacity where it's not needed

## cl-hive Changes

### New RPC: `hive-member-health`

**File**: `/home/sat/bin/cl-hive/cl-hive.py`

```python
@plugin.method("hive-member-health")
def hive_member_health(plugin, member_id=None, action="query"):
    """
    Query NNLB health scores for fleet members.

    This is INFORMATION SHARING only - no fund movement.

    Args:
        member_id: Specific member (None for self, "all" for fleet)
        action: "query" (default), "aggregate" (fleet summary)

    Returns for single member:
    {
        "member_id": "02abc...",
        "alias": "HiveNode1",
        "health_score": 65,           # 0-100 overall health
        "health_tier": "stable",      # struggling/vulnerable/stable/thriving
        "capacity_sats": 50000000,
        "profitable_channels": 12,
        "underwater_channels": 3,
        "stagnant_channels": 2,
        "revenue_trend": "improving", # declining/stable/improving
        "liquidity_score": 72,        # Balance distribution health
        "rebalance_budget_multiplier": 1.0,  # For own operations
        "last_updated": 1705000000
    }

    Returns for "aggregate":
    {
        "fleet_health": 58,
        "struggling_count": 1,
        "vulnerable_count": 2,
        "stable_count": 3,
        "thriving_count": 1,
        "members": [...]
    }
    """
```

### New RPC: `hive-report-health`

```python
@plugin.method("hive-report-health")
def hive_report_health(
    plugin,
    profitable_channels: int,
    underwater_channels: int,
    stagnant_channels: int,
    revenue_trend: str
):
    """
    Report our health status to the hive.

    Called periodically by cl-revenue-ops profitability analyzer.
    This shares INFORMATION - no sats move.

    Returns:
        {"status": "reported", "health_score": 65, "tier": "stable"}
    """
```

### Database: Health Score Tracking

**File**: `/home/sat/bin/cl-hive/modules/database.py`

```sql
-- Health tracking columns in hive_members
ALTER TABLE hive_members ADD COLUMN health_score INTEGER DEFAULT 50;
ALTER TABLE hive_members ADD COLUMN health_tier TEXT DEFAULT 'stable';
ALTER TABLE hive_members ADD COLUMN liquidity_score INTEGER DEFAULT 50;
ALTER TABLE hive_members ADD COLUMN profitable_channels INTEGER DEFAULT 0;
ALTER TABLE hive_members ADD COLUMN underwater_channels INTEGER DEFAULT 0;
ALTER TABLE hive_members ADD COLUMN revenue_trend TEXT DEFAULT 'stable';
ALTER TABLE hive_members ADD COLUMN health_updated_at INTEGER DEFAULT 0;
```

### Module: `health_aggregator.py` (NEW)

**File**: `/home/sat/bin/cl-hive/modules/health_aggregator.py`

```python
"""
Health Score Aggregator for NNLB prioritization.

Aggregates health data from fleet members for INFORMATION SHARING.
No fund movement - each node uses this to optimize its own operations.
"""

from enum import Enum
from typing import Dict, Tuple, Any

class HealthTier(Enum):
    STRUGGLING = "struggling"    # 0-30
    VULNERABLE = "vulnerable"    # 31-50
    STABLE = "stable"            # 51-70
    THRIVING = "thriving"        # 71-100

class HealthScoreAggregator:
    """Aggregates and distributes NNLB health scores."""

    def calculate_health_score(
        self,
        profitable_pct: float,
        underwater_pct: float,
        liquidity_score: float,
        revenue_trend: str
    ) -> Tuple[int, HealthTier]:
        """
        Calculate overall health score from components.

        Components:
        - Profitable channels % (40% weight)
        - Inverse underwater % (30% weight)
        - Liquidity balance score (20% weight)
        - Revenue trend bonus (10% weight)

        Returns:
            (score, tier) tuple
        """
        # Profitable channels contribution (0-40 points)
        profitable_score = profitable_pct * 40

        # Underwater penalty (0-30 points, inverted)
        underwater_score = (1.0 - underwater_pct) * 30

        # Liquidity score (0-20 points)
        liquidity_contribution = (liquidity_score / 100) * 20

        # Revenue trend (0-10 points)
        trend_bonus = {
            "improving": 10,
            "stable": 5,
            "declining": 0
        }.get(revenue_trend, 5)

        total = int(profitable_score + underwater_score +
                   liquidity_contribution + trend_bonus)
        total = max(0, min(100, total))

        # Determine tier
        if total <= 30:
            tier = HealthTier.STRUGGLING
        elif total <= 50:
            tier = HealthTier.VULNERABLE
        elif total <= 70:
            tier = HealthTier.STABLE
        else:
            tier = HealthTier.THRIVING

        return total, tier

    def get_budget_multiplier(self, tier: HealthTier) -> float:
        """
        Get rebalance budget multiplier for node's OWN operations.

        This affects how aggressively the node rebalances its own channels.
        """
        return {
            HealthTier.STRUGGLING: 2.0,   # Accept higher costs to recover
            HealthTier.VULNERABLE: 1.5,   # Elevated priority for self
            HealthTier.STABLE: 1.0,       # Normal operation
            HealthTier.THRIVING: 0.75     # Be selective, save fees
        }[tier]
```

## cl-revenue-ops Changes

### Bridge: Add Health Queries

**File**: `/home/sat/bin/cl_revenue_ops/modules/hive_bridge.py`

Add to `HiveFeeIntelligenceBridge` class:

```python
def query_member_health(self, member_id: str = None) -> Optional[Dict[str, Any]]:
    """
    Query NNLB health score for a member.

    Information sharing only - used to adjust OWN rebalancing priorities.

    Args:
        member_id: Member to query (None for self)

    Returns:
        Health data dict or None if unavailable
    """
    if self._is_circuit_open() or not self.is_available():
        return None

    try:
        result = self.plugin.rpc.call("hive-member-health", {
            "member_id": member_id,
            "action": "query"
        })
        return result if not result.get("error") else None
    except Exception as e:
        self._log(f"Failed to query member health: {e}", level="debug")
        self._record_failure()
        return None

def query_fleet_health(self) -> Optional[Dict[str, Any]]:
    """Query aggregated fleet health for situational awareness."""
    if self._is_circuit_open() or not self.is_available():
        return None

    try:
        result = self.plugin.rpc.call("hive-member-health", {
            "member_id": "all",
            "action": "aggregate"
        })
        return result if not result.get("error") else None
    except Exception as e:
        self._log(f"Failed to query fleet health: {e}", level="debug")
        self._record_failure()
        return None

def report_health_update(
    self,
    profitable_channels: int,
    underwater_channels: int,
    stagnant_channels: int,
    revenue_trend: str
) -> bool:
    """
    Report our health status to cl-hive.

    Shares information so fleet knows our state.
    No sats move - purely informational.
    """
    if not self.is_available():
        return False

    try:
        self.plugin.rpc.call("hive-report-health", {
            "profitable_channels": profitable_channels,
            "underwater_channels": underwater_channels,
            "stagnant_channels": stagnant_channels,
            "revenue_trend": revenue_trend
        })
        return True
    except Exception as e:
        self._log(f"Failed to report health: {e}", level="debug")
        return False
```

### Rebalancer: NNLB Integration

**File**: `/home/sat/bin/cl_revenue_ops/modules/rebalancer.py`

Add constants:

```python
# ==========================================================================
# NNLB Health-Aware Rebalancing
# ==========================================================================
# Each node adjusts its OWN rebalancing based on its health tier.
# No sats transfer between nodes - purely local optimization.
ENABLE_NNLB_BUDGET_SCALING = True
DEFAULT_BUDGET_MULTIPLIER = 1.0

# Tier multipliers for OWN operations
NNLB_BUDGET_MULTIPLIERS = {
    "struggling": 2.0,    # Accept higher costs to recover own channels
    "vulnerable": 1.5,    # Elevated priority for own recovery
    "stable": 1.0,        # Normal operation
    "thriving": 0.75      # Be selective, save on routing fees
}

MIN_BUDGET_MULTIPLIER = 0.5
MAX_BUDGET_MULTIPLIER = 2.5
```

Add to `__init__`:

```python
def __init__(self, plugin: Plugin, config: Config, database: Database,
             clboss_manager: ClbossManager, sling_manager: Any = None,
             hive_bridge: Optional["HiveFeeIntelligenceBridge"] = None):
    # ... existing init ...
    self.hive_bridge = hive_bridge
    self._cached_health = None
    self._health_cache_time = 0
    self._health_cache_ttl = 300  # 5 minutes
```

New method:

```python
def _calculate_nnlb_budget_multiplier(self) -> float:
    """
    Calculate OUR rebalance budget multiplier based on OUR health.

    This adjusts how aggressively WE rebalance OUR OWN channels.
    No sats transfer to other nodes.
    """
    if not ENABLE_NNLB_BUDGET_SCALING or not self.hive_bridge:
        return DEFAULT_BUDGET_MULTIPLIER

    # Check cache
    now = time.time()
    if (self._cached_health is not None and
            now - self._health_cache_time < self._health_cache_ttl):
        return self._cached_health.get("budget_multiplier", DEFAULT_BUDGET_MULTIPLIER)

    # Query hive for OUR health
    health = self.hive_bridge.query_member_health()  # None = self
    if not health:
        return DEFAULT_BUDGET_MULTIPLIER

    # Cache result
    self._cached_health = health
    self._health_cache_time = now

    tier = health.get("health_tier", "stable")
    multiplier = NNLB_BUDGET_MULTIPLIERS.get(tier, DEFAULT_BUDGET_MULTIPLIER)

    self.plugin.log(
        f"NNLB: Our health tier={tier}, our budget_multiplier={multiplier:.2f}",
        level='debug'
    )

    return max(MIN_BUDGET_MULTIPLIER, min(MAX_BUDGET_MULTIPLIER, multiplier))
```

Integration in EV calculation:

```python
def _calculate_ev_rebalance(
    self,
    source_channel: Dict,
    sink_channel: Dict,
    amount_sats: int
) -> Tuple[float, Dict]:
    """Calculate expected value of a rebalance for OUR channels."""
    # ... existing EV calculation ...

    # Apply OUR NNLB budget multiplier to OUR acceptance threshold
    nnlb_multiplier = self._calculate_nnlb_budget_multiplier()

    # Adjust EV threshold based on OUR health
    # When struggling: accept lower EV (more willing to pay fees)
    # When thriving: require higher EV (be selective)
    adjusted_threshold = self.config.min_rebalance_ev / nnlb_multiplier

    if expected_value < adjusted_threshold:
        return expected_value, {
            "accepted": False,
            "reason": f"EV {expected_value:.2f} below our threshold {adjusted_threshold:.2f}",
            "nnlb_multiplier": nnlb_multiplier,
            "our_health_tier": self._cached_health.get("health_tier", "unknown")
        }

    # ... rest of calculation ...
```

## Files Summary (Phase 1)

| File | Changes | Lines |
|------|---------|-------|
| `/home/sat/bin/cl-hive/cl-hive.py` | Add `hive-member-health`, `hive-report-health` RPCs | ~80 |
| `/home/sat/bin/cl-hive/modules/database.py` | Add health tracking columns | ~40 |
| `/home/sat/bin/cl-hive/modules/health_aggregator.py` | **NEW** module | ~120 |
| `/home/sat/bin/cl_revenue_ops/modules/hive_bridge.py` | Add health query/report methods | ~70 |
| `/home/sat/bin/cl_revenue_ops/modules/rebalancer.py` | Add NNLB budget scaling | ~80 |
| `/home/sat/bin/cl_revenue_ops/modules/profitability.py` | Add health reporting | ~25 |

**Total Phase 1**: ~415 lines

---

# Phase 2: Liquidity Intelligence Sharing

## Goal
Nodes share *information* about their liquidity state so the fleet can make coordinated *individual* decisions. Each node still manages its own funds independently.

## What Coordination Means (Without Fund Transfer)

When Node A shares "I need outbound to Peer X":
- **Node B can adjust fees**: Lower fees toward Peer X to attract flow that routes *through* Node A
- **Node C can avoid conflict**: Delay rebalancing through Peer X to not compete with Node A
- **Planner awareness**: Prioritize opening channels that help the fleet, not just one node

When Node A shares "I have excess outbound to Peer Y":
- **Fee intelligence**: Others know Node A will likely lower fees to drain excess
- **Routing optimization**: Others can route *through* Node A's excess capacity
- **No fund transfer**: Node A keeps its sats, others just have better information

## cl-hive Changes

### Updated Module: `liquidity_coordinator.py`

The existing module needs clarification that it coordinates *information*, not fund transfers:

**File**: `/home/sat/bin/cl-hive/modules/liquidity_coordinator.py`

Update docstring at top:

```python
"""
Liquidity Coordinator Module

Coordinates INFORMATION SHARING about liquidity state between hive members.
Each node manages its own funds independently - no sats transfer between nodes.

Information shared:
- Which channels are depleted/saturated
- Which peers need more capacity
- Rebalancing activity (to avoid conflicts)

How this helps without fund transfer:
- Fee coordination: Adjust fees to direct public flow toward peers that help struggling members
- Conflict avoidance: Don't compete for same rebalance routes
- Topology planning: Open channels that benefit the fleet
"""
```

### New RPC: `hive-liquidity-state`

```python
@plugin.method("hive-liquidity-state")
def hive_liquidity_state(plugin, action="status"):
    """
    Query fleet liquidity state for coordination.

    INFORMATION ONLY - no sats move between nodes.

    Args:
        action: "status" (overview), "needs" (who needs what),
                "excess" (who has excess where)

    Returns for "status":
    {
        "active": True,
        "fleet_summary": {
            "members_with_depleted_channels": 2,
            "members_with_excess_outbound": 3,
            "common_bottleneck_peers": ["02abc...", "03xyz..."]
        },
        "our_state": {
            "depleted_channels": 1,
            "saturated_channels": 2,
            "balanced_channels": 5
        }
    }

    Returns for "needs":
    {
        "fleet_needs": [
            {
                "member_id": "02abc...",
                "need_type": "outbound",
                "peer_id": "03xyz...",   # External peer
                "severity": "high",       # How badly they need it
                "our_relevance": 0.8      # How much we could help via fees/routing
            }
        ]
    }
    """
```

### New RPC: `hive-report-liquidity-state`

```python
@plugin.method("hive-report-liquidity-state")
def hive_report_liquidity_state(
    plugin,
    depleted_channels: List[Dict],
    saturated_channels: List[Dict],
    rebalancing_active: bool = False,
    rebalancing_peers: List[str] = None
):
    """
    Report our liquidity state to the hive.

    INFORMATION SHARING - enables coordinated fee/rebalance decisions.
    No sats transfer.

    Args:
        depleted_channels: List of {peer_id, local_pct, capacity_sats}
        saturated_channels: List of {peer_id, local_pct, capacity_sats}
        rebalancing_active: Whether we're currently rebalancing
        rebalancing_peers: Which peers we're rebalancing through

    Returns:
        {"status": "reported"}
    """
```

## cl-revenue-ops Changes

### Bridge: Add Liquidity Intelligence

**File**: `/home/sat/bin/cl_revenue_ops/modules/hive_bridge.py`

```python
def query_fleet_liquidity_state(self) -> Optional[Dict[str, Any]]:
    """
    Query fleet liquidity state for coordinated decision-making.

    Information only - helps us make better decisions about
    our own rebalancing and fee adjustments.
    """
    if self._is_circuit_open() or not self.is_available():
        return None

    try:
        result = self.plugin.rpc.call("hive-liquidity-state", {
            "action": "status"
        })
        return result if not result.get("error") else None
    except Exception as e:
        self._log(f"Failed to query liquidity state: {e}", level="debug")
        return None

def query_fleet_liquidity_needs(self) -> List[Dict[str, Any]]:
    """
    Get fleet liquidity needs for coordination.

    Knowing what others need helps us:
    - Adjust our fees to direct flow helpfully
    - Avoid rebalancing through congested routes
    """
    if self._is_circuit_open() or not self.is_available():
        return []

    try:
        result = self.plugin.rpc.call("hive-liquidity-state", {
            "action": "needs"
        })
        return result.get("fleet_needs", []) if not result.get("error") else []
    except Exception as e:
        self._log(f"Failed to query fleet needs: {e}", level="debug")
        return []

def report_liquidity_state(
    self,
    depleted_channels: List[Dict],
    saturated_channels: List[Dict],
    rebalancing_active: bool = False,
    rebalancing_peers: List[str] = None
) -> bool:
    """
    Report our liquidity state to the fleet.

    Sharing this information helps the fleet make better
    coordinated decisions. No sats transfer.
    """
    if not self.is_available():
        return False

    try:
        self.plugin.rpc.call("hive-report-liquidity-state", {
            "depleted_channels": depleted_channels,
            "saturated_channels": saturated_channels,
            "rebalancing_active": rebalancing_active,
            "rebalancing_peers": rebalancing_peers or []
        })
        return True
    except Exception as e:
        self._log(f"Failed to report liquidity state: {e}", level="debug")
        return False
```

### Fee Controller: Fleet-Aware Fee Adjustments

**File**: `/home/sat/bin/cl_revenue_ops/modules/fee_controller.py`

```python
def _get_fleet_aware_fee_adjustment(
    self,
    peer_id: str,
    base_fee: int
) -> int:
    """
    Adjust fees considering fleet liquidity state.

    If a struggling member needs flow toward this peer,
    we might lower our fees slightly to help direct traffic.
    This is indirect help through the public network - no fund transfer.
    """
    if not self.hive_bridge:
        return base_fee

    fleet_needs = self.hive_bridge.query_fleet_liquidity_needs()
    if not fleet_needs:
        return base_fee

    # Check if any struggling member needs outbound to this peer
    for need in fleet_needs:
        if (need.get("peer_id") == peer_id and
            need.get("severity") == "high" and
            need.get("need_type") == "outbound"):

            # Slightly lower our fee to attract flow toward this peer
            # This routes through the network, potentially helping the struggling member
            adjusted = int(base_fee * 0.95)  # 5% reduction

            self.plugin.log(
                f"FLEET_AWARE: Lowering fee to {peer_id[:12]}... from {base_fee} to {adjusted} "
                f"(fleet member needs outbound)",
                level='debug'
            )
            return adjusted

    return base_fee
```

### Rebalancer: Conflict Avoidance

```python
def _check_rebalance_conflicts(self, target_peer: str) -> bool:
    """
    Check if another fleet member is actively rebalancing through this peer.

    Avoids competing for the same routes, which wastes fees.
    Information-based coordination - no fund transfer.
    """
    if not self.hive_bridge:
        return False  # No conflict info available

    fleet_state = self.hive_bridge.query_fleet_liquidity_state()
    if not fleet_state:
        return False

    # Check if others are rebalancing through this peer
    # Implementation would check rebalancing_peers from fleet reports
    return False  # Simplified - full implementation checks fleet state
```

## Files Summary (Phase 2)

| File | Changes | Lines |
|------|---------|-------|
| `/home/sat/bin/cl-hive/cl-hive.py` | Add `hive-liquidity-state` RPCs | ~80 |
| `/home/sat/bin/cl-hive/modules/liquidity_coordinator.py` | Update for info-only coordination | ~60 |
| `/home/sat/bin/cl_revenue_ops/modules/hive_bridge.py` | Add liquidity intelligence methods | ~80 |
| `/home/sat/bin/cl_revenue_ops/modules/fee_controller.py` | Add fleet-aware fee adjustment | ~40 |
| `/home/sat/bin/cl_revenue_ops/modules/rebalancer.py` | Add conflict avoidance | ~30 |

**Total Phase 2**: ~290 lines

---

# Phase 3: Splice Coordination

## Goal
Coordinate splice-out operations to prevent connectivity gaps. This is a *safety check* - no fund movement between nodes.

## How Splice Coordination Works

When Node A wants to splice-out from Peer X:
1. Node A asks cl-hive: "Is this safe for fleet connectivity?"
2. cl-hive checks: Does another member have capacity to Peer X?
3. Response options:
   - **Safe**: Other members have sufficient capacity, proceed
   - **Coordinate**: Wait for another member to open/splice-in to Peer X first
   - **Blocked**: Would create connectivity gap, don't proceed

**No sats transfer** - just timing coordination and safety checks.

## cl-hive Changes

### New Module: `splice_coordinator.py`

**File**: `/home/sat/bin/cl-hive/modules/splice_coordinator.py`

```python
"""
Splice Coordinator Module

Coordinates timing of splice operations to maintain fleet connectivity.
SAFETY CHECKS ONLY - no fund movement between nodes.

Each node manages its own splices independently, but checks with
the fleet before splice-out to avoid creating connectivity gaps.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# Safety levels
SPLICE_SAFE = "safe"
SPLICE_COORDINATE = "coordinate"  # Wait for another member to add capacity
SPLICE_BLOCKED = "blocked"         # Would break connectivity

# Minimum fleet capacity to maintain to any peer
MIN_FLEET_CAPACITY_PCT = 0.10  # 10% of peer's total


class SpliceCoordinator:
    """
    Coordinates splice timing to maintain fleet connectivity.

    Safety checks only - each node manages its own funds.
    """

    def __init__(self, database: Any, plugin: Any, state_manager: Any):
        self.database = database
        self.plugin = plugin
        self.state_manager = state_manager

    def check_splice_out_safety(
        self,
        peer_id: str,
        amount_sats: int
    ) -> Dict[str, Any]:
        """
        Check if splice-out is safe for fleet connectivity.

        SAFETY CHECK ONLY - no fund movement.

        Args:
            peer_id: External peer we're splicing from
            amount_sats: Amount to splice out

        Returns:
            Safety assessment with recommendation
        """
        # Get current fleet capacity to this peer
        fleet_capacity = self._get_fleet_capacity_to_peer(peer_id)
        our_capacity = self._get_our_capacity_to_peer(peer_id)
        peer_total = self._get_peer_total_capacity(peer_id)

        if peer_total == 0:
            return {
                "safety": SPLICE_SAFE,
                "reason": "Unknown peer, proceed with local decision"
            }

        current_share = fleet_capacity / peer_total if peer_total > 0 else 0
        new_fleet_capacity = fleet_capacity - amount_sats
        new_share = new_fleet_capacity / peer_total if peer_total > 0 else 0

        # Check if we'd maintain minimum connectivity
        if new_share >= MIN_FLEET_CAPACITY_PCT:
            return {
                "safety": SPLICE_SAFE,
                "reason": f"Post-splice fleet share {new_share:.1%} above minimum",
                "fleet_capacity": fleet_capacity,
                "new_fleet_capacity": new_fleet_capacity,
                "fleet_share": current_share,
                "new_share": new_share
            }

        # Check if other members have capacity
        other_member_capacity = fleet_capacity - our_capacity
        if other_member_capacity > 0:
            return {
                "safety": SPLICE_SAFE,
                "reason": f"Other members have {other_member_capacity} sats to this peer",
                "other_member_capacity": other_member_capacity
            }

        # Would create connectivity gap
        return {
            "safety": SPLICE_BLOCKED,
            "reason": f"Would drop fleet share to {new_share:.1%}, breaking connectivity",
            "recommendation": "Another member should open channel to this peer first",
            "fleet_capacity": fleet_capacity,
            "new_share": new_share
        }

    def _get_fleet_capacity_to_peer(self, peer_id: str) -> int:
        """Get total fleet capacity to an external peer."""
        total = 0
        members = self.database.get_all_members()

        for member in members:
            member_state = self.state_manager.get_member_state(member["peer_id"])
            if member_state:
                for ch in member_state.get("channels", []):
                    if ch.get("peer_id") == peer_id:
                        total += ch.get("capacity_sats", 0)

        return total

    def _get_our_capacity_to_peer(self, peer_id: str) -> int:
        """Get our capacity to an external peer."""
        try:
            channels = self.plugin.rpc.listpeerchannels(id=peer_id)
            return sum(
                ch.get("total_msat", 0) // 1000
                for ch in channels.get("channels", [])
            )
        except Exception:
            return 0

    def _get_peer_total_capacity(self, peer_id: str) -> int:
        """Get external peer's total public capacity."""
        try:
            channels = self.plugin.rpc.listchannels(source=peer_id)
            return sum(
                ch.get("amount_msat", 0) // 1000
                for ch in channels.get("channels", [])
            )
        except Exception:
            return 0
```

### New RPC: `hive-splice-check`

**File**: `/home/sat/bin/cl-hive/cl-hive.py`

```python
@plugin.method("hive-splice-check")
def hive_splice_check(
    plugin,
    peer_id: str,
    splice_type: str,
    amount_sats: int
):
    """
    Check if a splice operation is safe for fleet connectivity.

    SAFETY CHECK ONLY - no fund movement between nodes.
    Each node manages its own splices.

    Returns:
        Safety assessment with recommendation
    """
    if splice_type == "splice_in":
        return {
            "safety": "safe",
            "reason": "Splice-in always safe (increases capacity)"
        }

    return splice_coordinator.check_splice_out_safety(peer_id, amount_sats)
```

## cl-revenue-ops Changes

### Bridge: Add Splice Check

**File**: `/home/sat/bin/cl_revenue_ops/modules/hive_bridge.py`

```python
def check_splice_safety(
    self,
    peer_id: str,
    splice_type: str,
    amount_sats: int
) -> Dict[str, Any]:
    """
    Check if a splice operation is safe for fleet connectivity.

    SAFETY CHECK ONLY - no fund movement.
    We manage our own splice, just checking if timing is safe.
    """
    if not self.is_available():
        # Default to safe if hive unavailable (fail open)
        return {
            "safe": True,
            "safety_level": "safe",
            "reason": "Hive unavailable, local decision",
            "can_proceed": True
        }

    try:
        result = self.plugin.rpc.call("hive-splice-check", {
            "peer_id": peer_id,
            "splice_type": splice_type,
            "amount_sats": amount_sats
        })

        safety = result.get("safety", "safe")
        return {
            "safe": safety == "safe",
            "safety_level": safety,
            "reason": result.get("reason", ""),
            "can_proceed": safety != "blocked",
            "recommendation": result.get("recommendation"),
            "fleet_share": result.get("fleet_share"),
            "new_share": result.get("new_share")
        }

    except Exception as e:
        self._log(f"Splice safety check failed: {e}", level="debug")
        return {
            "safe": True,
            "safety_level": "safe",
            "reason": f"Check failed, local decision",
            "can_proceed": True
        }
```

## MCP Exposure

### New Tool: `hive_splice_check`

**File**: `/home/sat/bin/cl-hive/tools/mcp-hive-server.py`

```python
@server.tool()
async def hive_splice_check(
    node: str,
    peer_id: str,
    splice_type: str,
    amount_sats: int
) -> Dict:
    """
    Check if a splice operation is safe for fleet connectivity.

    Safety check only - each node manages its own funds.
    Use before recommending splice-out operations.

    Returns:
        Safety assessment with fleet capacity analysis
    """
```

### New Tool: `hive_liquidity_intelligence`

```python
@server.tool()
async def hive_liquidity_intelligence(node: str) -> Dict:
    """
    Get fleet liquidity intelligence for coordinated decisions.

    Information sharing only - no fund movement between nodes.
    Shows which members need what, enabling coordinated fee/rebalance decisions.

    Returns:
        Fleet liquidity state and coordination opportunities
    """
```

## Files Summary (Phase 3)

| File | Changes | Lines |
|------|---------|-------|
| `/home/sat/bin/cl-hive/modules/splice_coordinator.py` | **NEW** module | ~130 |
| `/home/sat/bin/cl-hive/cl-hive.py` | Add `hive-splice-check` RPC | ~25 |
| `/home/sat/bin/cl_revenue_ops/modules/hive_bridge.py` | Add `check_splice_safety()` | ~50 |
| `/home/sat/bin/cl-hive/tools/mcp-hive-server.py` | Add MCP tools | ~60 |

**Total Phase 3**: ~265 lines

---

# Summary

## Total Implementation Scope

| Phase | Description | Lines |
|-------|-------------|-------|
| 1 | NNLB-Aware Rebalancing | ~415 |
| 2 | Liquidity Intelligence Sharing | ~290 |
| 3 | Splice Coordination | ~265 |

**Grand Total**: ~970 lines

## Critical Design Principles

### Node Balance Separation
- **NEVER** transfer sats between nodes to "help" each other
- Each node manages its own funds completely independently
- Coordination is purely informational

### How Coordination Helps Without Fund Transfer

| Mechanism | What's Shared | How It Helps |
|-----------|--------------|--------------|
| Health scores | Profitability metrics | Nodes know who is struggling |
| Liquidity state | Which channels are depleted | Fee coordination to direct flow |
| Rebalancing activity | Who is rebalancing where | Avoid competing for routes |
| Splice checks | Capacity to peers | Prevent connectivity gaps |

### Indirect Assistance Through Network Effects

When Node A struggles with Peer X, Node B can help *indirectly* by:
1. Lowering fees toward Peer X → attracts public flow → some routes through Node A
2. Not rebalancing through Peer X → less fee competition → Node A's rebalance succeeds
3. Opening a channel to Peer X → provides alternative route → reduces pressure on Node A

**None of these involve Node B giving sats to Node A.**

## Verification Checklist

- [ ] No RPC moves sats between nodes
- [ ] All "help" is through fee/routing coordination
- [ ] Splice checks are advisory only
- [ ] Each node can operate independently if hive unavailable
- [ ] Health reports contain only observable metrics, not fund requests

## Security Considerations

- No fund movement RPCs exist
- Rate limit all state reports
- Validate all gossip signatures
- Fail-open for local autonomy
- Cannot spoof health scores (derived from verifiable data)
- Splice checks are advisory, not mandatory
