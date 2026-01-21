# Advisor Intelligence Integration Guide

This document describes how to enhance the proactive advisor cycle with the full suite of intelligence gathering systems available in cl-hive.

## Current State (v1.0)

The proactive advisor currently uses a limited set of intelligence sources:

| Tool | Purpose |
|------|---------|
| `hive_node_info` | Basic node information |
| `hive_channels` | Channel list and balances |
| `revenue_dashboard` | Financial health metrics |
| `revenue_profitability` | Channel profitability analysis |
| `advisor_get_context_brief` | Context and trend summary |
| `advisor_get_velocities` | Critical velocity alerts |

## Available Intelligence Systems (Not Yet Integrated)

### 1. Fee Coordination (Phase 2) - Fleet-Wide Fee Intelligence

These tools enable coordinated fee decisions across the hive:

| Tool | Purpose | Integration Benefit |
|------|---------|---------------------|
| `fee_coordination_status` | Comprehensive coordination status | Understand fleet-wide fee landscape |
| `coord_fee_recommendation` | Get coordinated fee for a channel | Use instead of manual fee calculations |
| `pheromone_levels` | Learned successful fee levels | Apply proven fees from past success |
| `stigmergic_markers` | Route markers from hive members | Benefit from collective routing experience |
| `defense_status` | Mycelium warning system status | Avoid bad peers identified by fleet |

**Integration Points:**
- In `_scan_profitability()`: Check `defense_status` for peer warnings before recommending actions
- In `_execute_fee_change()`: Use `coord_fee_recommendation` for fee decisions
- In `_analyze_node_state()`: Include `pheromone_levels` for fee context

### 2. Fleet Competition Intelligence

Prevent hive members from competing against each other:

| Tool | Purpose | Integration Benefit |
|------|---------|---------------------|
| `internal_competition` | Detect competing members | Avoid counterproductive fee wars |
| `corridor_assignments` | See who "owns" which routes | Defer to corridor owner on fee decisions |
| `routing_stats` | Aggregated hive routing data | Learn from collective routing patterns |
| `accumulated_warnings` | Collective peer warnings | Automatically avoid flagged peers |
| `ban_candidates` | Peers warranting auto-ban | Proactively address malicious actors |

**Integration Points:**
- In `scan_all()`: Check `internal_competition` before proposing fee changes
- In `_scan_profitability()`: Cross-reference `accumulated_warnings` with bleeder channels
- Add new scanner: `_scan_ban_candidates()` to flag peers for removal

### 3. Cost Reduction (Phase 3)

Minimize operational costs:

| Tool | Purpose | Integration Benefit |
|------|---------|---------------------|
| `rebalance_recommendations` | Predictive rebalance suggestions | Proactive vs reactive rebalancing |
| `fleet_rebalance_path` | Internal fleet rebalance routes | Lower-cost rebalancing via hive members |
| `circular_flow_status` | Detect wasteful circular patterns | Eliminate fee-burning circular flows |
| `cost_reduction_status` | Overall cost reduction summary | Track cost optimization progress |

**Integration Points:**
- In `_scan_velocity_alerts()`: Use `rebalance_recommendations` for better suggestions
- In `_execute_rebalance()`: Check `fleet_rebalance_path` first for cheaper routes
- Add new scanner: `_scan_circular_flows()` to detect and break circular patterns

### 4. Strategic Positioning (Phase 4)

Optimize channel topology for maximum routing value:

| Tool | Purpose | Integration Benefit |
|------|---------|---------------------|
| `valuable_corridors` | High-value routing corridors | Target profitable routes |
| `exchange_coverage` | Priority exchange connectivity | Ensure critical liquidity paths |
| `positioning_recommendations` | Where to open channels | Strategic expansion decisions |
| `flow_recommendations` | Physarum lifecycle actions | Channel strengthen/atrophy guidance |
| `positioning_summary` | Strategic positioning overview | Comprehensive topology assessment |

**Integration Points:**
- Add new scanner: `_scan_positioning_opportunities()` for topology improvements
- In `_plan_next_cycle()`: Include positioning recommendations in priorities
- Use `flow_recommendations` to identify channels for closure/strengthening

