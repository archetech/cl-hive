# AI Advisor Local Database Design

## Problem Statement

The MCP server and AI advisor currently operate statelessly - each query fetches real-time data but has no memory of:
- Historical observations and trends
- Past recommendations and their outcomes
- Peer behavior patterns over time
- What strategies worked or failed

This limits the AI's ability to make intelligent, learning-based decisions.

## Proposed Solution

A local SQLite database maintained by the AI advisor that tracks:
1. Historical metrics for trend analysis
2. Decision audit trail with outcomes
3. Peer intelligence accumulated over time
4. Learned correlations and model state

## Schema Design

### 1. Historical Snapshots (Trend Analysis)

```sql
-- Periodic snapshots of fleet state (hourly/daily)
CREATE TABLE fleet_snapshots (
    id INTEGER PRIMARY KEY,
    timestamp INTEGER NOT NULL,
    snapshot_type TEXT NOT NULL,  -- 'hourly', 'daily'

    -- Fleet aggregates
    total_capacity_sats INTEGER,
    total_channels INTEGER,
    nodes_healthy INTEGER,
    nodes_unhealthy INTEGER,

    -- Financial
    total_revenue_sats INTEGER,
    total_costs_sats INTEGER,
    net_profit_sats INTEGER,

    -- Health
    channels_balanced INTEGER,
    channels_needs_inbound INTEGER,
    channels_needs_outbound INTEGER,

    -- Raw JSON for detailed analysis
    full_report TEXT
);

-- Per-channel historical data
CREATE TABLE channel_history (
    id INTEGER PRIMARY KEY,
    timestamp INTEGER NOT NULL,
    node_name TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    peer_id TEXT NOT NULL,

    -- Balance state
    capacity_sats INTEGER,
    local_sats INTEGER,
    balance_ratio REAL,

    -- Flow metrics
    flow_state TEXT,
    flow_ratio REAL,
    forward_count INTEGER,

    -- Fees
    fee_ppm INTEGER,
    fee_base_msat INTEGER,

    -- Computed velocity (change since last snapshot)
    balance_velocity REAL,  -- sats/hour change rate
    volume_velocity REAL    -- forwards/hour
);
CREATE INDEX idx_channel_history_lookup ON channel_history(node_name, channel_id, timestamp);
```

### 2. Decision Audit Trail (Learning)

```sql
-- Every recommendation made by AI
CREATE TABLE ai_decisions (
    id INTEGER PRIMARY KEY,
    timestamp INTEGER NOT NULL,
    decision_type TEXT NOT NULL,  -- 'fee_change', 'rebalance', 'channel_open', 'channel_close'
    node_name TEXT NOT NULL,
    channel_id TEXT,
    peer_id TEXT,

    -- What was recommended
    recommendation TEXT NOT NULL,  -- JSON with details
    reasoning TEXT,                -- Why this was recommended
    confidence REAL,               -- 0-1 confidence score

    -- Execution status
    status TEXT DEFAULT 'recommended',  -- 'recommended', 'approved', 'rejected', 'executed', 'failed'
    executed_at INTEGER,
    execution_result TEXT,

    -- Outcome tracking (filled in later)
    outcome_measured_at INTEGER,
    outcome_success INTEGER,       -- 1=positive, 0=neutral, -1=negative
    outcome_metrics TEXT           -- JSON with before/after comparison
);
CREATE INDEX idx_decisions_type ON ai_decisions(decision_type, timestamp);

-- Track metric changes after decisions
CREATE TABLE decision_outcomes (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER REFERENCES ai_decisions(id),
    metric_name TEXT NOT NULL,     -- 'revenue', 'volume', 'balance_ratio', etc.
    value_before REAL,
    value_after REAL,
    change_pct REAL,
    measurement_window_hours INTEGER
);
```

### 3. Peer Intelligence

```sql
-- Long-term peer behavior tracking
CREATE TABLE peer_intelligence (
    peer_id TEXT PRIMARY KEY,
    first_seen INTEGER,
    last_seen INTEGER,

    -- Reliability metrics
    total_channels_opened INTEGER DEFAULT 0,
    total_channels_closed INTEGER DEFAULT 0,
    avg_channel_lifetime_days REAL,

    -- Performance
    total_forwards INTEGER DEFAULT 0,
    total_volume_sats INTEGER DEFAULT 0,
    avg_fee_earned_ppm REAL,

    -- Behavior patterns
    typical_balance_ratio REAL,    -- Where balance tends to settle
    rebalance_responsiveness REAL, -- How quickly they rebalance
    fee_competitiveness TEXT,      -- 'aggressive', 'moderate', 'passive'

    -- Reputation
    success_rate REAL,             -- Successful forwards / attempts
    profitability_score REAL,      -- Revenue - costs for this peer
    recommendation TEXT            -- 'excellent', 'good', 'neutral', 'avoid'
);

-- Peer behavior events
CREATE TABLE peer_events (
    id INTEGER PRIMARY KEY,
    timestamp INTEGER NOT NULL,
    peer_id TEXT NOT NULL,
    event_type TEXT NOT NULL,      -- 'channel_open', 'channel_close', 'fee_change', 'large_payment'
    details TEXT                   -- JSON
);
CREATE INDEX idx_peer_events ON peer_events(peer_id, timestamp);
```

### 4. Learned Correlations

