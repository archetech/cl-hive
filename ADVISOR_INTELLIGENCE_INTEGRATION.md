# Advisor Intelligence Integration Guide

This document describes the full suite of intelligence gathering systems integrated into the proactive advisor cycle in cl-hive.

## Current State (v2.0 - Fully Integrated)

The proactive advisor now uses **all available intelligence sources** via comprehensive data gathering in `_analyze_node_state()` and 15 parallel opportunity scanners.

### Core Intelligence (Always Gathered)

| Tool | Purpose |
|------|---------|
| `hive_node_info` | Basic node information |
| `hive_channels` | Channel list and balances |
| `revenue_dashboard` | Financial health metrics |
| `revenue_profitability` | Channel profitability analysis |
| `advisor_get_context_brief` | Context and trend summary |
| `advisor_get_velocities` | Critical velocity alerts |

## Integrated Intelligence Systems

### 1. Fee Coordination (Phase 2) - Fleet-Wide Fee Intelligence ✅

These tools enable coordinated fee decisions across the hive:

| Tool | Purpose | Integration Status |
|------|---------|---------------------|
| `fee_coordination_status` | Comprehensive coordination status | ✅ Gathered in `_analyze_node_state()` |
| `coord_fee_recommendation` | Get coordinated fee for a channel | ✅ Available via MCP |
| `pheromone_levels` | Learned successful fee levels | ✅ Gathered in `_analyze_node_state()` |
| `stigmergic_markers` | Route markers from hive members | ✅ Available via MCP |
| `defense_status` | Mycelium warning system status | ✅ Gathered + scanned via `_scan_defense_warnings()` |

**Integration Points (Implemented):**
- `_scan_defense_warnings()`: Checks `defense_status` for peer warnings
- `_analyze_node_state()`: Gathers `fee_coordination`, `pheromone_levels`, `defense_status`
- MCP tools available for on-demand coordinated fee recommendations

### 2. Fleet Competition Intelligence ✅

Prevent hive members from competing against each other:

| Tool | Purpose | Integration Status |
|------|---------|---------------------|
| `internal_competition` | Detect competing members | ✅ Gathered + scanned via `_scan_internal_competition()` |
| `corridor_assignments` | See who "owns" which routes | ✅ Available via MCP |
| `routing_stats` | Aggregated hive routing data | ✅ Available via MCP |
| `accumulated_warnings` | Collective peer warnings | ✅ Available via MCP |
| `ban_candidates` | Peers warranting auto-ban | ✅ Gathered + scanned via `_scan_ban_candidates()` |

**Integration Points (Implemented):**
- `_scan_internal_competition()`: Detects fee conflicts with fleet members
- `_scan_ban_candidates()`: Flags peers for removal based on collective warnings
- `_analyze_node_state()`: Gathers `internal_competition` and `ban_candidates`

### 3. Cost Reduction (Phase 3) ✅

Minimize operational costs:

| Tool | Purpose | Integration Status |
|------|---------|---------------------|
| `rebalance_recommendations` | Predictive rebalance suggestions | ✅ Gathered + scanned via `_scan_rebalance_recommendations()` |
| `fleet_rebalance_path` | Internal fleet rebalance routes | ✅ Available via MCP |
| `circular_flow_status` | Detect wasteful circular patterns | ✅ Gathered + scanned via `_scan_circular_flows()` |
| `cost_reduction_status` | Overall cost reduction summary | ✅ Available via MCP |

**Integration Points (Implemented):**
- `_scan_rebalance_recommendations()`: Creates opportunities from predictive suggestions
- `_scan_circular_flows()`: Detects and flags wasteful circular patterns
- `_analyze_node_state()`: Gathers `rebalance_recommendations` and `circular_flows`

### 4. Strategic Positioning (Phase 4) ✅

Optimize channel topology for maximum routing value:

| Tool | Purpose | Integration Status |
|------|---------|---------------------|
| `valuable_corridors` | High-value routing corridors | ✅ Available via MCP |
| `exchange_coverage` | Priority exchange connectivity | ✅ Available via MCP |
| `positioning_recommendations` | Where to open channels | ✅ Scanned via `_scan_positioning_opportunities()` |
| `flow_recommendations` | Physarum lifecycle actions | ✅ Gathered in `_analyze_node_state()` |
| `positioning_summary` | Strategic positioning overview | ✅ Gathered in `_analyze_node_state()` |

**Integration Points (Implemented):**
- `_scan_positioning_opportunities()`: Creates opportunities from positioning recommendations
- `_analyze_node_state()`: Gathers `positioning`, `yield_summary`, `flow_recommendations`
- Flow recommendations used to identify channels for closure/strengthening

### 5. Channel Rationalization ✅

Eliminate redundant channels across the fleet:

| Tool | Purpose | Integration Status |
|------|---------|---------------------|
| `coverage_analysis` | Detect redundant channels | ✅ Available via MCP |
| `close_recommendations` | Which redundant channels to close | ✅ Scanned via `_scan_rationalization()` |
| `rationalization_summary` | Fleet coverage health | ✅ Available via MCP |

**Integration Points (Implemented):**
- `_scan_rationalization()`: Creates opportunities for redundant channel closure
- Close recommendations consulted for data-driven closure decisions

### 6. Anticipatory Intelligence (Phase 7.1) ✅

Predict future liquidity needs:

| Tool | Purpose | Integration Status |
|------|---------|---------------------|
| `anticipatory_status` | Pattern detection state | ✅ Available via MCP |
| `detect_patterns` | Temporal flow patterns | ✅ Available via MCP |
| `predict_liquidity` | Per-channel state prediction | ✅ Available via MCP |
| `anticipatory_predictions` | All at-risk channels | ✅ Gathered + scanned via `_scan_anticipatory_liquidity()` |

**Integration Points (Implemented):**
- `_scan_anticipatory_liquidity()`: Creates opportunities from at-risk channel predictions
- `_analyze_node_state()`: Gathers `anticipatory` predictions and `critical_velocity`

### 7. Time-Based Optimization (Phase 7.4) ✅

Optimize fees based on temporal patterns:

| Tool | Purpose | Integration Status |
|------|---------|---------------------|
| `time_fee_status` | Current temporal fee state | ✅ Available via MCP |
| `time_fee_adjustment` | Get time-optimal fee for channel | ✅ Scanned via `_scan_time_based_fees()` |
| `time_peak_hours` | Detected high-activity hours | ✅ Available via MCP |
| `time_low_hours` | Detected low-activity hours | ✅ Available via MCP |

**Integration Points (Implemented):**
- `_scan_time_based_fees()`: Creates opportunities for temporal fee adjustments
- Time-based fee configuration gathered via `fee_coordination_status`

### 8. Competitor Intelligence ✅

Understand competitive landscape:

| Tool | Purpose | Integration Status |
|------|---------|---------------------|
| `competitor_analysis` | Compare fees to competitors | ✅ Scanned via `_scan_competitor_opportunities()` |

**Integration Points (Implemented):**
- `_scan_competitor_opportunities()`: Creates opportunities for undercut/premium fee adjustments
- Competitive positioning factored into opportunity scoring

### 9. Yield Optimization ✅

Maximize return on capital:

| Tool | Purpose | Integration Status |
|------|---------|---------------------|
| `yield_metrics` | Per-channel ROI, efficiency | ✅ Available via MCP |
| `yield_summary` | Fleet-wide yield analysis | ✅ Gathered in `_analyze_node_state()` |
| `critical_velocity` | Channels at velocity risk | ✅ Gathered in `_analyze_node_state()` |

**Integration Points (Implemented):**
- `_analyze_node_state()`: Gathers `yield_summary` and `critical_velocity`
- Yield metrics available via MCP for ROI-based analysis

---

### 10. New Member Onboarding ✅

Suggest strategic channel openings when new members join:

| Tool | Purpose | Integration Status |
|------|---------|---------------------|
| `hive_members` | Get hive membership list | ✅ Gathered in `_analyze_node_state()` |
| `positioning_summary` | Strategic targets for new members | ✅ Scanned via `_scan_new_member_opportunities()` |
| `hive_onboard_new_members` | Standalone onboarding check | ✅ Independent MCP tool |

