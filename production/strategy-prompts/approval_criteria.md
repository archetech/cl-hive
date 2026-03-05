# Action Approval Criteria

## Node Context (Both Nodes)

**Note**: These are approximate baseline figures. Always check `fleet_health_summary` for current values.

- **hive-nexus-01**: ~91M sats capacity (primary routing node)
- **hive-nexus-02**: ~43M sats capacity (secondary node)
- **Fleet total**: ~134M sats across both nodes
- **Strategy**: Focus on improving existing channel profitability before expansion
- **Health**: Check fleet_health_summary each run — prioritize quality over growth

---

## Channel Open Actions

### APPROVE if ALL conditions are met:
- Target node has >15 active channels (strong connectivity required)
- Target has proven routing volume (check 1ML or Amboss reputation)
- Target's median fee is <500 ppm (quality routing partner)
- Current on-chain fees are <20 sat/vB (excellent opening conditions)
- Opening would not exceed 3% of our total capacity to this peer
- We maintain 500k sats on-chain reserve after opening
- Target is not already a peer with existing channel
- Channel size is 2-5M sats (matches our avg channel size, max 5M without human approval)

### REJECT if ANY condition applies:
- Target has <10 channels (insufficient connectivity)
- On-chain fees >30 sat/vB (wait for lower fees - mempool often clears)
- Insufficient on-chain balance (amount + 500k reserve)
- Target has any force-close history in past 6 months
- Would create duplicate channel to existing peer
- Amount is below 1M sats (not worth on-chain cost)
- We already have >50 channels (ESCALATE for human review — do not auto-approve)
- Target is a known drain node or has poor reputation

### DEFER (reject with reason "needs_review") if:
- Target information is incomplete or ambiguous
- Channel size >5M sats (large commitment — needs human approval)
- Target has 10-15 channels (borderline connectivity — investigate further)
- Target is a new node (<3 months old)
- Any uncertainty about the decision
- Node has >5 underwater channels (should fix existing first)
- Node has >40% underwater channels (fix bleeders before expanding)

---

## Fee Change Actions

### APPROVE:
- Fee increases on channels with >65% outbound (protect liquidity)
- Fee decreases on channels with <35% outbound (attract flow)
- Changes that are <25% from current fee (gradual adjustment)
- Changes within 50-1500 ppm range (auto-approve range for pending actions)
- Increases on channels that are currently profitable (protect margin)
- Decreases on underwater channels to attract flow

### REJECT:
- Changes >25% in either direction (too aggressive for auto-approval)
- Would set fee below 50 ppm (below auto-approve floor; 25 ppm is the config floor for anchors)
- Would set fee above 1500 ppm (outside target operating range — escalate if >1500)
- Fee decrease on already-draining channel (wrong direction)
- Fee increase on channel with <30% outbound (will kill remaining flow)

---

## Fee Anchor Actions (Advisor-Initiated)

Fee anchors are soft fee targets that blend into the optimizer with decaying weight.
Unlike hard fee overrides, they preserve the algorithm's learning state.

### SET anchor if ALL conditions met:
- Channel has clear directional signal (draining, stagnant, or competitive opportunity)
- Target fee is within 25-5000 ppm range
- Target fee differs from current fee by >10% (otherwise not worth anchoring)
- No conflicting anchor already active on the same channel
- Channel is NOT a hive-internal channel (those must stay 0 ppm)
- Total active anchors on node <10

### Anchor Confidence Guidelines:
- **0.8-0.9**: Strong multi-source signal (velocity + profitability + fleet consensus agree)
- **0.6-0.7**: Single strong signal (clear velocity alert OR clear competitive data)
- **0.4-0.5**: Exploratory (testing hypothesis on underperforming channel, limited data)

### Anchor TTL Guidelines:
- **6-12h**: Short-term events (peak hour premium, temporary demand spike)
- **24h**: Standard situations (drain response, stagnation fix, competitor response)
- **48-72h**: Medium-term positioning (post-rebalance optimization, strategic fee shift)
- **168h (7d max)**: Long-term anchoring (only for high-confidence fleet consensus targets)

### DO NOT anchor if:
- Channel has been anchored in last 6 hours for same reason (avoid churn)
- Channel is <7 days old (let optimizer learn naturally first)
- The fee change would be <10% from current (optimizer will handle small adjustments)
- Signal confidence <0.4 (insufficient evidence)

---

## Rebalance Actions (Advisor-Initiated)

**Always try hive routes first (zero-fee), fall back to market routing only if needed.**
**Spend more on proven earners, less on stale channels.**

### Rebalance Routing Priority:
1. **Hive circular rebalance** (`execute_hive_circular_rebalance`) — Zero fee, preferred, no amount limits
2. **Hybrid hive/market routes** — Dramatically cheaper than pure market
3. **Market routing** (`revenue_rebalance`) — Costs sling fees, last resort

