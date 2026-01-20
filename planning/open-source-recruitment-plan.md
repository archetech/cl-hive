# Open Source Recruitment Strategy - Updated for Permissionless Join

## Executive Summary

Release cl_revenue_ops as a standalone open-source CLN plugin that provides immediate value to any node operator. The new **permissionless join flow** allows any node with a channel to a hive member to join automatically - no tickets or admin approval required. This creates an even more natural recruitment funnel.

### Key Changes from Previous Plan

| Before | After |
|--------|-------|
| Genesis tickets required | Channel existence = proof of stake |
| Admin tier for governance | Only member and neophyte tiers |
| Admin generates tickets | Any member can accept joins |
| Manual portal application | Autodiscovery via peer_connected |
| Admin approval required | Automatic join, 90-day probation |

---

## Part 1: Permissionless Join Flow

### 1.1 How Joining Works Now

```
┌─────────────────────────────────────────────────────────────────────┐
│                    PERMISSIONLESS JOIN FLOW                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  Step 1: CHANNEL OPEN                                               │
│  ├── Prospect opens channel to any hive member                      │
│  └── Channel existence = economic commitment (proof of stake)       │
│                                                                      │
│  Step 2: AUTODISCOVERY (peer_connected hook)                        │
│  ├── On connection, prospect's node sends HIVE_HELLO                │
│  └── Contains only pubkey (no ticket needed)                        │
│                                                                      │
│  Step 3: CHALLENGE-RESPONSE                                         │
│  ├── Member verifies channel exists with prospect                   │
│  ├── Member sends CHALLENGE with random nonce                       │
│  ├── Prospect signs nonce + manifest with node key                  │
│  └── Member verifies signatures via HSM                             │
│                                                                      │
│  Step 4: WELCOME                                                    │
│  ├── Prospect added as NEOPHYTE (90-day probation)                  │
│  ├── Gets 50% revenue share during probation                        │
│  └── Can route but cannot vote                                      │
│                                                                      │
│  Step 5: PROMOTION (after 90 days OR majority vote)                 │
│  ├── Auto-promotion if: uptime ≥95%, contribution ratio ≥1.0       │
│  └── OR: Majority of members vote to promote early                  │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.2 Membership Tiers (Simplified)

| Tier | Description | Revenue Share | Voting |
|------|-------------|---------------|--------|
| **NEOPHYTE** | New member, 90-day probation | 50% | No |
| **MEMBER** | Full member after promotion | 100% | Yes |

*Note: Admin tier has been removed. Governance is now fully democratic.*

### 1.3 Promotion Paths

```
┌─────────────────────────────────────────────────────────────────────┐
│                        PROMOTION PATHS                               │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  PATH A: AUTOMATIC (Meritocratic)                                   │
│  ├── Complete 90-day probation period                               │
│  ├── Maintain ≥95% uptime                                           │
│  ├── Contribution ratio ≥1.0 (forward at least as much as receive) │
│  └── Bring ≥1 unique peer to hive topology                         │
│                                                                      │
│  PATH B: MANUAL (Majority Vote)                                     │
│  ├── Any member can propose a neophyte for early promotion          │
│  ├── Members vote (51% quorum required)                             │
│  ├── Bootstrap case: single member can approve with 1 vote          │
│  └── Promotion executes immediately when quorum reached             │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Part 2: Updated Portal Design

The portal shifts from "application processing" to "discovery and monitoring":

### 2.1 Portal Purpose (Revised)