```sql
-- What the AI has learned works
CREATE TABLE learned_strategies (
    id INTEGER PRIMARY KEY,
    strategy_type TEXT NOT NULL,   -- 'fee_optimization', 'rebalance_timing', 'peer_selection'
    context TEXT NOT NULL,         -- JSON describing when this applies

    -- The learning
    observation TEXT NOT NULL,     -- What was observed
    conclusion TEXT NOT NULL,      -- What was learned
    confidence REAL,               -- How confident (based on sample size)
    sample_size INTEGER,           -- How many data points

    -- Validity
    learned_at INTEGER,
    last_validated INTEGER,
    still_valid INTEGER DEFAULT 1
);

-- Example entries:
-- "Raising fees above 1000ppm on sink channels reduces volume by 40% on average"
-- "Rebalancing during low-fee periods (weekends) saves 30% on costs"
-- "Channels to peer X tend to deplete within 48 hours - preemptive rebalancing recommended"
```

### 5. Alert State (Reduce Noise)

```sql
-- Track alerts to prevent fatigue
CREATE TABLE alert_history (
    id INTEGER PRIMARY KEY,
    timestamp INTEGER NOT NULL,
    alert_type TEXT NOT NULL,
    node_name TEXT,
    channel_id TEXT,
    message TEXT,
    severity TEXT,

    -- Deduplication
    alert_hash TEXT,               -- Hash of type+node+channel for dedup
    repeat_count INTEGER DEFAULT 1,
    first_fired INTEGER,
    last_fired INTEGER,

    -- Resolution
    resolved INTEGER DEFAULT 0,
    resolved_at INTEGER,
    resolution_action TEXT
);
CREATE INDEX idx_alert_hash ON alert_history(alert_hash);
```

## Key Queries Enabled

### Trend Analysis
```sql
-- Channel depletion velocity (is rebalancing urgent?)
SELECT
    channel_id,
    (SELECT local_sats FROM channel_history WHERE channel_id = ch.channel_id
     ORDER BY timestamp DESC LIMIT 1) as current_local,
    (SELECT local_sats FROM channel_history WHERE channel_id = ch.channel_id
     AND timestamp < strftime('%s','now') - 86400 LIMIT 1) as yesterday_local,
    (current_local - yesterday_local) / 24.0 as hourly_velocity
FROM channel_history ch
GROUP BY channel_id
HAVING hourly_velocity < -1000;  -- Depleting more than 1000 sats/hour
```

### Decision Effectiveness
```sql
-- How effective were fee changes?
SELECT
    decision_type,
    COUNT(*) as total_decisions,
    AVG(CASE WHEN outcome_success = 1 THEN 1.0 ELSE 0.0 END) as success_rate,
    AVG(json_extract(outcome_metrics, '$.revenue_change_pct')) as avg_revenue_impact
FROM ai_decisions
WHERE decision_type = 'fee_change'
AND outcome_measured_at IS NOT NULL
GROUP BY decision_type;
```

### Peer Quality
```sql
-- Best peers to open channels with
SELECT
    peer_id,
    profitability_score,
    success_rate,
    avg_channel_lifetime_days,
    recommendation
FROM peer_intelligence
WHERE recommendation IN ('excellent', 'good')
ORDER BY profitability_score DESC
LIMIT 10;
```

## Data Collection Strategy

### Continuous (Every Monitor Cycle)
- Channel balances and flow states
- Alert conditions

### Hourly
- Channel history snapshots
- Fee changes detected
- Forward counts

### Daily
- Fleet summary snapshots
- Peer intelligence updates
- Decision outcome measurements
- Learned strategy validation

### On-Event
- Decision made → Record immediately
- Channel opened/closed → Peer event
- Fee changed → Channel history entry

## Integration Points

```
┌─────────────────┐
│  Claude Code    │ ← Queries for context
│  (MCP Client)   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐      ┌──────────────────┐
│ MCP Hive Server │ ←──→ │ AI Advisor DB    │
│ (tools/mcp-*)   │      │ (advisor.db)     │
└────────┬────────┘      └──────────────────┘
         │                        ↑
         ▼                        │
┌─────────────────┐      ┌────────┴─────────┐
│  Hive Monitor   │ ───→ │ Data Collection  │
│ (tools/hive-*)  │      │ (writes history) │
└────────┬────────┘      └──────────────────┘
         │
         ▼
┌─────────────────────────────────────┐
│  Hive Fleet (alice, carol, ...)    │
└─────────────────────────────────────┘
```

## Value Summary

| Capability | Without DB | With DB |
|------------|------------|---------|
| Current state | ✓ Real-time query | ✓ Real-time query |
| Historical trends | ✗ | ✓ "Depleting at 1k sats/hr" |
| Decision tracking | ✗ | ✓ "Last fee change failed" |
| Learn from outcomes | ✗ | ✓ "Fee >800ppm hurts volume here" |
| Peer reputation | ✗ | ✓ "Peer X channels last 6 months avg" |
| Alert deduplication | ✗ | ✓ "Already alerted 3x today" |
| Predictive ability | ✗ | ✓ "Will deplete in ~4 hours" |

## Recommended Implementation Order

1. **Phase 1**: Channel history + fleet snapshots (trend analysis)
2. **Phase 2**: Decision audit trail (track recommendations)
3. **Phase 3**: Outcome measurement (learn what works)
4. **Phase 4**: Peer intelligence (long-term peer tracking)
5. **Phase 5**: Learned strategies (accumulated wisdom)
