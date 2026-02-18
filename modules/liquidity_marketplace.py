"""Phase 5C liquidity marketplace manager."""

import json
import time
import uuid
from typing import Any, Dict, List, Optional


class LiquidityMarketplaceManager:
    """Liquidity marketplace: offers, leases, and heartbeat attestations."""

    MAX_ACTIVE_LEASES = 50
    MAX_ACTIVE_OFFERS = 200
    HEARTBEAT_MISS_THRESHOLD = 3

    def __init__(self, database, plugin, nostr_transport, cashu_escrow_mgr,
                 settlement_mgr, did_credential_mgr):
        self.db = database
        self.plugin = plugin
        self.nostr_transport = nostr_transport
        self.cashu_escrow_mgr = cashu_escrow_mgr
        self.settlement_mgr = settlement_mgr
        self.did_credential_mgr = did_credential_mgr

        self._last_offer_republish_at = 0

    def _log(self, msg: str, level: str = "info") -> None:
        self.plugin.log(f"cl-hive: liquidity: {msg}", level=level)

    def discover_offers(self, service_type: Optional[int] = None,
                        min_capacity: int = 0,
                        max_rate: Optional[int] = None) -> List[Dict[str, Any]]:
        """Discover active liquidity offers from cache."""
        conn = self.db._get_connection()
        query = "SELECT * FROM liquidity_offers WHERE status = 'active'"
        params: List[Any] = []
        if service_type is not None:
            query += " AND service_type = ?"
            params.append(int(service_type))
        if min_capacity > 0:
            query += " AND capacity_sats >= ?"
            params.append(int(min_capacity))
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(self.MAX_ACTIVE_OFFERS)
        rows = conn.execute(query, params).fetchall()

        offers = [dict(r) for r in rows]
        if max_rate is not None:
            filtered = []
            for offer in offers:
                rate = json.loads(offer.get("rate_json") or "{}")
                ppm = int(rate.get("rate_ppm", 0)) if isinstance(rate, dict) else 0
                if ppm <= int(max_rate):
                    filtered.append(offer)
            return filtered
        return offers

    def publish_offer(self, provider_id: str, service_type: int, capacity_sats: int,
                      duration_hours: int, pricing_model: str,
                      rate: Dict[str, Any], min_reputation: int = 0,
                      expires_at: Optional[int] = None) -> Dict[str, Any]:
        """Publish and cache a liquidity offer."""
        if self.db.count_rows("liquidity_offers") >= self.db.MAX_LIQUIDITY_OFFER_ROWS:
            return {"error": "liquidity offer row cap reached"}

        now = int(time.time())
        offer_id = str(uuid.uuid4())
        conn = self.db._get_connection()

        event_id = None
        if self.nostr_transport:
            event = self.nostr_transport.publish({
                "kind": 38901,
                "content": json.dumps({
                    "offer_id": offer_id,
                    "provider_id": provider_id,
                    "service_type": int(service_type),
                    "capacity_sats": int(capacity_sats),
                    "duration_hours": int(duration_hours),
                    "pricing_model": pricing_model,
                    "rate": rate or {},
                    "min_reputation": int(min_reputation),
                }, separators=(",", ":"), sort_keys=True),
                "tags": [["t", "hive-liquidity-offer"]],
            })
            event_id = event.get("id")

        conn.execute(
            "INSERT INTO liquidity_offers (offer_id, provider_id, service_type, capacity_sats, duration_hours, "
            "pricing_model, rate_json, min_reputation, nostr_event_id, status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
            (
                offer_id,
                provider_id,
                int(service_type),
                int(capacity_sats),
                int(duration_hours),
                pricing_model,
                json.dumps(rate or {}, sort_keys=True, separators=(",", ":")),
                int(min_reputation),
                event_id,
                now,
                expires_at,
            ),
        )
        return {"ok": True, "offer_id": offer_id, "nostr_event_id": event_id}

    def accept_offer(self, offer_id: str, client_id: str,
                     heartbeat_interval: int = 3600) -> Dict[str, Any]:
        """Accept an active offer and create a lease."""
        conn = self.db._get_connection()
        row = conn.execute(
            "SELECT * FROM liquidity_offers WHERE offer_id = ?",
            (offer_id,),
        ).fetchone()
        if not row:
            return {"error": "offer not found"}
        offer = dict(row)
        if offer.get("status") != "active":
            return {"error": "offer not active"}

        active_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM liquidity_leases WHERE status = 'active'"
        ).fetchone()
        if active_count and int(active_count["cnt"]) >= self.MAX_ACTIVE_LEASES:
            return {"error": "max active leases reached"}

        if self.db.count_rows("liquidity_leases") >= self.db.MAX_LIQUIDITY_LEASE_ROWS:
            return {"error": "liquidity lease row cap reached"}

        now = int(time.time())
        duration_hours = int(offer.get("duration_hours") or 24)
        lease_id = str(uuid.uuid4())
        end_at = now + (duration_hours * 3600)

        conn.execute(
            "INSERT INTO liquidity_leases (lease_id, offer_id, provider_id, client_id, service_type, capacity_sats, "
            "start_at, end_at, heartbeat_interval, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            (
                lease_id,
                offer_id,
                offer["provider_id"],
                client_id,
                int(offer["service_type"]),
                int(offer["capacity_sats"]),
                now,
                end_at,
                max(300, int(heartbeat_interval)),
                now,
            ),
        )
        conn.execute(
            "UPDATE liquidity_offers SET status = 'filled' WHERE offer_id = ?",
            (offer_id,),
        )
        return {"ok": True, "lease_id": lease_id, "end_at": end_at}

    def send_heartbeat(self, lease_id: str, channel_id: str,
                       remote_balance_sats: int,
                       capacity_sats: Optional[int] = None) -> Dict[str, Any]:
        """Record and publish a lease heartbeat."""
        conn = self.db._get_connection()
        row = conn.execute(
            "SELECT * FROM liquidity_leases WHERE lease_id = ?",
            (lease_id,),
        ).fetchone()
        if not row:
            return {"error": "lease not found"}
        lease = dict(row)
        if lease.get("status") != "active":
            return {"error": "lease not active"}

        now = int(time.time())
        interval = int(lease.get("heartbeat_interval") or 3600)
        last = int(lease.get("last_heartbeat") or 0)
        if last and now - last < int(interval * 0.5):
            return {"error": "heartbeat rate-limited"}

        if self.db.count_rows("liquidity_heartbeats") >= self.db.MAX_HEARTBEAT_ROWS:
            return {"error": "heartbeat row cap reached"}

        hb_row = conn.execute(
            "SELECT MAX(period_number) as maxp FROM liquidity_heartbeats WHERE lease_id = ?",
            (lease_id,),
        ).fetchone()
        period_number = int(hb_row["maxp"] or 0) + 1
        heartbeat_id = str(uuid.uuid4())
        cap = int(capacity_sats if capacity_sats is not None else lease["capacity_sats"])

        signature = ""
        rpc = getattr(self.plugin, "rpc", None)
        if rpc:
            try:
                payload = json.dumps({
                    "lease_id": lease_id,
                    "period_number": period_number,
                    "channel_id": channel_id,
                    "capacity_sats": cap,
                    "remote_balance_sats": int(remote_balance_sats),
                    "timestamp": now,
                }, sort_keys=True, separators=(",", ":"))
                sig = rpc.signmessage(payload)
                signature = sig.get("zbase", "") if isinstance(sig, dict) else ""
            except Exception:
                signature = ""

        conn.execute(
            "INSERT INTO liquidity_heartbeats (heartbeat_id, lease_id, period_number, channel_id, capacity_sats, "
            "remote_balance_sats, provider_signature, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                heartbeat_id,
                lease_id,
                period_number,
                channel_id,
                cap,
                int(remote_balance_sats),
                signature,
                now,
            ),
        )
        conn.execute(
            "UPDATE liquidity_leases SET last_heartbeat = ?, missed_heartbeats = 0 WHERE lease_id = ?",
            (now, lease_id),
        )
        return {"ok": True, "heartbeat_id": heartbeat_id, "period_number": period_number}

    def verify_heartbeat(self, lease_id: str, heartbeat_id: str) -> Dict[str, Any]:
        """Mark a heartbeat as verified by the client side."""
        conn = self.db._get_connection()
        cursor = conn.execute(
            "UPDATE liquidity_heartbeats SET client_verified = 1 WHERE lease_id = ? AND heartbeat_id = ?",
            (lease_id, heartbeat_id),
        )
        if cursor.rowcount <= 0:
            return {"error": "heartbeat not found"}
        return {"ok": True, "lease_id": lease_id, "heartbeat_id": heartbeat_id}

    def check_heartbeat_deadlines(self) -> int:
        """Increment missed heartbeat counters for overdue active leases."""
        conn = self.db._get_connection()
        now = int(time.time())
        rows = conn.execute(
            "SELECT lease_id, heartbeat_interval, last_heartbeat, start_at, missed_heartbeats "
            "FROM liquidity_leases WHERE status = 'active'"
        ).fetchall()
        updates = 0
        for row in rows:
            lease = dict(row)
            interval = int(lease.get("heartbeat_interval") or 3600)
            last = int(lease.get("last_heartbeat") or lease.get("start_at") or 0)
            missed = int(lease.get("missed_heartbeats") or 0)
            # Increment at most once per missed interval window.
            next_deadline = last + (interval * (missed + 1))
            if last and now > next_deadline:
                conn.execute(
                    "UPDATE liquidity_leases SET missed_heartbeats = missed_heartbeats + 1 WHERE lease_id = ?",
                    (lease["lease_id"],),
                )
                updates += 1
        return updates

    def terminate_dead_leases(self) -> int:
        """Terminate leases with too many consecutive missed heartbeats."""
        conn = self.db._get_connection()
        cursor = conn.execute(
            "UPDATE liquidity_leases SET status = 'terminated' "
            "WHERE status = 'active' AND missed_heartbeats >= ?",
            (self.HEARTBEAT_MISS_THRESHOLD,),
        )
        return int(cursor.rowcount or 0)

    def expire_stale_offers(self) -> int:
        """Expire offers past their expiration timestamp."""
        conn = self.db._get_connection()
        now = int(time.time())
        cursor = conn.execute(
            "UPDATE liquidity_offers SET status = 'expired' "
            "WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        return int(cursor.rowcount or 0)

    def republish_offers(self) -> int:
        """Re-publish active offers every 2 hours."""
        now = int(time.time())
        if now - self._last_offer_republish_at < (2 * 3600):
            return 0
        if not self.nostr_transport:
            return 0

        conn = self.db._get_connection()
        rows = conn.execute(
            "SELECT * FROM liquidity_offers WHERE status = 'active' ORDER BY created_at DESC LIMIT ?",
            (self.MAX_ACTIVE_OFFERS,),
        ).fetchall()
        published = 0
        for row in rows:
            offer = dict(row)
            event = self.nostr_transport.publish({
                "kind": 38901,
                "content": json.dumps({
                    "offer_id": offer["offer_id"],
                    "provider_id": offer["provider_id"],
                    "service_type": offer["service_type"],
                    "capacity_sats": offer["capacity_sats"],
                    "duration_hours": offer["duration_hours"],
                    "pricing_model": offer["pricing_model"],
                }, sort_keys=True, separators=(",", ":")),
                "tags": [["t", "hive-liquidity-offer"]],
            })
            conn.execute(
                "UPDATE liquidity_offers SET nostr_event_id = ? WHERE offer_id = ?",
                (event.get("id", ""), offer["offer_id"]),
            )
            published += 1

        self._last_offer_republish_at = now
        return published

    def get_lease_status(self, lease_id: str) -> Dict[str, Any]:
        """Return lease details with heartbeat history."""
        conn = self.db._get_connection()
        row = conn.execute(
            "SELECT * FROM liquidity_leases WHERE lease_id = ?",
            (lease_id,),
        ).fetchone()
        if not row:
            return {"error": "lease not found"}

        heartbeats = conn.execute(
            "SELECT * FROM liquidity_heartbeats WHERE lease_id = ? ORDER BY period_number ASC LIMIT 500",
            (lease_id,),
        ).fetchall()
        return {
            "lease": dict(row),
            "heartbeats": [dict(h) for h in heartbeats],
        }

    def terminate_lease(self, lease_id: str, reason: str = "") -> Dict[str, Any]:
        """Terminate a lease manually."""
        conn = self.db._get_connection()
        cursor = conn.execute(
            "UPDATE liquidity_leases SET status = 'terminated' WHERE lease_id = ?",
            (lease_id,),
        )
        if cursor.rowcount <= 0:
            return {"error": "lease not found"}
        if reason:
            self._log(f"lease {lease_id} terminated: {reason}", level="warn")
        return {"ok": True, "lease_id": lease_id}