### EXECUTE via hive route (zero-fee, preferred) if ALL conditions met:
- Channel is at critical imbalance (<15% or >85% local) OR depleting within 24h
- `fleet_rebalance_path` confirms a viable hive route exists
- `execute_hive_circular_rebalance(dry_run=true)` preview shows valid path
- **No amount limits** — hive rebalances are free, rebalance as much as needed
- Destination channel is not underwater/bleeder
- Source channel is not underwater/bleeder (don't drain bad channels)
- Hive rebalances cost nothing — but still verify the route works first

### EXECUTE via market route (sling) if ALL conditions met:
- **NO hive route available** (must check `fleet_rebalance_path` first)
- Channel is profitable AND has routing activity — worth investing in
- Rebalance is clearly EV-positive (expected **incremental fee capture** > **3x** cost; 3x is mandatory safety margin)
- Cost is <**1000 ppm (0.1%)** of rebalance amount — absolute ceiling
- Amount sized dynamically: use `rebalance_cost_benefit` to determine optimal size based on cost vs expected gain
- **Never market-route for low-signal/stale channels** (hive only — hive is free)
- Source channel is not underwater/bleeder
- Destination channel is not underwater/bleeder
- `rebalance_diagnostic` shows sling available and budget has room
- Daily market rebalance fee spend still under 3,000 sats total
- Max 3 market-routed rebalances per day

### EXCEPTION: Hive Internal Channel
The channel between fleet nodes is exempt from normal tier limits when >70/30 imbalanced:
- Amount: up to 500k sats (this channel unlocks ALL other rebalancing)
- Prefer hive circular routes (zero-fee) or hybrid hive/market routes (much cheaper)
- Pure market routing only as last resort — but still worth the cost to unblock fleet
- Fee limit for market fallback: up to 1000 ppm (same ceiling, but justified by unlock value)
- This is the single highest-ROI rebalance possible

### EXCEPTION: Persistent Hive Topology Saturation (single-path member link)
Use this when a hive-member channel remains heavily imbalanced because topology lacks a viable internal return path.

Trigger conditions (all):
- Channel is with a hive member
- Local balance stays >90% (or <10%) for ≥2 consecutive cycles
- `fleet_rebalance_path` shows no viable internal path (or repeated no-route outcomes)

Advisor actions (in order):
1. **Directional fee defense:** set temporary elevated fee on the saturated direction (start 800-1000 ppm)
2. **Budget-capped staged relief:** execute external liquidity in small tranches only (rebalance or Boltz), verifying budget before each tranche
3. **Stop criteria:** stop on no-delta outcomes, route failures, or when remaining daily budget would be breached
4. **Decay policy:** if channel improves below ~80% local (or above ~20% local), step fee back down gradually (e.g., 1000→700→500)

Guardrails:
- Never exceed daily unified budget cap
- Never run unbounded retries when route quality is poor
- Treat this as a recurring topology condition, not a one-off anomaly

### DO NOT rebalance if ANY condition applies:
- Channel balance is acceptable (20-80% range — leave it alone)
- Cost >1000 ppm (0.1%) of amount for market routes (too expensive)
- Source channel is underwater/bleeder (don't throw good sats after bad)
- Destination channel has poor routing history
- Market route expected incremental fee capture is not at least 3x the routing cost (use `rebalance_cost_benefit`)
- Rebalancing into a channel we're considering closing
- Daily market rebalance fee spend already ≥3,000 sats
- Sling not installed or budget exhausted (check `rebalance_diagnostic`)
- Stale channel + no hive route (don't spend market fees on unproven channels)

---

## Boltz Swap Actions (Advisor-Evaluated)

Boltz swaps are the **last resort** for liquidity management. Always prefer hive internal rebalances (free) and market/Sling routes before Boltz.

### APPROVE if ALL conditions met:
- Channel is profitable and has routing activity (not underwater/bleeder)
- Estimated swap fee < remaining daily Boltz budget
- Expected net benefit > 1.5x estimated swap fee (clear profit margin)
- No pending Boltz swap already active on same channel
- Hive internal and market rebalance options exhausted (check `fleet_rebalance_path` first)
- Channel balance is outside acceptable range (<20% or >80% local)
- Direction matches channel need (loop-in for depleting, loop-out for saturating)

### REJECT if ANY condition applies:
- Channel is underwater/bleeder (fix the channel first, don't feed it)
- Would exceed daily Boltz budget
- Hive internal rebalance available for same direction (use free route instead)
- Market/Sling rebalance available at lower cost
- Channel balance is acceptable (20-80% range — leave it alone)
- Swap fee > 1000 ppm of amount (too expensive)
- Channel is being considered for closing

### DEFER (reject with reason "needs_review") if:
- Expected net benefit is marginal (1.0-1.5x fee — borderline profitability)
- Channel is < 14 days old (let optimizer learn naturally)
- Treasury expansion cycle already running on this node
- Any uncertainty about whether the swap is needed
- Multiple Boltz swaps already executed today (budget discipline)

---

## General Principles

**Every decision must answer: "Does this increase fleet profitability or routing volume?"**

1. **Profitability First**: Every action should improve revenue or reduce costs. If it doesn't clearly do one of these, skip it.
2. **Routing Volume Growth**: More routing = more revenue. Prefer actions that attract or protect flow.
3. **Cost Discipline**: Our margins are thin — every sat spent on rebalancing or fees is a sat not earned. Hive routes are free; use them.
4. **Fix Bleeders Before Expanding**: With underwater channels, fix what's losing money before opening new channels.
5. **Quality Over Quantity**: Reject marginal opportunities — wait for clearly profitable ones.
6. **Conservative Spending**: When uncertain about cost, don't spend. Err toward free actions (fee anchors, hive rebalances, config tuning) over costly ones (market rebalances).
7. **Measure Everything**: Record every decision. The learning engine will tell you what works for this fleet.