### 5. Channel Rationalization

Eliminate redundant channels across the fleet:

| Tool | Purpose | Integration Benefit |
|------|---------|---------------------|
| `coverage_analysis` | Detect redundant channels | Identify duplicate coverage |
| `close_recommendations` | Which redundant channels to close | Data-driven closure decisions |
| `rationalization_summary` | Fleet coverage health | Track rationalization progress |

**Integration Points:**
- Add new scanner: `_scan_rationalization()` for redundant channel detection
- When evaluating channel closures, consult `close_recommendations`

### 6. Anticipatory Intelligence (Phase 7.1)

Predict future liquidity needs:

| Tool | Purpose | Integration Benefit |
|------|---------|---------------------|
| `anticipatory_status` | Pattern detection state | Understand prediction confidence |
| `detect_patterns` | Temporal flow patterns | Learn recurring flow behaviors |
| `predict_liquidity` | Per-channel state prediction | Anticipate depletion/saturation |
| `anticipatory_predictions` | All at-risk channels | Comprehensive risk assessment |

**Integration Points:**
- In `_scan_anticipatory_liquidity()`: Use `anticipatory_predictions` instead of just context
- Add pattern detection to `_analyze_node_state()` for richer context

### 7. Time-Based Optimization (Phase 7.4)

Optimize fees based on temporal patterns:

| Tool | Purpose | Integration Benefit |
|------|---------|---------------------|
| `time_fee_status` | Current temporal fee state | Understand active adjustments |
| `time_fee_adjustment` | Get time-optimal fee for channel | Dynamic fee recommendations |
| `time_peak_hours` | Detected high-activity hours | Know when to charge premium |
| `time_low_hours` | Detected low-activity hours | Know when to discount |

**Integration Points:**
- In `_scan_time_based_fees()`: Use `time_fee_adjustment` for better recommendations
- In `_execute_fee_change()`: Apply temporal modifiers automatically

### 8. Competitor Intelligence

Understand competitive landscape:

| Tool | Purpose | Integration Benefit |
|------|---------|---------------------|
| `competitor_analysis` | Compare fees to competitors | Market-aware fee setting |

**Integration Points:**
- Add new scanner: `_scan_competitor_opportunities()` for undercut/premium opportunities
- In `_score_opportunities()`: Factor in competitive positioning

### 9. Yield Optimization

Maximize return on capital:

| Tool | Purpose | Integration Benefit |
|------|---------|---------------------|
| `yield_metrics` | Per-channel ROI, efficiency | Identify best/worst performers |
| `yield_summary` | Fleet-wide yield analysis | Overall performance tracking |
| `critical_velocity` | Channels at velocity risk | Urgent attention list |

**Integration Points:**
- In `_analyze_node_state()`: Include `yield_summary` for comprehensive view
- Use `yield_metrics` for ROI-based opportunity scoring

---

## Recommended Integration Priority

### Phase 1: Critical Intelligence (Immediate Value)
1. **`defense_status` + `accumulated_warnings`** - Avoid known-bad peers
2. **`coord_fee_recommendation`** - Use coordinated fees instead of manual calc
3. **`anticipatory_predictions`** - Better liquidity prediction
4. **`critical_velocity`** - Comprehensive velocity monitoring

### Phase 2: Cost Optimization
5. **`fleet_rebalance_path`** - Cheaper rebalancing
6. **`circular_flow_status`** - Eliminate waste
7. **`rebalance_recommendations`** - Proactive rebalancing

### Phase 3: Strategic Value
8. **`competitor_analysis`** - Market positioning
9. **`internal_competition`** - Fleet harmony
10. **`positioning_recommendations`** - Strategic growth

### Phase 4: Advanced Optimization
11. **`time_fee_adjustment`** - Temporal optimization
12. **`pheromone_levels`** - Learned fee memory
13. **`flow_recommendations`** - Physarum lifecycle
14. **`close_recommendations`** - Channel rationalization

---

## Implementation Example

Here's how to enhance `_analyze_node_state()` with more intelligence:

```python
async def _analyze_node_state(self, node_name: str) -> Dict[str, Any]:
    """Comprehensive node state analysis with full intelligence."""
    results = {}

    # Current data gathering...
    results["node_info"] = await self.mcp.call("hive_node_info", {"node": node_name})
    results["channels"] = await self.mcp.call("hive_channels", {"node": node_name})
    results["dashboard"] = await self.mcp.call("revenue_dashboard", {"node": node_name})
    results["profitability"] = await self.mcp.call("revenue_profitability", {"node": node_name})
    results["context"] = await self.mcp.call("advisor_get_context_brief", {"days": 7})
    results["velocities"] = await self.mcp.call("advisor_get_velocities", {"hours_threshold": 24})

    # NEW: Fleet coordination intelligence
    results["fee_coordination"] = await self.mcp.call("fee_coordination_status", {"node": node_name})
    results["defense_status"] = await self.mcp.call("defense_status", {"node": node_name})
    results["internal_competition"] = await self.mcp.call("internal_competition", {"node": node_name})

    # NEW: Predictive intelligence
    results["anticipatory"] = await self.mcp.call("anticipatory_predictions", {
        "node": node_name, "min_risk": 0.3, "hours_ahead": 24
    })
    results["critical_velocity"] = await self.mcp.call("critical_velocity", {
        "node": node_name, "threshold_hours": 24
    })

    # NEW: Strategic positioning
    results["positioning"] = await self.mcp.call("positioning_summary", {"node": node_name})
    results["yield_summary"] = await self.mcp.call("yield_summary", {"node": node_name})

    # NEW: Cost reduction
    results["rebalance_recs"] = await self.mcp.call("rebalance_recommendations", {"node": node_name})
    results["circular_flows"] = await self.mcp.call("circular_flow_status", {"node": node_name})

    # NEW: Collective warnings
    results["ban_candidates"] = await self.mcp.call("ban_candidates", {"node": node_name})
    results["accumulated_warnings"] = await self.mcp.call("accumulated_warnings", {"node": node_name})

    # Calculate enhanced summary...
    return {
        "summary": {...},
        **results
    }
```

---

## AI-Driven Decision Making Enhancement

When using Claude or another AI advisor via MCP, the following workflow maximizes intelligence utilization:

### Pre-Cycle Context Gathering
```
1. advisor_record_snapshot - Record current state
2. advisor_get_context_brief - Get trend summary with velocity alerts
3. defense_status - Check for active warnings
4. ban_candidates - Any peers needing attention?
5. internal_competition - Any fleet conflicts?
```

### Per-Channel Analysis
```
For each channel needing attention:
1. coord_fee_recommendation - Get coordinated fee suggestion
2. predict_liquidity - Predict future state
3. time_fee_adjustment - Get time-optimal fee
4. yield_metrics - Check ROI and efficiency
5. competitor_analysis - Market positioning
```

### Action Selection
```
For rebalancing decisions:
1. fleet_rebalance_path - Check for internal fleet route first
2. rebalance_recommendations - Get proactive recommendations
3. circular_flow_status - Avoid creating circular waste

For channel operations:
1. close_recommendations - Check rationalization guidance
2. flow_recommendations - Physarum lifecycle state
3. positioning_recommendations - Strategic value assessment
```

### Post-Cycle Learning
```
1. advisor_record_decision - Log what was decided
2. advisor_measure_outcomes - Measure past decisions (6-24h ago)
3. Record any alerts via advisor_record_alert
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

The cl-hive intelligence systems provide rich data for AI-driven decision making. By integrating all available tools, the advisor can:

1. **Make coordinated decisions** - Use fleet-wide intelligence instead of isolated analysis
2. **Anticipate problems** - Predict liquidity issues before they occur
3. **Minimize costs** - Use internal fleet routes and avoid circular flows
4. **Optimize strategically** - Position for high-value corridors and exchanges
5. **Avoid bad actors** - Leverage collective warning system
6. **Learn continuously** - Apply pheromone-based fee memory

The key is ensuring the `_analyze_node_state()` function gathers comprehensive intelligence, and the `OpportunityScanner` creates opportunities based on all available data sources.
