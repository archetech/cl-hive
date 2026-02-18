"""
Settlement module for cl-hive

Implements BOLT12-based revenue settlement for hive fleet members.

Fair Share Algorithm:
- 30% weight: Capacity contribution (member_capacity / fleet_capacity)
- 60% weight: Routing contribution (member_forwards / fleet_forwards)
- 10% weight: Uptime contribution (member_uptime / fleet_uptime)

Settlement Flow:
1. Each member registers a BOLT12 offer for receiving payments
2. At settlement time, collect fees_earned from each member
3. Calculate fair_share for each member
4. Generate payment list (surplus members pay deficit members)
5. Execute payments via BOLT12

Thread Safety:
- Uses thread-local database connections via HiveDatabase pattern
"""

import os
import time
import json
import sqlite3
import threading
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional, Any, Tuple
from decimal import Decimal, ROUND_DOWN

from . import network_metrics


# Settlement period (weekly)
SETTLEMENT_PERIOD_SECONDS = 7 * 24 * 60 * 60  # 1 week

# Minimum payment threshold floor (absolute minimum to avoid dust)
MIN_PAYMENT_FLOOR_SATS = 100

# Distributed settlement plan version. Bump when the deterministic payment-plan
# algorithm changes in a way that affects plan hashes.
DISTRIBUTED_SETTLEMENT_PLAN_VERSION = 2


def calculate_min_payment(total_fees: int, member_count: int) -> int:
    """
    Calculate dynamic minimum payment threshold.

    Formula: max(FLOOR, total_fees / (members * 10))

    This ensures:
    - Small fleets with small fees can still settle (100 sat floor)
    - Larger fleets don't spam tiny payments
    - Scales with fee volume

    Examples:
    - 307 sats, 2 members: max(100, 307/20) = 100 sats
    - 10000 sats, 5 members: max(100, 10000/50) = 200 sats
    - 100000 sats, 10 members: max(100, 100000/100) = 1000 sats
    """
    if member_count <= 0:
        return MIN_PAYMENT_FLOOR_SATS
    dynamic_min = total_fees // (member_count * 10)
    return max(MIN_PAYMENT_FLOOR_SATS, dynamic_min)

# Fair share weights (standard mode)
WEIGHT_CAPACITY = 0.30
WEIGHT_FORWARDS = 0.60
WEIGHT_UPTIME = 0.10

# Fair share weights (network-optimized mode - Use Case 6)
# Rewards members who contribute more to fleet connectivity
WEIGHT_CAPACITY_NETWORK = 0.25
WEIGHT_FORWARDS_NETWORK = 0.55
WEIGHT_UPTIME_NETWORK = 0.10
WEIGHT_NETWORK_POSITION = 0.10  # 10% for hive centrality contribution

# Network position calculation settings
HIGH_CENTRALITY_THRESHOLD = 0.7  # Members above this get full network bonus
MIN_CENTRALITY_FOR_BONUS = 0.3   # Members below this get no network bonus


@dataclass
class MemberContribution:
    """A member's contribution metrics for a settlement period."""
    peer_id: str
    capacity_sats: int
    forwards_sats: int  # Routing activity metric: forward count from gossip (not sats volume)
    fees_earned_sats: int
    uptime_pct: float
    bolt12_offer: Optional[str] = None
    # Rebalancing costs for net profit calculation (Issue #42)
    rebalance_costs_sats: int = 0
    # Network position metrics (Use Case 6)
    hive_centrality: float = 0.0
    rebalance_hub_score: float = 0.0

    @property
    def net_profit_sats(self) -> int:
        """Net profit capped at 0 (no negative contributions)."""
        return max(0, self.fees_earned_sats - self.rebalance_costs_sats)


@dataclass
class SettlementResult:
    """Result of settlement calculation for one member."""
    peer_id: str
    fees_earned: int
    fair_share: int
    balance: int  # positive = owed money, negative = owes money
    bolt12_offer: Optional[str] = None
    # Rebalancing costs for net profit settlement (Issue #42)
    rebalance_costs: int = 0
    net_profit: int = 0
    # Network position contribution (Use Case 6)
    network_score: float = 0.0
    network_bonus_sats: int = 0


@dataclass
class SettlementPayment:
    """A payment to execute in settlement."""
    from_peer: str
    to_peer: str
    amount_sats: int
    bolt12_offer: str
    status: str = "pending"
    payment_hash: Optional[str] = None
    error: Optional[str] = None