```
┌─────────────────────────────────────────────────────────────────────┐
│                    HIVE PORTAL (hive.lightning-goats.com)            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  DISCOVERY (Public)                                                  │
│  ├── What is a Lightning Hive?                                      │
│  ├── Benefits of joining (coordinated fees, revenue share, etc.)    │
│  ├── How to join: Open channel to any member → automatic join       │
│  └── List of current hive members with pubkeys                      │
│                                                                      │
│  MEMBER DASHBOARD (Authenticated)                                    │
│  ├── Your stats: uptime, contribution ratio, revenue share          │
│  ├── Hive stats: total capacity, routing volume, health             │
│  ├── Pending promotions: vote on neophyte promotions                │
│  └── Settlement history: past payouts and calculations              │
│                                                                      │
│  NODE STATUS (Public)                                                │
│  ├── Check if your node is already a member                         │
│  ├── See which hive members you have channels with                  │
│  └── Verify your membership tier and probation status               │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 Simplified Application Flow

No application form needed! The new flow is:

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   DISCOVER   │────▶│ OPEN CHANNEL │────▶│  AUTO-JOIN   │
│              │     │              │     │              │
│ • Visit site │     │ • To any     │     │ • Automatic  │
│ • See members│     │   member     │     │ • No approval│
│ • Learn      │     │ • Any size   │     │ • Neophyte   │
└──────────────┘     └──────────────┘     └──────┬───────┘
                                                  │
                                                  ▼
                     ┌──────────────┐     ┌──────────────┐
                     │   MEMBER     │◀────│  NEOPHYTE    │
                     │              │     │  (90 days)   │
                     │ • Full share │     │              │
                     │ • Voting     │     │ • 50% share  │
                     │ • Full rights│     │ • No voting  │
                     └──────────────┘     └──────────────┘
                            ▲
                            │ (or majority vote)
                            │
```

### 2.3 What the Portal Needs to Do

| Feature | Before | After |
|---------|--------|-------|
| Application form | Complex form with node details | Not needed |
| Genesis tickets | Generate and deliver | Deprecated |
| Node verification | Pre-join vetting | Post-join monitoring |
| Approval workflow | Admin reviews | Automatic join |
| Member listing | Private | Public (for discovery) |

---

## Part 3: Implementation Tasks

### Phase 1: Update cl-hive for Recruitment (Already Done ✅)

| Task | Status | Files |
|------|--------|-------|
| Remove admin tier | ✅ | `modules/membership.py` |
| Remove tickets from HELLO | ✅ | `modules/protocol.py` |
| Add peer_connected autodiscovery | ✅ | `cl-hive.py` |
| Channel-as-proof-of-stake | ✅ | `cl-hive.py` |
| Manual promotion via majority vote | ✅ | `modules/membership.py` |
| Single-member bootstrap quorum | ✅ | `modules/membership.py` |
| MCP tools for promotion | ✅ | `tools/mcp-hive-server.py` |

### Phase 2: Update cl_revenue_ops for Standalone (Week 1-2)

| Task | Description | Files |
|------|-------------|-------|
| **2.1** | Update hive detection (remove admin tier check) | `cl_revenue_ops.py` |
| **2.2** | Make hive callbacks optional | `fee_controller.py`, `rebalancer.py` |
| **2.3** | Add `--hive-enabled` option (default: auto-detect) | Plugin options |
| **2.4** | Ensure standalone mode works without cl-hive | All modules |

**Updated Hive Detection Logic:**
```python
def detect_hive_connection():
    """Auto-detect if cl-hive plugin is loaded and we're a member."""
    try:
        plugins = plugin.rpc.plugin("list")
        hive_loaded = any("cl-hive" in p.get("name", "") for p in plugins.get("plugins", []))

        if not hive_loaded:
            return False

        # Check if we're a hive member (member or neophyte)
        status = plugin.rpc.call("hive-status")
        tier = status.get("membership", {}).get("tier")
        return tier in ["member", "neophyte"]  # No more admin tier

    except Exception:
        return False
```

### Phase 3: Portal Development (Week 2-4)

| Task | Description | Tech |
|------|-------------|------|
| **3.1** | Landing page: "What is a Hive?" | Static HTML/Tailwind |
| **3.2** | Member directory (public list for discovery) | FastAPI + SQLite |
| **3.3** | Node status checker ("Am I a member?") | Lightning API |
| **3.4** | Member dashboard (authenticated) | LNURL-auth |
| **3.5** | Promotion voting interface | MCP integration |
| **3.6** | Settlement history viewer | Read from cl-hive DB |

### Phase 4: cl_revenue_ops Documentation (Week 3)

| Task | Description |
|------|-------------|
| **4.1** | Comprehensive README.md |
| **4.2** | QUICKSTART.md (5-minute setup) |
| **4.3** | RPC command reference |
| **4.4** | "Hive Benefits" section |
| **4.5** | CONTRIBUTING.md |

### Phase 5: App Store Packages (Week 4)

| Task | Description |
|------|-------------|
| **5.1** | Umbrel app package |
| **5.2** | Start9 package |
| **5.3** | RaspiBlitz bonus script |
| **5.4** | Self-hosted community store |

---