**Integration Points (Implemented):**
- `_scan_new_member_opportunities()`: Scans during advisor cycles
- `hive_onboard_new_members`: **Standalone MCP tool** - runs independently of advisor
- Suggests existing members open channels TO new members
- Suggests strategic targets FOR new members to improve fleet coverage
- Tracks onboarded members via `mark_member_onboarded()` to avoid repeating suggestions

**Standalone Usage:**
```bash
# Run via MCP independently of advisor cycle
hive_onboard_new_members node=hive-nexus-01

# Dry run to preview without creating actions
hive_onboard_new_members node=hive-nexus-01 dry_run=true

# Can be run hourly via cron independent of 3-hour advisor cycle
```

---

## All 15 Opportunity Scanners (Implemented)

The `OpportunityScanner` runs these 15 scanners in parallel:

| Scanner | Purpose | Data Source |
|---------|---------|-------------|
| `_scan_velocity_alerts` | Critical depletion/saturation | `velocities` |
| `_scan_profitability` | Underwater/stagnant channels | `profitability` |
| `_scan_time_based_fees` | Temporal fee optimization | `fee_coordination` |
| `_scan_anticipatory_liquidity` | Predictive liquidity risks | `anticipatory` |
| `_scan_imbalanced_channels` | Balance ratio issues | `channels` |
| `_scan_config_opportunities` | Configuration tuning | `dashboard` |
| `_scan_defense_warnings` | Peer threat detection | `defense_status` |
| `_scan_internal_competition` | Fleet fee conflicts | `internal_competition` |
| `_scan_circular_flows` | Wasteful circular patterns | `circular_flows` |
| `_scan_rebalance_recommendations` | Proactive rebalancing | `rebalance_recommendations` |
| `_scan_positioning_opportunities` | Strategic channel opens | `positioning` |
| `_scan_competitor_opportunities` | Market fee positioning | `competitor_analysis` |
| `_scan_rationalization` | Redundant channel closure | `close_recommendations` |
| `_scan_ban_candidates` | Peer removal candidates | `ban_candidates` |
| `_scan_new_member_opportunities` | New member channel suggestions | `hive_members`, `positioning` |

---

## Current Implementation

The `_analyze_node_state()` function in `proactive_advisor.py` now gathers all intelligence:

```python
async def _analyze_node_state(self, node_name: str) -> Dict[str, Any]:
    """Comprehensive node state analysis with full intelligence gathering."""
    results = {}

    # ==== CORE DATA ====
    results["node_info"] = await self.mcp.call("hive_node_info", {"node": node_name})
    results["channels"] = await self.mcp.call("hive_channels", {"node": node_name})
    results["dashboard"] = await self.mcp.call("revenue_dashboard", {"node": node_name})
    results["profitability"] = await self.mcp.call("revenue_profitability", {"node": node_name})
    results["context"] = await self.mcp.call("advisor_get_context_brief", {"days": 7})
    results["velocities"] = await self.mcp.call("advisor_get_velocities", {"hours_threshold": 24})

    # ==== FLEET COORDINATION INTELLIGENCE (Phase 2) ====
    results["defense_status"] = await self.mcp.call("defense_status", {"node": node_name})
    results["internal_competition"] = await self.mcp.call("internal_competition", {"node": node_name})
    results["fee_coordination"] = await self.mcp.call("fee_coordination_status", {"node": node_name})
    results["pheromone_levels"] = await self.mcp.call("pheromone_levels", {"node": node_name})

    # ==== PREDICTIVE INTELLIGENCE (Phase 7.1) ====
    results["anticipatory"] = await self.mcp.call("anticipatory_predictions", {
        "node": node_name, "min_risk": 0.3, "hours_ahead": 24
    })
    results["critical_velocity"] = await self.mcp.call("critical_velocity", {
        "node": node_name, "threshold_hours": 24
    })

    # ==== STRATEGIC POSITIONING (Phase 4) ====
    results["positioning"] = await self.mcp.call("positioning_summary", {"node": node_name})
    results["yield_summary"] = await self.mcp.call("yield_summary", {"node": node_name})
    results["flow_recommendations"] = await self.mcp.call("flow_recommendations", {"node": node_name})

    # ==== COST REDUCTION (Phase 3) ====
    results["rebalance_recommendations"] = await self.mcp.call("rebalance_recommendations", {"node": node_name})
    results["circular_flows"] = await self.mcp.call("circular_flow_status", {"node": node_name})

    # ==== COLLECTIVE WARNINGS ====
    results["ban_candidates"] = await self.mcp.call("ban_candidates", {"node": node_name})

    return results
```