class SettlementManager:
    """
    Manages BOLT12-based revenue settlement for the hive fleet.

    Responsibilities:
    - BOLT12 offer registration for members
    - Fair share calculation based on contributions
    - Settlement payment generation and execution
    - Settlement history tracking
    """

    def __init__(self, database, plugin, rpc=None):
        """
        Initialize the settlement manager.

        Args:
            database: HiveDatabase instance for persistence
            plugin: Reference to the pyln Plugin for logging
            rpc: RPC interface for Lightning operations (optional)
        """
        self.db = database
        self.plugin = plugin
        self.rpc = rpc
        self._local = threading.local()
        self.did_credential_mgr = None  # Set after DID init (Phase 16)

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        return self.db._get_connection()

    def initialize_tables(self):
        """Create settlement-related database tables."""
        conn = self._get_connection()

        # =====================================================================
        # SETTLEMENT OFFERS TABLE
        # =====================================================================
        # BOLT12 offers registered by each member for receiving payments
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settlement_offers (
                peer_id TEXT PRIMARY KEY,
                bolt12_offer TEXT NOT NULL,
                registered_at INTEGER NOT NULL,
                last_verified INTEGER,
                active INTEGER DEFAULT 1
            )
        """)

        # =====================================================================
        # SETTLEMENT PERIODS TABLE
        # =====================================================================
        # Record of each settlement period
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settlement_periods (
                period_id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time INTEGER NOT NULL,
                end_time INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                total_fees_sats INTEGER DEFAULT 0,
                total_members INTEGER DEFAULT 0,
                settled_at INTEGER,
                metadata TEXT
            )
        """)

        # =====================================================================
        # SETTLEMENT CONTRIBUTIONS TABLE
        # =====================================================================
        # Per-member contributions for each settlement period
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settlement_contributions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period_id INTEGER NOT NULL,
                peer_id TEXT NOT NULL,
                capacity_sats INTEGER NOT NULL,
                forwards_sats INTEGER NOT NULL,
                fees_earned_sats INTEGER NOT NULL,
                uptime_pct REAL NOT NULL,
                fair_share_sats INTEGER NOT NULL,
                balance_sats INTEGER NOT NULL,
                rebalance_costs_sats INTEGER NOT NULL DEFAULT 0,
                net_profit_sats INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (period_id) REFERENCES settlement_periods(period_id),
                UNIQUE (period_id, peer_id)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_settlement_contrib_period
            ON settlement_contributions(period_id)
        """)
        # Add columns if upgrading from older schema (Issue #42: net profit settlement)
        try:
            conn.execute(
                "ALTER TABLE settlement_contributions ADD COLUMN rebalance_costs_sats INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass  # Column already exists
        try:
            conn.execute(
                "ALTER TABLE settlement_contributions ADD COLUMN net_profit_sats INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass  # Column already exists

        # =====================================================================
        # SETTLEMENT PAYMENTS TABLE
        # =====================================================================
        # Individual payment records
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settlement_payments (
                payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                period_id INTEGER NOT NULL,
                from_peer_id TEXT NOT NULL,
                to_peer_id TEXT NOT NULL,
                amount_sats INTEGER NOT NULL,
                bolt12_offer TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                payment_hash TEXT,
                paid_at INTEGER,
                error TEXT,
                FOREIGN KEY (period_id) REFERENCES settlement_periods(period_id)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_settlement_payments_period
            ON settlement_payments(period_id)
        """)

        self.plugin.log("Settlement tables initialized")

    # =========================================================================
    # BOLT12 OFFER MANAGEMENT
    # =========================================================================

    def register_offer(self, peer_id: str, bolt12_offer: str) -> Dict[str, Any]:
        """
        Register a BOLT12 offer for a member.

        Args:
            peer_id: Member's node public key
            bolt12_offer: BOLT12 offer string (lno1...)

        Returns:
            Dict with status and offer details
        """
        if not bolt12_offer.startswith("lno1"):
            return {"error": "Invalid BOLT12 offer format (must start with lno1)"}

        conn = self._get_connection()
        now = int(time.time())

        conn.execute("""
            INSERT INTO settlement_offers (peer_id, bolt12_offer, registered_at, active)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(peer_id) DO UPDATE SET
                bolt12_offer = excluded.bolt12_offer,
                registered_at = excluded.registered_at,
                active = 1
        """, (peer_id, bolt12_offer, now))

        self.plugin.log(f"Registered BOLT12 offer for {peer_id[:16]}...")

        return {
            "status": "registered",
            "peer_id": peer_id,
            "offer": bolt12_offer[:40] + "...",
            "registered_at": now
        }

    def get_offer(self, peer_id: str) -> Optional[str]:
        """Get the BOLT12 offer for a member."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT bolt12_offer FROM settlement_offers WHERE peer_id = ? AND active = 1",
            (peer_id,)
        ).fetchone()
        return row["bolt12_offer"] if row else None

    def list_offers(self) -> Dict[str, Any]:
        """List all registered BOLT12 offers."""
        conn = self._get_connection()
        rows = conn.execute("""
            SELECT peer_id, bolt12_offer, registered_at, last_verified, active
            FROM settlement_offers
            ORDER BY registered_at DESC
        """).fetchall()
        return {"offers": [dict(row) for row in rows]}

    def deactivate_offer(self, peer_id: str) -> Dict[str, Any]:
        """Deactivate a member's BOLT12 offer."""
        conn = self._get_connection()
        conn.execute(
            "UPDATE settlement_offers SET active = 0 WHERE peer_id = ?",
            (peer_id,)
        )
        return {"status": "deactivated", "peer_id": peer_id}

    def generate_and_register_offer(self, peer_id: str) -> Dict[str, Any]:
        """
        Generate a BOLT12 offer and register it for settlement.

        This is called automatically when a node joins the hive to ensure
        they can participate in revenue settlement from the start.

        Args:
            peer_id: The member's node public key (must be our own pubkey)

        Returns:
            Dict with status, offer details, or error
        """
        if not self.rpc:
            return {"error": "No RPC interface available"}

        # Check if we already have an active offer
        existing = self.get_offer(peer_id)
        if existing:
            self.plugin.log(f"Settlement offer already registered for {peer_id[:16]}...")
            return {
                "status": "already_registered",
                "peer_id": peer_id,
                "offer": existing[:40] + "..."
            }

        try:
            # Generate BOLT12 offer using CLN's offer RPC
            # 'any' means any amount, description identifies purpose
            result = self.rpc.offer(
                amount="any",
                description="hive settlement"
            )

            if "bolt12" not in result:
                return {"error": "Failed to generate BOLT12 offer: no bolt12 in response"}

            bolt12_offer = result["bolt12"]

            # Register the offer
            reg_result = self.register_offer(peer_id, bolt12_offer)

            self.plugin.log(f"Auto-generated and registered settlement offer for {peer_id[:16]}...")

            return {
                "status": "generated_and_registered",
                "peer_id": peer_id,
                "offer": bolt12_offer[:40] + "...",
                "offer_id": result.get("offer_id")
            }

        except Exception as e:
            self.plugin.log(f"Failed to generate settlement offer: {e}", level='warn')
            return {"error": f"Failed to generate offer: {e}"}

    # =========================================================================
    # FAIR SHARE CALCULATION
    # =========================================================================

    def calculate_fair_shares(
        self,
        contributions: List[MemberContribution],
        network_optimized: bool = False
    ) -> List[SettlementResult]:
        """
        Calculate fair share for each member based on contributions.

        Standard Fair Share Algorithm (all scores normalized across fleet):
        - 30% weight: capacity_contribution = member_capacity / total_capacity
        - 60% weight: routing_contribution = member_forwards / total_forwards
        - 10% weight: uptime_contribution = member_uptime / total_uptime

        Network-Optimized Mode (Use Case 6):
        - 25% weight: capacity_contribution
        - 55% weight: routing_contribution
        - 10% weight: uptime_contribution
        - 10% weight: network_position = normalized hive centrality

        Network position rewards members who maintain better fleet connectivity,
        contributing to overall routing capability even if they don't earn direct fees.

        Each member's fair_share = total_net_profit * weighted_contribution_score
        Balance = fair_share - net_profit (what member keeps minus their fair share)
        - Positive balance = member is owed money
        - Negative balance = member owes money

        Issue #42: Settlement now uses NET PROFIT (fees - rebalance costs) instead of
        gross fees. This ensures members who spend heavily on rebalancing don't
        subsidize those who don't. Net profit is capped at 0 (no negative contributions).

        Args:
            contributions: List of member contributions
            network_optimized: If True, include network position in calculation

        Returns:
            List of settlement results with fair shares and balances
        """
        if not contributions:
            return []

        # Calculate totals - use net profit instead of gross fees (Issue #42)
        total_capacity = sum(c.capacity_sats for c in contributions)
        total_forwards = sum(c.forwards_sats for c in contributions)
        total_net_profit = sum(c.net_profit_sats for c in contributions)
        total_uptime = sum(c.uptime_pct for c in contributions)

        if total_net_profit == 0:
            return [
                SettlementResult(
                    peer_id=c.peer_id,
                    fees_earned=c.fees_earned_sats,
                    rebalance_costs=c.rebalance_costs_sats,
                    net_profit=c.net_profit_sats,
                    fair_share=0,
                    balance=0,
                    bolt12_offer=c.bolt12_offer
                )
                for c in contributions
            ]

        # Enrich contributions with network metrics if needed
        if network_optimized:
            contributions = self._enrich_with_network_metrics(contributions)

        # Calculate total network score for normalization
        total_network_score = sum(c.hive_centrality for c in contributions)

        # Step 1: compute unnormalized weighted contribution scores per member.
        raw_scores: Dict[str, float] = {}
        raw_network_component: Dict[str, float] = {}
        for member in contributions:
            capacity_score = (member.capacity_sats / total_capacity) if total_capacity > 0 else 0.0
            forwards_score = (member.forwards_sats / total_forwards) if total_forwards > 0 else 0.0
            uptime_score = (member.uptime_pct / total_uptime) if total_uptime > 0 else 0.0

            if network_optimized:
                network_score = (
                    (member.hive_centrality / total_network_score) if total_network_score > 0 else 0.0
                )
                if member.hive_centrality < MIN_CENTRALITY_FOR_BONUS:
                    network_score = 0.0

                base_component = (
                    WEIGHT_CAPACITY_NETWORK * capacity_score +
                    WEIGHT_FORWARDS_NETWORK * forwards_score +
                    WEIGHT_UPTIME_NETWORK * uptime_score
                )
                network_component = WEIGHT_NETWORK_POSITION * network_score
                score = base_component + network_component

                raw_scores[member.peer_id] = score
                raw_network_component[member.peer_id] = network_component
            else:
                score = (
                    WEIGHT_CAPACITY * capacity_score +
                    WEIGHT_FORWARDS * forwards_score +
                    WEIGHT_UPTIME * uptime_score
                )
                raw_scores[member.peer_id] = score
                raw_network_component[member.peer_id] = 0.0

        total_score = sum(raw_scores.values())
        if total_score <= 0:
            # Extremely defensive fallback: equal split.
            raw_scores = {m.peer_id: 1.0 for m in contributions}
            raw_network_component = {m.peer_id: 0.0 for m in contributions}
            total_score = float(len(contributions))

        # Step 2: normalize scores so they sum to 1.0 across the fleet.
        norm_scores: Dict[str, float] = {pid: (s / total_score) for pid, s in raw_scores.items()}

        # Step 3: allocate integer fair_shares that sum exactly to total_net_profit
        # using a largest-remainder method (deterministic tie-break by peer_id).
        ideals: Dict[str, float] = {pid: (total_net_profit * w) for pid, w in norm_scores.items()}
        floors: Dict[str, int] = {pid: int(v) for pid, v in ideals.items()}
        allocated = sum(floors.values())
        remainder = total_net_profit - allocated

        # Sort by fractional remainder desc, then peer_id asc for determinism.
        frac_order = sorted(
            ideals.keys(),
            key=lambda pid: (-(ideals[pid] - floors[pid]), pid)
        )
        for i in range(max(0, min(remainder, len(frac_order)))):
            floors[frac_order[i]] += 1

        # Step 4: build SettlementResult list
        results: List[SettlementResult] = []
        for member in sorted(contributions, key=lambda m: m.peer_id):
            fair_share = floors.get(member.peer_id, 0)
            member_net_profit = member.net_profit_sats
            balance = fair_share - member_net_profit

            network_component = raw_network_component.get(member.peer_id, 0.0)
            network_score = 0.0
            network_bonus_sats = 0
            if network_optimized and total_net_profit > 0 and total_score > 0:
                # Report network contribution as a proportion of total normalized score.
                network_score = round(network_component / total_score, 6)
                network_bonus_sats = int(total_net_profit * (network_component / total_score))

            results.append(SettlementResult(
                peer_id=member.peer_id,
                fees_earned=member.fees_earned_sats,
                rebalance_costs=member.rebalance_costs_sats,
                net_profit=member_net_profit,
                fair_share=fair_share,
                balance=balance,
                bolt12_offer=member.bolt12_offer,
                network_score=network_score,
                network_bonus_sats=network_bonus_sats
            ))

        # Accounting identity should now hold exactly.
        total_balance = sum(r.balance for r in results)
        if total_balance != 0:
            self.plugin.log(
                f"Warning: Settlement balance mismatch of {total_balance} sats",
                level='warn'
            )

        return results

    @staticmethod
    def _plan_hash(
        plan_version: int,
        period: str,
        data_hash: str,
        min_payment_sats: int,
        payments: List[Dict[str, Any]],
    ) -> str:
        import hashlib

        # Canonicalize payments ordering.
        canon_payments = sorted(
            payments,
            key=lambda p: (p.get("from_peer", ""), p.get("to_peer", ""), int(p.get("amount_sats", 0)))
        )
        payload = {
            "v": plan_version,
            "period": period,
            "data_hash": data_hash,
            "min_payment_sats": int(min_payment_sats),
            "payments": canon_payments,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def generate_payment_plan(
        self,
        results: List[SettlementResult],
        total_fees: int,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        Deterministically generate a full settlement payment plan.

        Unlike generate_payments(), this does NOT filter based on BOLT12 offer
        availability. The plan is the authoritative expected transfers; offer
        availability is an execution-time concern.

        Returns:
            (payments, min_payment_sats)
        """
        member_count = len(results)
        min_payment = calculate_min_payment(total_fees, member_count)

        # Deterministic ordering, including tie-break by peer_id.
        payers = [r for r in results if r.balance < -min_payment]
        receivers = [r for r in results if r.balance > min_payment]
        payers.sort(key=lambda r: (r.balance, r.peer_id))
        receivers.sort(key=lambda r: (-r.balance, r.peer_id))

        payer_remaining = {p.peer_id: -p.balance for p in payers}
        receiver_remaining = {r.peer_id: r.balance for r in receivers}

        payments: List[Dict[str, Any]] = []
        for payer in payers:
            owing = payer_remaining.get(payer.peer_id, 0)
            if owing <= 0:
                continue
            for receiver in receivers:
                owed = receiver_remaining.get(receiver.peer_id, 0)
                if owed <= 0:
                    continue
                amount = min(owing, owed)
                if amount < min_payment:
                    continue
                payments.append(
                    {"from_peer": payer.peer_id, "to_peer": receiver.peer_id, "amount_sats": int(amount)}
                )
                owing -= amount
                owed -= amount
                payer_remaining[payer.peer_id] = owing
                receiver_remaining[receiver.peer_id] = owed
                if owing <= 0:
                    break

        return payments, min_payment

    def compute_settlement_plan(
        self,
        period: str,
        contributions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Compute a deterministic settlement plan (payments + hashes) from a canonical
        contributions snapshot.
        """
        member_contributions: List[MemberContribution] = []
        for c in contributions:
            uptime = c.get("uptime", 100)
            try:
                uptime_pct = float(uptime) / 100.0
            except Exception:
                uptime_pct = 1.0

            member_contributions.append(
                MemberContribution(
                    peer_id=c["peer_id"],
                    capacity_sats=int(c.get("capacity", 0)),
                    # forward_count is the routing activity metric from gossip
                    forwards_sats=int(c.get("forward_count", 0)),
                    fees_earned_sats=int(c.get("fees_earned", 0)),
                    rebalance_costs_sats=int(c.get("rebalance_costs", 0)),
                    uptime_pct=uptime_pct,
                )
            )

        data_hash = self.calculate_settlement_hash(period, contributions)
        results = self.calculate_fair_shares(member_contributions)
        total_fees = sum(int(c.get("fees_earned", 0)) for c in contributions)
        payments, min_payment = self.generate_payment_plan(results, total_fees=total_fees)

        # Track residual dust that couldn't be settled (below min_payment threshold)
        total_payer_debt = sum(-r.balance for r in results if r.balance < -min_payment)
        total_in_payments = sum(int(p["amount_sats"]) for p in payments)
        residual_sats = max(0, total_payer_debt - total_in_payments)

        plan_hash = self._plan_hash(
            plan_version=DISTRIBUTED_SETTLEMENT_PLAN_VERSION,
            period=period,
            data_hash=data_hash,
            min_payment_sats=min_payment,
            payments=payments,
        )

        expected_sent: Dict[str, int] = {}
        for p in payments:
            expected_sent[p["from_peer"]] = expected_sent.get(p["from_peer"], 0) + int(p["amount_sats"])

        return {
            "plan_version": DISTRIBUTED_SETTLEMENT_PLAN_VERSION,
            "period": period,
            "data_hash": data_hash,
            "plan_hash": plan_hash,
            "min_payment_sats": min_payment,
            "payments": payments,
            "expected_sent_sats": expected_sent,
            "total_fees_sats": total_fees,
            "residual_sats": residual_sats,
        }

    def _enrich_with_network_metrics(
        self,
        contributions: List[MemberContribution]
    ) -> List[MemberContribution]:
        """
        Enrich member contributions with network position metrics.

        Fetches hive centrality and rebalance hub score for each member.

        Args:
            contributions: List of member contributions

        Returns:
            Updated list with network metrics populated
        """
        calculator = network_metrics.get_calculator()
        if not calculator:
            return contributions

        for contrib in contributions:
            metrics = calculator.get_member_metrics(contrib.peer_id)
            if metrics:
                contrib.hive_centrality = metrics.hive_centrality
                contrib.rebalance_hub_score = metrics.rebalance_hub_score

        return contributions

    # =========================================================================
    # PAYMENT GENERATION
    # =========================================================================

    def generate_payments(
        self,
        results: List[SettlementResult],
        total_fees: int = 0
    ) -> List[SettlementPayment]:
        """
        Generate payment list from settlement results.

        Delegates to generate_payment_plan() for deterministic matching,
        then filters by BOLT12 offer availability and converts to
        SettlementPayment objects.

        Args:
            results: List of settlement results
            total_fees: Total fees for dynamic minimum calculation

        Returns:
            List of payments to execute
        """
        raw_payments, min_payment = self.generate_payment_plan(results, total_fees)
        if not raw_payments:
            return []

        # Build offer lookup — both payer and receiver must have offers
        offer_map = {r.peer_id: r.bolt12_offer for r in results if r.bolt12_offer}

        payments = []
        for p in raw_payments:
            from_peer = p["from_peer"]
            to_peer = p["to_peer"]
            if from_peer not in offer_map or to_peer not in offer_map:
                continue
            payments.append(SettlementPayment(
                from_peer=from_peer,
                to_peer=to_peer,
                amount_sats=int(p["amount_sats"]),
                bolt12_offer=offer_map[to_peer],
            ))

        return payments

    # =========================================================================
    # SETTLEMENT EXECUTION
    # =========================================================================

    def create_settlement_period(self) -> int:
        """Create a new settlement period record."""
        conn = self._get_connection()
        now = int(time.time())

        cursor = conn.execute("""
            INSERT INTO settlement_periods (start_time, end_time, status)
            VALUES (?, ?, 'pending')
        """, (now - SETTLEMENT_PERIOD_SECONDS, now))

        return cursor.lastrowid

    def record_contributions(
        self,
        period_id: int,
        results: List[SettlementResult],
        contributions: List[MemberContribution]
    ):
        """Record contributions and results for a settlement period."""
        conn = self._get_connection()

        # Create lookup for contributions
        contrib_map = {c.peer_id: c for c in contributions}

        total_fees = sum(r.fees_earned for r in results)

        for result in results:
            contrib = contrib_map.get(result.peer_id)
            if not contrib:
                continue

            conn.execute("""
                INSERT INTO settlement_contributions (
                    period_id, peer_id, capacity_sats, forwards_sats,
                    fees_earned_sats, uptime_pct, fair_share_sats, balance_sats,
                    rebalance_costs_sats, net_profit_sats
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                period_id,
                result.peer_id,
                contrib.capacity_sats,
                contrib.forwards_sats,
                result.fees_earned,
                contrib.uptime_pct,
                result.fair_share,
                result.balance,
                result.rebalance_costs,
                result.net_profit
            ))

        # Update period totals
        conn.execute("""
            UPDATE settlement_periods
            SET total_fees_sats = ?, total_members = ?
            WHERE period_id = ?
        """, (total_fees, len(results), period_id))

    def record_payments(self, period_id: int, payments: List[SettlementPayment]):
        """Record planned payments for a settlement period."""
        conn = self._get_connection()

        for payment in payments:
            conn.execute("""
                INSERT INTO settlement_payments (
                    period_id, from_peer_id, to_peer_id, amount_sats,
                    bolt12_offer, status
                ) VALUES (?, ?, ?, ?, ?, 'pending')
            """, (
                period_id,
                payment.from_peer,
                payment.to_peer,
                payment.amount_sats,
                payment.bolt12_offer
            ))

    async def execute_payment(self, payment: SettlementPayment) -> SettlementPayment:
        """
        Execute a single settlement payment via BOLT12.

        Args:
            payment: Payment to execute

        Returns:
            Updated payment with status and payment_hash
        """
        if not self.rpc:
            payment.status = "error"
            payment.error = "No RPC interface available"
            return payment

        try:
            # Use fetchinvoice to get invoice from BOLT12 offer
            invoice_result = self.rpc.fetchinvoice(
                offer=payment.bolt12_offer,
                amount_msat=f"{payment.amount_sats * 1000}msat"
            )

            if "invoice" not in invoice_result:
                payment.status = "error"
                payment.error = "Failed to fetch invoice from offer"
                return payment

            bolt12_invoice = invoice_result["invoice"]

            # Pay the invoice
            pay_result = self.rpc.pay(bolt12_invoice)

            if pay_result.get("status") == "complete":
                payment.status = "completed"
                payment.payment_hash = pay_result.get("payment_hash")
            else:
                payment.status = "error"
                payment.error = pay_result.get("message", "Payment failed")

        except Exception as e:
            payment.status = "error"
            payment.error = str(e)

        return payment

    def update_payment_status(
        self,
        period_id: int,
        from_peer: str,
        to_peer: str,
        status: str,
        payment_hash: Optional[str] = None,
        error: Optional[str] = None
    ):
        """Update payment status in database."""
        conn = self._get_connection()
        now = int(time.time())

        conn.execute("""
            UPDATE settlement_payments
            SET status = ?, payment_hash = ?, paid_at = ?, error = ?
            WHERE period_id = ? AND from_peer_id = ? AND to_peer_id = ?
        """, (status, payment_hash, now if status == "completed" else None, error,
              period_id, from_peer, to_peer))

    def complete_settlement_period(self, period_id: int):
        """Mark a settlement period as complete."""
        conn = self._get_connection()
        now = int(time.time())

        conn.execute("""
            UPDATE settlement_periods
            SET status = 'completed', settled_at = ?
            WHERE period_id = ?
        """, (now, period_id))

    # =========================================================================
    # REPORTING
    # =========================================================================

    def get_settlement_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent settlement periods."""
        conn = self._get_connection()
        rows = conn.execute("""
            SELECT period_id, start_time, end_time, status,
                   total_fees_sats, total_members, settled_at
            FROM settlement_periods
            ORDER BY period_id DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(row) for row in rows]

    def get_period_details(self, period_id: int) -> Dict[str, Any]:
        """Get detailed information about a settlement period."""
        conn = self._get_connection()

        # Get period info
        period = conn.execute("""
            SELECT * FROM settlement_periods WHERE period_id = ?
        """, (period_id,)).fetchone()

        if not period:
            return {"error": "Period not found"}

        # Get contributions
        contributions = conn.execute("""
            SELECT * FROM settlement_contributions WHERE period_id = ?
        """, (period_id,)).fetchall()

        # Get payments
        payments = conn.execute("""
            SELECT * FROM settlement_payments WHERE period_id = ?
        """, (period_id,)).fetchall()

        return {
            "period": dict(period),
            "contributions": [dict(c) for c in contributions],
            "payments": [dict(p) for p in payments]
        }

    def get_member_settlement_history(
        self,
        peer_id: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Get settlement history for a specific member."""
        conn = self._get_connection()
        rows = conn.execute("""
            SELECT c.*, p.start_time, p.end_time, p.status as period_status
            FROM settlement_contributions c
            JOIN settlement_periods p ON c.period_id = p.period_id
            WHERE c.peer_id = ?
            ORDER BY c.period_id DESC
            LIMIT ?
        """, (peer_id, limit)).fetchall()
        return [dict(row) for row in rows]

    # =========================================================================
    # DISTRIBUTED SETTLEMENT (Phase 12)
    # =========================================================================

    @staticmethod
    def get_period_string(timestamp: Optional[int] = None) -> str:
        """
        Get the YYYY-WW period string for a given timestamp.

        Args:
            timestamp: Unix timestamp (defaults to now)

        Returns:
            Period string in YYYY-WW format (ISO week)
        """
        import datetime
        if timestamp is None:
            timestamp = int(time.time())
        dt = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
        iso_year, iso_week, _ = dt.isocalendar()
        return f"{iso_year}-{iso_week:02d}"

    @staticmethod
    def get_previous_period() -> str:
        """Get the period string for the previous week."""
        import datetime
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        prev_week = now - datetime.timedelta(weeks=1)
        iso_year, iso_week, _ = prev_week.isocalendar()
        return f"{iso_year}-{iso_week:02d}"

    @staticmethod
    def calculate_settlement_hash(
        period: str,
        contributions: List[Dict[str, Any]]
    ) -> str:
        """
        Calculate the canonical hash for settlement data.

        This ensures all nodes calculate the same amounts by using
        a deterministic hash of the contribution data.

        Args:
            period: Settlement period (YYYY-WW)
            contributions: List of contribution dicts with peer_id, fees_earned, capacity, costs

        Returns:
            SHA256 hash (64 hex chars)
        """
        import hashlib

        # Sort contributions by peer_id for determinism
        sorted_contribs = sorted(contributions, key=lambda x: x.get('peer_id', ''))

        # Build canonical string - include costs for net profit settlement (Issue #42)
        canonical_parts = [period]
        for c in sorted_contribs:
            peer_id = c.get('peer_id', '')
            fees = c.get('fees_earned', 0)
            costs = c.get('rebalance_costs', 0)
            capacity = c.get('capacity', 0)
            uptime = c.get('uptime', 100)
            canonical_parts.append(f"{peer_id}:{fees}:{costs}:{capacity}:{uptime}")

        canonical_string = "|".join(canonical_parts)
        return hashlib.sha256(canonical_string.encode()).hexdigest()

    def gather_contributions_from_gossip(
        self,
        state_manager,
        period: str
    ) -> List[Dict[str, Any]]:
        """
        Gather contribution data from gossiped FEE_REPORT messages.

        This uses PERSISTED fee reports from the database (survives restarts),
        falling back to in-memory state_manager data if needed.

        Args:
            state_manager: HiveStateManager with gossiped fee data
            period: Settlement period (for filtering)

        Returns:
            List of contribution dicts with peer_id, fees_earned, rebalance_costs, capacity, uptime
        """
        contributions = []

        # Get all members
        all_members = self.db.get_all_members()

        # Get persisted fee reports for this period from database
        db_fee_reports = self.db.get_fee_reports_for_period(period)
        db_fees_by_peer = {r['peer_id']: r for r in db_fee_reports}

        for member in all_members:
            peer_id = member['peer_id']

            # First try database (persisted), then fall back to state manager (in-memory)
            if peer_id in db_fees_by_peer:
                db_report = db_fees_by_peer[peer_id]
                fees_earned = db_report.get('fees_earned_sats', 0)
                forward_count = db_report.get('forward_count', 0)
                rebalance_costs = db_report.get('rebalance_costs_sats', 0)
            else:
                # Fall back to in-memory state (may be from current session)
                fee_data = state_manager.get_peer_fees(peer_id)
                fees_earned = fee_data.get('fees_earned_sats', 0)
                forward_count = fee_data.get('forward_count', 0)
                rebalance_costs = fee_data.get('rebalance_costs_sats', 0)

            # Get capacity from state
            peer_state = state_manager.get_peer_state(peer_id)

            # Canonicalize uptime for hashing/settlement math.
            # hive_members.uptime_pct is stored as a fraction (0-1) by
            # HiveDatabase.sync_uptime_from_presence(); some older code paths
            # may store a percentage (0-100). Normalize to an integer percent.
            uptime_val = member.get('uptime_pct', 1.0)
            try:
                uptime_f = float(uptime_val)
                if uptime_f <= 1.0:
                    uptime_f *= 100.0
                uptime = int(round(max(0.0, min(100.0, uptime_f))))
            except Exception:
                uptime = 100

            # Phase 16: Get reputation tier for settlement terms metadata
            reputation_tier = "newcomer"
            if self.did_credential_mgr:
                try:
                    reputation_tier = self.did_credential_mgr.get_credit_tier(peer_id)
                except Exception:
                    pass

            contributions.append({
                'peer_id': peer_id,
                'fees_earned': fees_earned,
                'rebalance_costs': rebalance_costs,
                'capacity': peer_state.capacity_sats if peer_state else 0,
                'uptime': uptime,
                'forward_count': forward_count,
                'reputation_tier': reputation_tier,
            })

        return contributions

    def create_proposal(
        self,
        period: str,
        our_peer_id: str,
        state_manager,
        rpc
    ) -> Optional[Dict[str, Any]]:
        """
        Create a settlement proposal for a given period.

        This gathers contribution data from gossiped FEE_REPORT messages,
        calculates the canonical hash, and creates the proposal.

        Args:
            period: Settlement period (YYYY-WW)
            our_peer_id: Our node's public key
            state_manager: HiveStateManager with gossiped fee data
            rpc: RPC proxy for signing

        Returns:
            Proposal dict if created, None if period already has proposal
        """
        import secrets

        # Check if period already has a proposal
        existing = self.db.get_settlement_proposal_by_period(period)
        if existing:
            self.plugin.log(
                f"Settlement proposal already exists for {period}",
                level='debug'
            )
            return None

        # Check if period is already settled
        if self.db.is_period_settled(period):
            self.plugin.log(
                f"Period {period} already settled",
                level='debug'
            )
            return None

        # Gather contribution data from gossip
        contributions = self.gather_contributions_from_gossip(state_manager, period)

        if not contributions:
            self.plugin.log("No contributions to settle", level='debug')
            return None

        # Calculate canonical hash + deterministic payment plan hash.
        plan = self.compute_settlement_plan(period, contributions)
        data_hash = plan["data_hash"]
        plan_hash = plan["plan_hash"]

        # Calculate totals
        total_fees = plan["total_fees_sats"]
        member_count = len(contributions)

        # Skip zero-fee periods: they add noise to participation metrics and
        # create "successful" settlements with no economic transfer.
        if total_fees <= 0:
            self.plugin.log(
                f"Skipping settlement proposal for {period}: total_fees_sats=0",
                level='debug'
            )
            return None

        # Generate proposal ID
        proposal_id = secrets.token_hex(16)
        timestamp = int(time.time())

        # Create proposal in database (store contributions for rebroadcast - Issue #49)
        contributions_json = json.dumps(contributions)
        if not self.db.add_settlement_proposal(
            proposal_id=proposal_id,
            period=period,
            proposer_peer_id=our_peer_id,
            data_hash=data_hash,
            plan_hash=plan_hash,
            total_fees_sats=total_fees,
            member_count=member_count,
            contributions_json=contributions_json
        ):
            return None

        self.plugin.log(
            f"Created settlement proposal {proposal_id[:16]}... for {period}: "
            f"{total_fees} sats, {member_count} members"
        )

        return {
            'proposal_id': proposal_id,
            'period': period,
            'proposer_peer_id': our_peer_id,
            'data_hash': data_hash,
            'plan_hash': plan_hash,
            'total_fees_sats': total_fees,
            'member_count': member_count,
            'contributions': contributions,
            'timestamp': timestamp,
        }

    def verify_and_vote(
        self,
        proposal: Dict[str, Any],
        our_peer_id: str,
        state_manager,
        rpc,
        skip_hash_verify: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Verify a settlement proposal's data hash and vote if it matches.

        This independently calculates the data hash from gossiped FEE_REPORT
        data and votes if it matches the proposal.

        Args:
            proposal: Proposal dict from SETTLEMENT_PROPOSE message
            our_peer_id: Our node's public key
            state_manager: HiveStateManager with gossiped fee data
            rpc: RPC proxy for signing
            skip_hash_verify: If True, skip hash re-verification (for proposer's
                own auto-vote where data was just computed)

        Returns:
            Vote dict if vote cast, None if hash mismatch or already voted
        """
        proposal_id = proposal.get('proposal_id')
        period = proposal.get('period')
        proposed_hash = proposal.get('data_hash')
        proposed_plan_hash = proposal.get('plan_hash')

        # Check if we already voted
        if self.db.has_voted_settlement(proposal_id, our_peer_id):
            self.plugin.log(
                f"Already voted on proposal {proposal_id[:16]}...",
                level='debug'
            )
            return None

        # Check if period already settled
        if self.db.is_period_settled(period):
            self.plugin.log(
                f"Period {period} already settled, skipping vote",
                level='debug'
            )
            return None

        if not skip_hash_verify:
            # Gather our own contribution data and calculate hashes.
            # We verify both the canonical data hash and the derived deterministic plan hash.
            our_contributions = self.gather_contributions_from_gossip(state_manager, period)
            our_plan = self.compute_settlement_plan(period, our_contributions)
            our_hash = our_plan["data_hash"]
            our_plan_hash = our_plan["plan_hash"]

            # Verify hash matches
            if our_hash != proposed_hash:
                self.plugin.log(
                    f"Hash mismatch for proposal {proposal_id[:16]}...: "
                    f"ours={our_hash[:16]}... theirs={proposed_hash[:16]}...",
                    level='warn'
                )
                return None

            if not isinstance(proposed_plan_hash, str) or len(proposed_plan_hash) != 64:
                self.plugin.log(
                    f"Missing/invalid plan_hash for proposal {proposal_id[:16]}...",
                    level='warn'
                )
                return None

            if our_plan_hash != proposed_plan_hash:
                self.plugin.log(
                    f"Plan hash mismatch for proposal {proposal_id[:16]}...: "
                    f"ours={our_plan_hash[:16]}... theirs={proposed_plan_hash[:16]}...",
                    level='warn'
                )
                return None

        # When skipping verification, trust the proposal's hash (proposer auto-vote)
        data_hash_for_vote = our_hash if not skip_hash_verify else proposed_hash

        timestamp = int(time.time())

        # Sign the vote
        from modules.protocol import get_settlement_ready_signing_payload
        vote_payload = {
            'proposal_id': proposal_id,
            'voter_peer_id': our_peer_id,
            'data_hash': data_hash_for_vote,
            'timestamp': timestamp,
        }
        signing_payload = get_settlement_ready_signing_payload(vote_payload)

        try:
            sig_result = rpc.signmessage(signing_payload)
            signature = sig_result.get('zbase', '')
        except Exception as e:
            self.plugin.log(f"Failed to sign settlement vote: {e}", level='warn')
            return None

        # Record vote in database
        if not self.db.add_settlement_ready_vote(
            proposal_id=proposal_id,
            voter_peer_id=our_peer_id,
            data_hash=data_hash_for_vote,
            signature=signature
        ):
            return None

        self.plugin.log(
            f"Voted on settlement proposal {proposal_id[:16]}... "
            f"({'proposer auto-vote' if skip_hash_verify else 'hash verified'})"
        )

        return {
            'proposal_id': proposal_id,
            'voter_peer_id': our_peer_id,
            'data_hash': data_hash_for_vote,
            'timestamp': timestamp,
            'signature': signature,
        }

    def check_quorum_and_mark_ready(
        self,
        proposal_id: str,
        member_count: int
    ) -> bool:
        """
        Check if a proposal has reached quorum (51%) and mark it ready.

        Args:
            proposal_id: Proposal to check
            member_count: Total number of members in the proposal

        Returns:
            True if quorum reached and status updated
        """
        vote_count = self.db.count_settlement_ready_votes(proposal_id)
        quorum_needed = (member_count // 2) + 1

        if vote_count >= quorum_needed:
            proposal = self.db.get_settlement_proposal(proposal_id)
            if proposal and proposal.get('status') == 'pending':
                self.db.update_settlement_proposal_status(proposal_id, 'ready')
                self.plugin.log(
                    f"Settlement proposal {proposal_id[:16]}... reached quorum "
                    f"({vote_count}/{member_count})"
                )
                return True

        return False

    def calculate_our_balance(
        self,
        proposal: Dict[str, Any],
        contributions: List[Dict[str, Any]],
        our_peer_id: str
    ) -> Tuple[int, Optional[str], int]:
        """
        Calculate our balance in a settlement using the deterministic plan.

        Uses compute_settlement_plan() to ensure results are consistent
        with what execute_our_settlement() would actually pay.

        Args:
            proposal: Proposal dict
            contributions: List of contribution dicts
            our_peer_id: Our node's public key

        Returns:
            Tuple of (balance_sats, creditor_peer_id or None, min_payment_threshold)
              balance > 0: we are owed money (net receiver)
              balance < 0: we owe money (net payer)
        """
        period = proposal.get('period', '') if isinstance(proposal, dict) else str(proposal)
        plan = self.compute_settlement_plan(period, contributions)
        min_payment = plan["min_payment_sats"]

        # Determine our net position from the deterministic payment plan
        expected_sent = int(plan["expected_sent_sats"].get(our_peer_id, 0))
        expected_received = sum(
            int(p["amount_sats"]) for p in plan["payments"]
            if p.get("to_peer") == our_peer_id
        )

        # Positive = net receiver (owed money), negative = net payer (owe money)
        balance = expected_received - expected_sent

        # Find who we owe the most to (primary creditor)
        creditor = None
        if expected_sent > 0:
            our_payments = sorted(
                [p for p in plan["payments"] if p.get("from_peer") == our_peer_id],
                key=lambda p: -int(p["amount_sats"])
            )
            if our_payments:
                creditor = our_payments[0]["to_peer"]

        return (balance, creditor, min_payment)

    async def execute_our_settlement(
        self,
        proposal: Dict[str, Any],
        contributions: List[Dict[str, Any]],
        our_peer_id: str,
        rpc
    ) -> Optional[Dict[str, Any]]:
        """
        Execute our settlement payment if we owe money.

        Args:
            proposal: Proposal dict
            contributions: List of contribution dicts
            our_peer_id: Our node's public key
            rpc: RPC proxy for payment

        Returns:
            Execution result dict if payment made, None otherwise
        """
        proposal_id = proposal.get('proposal_id')
        period = proposal.get('period')
        if not proposal_id or not period:
            return None

        # Check if already executed
        if self.db.has_executed_settlement(proposal_id, our_peer_id):
            self.plugin.log(
                f"Already executed settlement for {proposal_id[:16]}...",
                level='debug'
            )
            return None

        # Compute the authoritative plan from the proposal's canonical contributions snapshot.
        plan = self.compute_settlement_plan(period, contributions)
        expected_plan_hash = proposal.get("plan_hash")
        if isinstance(expected_plan_hash, str) and len(expected_plan_hash) == 64:
            if plan["plan_hash"] != expected_plan_hash:
                self.plugin.log(
                    f"SETTLEMENT: Refusing to execute {proposal_id[:16]}... "
                    f"(plan hash mismatch ours={plan['plan_hash'][:16]}... "
                    f"theirs={expected_plan_hash[:16]}...)",
                    level="warn"
                )
                return None

        expected_sent = int(plan["expected_sent_sats"].get(our_peer_id, 0))
        our_payments = [p for p in plan["payments"] if p.get("from_peer") == our_peer_id]

        total_sent = 0
        payment_hashes: List[str] = []

        for p in our_payments:
            to_peer = p["to_peer"]
            amount = int(p["amount_sats"])

            # Check if we already paid this sub-payment (crash recovery)
            already_paid = self.db.get_settlement_sub_payment(proposal_id, our_peer_id, to_peer) if self.db else None
            if already_paid and already_paid.get("status") == "completed":
                self.plugin.log(
                    f"SETTLEMENT: Skipping already-completed payment to {to_peer[:16]}... "
                    f"({amount} sats, proposal {proposal_id[:16]}...)",
                    level="info"
                )
                total_sent += amount
                ph = already_paid.get("payment_hash", "")
                if ph:
                    payment_hashes.append(ph)
                continue

            offer = self.get_offer(to_peer)
            if not offer:
                self.plugin.log(
                    f"SETTLEMENT: Missing BOLT12 offer for receiver {to_peer[:16]}... "
                    f"(proposal {proposal_id[:16]}...)",
                    level="warn"
                )
                return None

            pay = SettlementPayment(
                from_peer=our_peer_id,
                to_peer=to_peer,
                amount_sats=amount,
                bolt12_offer=offer,
            )
            pay = await self.execute_payment(pay)
            if pay.status != "completed":
                self.plugin.log(
                    f"SETTLEMENT: Payment failed to {to_peer[:16]}... for {amount} sats: {pay.error}",
                    level="warn"
                )
                return None

            # Record successful sub-payment for crash recovery
            if self.db:
                self.db.record_settlement_sub_payment(
                    proposal_id, our_peer_id, to_peer, amount,
                    pay.payment_hash or "", "completed"
                )

            total_sent += amount
            if pay.payment_hash:
                payment_hashes.append(pay.payment_hash)

        if total_sent != expected_sent:
            self.plugin.log(
                f"SETTLEMENT: Refusing to confirm execution for {proposal_id[:16]}... "
                f"(sent {total_sent} sats, expected {expected_sent} sats)",
                level="warn"
            )
            return None

        timestamp = int(time.time())
        from modules.protocol import get_settlement_executed_signing_payload
        exec_payload = {
            'proposal_id': proposal_id,
            'executor_peer_id': our_peer_id,
            'plan_hash': plan["plan_hash"],
            # total_sent is the authoritative value for completion checks.
            'total_sent_sats': total_sent,
            # Keep legacy fields for older listeners; these are informational only.
            'payment_hash': payment_hashes[0] if len(payment_hashes) == 1 else '',
            'amount_paid_sats': total_sent,
            'timestamp': timestamp,
        }
        signing_payload = get_settlement_executed_signing_payload(exec_payload)
        sig_result = rpc.signmessage(signing_payload)
        signature = sig_result.get('zbase', '')

        self.db.add_settlement_execution(
            proposal_id=proposal_id,
            executor_peer_id=our_peer_id,
            signature=signature,
            payment_hash=exec_payload.get("payment_hash") or None,
            amount_paid_sats=total_sent,
            plan_hash=plan["plan_hash"],
        )

        return {
            'proposal_id': proposal_id,
            'executor_peer_id': our_peer_id,
            'plan_hash': plan["plan_hash"],
            'total_sent_sats': total_sent,
            'payment_hash': exec_payload.get("payment_hash") or None,
            'amount_paid_sats': total_sent,
            'timestamp': timestamp,
            'signature': signature,
        }

    def check_and_complete_settlement(self, proposal_id: str) -> bool:
        """
        Check if all members have executed and complete the settlement.

        Args:
            proposal_id: Proposal to check

        Returns:
            True if settlement completed
        """
        proposal = self.db.get_settlement_proposal(proposal_id)
        if not proposal:
            return False

        if proposal.get('status') != 'ready':
            return False

        period = proposal.get('period')
        member_count = proposal.get('member_count', 0)
        total_fees = proposal.get('total_fees_sats', 0)

        # Get all executions
        executions = self.db.get_settlement_executions(proposal_id)
        exec_count = len(executions)

        # Require a canonical contributions snapshot to validate the plan.
        contributions_json = proposal.get("contributions_json")
        if not contributions_json:
            return False
        try:
            contributions = json.loads(contributions_json)
        except Exception:
            return False

        plan = self.compute_settlement_plan(period, contributions)
        expected_plan_hash = proposal.get("plan_hash")
        if isinstance(expected_plan_hash, str) and len(expected_plan_hash) == 64:
            if plan["plan_hash"] != expected_plan_hash:
                self.plugin.log(
                    f"SETTLEMENT: Cannot complete {proposal_id[:16]}... (plan hash mismatch)",
                    level="warn"
                )
                return False

        # Only require execution from members who have payments to make.
        # Receivers (positive balance) don't send payments and shouldn't
        # block settlement completion by being offline.
        payers = {
            pid: amount
            for pid, amount in plan["expected_sent_sats"].items()
            if amount > 0
        }

        if not payers:
            # No payments needed (all balances within min_payment threshold)
            self.db.update_settlement_proposal_status(proposal_id, 'completed')
            self.db.mark_period_settled(period, proposal_id, 0)
            self.plugin.log(
                f"Settlement {proposal_id[:16]}... completed (no payments needed)"
            )
            return True

        executions_by_peer = {e.get("executor_peer_id"): e for e in executions}

        for peer_id, expected_amount in payers.items():
            ex = executions_by_peer.get(peer_id)
            if not ex:
                return False

            # Check plan hash binding (newer clients).
            ex_plan_hash = ex.get("plan_hash")
            if isinstance(ex_plan_hash, str) and len(ex_plan_hash) == 64:
                if ex_plan_hash != plan["plan_hash"]:
                    return False

            actual_sent = int(ex.get("amount_paid_sats", 0) or 0)
            if actual_sent != expected_amount:
                return False

        # All payers have confirmed correctly - mark as complete
        total_distributed = sum(payers.values())
        self.db.update_settlement_proposal_status(proposal_id, 'completed')
        self.db.mark_period_settled(period, proposal_id, total_distributed)

        self.plugin.log(
            f"Settlement {proposal_id[:16]}... completed: "
            f"{total_distributed} sats distributed for {period}"
        )
        return True

    def get_distributed_settlement_status(self) -> Dict[str, Any]:
        """
        Get current distributed settlement status for monitoring.

        Returns:
            Status dict with pending/ready proposals, recent settlements
        """
        pending = self.db.get_pending_settlement_proposals()
        ready = self.db.get_ready_settlement_proposals()
        settled = self.db.get_settled_periods(limit=5)

        return {
            'pending_proposals': len(pending),
            'ready_proposals': len(ready),
            'recent_settlements': len(settled),
            'pending': pending,
            'ready': ready,
            'settled_periods': settled,
        }

    def register_extended_types(self, cashu_escrow_mgr, did_credential_mgr):
        """Wire Phase 4 managers after init."""
        self.cashu_escrow_mgr = cashu_escrow_mgr
        self.did_credential_mgr = did_credential_mgr
        if hasattr(self, '_type_registry'):
            self._type_registry.cashu_escrow_mgr = cashu_escrow_mgr
            self._type_registry.did_credential_mgr = did_credential_mgr


# =============================================================================
# PHASE 4B: SETTLEMENT TYPE REGISTRY
# =============================================================================

VALID_SETTLEMENT_TYPE_IDS = frozenset([
    "routing_revenue", "rebalancing_cost", "channel_lease",
    "cooperative_splice", "shared_channel", "pheromone_market",
    "intelligence", "penalty", "advisor_fee",
])

# Bond tier sizing (sats)
BOND_TIER_SIZING = {
    "observer": 0,
    "basic": 50_000,
    "full": 150_000,
    "liquidity": 300_000,
    "founding": 500_000,
}

# Credit tier definitions
CREDIT_TIERS = {
    "newcomer": {"credit_line": 0, "window": "per_event", "model": "prepaid_escrow"},
    "recognized": {"credit_line": 10_000, "window": "hourly", "model": "escrow_above_credit"},
    "trusted": {"credit_line": 50_000, "window": "daily", "model": "bilateral_netting"},
    "senior": {"credit_line": 200_000, "window": "weekly", "model": "multilateral_netting"},
}


class SettlementTypeHandler:
    """Base class for settlement type handlers."""

    type_id: str = ""

    def calculate(self, obligations: List[Dict], window_id: str) -> List[Dict]:
        """Calculate settlement amounts for this type. Returns obligation dicts."""
        return obligations

    def verify_receipt(self, receipt_data: Dict) -> Tuple[bool, str]:
        """Verify a settlement receipt for this type. Returns (valid, error_msg)."""
        return True, ""

    def execute(self, payment: Dict, rpc=None) -> Optional[Dict]:
        """Execute a settlement payment. Returns result or None."""
        return None


class RoutingRevenueHandler(SettlementTypeHandler):
    type_id = "routing_revenue"

    def verify_receipt(self, receipt_data: Dict) -> Tuple[bool, str]:
        if "htlc_forwards" not in receipt_data:
            return False, "missing htlc_forwards"
        if not isinstance(receipt_data.get("htlc_forwards"), (list, int)):
            return False, "htlc_forwards must be list or count"
        return True, ""


class RebalancingCostHandler(SettlementTypeHandler):
    type_id = "rebalancing_cost"

    def verify_receipt(self, receipt_data: Dict) -> Tuple[bool, str]:
        if "rebalance_amount_sats" not in receipt_data:
            return False, "missing rebalance_amount_sats"
        return True, ""


class ChannelLeaseHandler(SettlementTypeHandler):
    type_id = "channel_lease"

    def verify_receipt(self, receipt_data: Dict) -> Tuple[bool, str]:
        if "lease_start" not in receipt_data or "lease_end" not in receipt_data:
            return False, "missing lease_start or lease_end"
        return True, ""


class CooperativeSpliceHandler(SettlementTypeHandler):
    type_id = "cooperative_splice"

    def verify_receipt(self, receipt_data: Dict) -> Tuple[bool, str]:
        if "txid" not in receipt_data:
            return False, "missing txid"
        return True, ""


class SharedChannelHandler(SettlementTypeHandler):
    type_id = "shared_channel"

    def verify_receipt(self, receipt_data: Dict) -> Tuple[bool, str]:
        if "funding_txid" not in receipt_data:
            return False, "missing funding_txid"
        return True, ""


class PheromoneMarketHandler(SettlementTypeHandler):
    type_id = "pheromone_market"

    def verify_receipt(self, receipt_data: Dict) -> Tuple[bool, str]:
        if "performance_metric" not in receipt_data:
            return False, "missing performance_metric"
        return True, ""


class IntelligenceHandler(SettlementTypeHandler):
    type_id = "intelligence"

    def calculate(self, obligations: List[Dict], window_id: str) -> List[Dict]:
        """Apply 70/30 base/bonus split."""
        result = []
        for ob in obligations:
            amount = ob.get("amount_sats", 0)
            base = amount * 70 // 100
            bonus = amount - base
            result.append({**ob, "base_sats": base, "bonus_sats": bonus})
        return result

    def verify_receipt(self, receipt_data: Dict) -> Tuple[bool, str]:
        if "intelligence_type" not in receipt_data:
            return False, "missing intelligence_type"
        return True, ""


class PenaltyHandler(SettlementTypeHandler):
    type_id = "penalty"

    def verify_receipt(self, receipt_data: Dict) -> Tuple[bool, str]:
        if "quorum_confirmations" not in receipt_data:
            return False, "missing quorum_confirmations"
        confirmations = receipt_data["quorum_confirmations"]
        if not isinstance(confirmations, int) or confirmations < 1:
            return False, "quorum_confirmations must be >= 1"
        return True, ""


class AdvisorFeeHandler(SettlementTypeHandler):
    type_id = "advisor_fee"

    def verify_receipt(self, receipt_data: Dict) -> Tuple[bool, str]:
        if "advisor_signature" not in receipt_data:
            return False, "missing advisor_signature"
        return True, ""


class SettlementTypeRegistry:
    """Registry of settlement type handlers."""

    def __init__(self, cashu_escrow_mgr=None, database=None, plugin=None,
                 did_credential_mgr=None, **kwargs):
        self.handlers: Dict[str, SettlementTypeHandler] = {}
        self.cashu_escrow_mgr = cashu_escrow_mgr
        self.database = database
        self.plugin = plugin
        self.did_credential_mgr = did_credential_mgr
        self._register_defaults()

    def _register_defaults(self):
        for handler_cls in [
            RoutingRevenueHandler, RebalancingCostHandler, ChannelLeaseHandler,
            CooperativeSpliceHandler, SharedChannelHandler, PheromoneMarketHandler,
            IntelligenceHandler, PenaltyHandler, AdvisorFeeHandler,
        ]:
            handler = handler_cls()
            self.handlers[handler.type_id] = handler

    def get_handler(self, type_id: str) -> Optional[SettlementTypeHandler]:
        return self.handlers.get(type_id)

    def list_types(self) -> List[str]:
        return list(self.handlers.keys())

    def verify_receipt(self, type_id: str, receipt_data: Dict) -> Tuple[bool, str]:
        handler = self.get_handler(type_id)
        if not handler:
            return False, f"unknown settlement type: {type_id}"
        return handler.verify_receipt(receipt_data)


# =============================================================================
# PHASE 4B: NETTING ENGINE
# =============================================================================

import hashlib


class NettingEngine:
    """
    Compute net payments from obligation sets.

    All computations use integer sats (no floats).
    Deterministic JSON serialization for obligation hashing.

    P4R4-L-2: Callers should compute obligations_hash before netting,
    then re-verify against the obligation snapshot at execution time
    to detect stale data.  bilateral_net() and multilateral_net()
    include the obligations_hash in their return value for this purpose.
    """

    @staticmethod
    def compute_obligations_hash(obligations: List[Dict]) -> str:
        """Compute deterministic hash of an obligation set."""
        canonical = json.dumps(
            sorted(obligations, key=lambda o: o.get("obligation_id", "")),
            sort_keys=True,
            separators=(',', ':'),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def verify_obligations_hash(obligations: List[Dict],
                                expected_hash: str) -> bool:
        """Verify obligations have not changed since hash was computed.

        P4R4-L-2: Call this at execution time to guard against stale data.
        """
        return NettingEngine.compute_obligations_hash(obligations) == expected_hash

    @staticmethod
    def bilateral_net(obligations: List[Dict],
                      peer_a: str, peer_b: str,
                      window_id: str) -> Dict[str, Any]:
        """
        Compute bilateral net between two peers.

        Returns single net payment direction + amount.
        Includes obligations_hash for staleness verification at execution time.
        """
        # P4R4-L-2: Compute hash at netting time so callers can re-verify
        # at execution time to detect stale obligations.
        ob_hash = NettingEngine.compute_obligations_hash(obligations)

        a_to_b = 0  # total A owes B
        b_to_a = 0  # total B owes A

        for ob in obligations:
            if ob.get("window_id") != window_id:
                continue
            if ob.get("status") != "pending":
                continue
            amount = ob.get("amount_sats", 0)
            if amount <= 0:
                continue
            from_p = ob.get("from_peer", "")
            to_p = ob.get("to_peer", "")
            if from_p == to_p:
                continue
            if from_p == peer_a and to_p == peer_b:
                a_to_b += amount
            elif from_p == peer_b and to_p == peer_a:
                b_to_a += amount

        net = a_to_b - b_to_a
        if net > 0:
            return {
                "from_peer": peer_a,
                "to_peer": peer_b,
                "amount_sats": net,
                "window_id": window_id,
                "obligations_netted": a_to_b + b_to_a,
                "obligations_hash": ob_hash,
            }
        elif net < 0:
            return {
                "from_peer": peer_b,
                "to_peer": peer_a,
                "amount_sats": -net,
                "window_id": window_id,
                "obligations_netted": a_to_b + b_to_a,
                "obligations_hash": ob_hash,
            }
        else:
            return {
                "from_peer": peer_a,
                "to_peer": peer_b,
                "amount_sats": 0,
                "window_id": window_id,
                "obligations_netted": a_to_b + b_to_a,
                "obligations_hash": ob_hash,
            }

    @staticmethod
    def multilateral_net(obligations: List[Dict],
                         window_id: str) -> List[Dict[str, Any]]:
        """
        Compute multilateral net from obligation set.

        Uses balance aggregation to find minimum payment set.
        All integer arithmetic.

        Returns list of net payments.

        P4R4-L-2: Callers should snapshot obligations and use
        verify_obligations_hash() at execution time to guard
        against stale obligation data.
        """
        # Aggregate net balances per peer
        balances: Dict[str, int] = {}
        for ob in obligations:
            if ob.get("window_id") != window_id:
                continue
            if ob.get("status") != "pending":
                continue
            amount = ob.get("amount_sats", 0)
            if amount <= 0:
                continue
            from_p = ob.get("from_peer", "")
            to_p = ob.get("to_peer", "")
            if not from_p or not to_p:
                continue
            if from_p == to_p:
                continue
            balances[from_p] = balances.get(from_p, 0) - amount
            balances[to_p] = balances.get(to_p, 0) + amount

        # Split into debtors (negative balance) and creditors (positive balance)
        debtors = []
        creditors = []
        for peer, balance in sorted(balances.items()):
            if balance < 0:
                debtors.append([peer, -balance])  # amount they owe
            elif balance > 0:
                creditors.append([peer, balance])  # amount they're owed

        # Greedy matching: match debtors with creditors in deterministic peer_id order
        payments = []
        di, ci = 0, 0
        while di < len(debtors) and ci < len(creditors):
            debtor_id, debt = debtors[di]
            creditor_id, credit = creditors[ci]
            pay = min(debt, credit)
            if pay > 0:
                payments.append({
                    "from_peer": debtor_id,
                    "to_peer": creditor_id,
                    "amount_sats": pay,
                    "window_id": window_id,
                })
            debtors[di][1] -= pay
            creditors[ci][1] -= pay
            if debtors[di][1] == 0:
                di += 1
            if creditors[ci][1] == 0:
                ci += 1

        return payments


# =============================================================================
# PHASE 4B: BOND MANAGER
# =============================================================================

class BondManager:
    """
    Manages settlement bonds: post, verify, slash, refund.

    Bond sizing:
        observer: 0, basic: 50K, full: 150K, liquidity: 300K, founding: 500K sats

    Time-weighted staking:
        effective_bond = amount * min(1.0, tenure_days / 180)

    Slashing formula:
        max(penalty * severity * repeat_mult, estimated_profit * 2.0)

    Distribution: 50% aggrieved, 30% panel, 20% burned
    """

    TENURE_MATURITY_DAYS = 180
    SLASH_DISTRIBUTION = {"aggrieved": 0.50, "panel": 0.30, "burned": 0.20}
    # P4R4-M-3: Class-level lock shared across all instances to provide
    # cross-request protection even if BondManager is instantiated per-message.
    _bond_lock = threading.Lock()

    def __init__(self, database, plugin, rpc=None):
        self.db = database
        self.plugin = plugin
        self.rpc = rpc

    def _log(self, msg: str, level: str = 'info') -> None:
        self.plugin.log(f"cl-hive: bonds: {msg}", level=level)

    def get_tier_for_amount(self, amount_sats: int) -> str:
        """Determine bond tier based on amount."""
        for tier in ["founding", "liquidity", "full", "basic", "observer"]:
            if amount_sats >= BOND_TIER_SIZING[tier]:
                return tier
        return "observer"

    def effective_bond(self, amount_sats: int, tenure_days: int) -> int:
        """Calculate time-weighted effective bond amount (integer arithmetic)."""
        if tenure_days >= self.TENURE_MATURITY_DAYS:
            return amount_sats
        return amount_sats * tenure_days // self.TENURE_MATURITY_DAYS

    def post_bond(self, peer_id: str, amount_sats: int,
                  token_json: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Post a new bond for a peer."""
        if amount_sats <= 0:
            return None

        # Reject if peer already has an active bond (allow re-bonding after slash/refund)
        existing = self.db.get_bond_for_peer(peer_id)
        if existing:
            self._log(f"bond rejected: {peer_id[:16]}... already has active bond")
            return None

        tier = self.get_tier_for_amount(amount_sats)
        nonce = os.urandom(16).hex()
        bond_id = hashlib.sha256(
            f"bond:{peer_id}:{int(time.time())}:{nonce}".encode()
        ).hexdigest()[:32]

        # 6-month timelock for refund path
        timelock = int(time.time()) + (180 * 86400)

        success = self.db.store_bond(
            bond_id=bond_id,
            peer_id=peer_id,
            amount_sats=amount_sats,
            token_json=token_json,
            posted_at=int(time.time()),
            timelock=timelock,
            tier=tier,
        )

        if not success:
            return None

        self._log(f"bond {bond_id[:16]}... posted by {peer_id[:16]}... "
                  f"amount={amount_sats} tier={tier}")

        return {
            "bond_id": bond_id,
            "peer_id": peer_id,
            "amount_sats": amount_sats,
            "tier": tier,
            "timelock": timelock,
            "status": "active",
        }

    def calculate_slash(self, penalty_base: int, severity: float = 1.0,
                        repeat_count: int = 1,
                        estimated_profit: int = 0) -> int:
        """
        Calculate slash amount (integer arithmetic).

        Formula: max(penalty * severity * repeat_mult, estimated_profit * 2)
        """
        repeat_mult_1000 = 1000 + (500 * max(0, repeat_count - 1))
        # severity is a float 0.0-1.0, scale to integer
        severity_1000 = int(severity * 1000)
        option_a = penalty_base * severity_1000 * repeat_mult_1000 // 1_000_000
        option_b = estimated_profit * 2
        return max(option_a, option_b)

    def distribute_slash(self, slash_amount: int) -> Dict[str, int]:
        """Distribute slashed funds per SLASH_DISTRIBUTION policy (integer arithmetic).

        P4R4-L-1: Uses pure integer arithmetic (// and * 100) to avoid
        floating-point rounding errors in sat amounts.
        Distribution: 50% aggrieved, 30% panel, 20% burned.
        """
        # Integer percentages: 50%, 30%, remainder to burned
        aggrieved = slash_amount * 50 // 100
        panel = slash_amount * 30 // 100
        burned = slash_amount - aggrieved - panel  # Remainder to burned
        return {
            "aggrieved": aggrieved,
            "panel": panel,
            "burned": burned,
        }

    def slash_bond(self, bond_id: str, slash_amount: int) -> Optional[Dict[str, Any]]:
        """Execute a bond slash."""
        with self._bond_lock:
            bond = self.db.get_bond(bond_id)
            if not bond:
                return None

            if bond['status'] != 'active':
                return None

            # Cap slash at bond amount
            prior_slashed = bond['slashed_amount']
            effective_slash = min(slash_amount, bond['amount_sats'] - prior_slashed)
            if effective_slash <= 0:
                return None

            success = self.db.slash_bond(bond_id, effective_slash)
            if not success:
                self._log(f"bond {bond_id[:16]}... slash failed at DB level", level='error')
                return None
            distribution = self.distribute_slash(effective_slash)

            remaining = bond['amount_sats'] - prior_slashed - effective_slash
            self._log(f"bond {bond_id[:16]}... slashed {effective_slash} sats")

            return {
                "bond_id": bond_id,
                "slashed_amount": effective_slash,
                "distribution": distribution,
                "remaining": remaining,
            }

    def refund_bond(self, bond_id: str) -> Optional[Dict[str, Any]]:
        """Refund a bond after timelock expiry."""
        with self._bond_lock:
            bond = self.db.get_bond(bond_id)
            if not bond:
                return None

            if bond['status'] not in ('active', 'slashed'):
                return {"error": f"bond status is {bond['status']}, cannot refund"}

            now = int(time.time())
            if now < bond['timelock']:
                return {"error": "timelock not expired", "timelock": bond['timelock']}

            remaining = bond['amount_sats'] - bond['slashed_amount']
            self.db.update_bond_status(bond_id, 'refunded')

        return {
            "bond_id": bond_id,
            "refund_amount": remaining,
            "status": "refunded",
        }

    def get_bond_status(self, peer_id: str) -> Optional[Dict[str, Any]]:
        """Get current bond status for a peer."""
        bond = self.db.get_bond_for_peer(peer_id)
        if not bond:
            return None

        tenure_days = (int(time.time()) - bond['posted_at']) // 86400
        effective = self.effective_bond(bond['amount_sats'], tenure_days)

        return {
            **bond,
            "tenure_days": tenure_days,
            "effective_bond": effective,
        }


# =============================================================================
# PHASE 4B: DISPUTE RESOLUTION
# =============================================================================

class DisputeResolver:
    """
    Deterministic dispute resolution with stake-weighted panel selection.

    Panel sizes:
    - >=15 eligible members: 7 members (5-of-7)
    - 10-14 eligible: 5 members (3-of-5)
    - 5-9 eligible: 3 members (2-of-3)

    Selection seed: SHA256(dispute_id || block_hash_at_filing_height)
    Weight: bond_amount + (tenure_days * 100)
    """

    MIN_ELIGIBLE_FOR_PANEL = 5
    # P4R4-M-3: Class-level lock shared across all instances to provide
    # cross-request protection even if DisputeResolver is instantiated per-message.
    _dispute_lock = threading.Lock()

    def __init__(self, database, plugin, rpc=None):
        self.db = database
        self.plugin = plugin
        self.rpc = rpc

    def _log(self, msg: str, level: str = 'info') -> None:
        self.plugin.log(f"cl-hive: disputes: {msg}", level=level)

    def select_arbitration_panel(self, dispute_id: str, block_hash: str,
                                  eligible_members: List[Dict]) -> Optional[Dict]:
        """
        Deterministic stake-weighted panel selection.

        Args:
            dispute_id: Unique dispute identifier
            block_hash: Block hash at filing height for determinism
            eligible_members: List of dicts with 'peer_id', 'bond_amount', 'tenure_days'

        Returns:
            Dict with panel_members, panel_size, quorum, seed.
        """
        if len(eligible_members) < self.MIN_ELIGIBLE_FOR_PANEL:
            return None

        # Determine panel size and quorum
        n = len(eligible_members)
        if n >= 15:
            panel_size, quorum = 7, 5
        elif n >= 10:
            panel_size, quorum = 5, 3
        else:
            panel_size, quorum = 3, 2

        # Compute deterministic seed
        seed_input = f"{dispute_id}{block_hash}"
        seed = hashlib.sha256(seed_input.encode()).digest()

        # Weight: bond_amount + tenure_days * 100
        weighted = []
        for m in eligible_members:
            bond = m.get("bond_amount", 0)
            tenure = m.get("tenure_days", 0)
            weight = bond + tenure * 100
            weighted.append((m["peer_id"], max(1, weight)))

        # Sort by peer_id for determinism
        weighted.sort(key=lambda x: x[0])

        # Deterministic weighted selection without replacement
        selected = []
        remaining = list(weighted)
        seed_state = seed

        for _ in range(min(panel_size, len(remaining))):
            if not remaining:
                break
            # Use seed_state to pick index
            total_weight = sum(w for _, w in remaining)
            seed_state = hashlib.sha256(seed_state).digest()
            pick_val = int.from_bytes(seed_state[:8], 'big') % total_weight

            cumulative = 0
            pick_idx = 0
            for idx, (_, w) in enumerate(remaining):
                cumulative += w
                if cumulative > pick_val:
                    pick_idx = idx
                    break

            selected.append(remaining[pick_idx][0])
            remaining.pop(pick_idx)

        return {
            "panel_members": selected,
            "panel_size": len(selected),
            "quorum": quorum,
            "seed": seed_input,
            "dispute_id": dispute_id,
        }

    def file_dispute(self, obligation_id: str, filing_peer: str,
                     evidence: Dict, block_hash: Optional[str] = None) -> Optional[Dict]:
        """File a new dispute."""
        obligation = self.db.get_obligation(obligation_id)

        if not obligation:
            return {"error": "obligation not found"}

        if filing_peer not in (obligation['from_peer'], obligation['to_peer']):
            return {"error": "not a party to this obligation"}

        respondent = obligation['from_peer'] if obligation['to_peer'] == filing_peer else obligation['to_peer']

        nonce = os.urandom(16).hex()
        dispute_id = hashlib.sha256(
            f"dispute:{obligation_id}:{filing_peer}:{int(time.time())}:{nonce}".encode()
        ).hexdigest()[:32]

        evidence_json = json.dumps(evidence, sort_keys=True, separators=(',', ':'))

        success = self.db.store_dispute(
            dispute_id=dispute_id,
            obligation_id=obligation_id,
            filing_peer=filing_peer,
            respondent_peer=respondent,
            evidence_json=evidence_json,
            filed_at=int(time.time()),
        )

        if not success:
            return None

        now = int(time.time())

        # Deterministically select an arbitration panel at filing time when possible.
        eligible_members = []
        try:
            all_members = self.db.get_all_members()
        except Exception:
            all_members = []
        for m in all_members:
            peer_id = m.get("peer_id", "")
            if not peer_id or peer_id in (filing_peer, respondent):
                continue
            joined_at = int(m.get("joined_at", now) or now)
            tenure_days = max(0, (now - joined_at) // 86400)
            bond = self.db.get_bond_for_peer(peer_id)
            bond_amount = int((bond or {}).get("amount_sats", 0) or 0)
            eligible_members.append({
                "peer_id": peer_id,
                "bond_amount": bond_amount,
                "tenure_days": tenure_days,
            })

        # R5-FIX-6: Use deterministic block_hash from violation report or
        # evidence so all nodes select the same arbitration panel.
        # Fall back to live RPC only if no block_hash was provided.
        resolved_block_hash = block_hash or evidence.get("block_hash") if isinstance(evidence, dict) else block_hash
        if not resolved_block_hash:
            resolved_block_hash = "0" * 64
            if self.rpc:
                try:
                    info = self.rpc.getinfo()
                    if isinstance(info, dict):
                        resolved_block_hash = (
                            info.get("bestblockhash")
                            or info.get("blockhash")
                            or f"height:{info.get('blockheight', 0)}"
                        )
                except Exception:
                    pass
        block_hash = resolved_block_hash

        panel_info = self.select_arbitration_panel(dispute_id, str(block_hash), eligible_members)
        if panel_info:
            panel_members_json = json.dumps(
                panel_info["panel_members"], sort_keys=True, separators=(',', ':')
            )
            self.db.update_dispute_outcome(
                dispute_id=dispute_id,
                outcome=None,
                slash_amount=0,
                panel_members_json=panel_members_json,
                votes_json=json.dumps({}, sort_keys=True, separators=(',', ':')),
                resolved_at=0,
            )

        # Mark obligation as disputed
        self.db.update_obligation_status(obligation_id, 'disputed')

        self._log(f"dispute {dispute_id[:16]}... filed by {filing_peer[:16]}...")

        result = {
            "dispute_id": dispute_id,
            "obligation_id": obligation_id,
            "filing_peer": filing_peer,
            "respondent_peer": respondent,
        }
        if panel_info:
            result["panel"] = panel_info
        elif len(eligible_members) < self.MIN_ELIGIBLE_FOR_PANEL:
            result["panel"] = {
                "panel_members": [],
                "panel_size": 0,
                "quorum": 0,
                "mode": "bilateral_negotiation",
            }
        return result

    def record_vote(self, dispute_id: str, voter_id: str,
                    vote: str, reason: str = "",
                    signature: str = "") -> Optional[Dict]:
        """Record an arbitration panel vote.

        After recording the vote, automatically checks quorum while still
        holding _dispute_lock to prevent TOCTOU races.  The return dict
        includes a 'quorum_result' key when quorum was reached.
        """
        if vote not in {"upheld", "rejected", "partial", "abstain"}:
            return {"error": "invalid vote"}

        with self._dispute_lock:
            dispute = self.db.get_dispute(dispute_id)
            if not dispute:
                return {"error": "dispute not found"}

            if dispute.get('resolved_at'):
                return {"error": "dispute already resolved"}

            # Check panel membership before accepting vote
            panel_members = []
            if dispute.get('panel_members_json'):
                try:
                    panel_members = json.loads(dispute['panel_members_json'])
                except (json.JSONDecodeError, TypeError):
                    panel_members = []

            if voter_id not in panel_members:
                return {"error": "voter not on arbitration panel"}

            # Parse existing votes
            votes = {}
            if dispute.get('votes_json'):
                try:
                    votes = json.loads(dispute['votes_json'])
                except (json.JSONDecodeError, TypeError):
                    votes = {}

            if voter_id in votes:
                return {"error": "voter has already cast a vote"}

            votes[voter_id] = {
                "vote": vote,
                "reason": reason,
                "signature": signature,
                "timestamp": int(time.time()),
            }

            votes_json = json.dumps(votes, sort_keys=True, separators=(',', ':'))

            # Update votes
            self.db.update_dispute_outcome(
                dispute_id=dispute_id,
                outcome=dispute.get('outcome'),
                slash_amount=dispute.get('slash_amount', 0),
                panel_members_json=dispute.get('panel_members_json'),
                votes_json=votes_json,
                resolved_at=dispute.get('resolved_at') or 0,
            )

            # Check quorum while still holding the lock (P4R3-M-2 fix)
            quorum = (len(panel_members) // 2) + 1 if panel_members else 1
            quorum_result = self._check_quorum_locked(dispute_id, quorum)

        result = {
            "dispute_id": dispute_id,
            "voter_id": voter_id,
            "vote": vote,
            "total_votes": len(votes),
        }
        if quorum_result:
            result["quorum_result"] = quorum_result
        return result

    def _check_quorum_locked(self, dispute_id: str, quorum: int) -> Optional[Dict]:
        """Check if quorum reached and determine outcome.

        MUST be called while holding _dispute_lock.  This is the internal
        implementation; the public check_quorum() acquires the lock itself.
        """
        dispute = self.db.get_dispute(dispute_id)
        if not dispute or dispute.get('resolved_at'):
            return None

        votes = {}
        if dispute.get('votes_json'):
            try:
                votes = json.loads(dispute['votes_json'])
            except (json.JSONDecodeError, TypeError):
                return None

        if len(votes) < quorum:
            return None

        # Count votes
        counts = {"upheld": 0, "rejected": 0, "partial": 0, "abstain": 0}
        for v in votes.values():
            vtype = v.get("vote", "abstain")
            if vtype in counts:
                counts[vtype] += 1

        # Determine outcome: majority of non-abstain votes
        # Priority: upheld > partial > rejected (deterministic tie-breaking)
        non_abstain = counts["upheld"] + counts["rejected"] + counts["partial"]
        if non_abstain == 0:
            outcome = "rejected"
        elif counts["upheld"] * 2 > non_abstain:
            outcome = "upheld"
        elif counts["partial"] * 2 > non_abstain:
            outcome = "partial"
        elif counts["upheld"] >= counts["rejected"] and counts["upheld"] >= counts["partial"]:
            outcome = "upheld"
        elif counts["partial"] >= counts["rejected"]:
            outcome = "partial"
        else:
            outcome = "rejected"

        now = int(time.time())
        updated = self.db.update_dispute_outcome(
            dispute_id=dispute_id,
            outcome=outcome,
            slash_amount=dispute.get('slash_amount', 0),
            panel_members_json=dispute.get('panel_members_json'),
            votes_json=dispute.get('votes_json'),
            resolved_at=now,
        )

        if not updated:
            # CAS guard prevented double resolution
            return None

        self._log(f"dispute {dispute_id[:16]}... resolved: {outcome}")

        return {
            "dispute_id": dispute_id,
            "outcome": outcome,
            "vote_counts": counts,
            "resolved_at": now,
        }

    def check_quorum(self, dispute_id: str, quorum: int) -> Optional[Dict]:
        """Check if quorum reached and determine outcome.

        Public API that acquires _dispute_lock.  Safe to call externally
        (e.g. from cl-hive.py) — the CAS guard in update_dispute_outcome
        prevents double resolution even without the lock, but the lock
        provides additional serialisation.
        """
        with self._dispute_lock:
            return self._check_quorum_locked(dispute_id, quorum)


# =============================================================================
# PHASE 4B: CREDIT TIER HELPER
# =============================================================================

def get_credit_tier_info(peer_id: str, did_credential_mgr=None) -> Dict[str, Any]:
    """
    Get credit tier information for a peer.

    Uses DID credential manager's get_credit_tier() if available,
    otherwise defaults to 'newcomer'.
    """
    tier = "newcomer"
    if did_credential_mgr:
        try:
            tier = did_credential_mgr.get_credit_tier(peer_id)
        except Exception:
            pass

    tier_info = CREDIT_TIERS.get(tier, CREDIT_TIERS["newcomer"])
    return {
        "peer_id": peer_id,
        "tier": tier,
        "credit_line": tier_info["credit_line"],
        "window": tier_info["window"],
        "model": tier_info["model"],
    }