## Part 4: Updated Revenue Sharing Model

```
┌─────────────────────────────────────────────────────────────────────┐
│                    REVENUE POOL (Simplified)                         │
│                                                                      │
│  Total Pool = Sum of all hive forward fees                           │
│                                                                      │
│  Distribution Formula:                                               │
│  ┌─────────────────────────────────────────────────────────────────┐│
│  │                                                                  ││
│  │  member_share = pool_total × (                                  ││
│  │      0.4 × capacity_ratio +     # 40% by capacity               ││
│  │      0.4 × forward_ratio +      # 40% by routing work           ││
│  │      0.2 × uptime_ratio         # 20% by reliability            ││
│  │  ) × tier_multiplier                                            ││
│  │                                                                  ││
│  │  tier_multiplier:                                               ││
│  │    neophyte = 0.5  (90-day probation)                           ││
│  │    member   = 1.0  (full share)                                 ││
│  │                                                                  ││
│  └─────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────┘
```

*Note: Admin tier removed - all full members have equal standing.*

---

## Part 5: Files to Modify/Create

### cl-hive Changes (Already Done ✅)

| File | Change | Status |
|------|--------|--------|
| `modules/membership.py` | Remove admin tier, add manual promotion | ✅ |
| `modules/protocol.py` | Remove ticket from HELLO | ✅ |
| `modules/handshake.py` | Update docstrings | ✅ |
| `modules/config.py` | Add auto_join_enabled | ✅ |
| `cl-hive.py` | Add peer_connected hook, update handle_hello | ✅ |
| `tools/mcp-hive-server.py` | Add promotion MCP tools | ✅ |

### cl_revenue_ops Changes (TODO)

| File | Change |
|------|--------|
| `cl_revenue_ops.py` | Update hive detection (no admin tier) |
| `hive_interface.py` | NEW - Abstraction for hive communication |
| `fee_controller.py` | Optional hive callbacks |
| `rebalancer.py` | Optional hive callbacks |

### Portal (New Repository: `hive-portal`)

| File | Purpose |
|------|---------|
| `api/main.py` | FastAPI application |
| `api/routes/members.py` | Public member directory |
| `api/routes/status.py` | Node status checker |
| `api/routes/dashboard.py` | Member dashboard data |
| `api/routes/voting.py` | Promotion voting |
| `frontend/index.html` | Landing page |
| `frontend/members.html` | Member directory |
| `frontend/dashboard.html` | Member dashboard |

---

## Part 6: Updated Timeline

### Week 1-2: cl_revenue_ops Standalone Mode
- Update hive detection logic
- Test standalone operation
- Write documentation

### Week 2-3: Portal Development
- Backend API (FastAPI)
- Frontend pages
- nginx setup at hive.lightning-goats.com

### Week 3-4: App Store Packages
- Umbrel app
- Start9 package
- RaspiBlitz script

### Week 4-5: Launch
- Public release cl_revenue_ops
- Portal goes live
- Community outreach

---

## Part 7: Success Metrics

| Metric | 1 Month | 3 Months | 6 Months |
|--------|---------|----------|----------|
| cl_revenue_ops installs | 20 | 100 | 300 |
| GitHub stars | 30 | 150 | 500 |
| Hive members (via channel) | 3 | 15 | 50 |
| Combined hive capacity | 500M sats | 2B sats | 10B sats |

---

## Part 8: Verification

### Test Permissionless Join Flow

```bash
# 1. Have a non-member open channel to hive member
# 2. Verify autodiscovery sends HELLO
# 3. Verify neophyte membership created
# 4. Test manual promotion with majority vote

# MCP tools for testing:
claude> Use hive_members to see current members
claude> Use hive_propose_promotion for <neophyte_pubkey>
claude> Use hive_vote_promotion for <neophyte_pubkey>
claude> Use hive_execute_promotion for <neophyte_pubkey>
```

### Test Standalone cl_revenue_ops

```bash
# 1. Install without cl-hive
# 2. Verify all features work
# 3. Install cl-hive and join hive
# 4. Verify hive mode activates
```

---

## Next Steps

1. ✅ Implement permissionless join flow in cl-hive
2. ⏳ Update cl_revenue_ops for standalone mode
3. ⏳ Build portal at hive.lightning-goats.com
4. ⏳ Create app store packages
5. ⏳ Public release and announcement