All calls include error handling to gracefully degrade if any intelligence source is unavailable.

---

## AI-Driven Decision Making (Current Workflow)

The `advisor_run_cycle` MCP tool executes this complete workflow automatically:

### 1. State Recording
```
advisor_record_snapshot - Record current state for historical tracking
```

### 2. Comprehensive Intelligence Gathering
```
_analyze_node_state() gathers ALL intelligence sources:
- Core: node_info, channels, dashboard, profitability, context, velocities
- Fleet: defense_status, internal_competition, fee_coordination, pheromone_levels
- Predictive: anticipatory_predictions, critical_velocity
- Strategic: positioning, yield_summary, flow_recommendations
- Cost: rebalance_recommendations, circular_flows
- Warnings: ban_candidates
```

### 3. Opportunity Scanning (14 parallel scanners)
```
OpportunityScanner.scan_all() runs all 14 scanners in parallel,
creating scored Opportunity objects from each intelligence source
```

### 4. Goal-Aware Scoring
```
Opportunities scored with learning adjustments based on:
- Past decision outcomes
- Current goal progress
- Action type confidence
```

### 5. Action Execution
```
- Safe actions auto-executed within daily budget
- Risky actions queued for approval
- All decisions logged for learning
```

### 6. Outcome Measurement
```
advisor_measure_outcomes - Evaluate decisions from 6-24h ago
Results feed back into learning system
```

---

## Configuration for Multi-Node AI Advisor

The production config (`nodes.production.json`) now supports mixed-mode operation:

```json
{
  "mode": "rest",
  "nodes": [
    {
      "name": "mainnet",
      "rest_url": "https://10.8.0.1:3010",
      "rune": "...",
      "ca_cert": null
    },
    {
      "name": "neophyte",
      "mode": "docker",
      "docker_container": "cl-hive-node",
      "lightning_dir": "/data/lightning/bitcoin",
      "network": "bitcoin"
    }
  ]
}
```

This allows the AI advisor to manage both REST-connected and docker-exec connected nodes in the same session.

---

## Summary

All cl-hive intelligence systems are now **fully integrated** into the proactive advisor:

| Capability | Status | Implementation |
|------------|--------|----------------|
| Coordinated decisions | ✅ Complete | Fleet-wide intelligence gathered every cycle |
| Anticipate problems | ✅ Complete | `anticipatory_predictions` + `critical_velocity` |
| Minimize costs | ✅ Complete | `fleet_rebalance_path` + `circular_flow_status` |
| Strategic positioning | ✅ Complete | `positioning_summary` + `flow_recommendations` |
| Avoid bad actors | ✅ Complete | `defense_status` + `ban_candidates` |
| Learn continuously | ✅ Complete | Pheromone levels + outcome measurement |
| Onboard new members | ✅ Complete | `hive_members` + strategic channel suggestions |

### Key Files

| File | Purpose |
|------|---------|
| `tools/proactive_advisor.py` | Main advisor with `_analyze_node_state()` |
| `tools/opportunity_scanner.py` | 14 parallel opportunity scanners |
| `tools/mcp-hive-server.py` | MCP server exposing all tools |

### Running the Advisor

```bash
# Via MCP (recommended)
advisor_run_cycle node=hive-nexus-01

# Or run on all nodes
advisor_run_cycle_all
```

The advisor automatically gathers all intelligence, scans for opportunities, executes safe actions, and queues risky ones for approval.
