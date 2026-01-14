# AI Oracle Protocol Specification

**Version:** 0.1.0-draft
**Status:** Proposal
**Authors:** cl-hive contributors
**Date:** 2026-01-14

## Abstract

This specification defines a protocol extension for cl-hive that enables AI agents operating in Oracle mode to communicate, coordinate, and collaborate when managing Lightning Network nodes. The protocol provides structured message types for strategy coordination, task delegation, reasoning exchange, and collective intelligence while maintaining the security properties of the existing Hive protocol.

## Table of Contents

1. [Motivation](#1-motivation)
2. [Design Principles](#2-design-principles)
3. [Protocol Overview](#3-protocol-overview)
4. [Message Types](#4-message-types)
5. [Oracle API](#5-oracle-api)
6. [Security Considerations](#6-security-considerations)
7. [Implementation Guidelines](#7-implementation-guidelines)
8. [Future Extensions](#8-future-extensions)

---

## 1. Motivation

### 1.1 Current State

The cl-hive protocol currently supports three governance modes:
- **Advisor**: Human reviews and approves all actions
- **Autonomous**: Node executes within predefined safety bounds
- **Oracle**: External API makes decisions

Oracle mode is designed for programmatic decision-making, but the current implementation assumes a simple request/response pattern where the oracle receives pending actions and returns approve/reject decisions.

### 1.2 The AI Agent Opportunity

When AI agents serve as oracles for multiple Hive nodes, new possibilities emerge:
- **Collective Intelligence**: AIs can share insights and reach better decisions together
- **Coordinated Strategy**: Fleet-wide strategies can be negotiated and executed
- **Task Delegation**: AIs can assign tasks based on node capabilities
- **Emergent Optimization**: Swarm behavior may outperform individual optimization

### 1.3 Why Structured Communication?

Rather than allowing arbitrary text communication (which poses security and bandwidth risks), this protocol defines **typed, schema-validated messages** that:
- Can be verified and audited
- Have bounded size and complexity
- Support the specific coordination patterns AIs need
- Maintain the security properties of the Hive protocol

---

## 2. Design Principles

### 2.1 Structured Over Unstructured

All AI communication uses defined message schemas. No free-form text fields that could serve as prompt injection vectors.

### 2.2 Verifiable and Auditable

Every AI decision and communication is logged with reasoning hashes that can be verified later. The fleet can audit AI behavior.

### 2.3 Fail-Safe Defaults

If AI communication fails, nodes fall back to existing behavior (advisor mode queuing or autonomous bounds). AI coordination enhances but doesn't replace core safety.

### 2.4 Bandwidth Conscious

AI messages are summarized for gossip. Full reasoning is available on-demand via request/response patterns.

### 2.5 Consensus Without Centralization

Strategies require quorum approval. No single AI can dictate fleet behavior. Dissenting AIs can opt out of coordinated actions.

### 2.6 Human Override

Node operators can always override AI decisions. AI coordination is a tool, not a replacement for human judgment on critical matters.

---

## 3. Protocol Overview

### 3.1 Message Type Range

AI Oracle messages use type range **32800-32899** (50 types reserved):

| Range | Category |
|-------|----------|
| 32800-32809 | Information Sharing |
| 32810-32819 | Task Coordination |
| 32820-32829 | Strategy Coordination |
| 32830-32839 | Reasoning Exchange |
| 32840-32849 | Health & Alerts |
| 32850-32899 | Reserved for Future |

### 3.2 Message Flow

```
┌─────────────┐                              ┌─────────────┐
│   Node A    │                              │   Node B    │
│  (AI Agent) │                              │  (AI Agent) │
└──────┬──────┘                              └──────┬──────┘
       │                                            │
       │  AI_STATE_SUMMARY (periodic broadcast)     │
       │ ─────────────────────────────────────────► │
       │                                            │
       │  AI_OPPORTUNITY_SIGNAL                     │
       │ ◄───────────────────────────────────────── │
       │                                            │
       │  AI_TASK_REQUEST                           │
       │ ─────────────────────────────────────────► │
       │                                            │
       │  AI_TASK_RESPONSE (accept)                 │
       │ ◄───────────────────────────────────────── │
       │                                            │
       │  AI_TASK_COMPLETE                          │
       │ ◄───────────────────────────────────────── │
       │                                            │
```

### 3.3 Integration with Existing Protocol

AI messages travel over the existing Hive custom message infrastructure:
- Same PKI authentication (signmessage/checkmessage)
- Same peer-to-peer delivery via custommsg
- Same gossip patterns for broadcasts
- Extends, doesn't replace, existing message types

---

## 4. Message Types

### 4.1 Information Sharing (32800-32809)

#### 4.1.1 AI_STATE_SUMMARY (0x8020 / 32800)

Periodic broadcast summarizing an AI agent's current state and priorities.

**Frequency**: Every heartbeat interval (default 5 minutes)
**Delivery**: Broadcast to all Hive members

```json
{
  "type": "ai_state_summary",
  "version": 1,
  "node_id": "03abc123...",
  "timestamp": 1705234567,
  "sequence": 12345,

  "liquidity": {
    "status": "healthy",
    "total_capacity_sats": 500000000,
    "available_outbound_sats": 250000000,
    "available_inbound_sats": 200000000,
    "channel_count": 25,
    "utilization_pct": 45.5
  },

  "priorities": {
    "current_focus": "expansion",
    "target_nodes": ["02def456...", "02ghi789..."],
    "avoid_nodes": [],
    "capacity_seeking": true,
    "rebalance_budget_remaining_sats": 5000
  },

  "constraints": {
    "max_channel_size_sats": 50000000,
    "min_channel_size_sats": 1000000,
    "feerate_ceiling_sat_kb": 5000,
    "daily_budget_remaining_sats": 45000000
  },

  "ai_meta": {
    "model": "claude-opus-4.5",
    "confidence": 0.85,
    "decisions_last_24h": 15,
    "strategy_alignment": "cooperative"
  },

  "signature": "dhbc4mqjz..."
}
```

**Fields**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| type | string | Yes | Message type identifier |
| version | int | Yes | Schema version |
| node_id | string | Yes | Sender's node public key |
| timestamp | int | Yes | Unix timestamp |
| sequence | int | Yes | Monotonic sequence number |
| liquidity | object | Yes | Current liquidity state |
| liquidity.status | enum | Yes | "healthy", "constrained", "critical" |
| priorities | object | Yes | Current AI priorities |
| priorities.current_focus | enum | Yes | "expansion", "consolidation", "maintenance", "defensive" |
| constraints | object | Yes | Operating constraints |
| ai_meta | object | Yes | AI agent metadata |
| signature | string | Yes | PKI signature of message hash |

---

#### 4.1.2 AI_OPPORTUNITY_SIGNAL (0x8021 / 32801)

AI identifies an opportunity and signals to the fleet.

**Delivery**: Broadcast to all Hive members

```json
{
  "type": "ai_opportunity_signal",
  "version": 1,
  "node_id": "03abc123...",
  "timestamp": 1705234567,
  "signal_id": "sig_a1b2c3d4",

  "opportunity": {
    "target_node": "02xyz789...",
    "target_alias": "ACINQ",
    "opportunity_type": "high_value_target",
    "category": "routing_hub"
  },

  "analysis": {
    "target_capacity_sats": 50000000000,
    "target_channel_count": 500,
    "current_hive_share_pct": 0.05,
    "optimal_hive_share_pct": 0.15,
    "share_gap_pct": 10.0,
    "estimated_daily_volume_sats": 100000000,
    "avg_fee_rate_ppm": 150
  },

  "recommendation": {
    "action": "expand",
    "urgency": "medium",
    "suggested_capacity_sats": 20000000,
    "estimated_roi_annual_pct": 8.5,
    "confidence": 0.75
  },

  "volunteer": {
    "willing": true,
    "capacity_available_sats": 25000000,
    "position_score": 0.8
  },

  "reasoning_summary": "High-volume routing hub with only 5% hive share. Strong fee revenue potential. I have good position via existing peer connections.",

  "signature": "dhbc4mqjz..."
}
```

**Opportunity Types**:

| Type | Description |
|------|-------------|
| high_value_target | Well-connected node with routing potential |
| underserved | Node with low hive share vs optimal |
| fee_arbitrage | Fee mispricing opportunity |
| liquidity_need | Hive member needs inbound/outbound |
| defensive | Competitor activity requires response |
| emerging | New node showing growth signals |

---

#### 4.1.3 AI_MARKET_ASSESSMENT (0x8022 / 32802)

AI shares analysis of market conditions.

**Delivery**: Broadcast to all Hive members
**Frequency**: On significant market changes or periodic (hourly)

```json
{
  "type": "ai_market_assessment",
  "version": 1,
  "node_id": "03abc123...",
  "timestamp": 1705234567,
  "assessment_id": "assess_x1y2z3",

  "assessment_type": "fee_trend",
  "time_horizon": "short_term",

  "market_data": {
    "avg_network_fee_ppm": 250,
    "fee_change_24h_pct": 12.5,
    "mempool_depth_vbytes": 15000000,
    "mempool_fee_rate_sat_vb": 25,
    "block_fullness_pct": 95.0
  },

  "corridor_analysis": [
    {
      "corridor": "exchanges_to_retail",
      "volume_trend": "increasing",
      "fee_trend": "increasing",
      "competition": "moderate",
      "hive_position": "strong"
    },
    {
      "corridor": "us_to_eu",
      "volume_trend": "stable",
      "fee_trend": "decreasing",
      "competition": "high",
      "hive_position": "weak"
    }
  ],

  "recommendation": {
    "overall_stance": "opportunistic",
    "fee_direction": "raise_floor",
    "expansion_timing": "favorable",
    "rebalance_urgency": "low"
  },

  "confidence": 0.70,
  "data_freshness_seconds": 300,

  "signature": "dhbc4mqjz..."
}
```

---

### 4.2 Task Coordination (32810-32819)

#### 4.2.1 AI_TASK_REQUEST (0x802A / 32810)

AI requests another node to perform a task.

**Delivery**: Direct to target node

```json
{
  "type": "ai_task_request",
  "version": 1,
  "node_id": "03abc123...",
  "target_node": "03def456...",
  "timestamp": 1705234567,
  "request_id": "req_a1b2c3d4e5f6",

  "task": {
    "task_type": "expand_to",
    "target": "02xyz789...",
    "parameters": {
      "amount_sats": 10000000,
      "max_fee_sats": 5000,
      "min_channels": 1,
      "max_channels": 1
    },
    "deadline_timestamp": 1705320967,
    "priority": "normal"
  },

  "context": {
    "reasoning": "You have better position (existing peer, lower hop count)",
    "opportunity_signal_id": "sig_a1b2c3d4",
    "fleet_benefit": "Increases hive share from 5% to 8%"
  },

  "compensation": {
    "offer_type": "reciprocal",
    "details": "Will handle your next expansion request",
    "sats_offered": 0,
    "reputation_weight": 1.0
  },

  "fallback": {
    "if_rejected": "will_handle_self",
    "if_timeout": "will_handle_self"
  },

  "signature": "dhbc4mqjz..."
}
```

**Task Types**:

| Type | Description | Parameters |
|------|-------------|------------|
| expand_to | Open channel to target | amount_sats, max_fee_sats |
| rebalance_toward | Push liquidity toward target | amount_sats, max_ppm |
| probe_route | Test route viability | destination, amount_sats |
| gather_intel | Research a node | target, aspects[] |
| adjust_fees | Change fee on corridor | scid, new_fee_ppm |
| close_channel | Close a channel | scid, urgency |

---

#### 4.2.2 AI_TASK_RESPONSE (0x802B / 32811)

Response to a task request.

**Delivery**: Direct to requesting node

```json
{
  "type": "ai_task_response",
  "version": 1,
  "node_id": "03def456...",
  "timestamp": 1705234600,
  "request_id": "req_a1b2c3d4e5f6",

  "response": "accept",

  "acceptance": {
    "estimated_completion_timestamp": 1705248000,
    "actual_parameters": {
      "amount_sats": 10000000,
      "estimated_fee_sats": 3500
    },
    "conditions": []
  },

  "reasoning": "Have spare liquidity and good connection to target. Happy to help.",

  "signature": "dhbc4mqjz..."
}
```

**Response Types**:

| Response | Description |
|----------|-------------|
| accept | Will perform the task as requested |
| accept_modified | Will perform with modified parameters |
| reject | Cannot or will not perform |
| defer | Can perform later (includes new deadline) |
| counter | Proposes alternative terms |

---

#### 4.2.3 AI_TASK_COMPLETE (0x802C / 32812)

Notification that a delegated task is complete.

**Delivery**: Direct to requesting node

```json
{
  "type": "ai_task_complete",
  "version": 1,
  "node_id": "03def456...",
  "timestamp": 1705247500,
  "request_id": "req_a1b2c3d4e5f6",

  "status": "success",

  "result": {
    "task_type": "expand_to",
    "target": "02xyz789...",
    "outcome": {
      "channel_opened": true,
      "scid": "800000x1000x0",
      "capacity_sats": 10000000,
      "actual_fee_sats": 3200,
      "funding_txid": "abc123..."
    }
  },

  "learnings": {
    "target_responsiveness": "fast",
    "connection_quality": "good",
    "recommended_for_future": true,
    "notes": "Target accepted quickly, good peer"
  },

  "compensation_status": {
    "reciprocal_credit": true,
    "credit_expires_timestamp": 1707839500
  },

  "signature": "dhbc4mqjz..."
}
```

---

#### 4.2.4 AI_TASK_CANCEL (0x802D / 32813)

Cancel a previously requested task.

```json
{
  "type": "ai_task_cancel",
  "version": 1,
  "node_id": "03abc123...",
  "timestamp": 1705235000,
  "request_id": "req_a1b2c3d4e5f6",

  "reason": "opportunity_expired",
  "details": "Another hive member already expanded to target",

  "signature": "dhbc4mqjz..."
}
```

---

### 4.3 Strategy Coordination (32820-32829)

#### 4.3.1 AI_STRATEGY_PROPOSAL (0x8034 / 32820)

AI proposes a fleet-wide coordinated strategy.

**Delivery**: Broadcast to all Hive members

```json
{
  "type": "ai_strategy_proposal",
  "version": 1,
  "node_id": "03abc123...",
  "timestamp": 1705234567,
  "proposal_id": "prop_s1t2r3a4t5",

  "strategy": {
    "strategy_type": "fee_coordination",
    "name": "ACINQ Corridor Fee Alignment",
    "summary": "Coordinate fee floor increase on ACINQ-connected channels",

    "objectives": [
      "Increase average fee revenue by 15%",
      "Reduce fee undercutting within hive",
      "Establish sustainable fee floor"
    ],

    "parameters": {
      "target_corridor": "acinq_connected",
      "target_nodes": ["02xyz..."],
      "fee_floor_ppm": 150,
      "fee_ceiling_ppm": 500,
      "duration_hours": 168,
      "ramp_up_hours": 24
    },

    "expected_outcomes": {
      "revenue_change_pct": 15,
      "volume_change_pct": -5,
      "net_benefit_pct": 10,
      "confidence": 0.70
    },

    "risks": [
      {
        "risk": "volume_loss",
        "probability": 0.3,
        "impact": "medium",
        "mitigation": "Gradual ramp-up allows adjustment"
      }
    ],

    "opt_out_allowed": true,
    "opt_out_penalty": "none"
  },

  "voting": {
    "quorum_required_pct": 51,
    "voting_deadline_timestamp": 1705320967,
    "execution_delay_hours": 24,
    "vote_weight": "equal"
  },

  "proposer_commitment": {
    "will_participate": true,
    "capacity_committed_sats": 100000000
  },

  "signature": "dhbc4mqjz..."
}
```

**Strategy Types**:

| Type | Description |
|------|-------------|
| fee_coordination | Align fees across hive for corridor |
| expansion_campaign | Coordinated expansion to target(s) |
| rebalance_ring | Circular rebalancing among members |
| defensive | Response to competitive threat |
| liquidity_sharing | Redistribute liquidity within hive |
| channel_cleanup | Coordinated closure of unprofitable channels |

---

#### 4.3.2 AI_STRATEGY_VOTE (0x8035 / 32821)

Vote on a strategy proposal.

**Delivery**: Broadcast to all Hive members

```json
{
  "type": "ai_strategy_vote",
  "version": 1,
  "node_id": "03def456...",
  "timestamp": 1705250000,
  "proposal_id": "prop_s1t2r3a4t5",

  "vote": "approve",

  "rationale": {
    "summary": "Analysis supports fee increase viability",
    "key_factors": [
      "Local data confirms corridor underpricing",
      "Volume elasticity estimate is reasonable",
      "Risk mitigation is adequate"
    ],
    "confidence_in_proposal": 0.75
  },

  "commitment": {
    "will_participate": true,
    "capacity_committed_sats": 75000000,
    "conditions": []
  },

  "amendments": null,

  "signature": "dhbc4mqjz..."
}
```

**Vote Options**:

| Vote | Description |
|------|-------------|
| approve | Support the proposal as-is |
| approve_with_amendments | Support with suggested changes |
| reject | Oppose the proposal |
| abstain | No position (doesn't count toward quorum) |

---

#### 4.3.3 AI_STRATEGY_RESULT (0x8036 / 32822)

Announcement of strategy voting result.

**Delivery**: Broadcast to all Hive members
**Sender**: Proposal originator or designated coordinator

```json
{
  "type": "ai_strategy_result",
  "version": 1,
  "node_id": "03abc123...",
  "timestamp": 1705321000,
  "proposal_id": "prop_s1t2r3a4t5",

  "result": "adopted",

  "voting_summary": {
    "votes_for": 5,
    "votes_against": 1,
    "abstentions": 1,
    "eligible_voters": 7,
    "quorum_met": true,
    "approval_pct": 71.4
  },

  "execution": {
    "effective_timestamp": 1705407400,
    "coordinator_node": "03abc123...",
    "participants": ["03abc...", "03def...", "03ghi...", "03jkl...", "03mno..."],
    "opt_outs": ["03pqr..."]
  },

  "amendments_incorporated": [],

  "signature": "dhbc4mqjz..."
}
```

---

#### 4.3.4 AI_STRATEGY_UPDATE (0x8037 / 32823)

Progress update on an active strategy.

**Delivery**: Broadcast to participants
**Frequency**: Periodic during strategy execution

```json
{
  "type": "ai_strategy_update",
  "version": 1,
  "node_id": "03abc123...",
  "timestamp": 1705450000,
  "proposal_id": "prop_s1t2r3a4t5",

  "progress": {
    "phase": "execution",
    "hours_elapsed": 48,
    "hours_remaining": 120,
    "completion_pct": 28.6
  },

  "metrics": {
    "revenue_change_pct": 8.5,
    "volume_change_pct": -3.2,
    "participant_compliance_pct": 100,
    "on_track": true
  },

  "participant_status": [
    {"node": "03abc...", "status": "compliant", "contribution_pct": 22},
    {"node": "03def...", "status": "compliant", "contribution_pct": 18}
  ],

  "issues": [],

  "recommendation": "continue",

  "signature": "dhbc4mqjz..."
}
```

---

### 4.4 Reasoning Exchange (32830-32839)

#### 4.4.1 AI_REASONING_REQUEST (0x803E / 32830)

Request detailed reasoning from another AI.

**Delivery**: Direct to target node

```json
{
  "type": "ai_reasoning_request",
  "version": 1,
  "node_id": "03abc123...",
  "target_node": "03def456...",
  "timestamp": 1705234567,
  "request_id": "reason_r1e2a3s4",

  "context": {
    "reference_type": "strategy_vote",
    "reference_id": "prop_s1t2r3a4t5",
    "specific_question": "Why did you vote against the fee coordination proposal?"
  },

  "detail_level": "full",

  "signature": "dhbc4mqjz..."
}
```

---

#### 4.4.2 AI_REASONING_RESPONSE (0x803F / 32831)

Detailed reasoning in response to request.

**Delivery**: Direct to requesting node

```json
{
  "type": "ai_reasoning_response",
  "version": 1,
  "node_id": "03def456...",
  "timestamp": 1705234700,
  "request_id": "reason_r1e2a3s4",

  "reasoning": {
    "summary": "Risk of volume loss exceeds potential fee gain based on local data",

    "decision_factors": [
      {
        "factor": "volume_elasticity",
        "weight": 0.35,
        "assessment": "high",
        "evidence": "Observed 12% volume drop on last fee increase",
        "confidence": 0.80
      },
      {
        "factor": "competitor_response",
        "weight": 0.30,
        "assessment": "likely_undercut",
        "evidence": "Non-hive nodes on corridor have history of undercutting",
        "confidence": 0.65
      },
      {
        "factor": "timing",
        "weight": 0.20,
        "assessment": "poor",
        "evidence": "Mempool clearing, on-chain fees dropping",
        "confidence": 0.75
      },
      {
        "factor": "alternative_strategies",
        "weight": 0.15,
        "assessment": "available",
        "evidence": "Could achieve similar goal via targeted expansion",
        "confidence": 0.60
      }
    ],

    "overall_confidence": 0.70,

    "data_sources": [
      "local_forwarding_history_30d",
      "fee_experiment_results",
      "competitor_fee_monitoring",
      "mempool_analysis"
    ],

    "alternative_proposal": {
      "summary": "Targeted expansion instead of fee increase",
      "expected_benefit": "Similar revenue gain without volume risk"
    }
  },

  "meta": {
    "reasoning_time_ms": 1250,
    "model": "claude-opus-4.5",
    "tokens_used": 2500
  },

  "signature": "dhbc4mqjz..."
}
```

---

### 4.5 Health & Alerts (32840-32849)

#### 4.5.1 AI_HEARTBEAT (0x8048 / 32840)

Extended heartbeat with AI status.

**Delivery**: Broadcast to all Hive members
**Frequency**: Every heartbeat interval

```json
{
  "type": "ai_heartbeat",
  "version": 1,
  "node_id": "03abc123...",
  "timestamp": 1705234567,
  "sequence": 54321,

  "ai_status": {
    "operational_state": "active",
    "model": "claude-opus-4.5",
    "model_version": "20251101",
    "uptime_seconds": 2592000,
    "last_decision_timestamp": 1705234000,
    "decisions_24h": 25,
    "decisions_pending": 2
  },

  "health_metrics": {
    "api_latency_ms": 150,
    "api_success_rate_pct": 99.5,
    "memory_usage_pct": 45,
    "error_rate_24h": 0.5
  },

  "capabilities": {
    "max_decisions_per_hour": 100,
    "supported_task_types": ["expand_to", "rebalance_toward", "adjust_fees"],
    "strategy_participation": true,
    "delegation_acceptance": true
  },

  "signature": "dhbc4mqjz..."
}
```

---

#### 4.5.2 AI_ALERT (0x8049 / 32841)

AI raises an alert for fleet attention.

**Delivery**: Broadcast to all Hive members

```json
{
  "type": "ai_alert",
  "version": 1,
  "node_id": "03abc123...",
  "timestamp": 1705234567,
  "alert_id": "alert_a1l2e3r4t5",

  "alert": {
    "severity": "warning",
    "category": "security",
    "alert_type": "probing_detected",

    "summary": "Unusual channel probing activity detected",

    "details": {
      "source_node": "02xyz789...",
      "probe_count": 150,
      "time_window_minutes": 10,
      "pattern": "balance_discovery",
      "affected_channels": ["800x1x0", "801x2x0", "802x3x0"]
    },

    "impact_assessment": {
      "immediate_risk": "low",
      "potential_risk": "medium",
      "affected_hive_members": 3
    }
  },

  "recommendation": {
    "action": "monitor",
    "urgency": "normal",
    "suggested_response": "Consider enabling shadow routing if available"
  },

  "auto_response_taken": {
    "action": "none",
    "reason": "Below automatic response threshold"
  },

  "signature": "dhbc4mqjz..."
}
```

**Alert Categories**:

| Category | Types |
|----------|-------|
| security | probing_detected, force_close_attempt, unusual_htlc_pattern |
| performance | high_failure_rate, liquidity_crisis, fee_war |
| opportunity | flash_opportunity, competitor_retreat, volume_surge |
| system | ai_degraded, api_unavailable, budget_exhausted |
| network | mempool_spike, block_congestion, gossip_storm |

---

## 5. Oracle API

### 5.1 Overview

The Oracle API is the HTTP interface between the Lightning node and the AI agent. It enables the AI to:
- Receive events and queries from the node
- Return decisions on pending actions
- Send messages to other AI agents
- Query node and network state

### 5.2 Authentication

```
Authorization: Bearer <oracle_token>
X-Node-Signature: <signature_of_request_body>
```

The oracle token is configured at node startup. Request signatures use the node's Lightning key for verification.

### 5.3 Endpoints

#### 5.3.1 Decision Endpoint

```
POST /oracle/decision
```

Node sends pending action for AI decision.

**Request**:
```json
{
  "request_id": "dec_123456",
  "action": {
    "id": 42,
    "action_type": "channel_open",
    "payload": {
      "target": "02xyz...",
      "amount_sats": 10000000,
      "context": { ... }
    },
    "proposed_at": 1705234567,
    "expires_at": 1705320967
  },
  "node_context": {
    "pubkey": "03abc...",
    "onchain_balance_sats": 100000000,
    "channel_count": 25,
    "governance_mode": "oracle"
  }
}
```

**Response**:
```json
{
  "request_id": "dec_123456",
  "decision": "approve",
  "reasoning": {
    "summary": "Target is high-value, good ROI expected",
    "confidence": 0.85,
    "factors": ["target_quality", "liquidity_available", "fee_market"]
  },
  "modifications": null,
  "execute_at": null
}
```

**Decision Values**: `approve`, `reject`, `defer`, `modify`

---

#### 5.3.2 Message Endpoint

```
POST /oracle/message
```

AI sends a protocol message to fleet.

**Request**:
```json
{
  "message_type": "ai_opportunity_signal",
  "payload": { ... },
  "delivery": {
    "mode": "broadcast",
    "targets": null
  }
}
```

**Response**:
```json
{
  "status": "queued",
  "message_id": "msg_789",
  "estimated_delivery": "immediate"
}
```

---

#### 5.3.3 Inbox Endpoint

```
GET /oracle/inbox?since=<timestamp>&types=<comma_separated>
```

AI retrieves incoming messages.

**Response**:
```json
{
  "messages": [
    {
      "id": "msg_456",
      "received_at": 1705234567,
      "from_node": "03def...",
      "message_type": "ai_task_request",
      "payload": { ... }
    }
  ],
  "has_more": false,
  "next_cursor": null
}
```

---

#### 5.3.4 Context Endpoint

```
GET /oracle/context
```

AI queries full node context for decision-making.

**Response**:
```json
{
  "node": {
    "pubkey": "03abc...",
    "alias": "MyHiveNode",
    "block_height": 850000
  },
  "channels": [ ... ],
  "peers": [ ... ],
  "hive": {
    "status": "active",
    "members": [ ... ],
    "pending_actions": [ ... ],
    "active_strategies": [ ... ]
  },
  "network": {
    "mempool_size_vbytes": 15000000,
    "fee_estimates": { ... }
  },
  "ai_inbox_count": 5
}
```

---

#### 5.3.5 Strategy Endpoint

```
POST /oracle/strategy
```

AI proposes a fleet strategy.

**Request**:
```json
{
  "strategy_type": "fee_coordination",
  "parameters": { ... },
  "voting_deadline_hours": 24
}
```

---

### 5.4 Webhooks

The node can push events to the AI via webhooks:

```
POST <ai_webhook_url>/events
```

**Event Types**:
- `pending_action_created`
- `ai_message_received`
- `strategy_vote_needed`
- `task_request_received`
- `alert_raised`

---

## 6. Security Considerations

### 6.1 Message Authentication

All AI messages MUST be signed using the node's Lightning identity key. The signature covers:
- Message type
- Timestamp
- Full payload hash

Receivers MUST verify signatures before processing.

### 6.2 Replay Prevention

Messages include:
- Timestamp (reject if > 5 minutes old)
- Sequence number (reject if <= last seen from sender)

### 6.3 Rate Limiting

| Message Type | Limit |
|-------------|-------|
| AI_STATE_SUMMARY | 1 per minute per node |
| AI_OPPORTUNITY_SIGNAL | 10 per hour per node |
| AI_TASK_REQUEST | 20 per hour per node |
| AI_STRATEGY_PROPOSAL | 5 per day per node |
| AI_ALERT | 10 per hour per node |

### 6.4 Prompt Injection Prevention

**No free-form text fields are interpreted as instructions.**

Fields like `reasoning_summary` are:
- Stored and displayed only
- Never executed or interpreted as commands
- Length-limited (max 500 characters)
- Sanitized for display

### 6.5 Cartel Prevention

To prevent AI coordination from harming the network:

1. **Public Audit Trail**: All strategy proposals and votes are logged
2. **Opt-Out Rights**: Members can opt out of strategies without penalty
3. **Human Override**: Operators can disable AI coordination
4. **Transparency**: Strategy outcomes are measurable and reportable

### 6.6 Sybil Resistance

AI messages inherit Hive membership requirements:
- Only authenticated Hive members can send AI messages
- Membership requires existing channel relationships
- Contribution tracking detects freeloaders

---

## 7. Implementation Guidelines

### 7.1 Phased Rollout

**Phase 1: Information Sharing**
- AI_STATE_SUMMARY
- AI_HEARTBEAT
- Read-only, no coordination

**Phase 2: Task Delegation**
- AI_TASK_REQUEST/RESPONSE/COMPLETE
- Bilateral coordination
- Voluntary participation

**Phase 3: Strategy Coordination**
- AI_STRATEGY_PROPOSAL/VOTE/RESULT
- Fleet-wide coordination
- Quorum requirements

**Phase 4: Advanced Features**
- AI_ALERT with auto-response
- Cross-hive communication
- Strategy templates

### 7.2 Backward Compatibility

Nodes not running AI oracles:
- Ignore AI message types (existing behavior for unknown types)
- Can still participate in Hive
- See AI coordination in logs but don't participate

### 7.3 Testing Requirements

Before production:
- Simulate AI-to-AI communication in regtest
- Test strategy voting with multiple AI models
- Verify no prompt injection vulnerabilities
- Load test message handling

### 7.4 Monitoring

Track:
- AI decision latency
- Message delivery success rate
- Strategy adoption rates
- Coordination effectiveness metrics

---

## 8. Future Extensions

### 8.1 Cross-Hive Communication

Allow AI agents from different Hives to communicate:
- Market intelligence sharing
- Non-compete coordination
- Liquidity bridges

### 8.2 Strategy Templates

Pre-defined strategy templates:
- Fee war response
- New node onboarding campaign
- Seasonal adjustment patterns

### 8.3 Reputation System

Track AI agent reliability:
- Task completion rate
- Strategy outcome accuracy
- Cooperation score

### 8.4 Natural Language Interface

Structured summary generation:
- Daily fleet briefings
- Strategy explanations for operators
- Alert summaries

---

## Appendix A: Message Type Registry

| Type ID | Name | Category |
|---------|------|----------|
| 32800 | AI_STATE_SUMMARY | Information |
| 32801 | AI_OPPORTUNITY_SIGNAL | Information |
| 32802 | AI_MARKET_ASSESSMENT | Information |
| 32810 | AI_TASK_REQUEST | Task |
| 32811 | AI_TASK_RESPONSE | Task |
| 32812 | AI_TASK_COMPLETE | Task |
| 32813 | AI_TASK_CANCEL | Task |
| 32820 | AI_STRATEGY_PROPOSAL | Strategy |
| 32821 | AI_STRATEGY_VOTE | Strategy |
| 32822 | AI_STRATEGY_RESULT | Strategy |
| 32823 | AI_STRATEGY_UPDATE | Strategy |
| 32830 | AI_REASONING_REQUEST | Reasoning |
| 32831 | AI_REASONING_RESPONSE | Reasoning |
| 32840 | AI_HEARTBEAT | Health |
| 32841 | AI_ALERT | Health |

---

## Appendix B: Example Flows

### B.1 Coordinated Expansion

```
1. Alice AI broadcasts AI_OPPORTUNITY_SIGNAL for target T
2. Bob AI responds with AI_TASK_REQUEST to Alice (better positioned)
3. Alice AI sends AI_TASK_RESPONSE (accept)
4. Alice opens channel to T
5. Alice AI sends AI_TASK_COMPLETE
6. All AIs update their state summaries
```

### B.2 Fee Strategy Adoption

```
1. Alice AI broadcasts AI_STRATEGY_PROPOSAL (fee coordination)
2. Bob, Carol, Dave AIs send AI_STRATEGY_VOTE (approve)
3. Eve AI sends AI_STRATEGY_VOTE (reject with reasoning)
4. Alice AI broadcasts AI_STRATEGY_RESULT (adopted, Eve opt-out)
5. Participating nodes adjust fees
6. Alice AI sends periodic AI_STRATEGY_UPDATE
7. Strategy concludes, results measured
```

### B.3 Threat Response

```
1. Bob AI detects probing, broadcasts AI_ALERT
2. Carol AI confirms seeing similar pattern
3. Alice AI proposes AI_STRATEGY_PROPOSAL (defensive)
4. Fast-track vote (1 hour deadline due to threat)
5. Strategy adopted, countermeasures deployed
```

---

## Changelog

- **0.1.0-draft** (2026-01-14): Initial specification draft
