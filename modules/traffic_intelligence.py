"""
Traffic Intelligence Manager for cl-hive.

Manages fleet-shared traffic profiles for peer channels. Follows the
fee_intelligence.py pattern: RPC ingest -> DB store -> gossip broadcast ->
fleet handler -> aggregated query.

Provides:
- Local profile storage from cl-revenue-ops
- Fleet gossip via TRAFFIC_INTELLIGENCE_BATCH (32905)
- Aggregated multi-reporter profiles
- Temporal rebalance conflict detection
- Fleet demand forecasting (Kalman + traffic data)
"""

import json
import time
import threading
import hashlib
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from modules.protocol import (
    VALID_PROFILE_TYPES,
    VALID_DRAIN_DIRECTIONS,
    MAX_PROFILES_IN_BATCH,
    TRAFFIC_INTELLIGENCE_BATCH_RATE_LIMIT,
    get_traffic_intelligence_batch_signing_payload,
    validate_traffic_intelligence_batch,
    create_traffic_intelligence_batch,
)


class TrafficIntelligenceManager:
    """
    Manages fleet-shared traffic intelligence.

    Collects traffic profiles from local cl-revenue-ops, broadcasts
    to fleet via gossip, and provides aggregated views for rebalance
    conflict detection and demand forecasting.
    """

    def __init__(
        self,
        database,
        plugin=None,
        our_pubkey: str = "",
        anticipatory_mgr=None,
        liquidity_coordinator=None,
        membership_mgr=None,
    ):
        self.db = database
        self.plugin = plugin
        self.our_pubkey = our_pubkey
        self.anticipatory_mgr = anticipatory_mgr
        self.liquidity_coordinator = liquidity_coordinator
        self.membership_mgr = membership_mgr

        self._rate_lock = threading.Lock()
        self._batch_rate: Dict[str, List[int]] = {}

    def _log(self, msg: str, level: str = "info"):
        if self.plugin:
            self.plugin.log(f"cl-hive: [traffic-intel] {msg}", level=level)

    # -- Rate Limiting -------------------------------------------------------

    def _check_rate_limit(
        self, sender_id: str, rate_dict: dict, limit: tuple
    ) -> bool:
        max_count, period = limit
        now = int(time.time())
        with self._rate_lock:
            timestamps = rate_dict.get(sender_id, [])
            timestamps = [t for t in timestamps if t > now - period]
            rate_dict[sender_id] = timestamps
            return len(timestamps) < max_count

    def _record_message(self, sender_id: str, rate_dict: dict):
        now = int(time.time())
        with self._rate_lock:
            if sender_id not in rate_dict:
                rate_dict[sender_id] = []
            rate_dict[sender_id].append(now)

    # -- Local Profile Storage -----------------------------------------------

    def store_local_profile(
        self,
        peer_id: str,
        profile_type: str,
        peak_hours_utc: List[int],
        quiet_hours_utc: List[int],
        avg_forward_size_sats: float,
        daily_volume_sats: float,
        drain_direction: str,
        confidence: float,
        observation_window_hours: int,
    ) -> bool:
        """
        Store a traffic profile reported by local cl-revenue-ops.

        Args:
            peer_id: External peer being profiled
            profile_type: retail | wholesale | burst | steady | mixed
            peak_hours_utc: Hours with highest traffic (0-23)
            quiet_hours_utc: Hours with lowest traffic (0-23)
            avg_forward_size_sats: Average forward size
            daily_volume_sats: Average daily volume
            drain_direction: inbound_heavy | outbound_heavy | balanced
            confidence: Profile confidence (0-1)
            observation_window_hours: How long peer was observed

        Returns:
            True if stored, False on validation failure
        """
        if profile_type not in VALID_PROFILE_TYPES:
            self._log(f"Invalid profile_type: {profile_type}", level="warn")
            return False
        if drain_direction not in VALID_DRAIN_DIRECTIONS:
            self._log(f"Invalid drain_direction: {drain_direction}", level="warn")
            return False

        return self.db.save_traffic_profile(
            peer_id=peer_id,
            reporter_id=self.our_pubkey or "local",
            profile_type=profile_type,
            peak_hours_utc=json.dumps(peak_hours_utc),
            quiet_hours_utc=json.dumps(quiet_hours_utc),
            avg_forward_size_sats=avg_forward_size_sats,
            daily_volume_sats=daily_volume_sats,
            drain_direction=drain_direction,
            confidence=confidence,
            observation_window_hours=observation_window_hours,
            received_at=time.time(),
        )

    # -- Aggregation ---------------------------------------------------------

    def get_aggregated_profile(
        self, peer_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get merged traffic profile for a peer from all reporters.

        Aggregation:
        - profile_type: highest-confidence reporter wins
        - peak/quiet hours: confidence-weighted union
        - volume/size: confidence-weighted average
        - drain_direction: highest-confidence reporter wins

        Returns:
            Aggregated profile dict or None if no data
        """
        profiles = self.db.get_traffic_profiles_for_peer(peer_id)
        if not profiles:
            return None

        # Sort by confidence descending -- first entry is highest
        profiles.sort(key=lambda p: p.get("confidence", 0), reverse=True)
        best = profiles[0]

        # Collect all peak/quiet hours (union)
        all_peak = set()
        all_quiet = set()
        total_weight = 0.0
        weighted_avg_size = 0.0
        weighted_daily_vol = 0.0

        for p in profiles:
            conf = p.get("confidence", 0.5)
            total_weight += conf

            peak_str = p.get("peak_hours_utc", "[]")
            peak = json.loads(peak_str) if isinstance(peak_str, str) else peak_str
            for h in peak:
                all_peak.add(h)

            quiet_str = p.get("quiet_hours_utc", "[]")
            quiet = json.loads(quiet_str) if isinstance(quiet_str, str) else quiet_str
            for h in quiet:
                all_quiet.add(h)

            weighted_avg_size += p.get("avg_forward_size_sats", 0) * conf
            weighted_daily_vol += p.get("daily_volume_sats", 0) * conf

        if total_weight > 0:
            weighted_avg_size /= total_weight
            weighted_daily_vol /= total_weight

        return {
            "peer_id": peer_id,
            "profile_type": best.get("profile_type"),
            "peak_hours_utc": sorted(all_peak),
            "quiet_hours_utc": sorted(all_quiet - all_peak),
            "avg_forward_size_sats": weighted_avg_size,
            "daily_volume_sats": weighted_daily_vol,
            "drain_direction": best.get("drain_direction"),
            "confidence": best.get("confidence", 0),
            "reporters": len(profiles),
        }

    def get_all_profiles(
        self,
        peer_id: Optional[str] = None,
        profile_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get traffic profiles, optionally filtered.

        Args:
            peer_id: Filter to specific peer
            profile_type: Filter to specific type

        Returns:
            List of profile dicts
        """
        if peer_id:
            profiles = self.db.get_traffic_profiles_for_peer(peer_id)
        else:
            profiles = self.db.get_all_traffic_profiles()

        if profile_type:
            profiles = [p for p in profiles if p.get("profile_type") == profile_type]

        # Parse JSON fields for caller convenience
        for p in profiles:
            for field in ("peak_hours_utc", "quiet_hours_utc"):
                val = p.get(field, "[]")
                if isinstance(val, str):
                    try:
                        p[field] = json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        p[field] = []

        return profiles

    def cleanup_expired_profiles(self) -> int:
        """Remove profiles past their TTL."""
        return self.db.cleanup_expired_traffic_profiles()

    # ── Gossip: Create Batch ───────────────────────────────────────

    def create_traffic_intelligence_batch_message(
        self, rpc
    ) -> Optional[bytes]:
        """
        Create a signed TRAFFIC_INTELLIGENCE_BATCH message from local profiles.

        Args:
            rpc: RPC proxy for signmessage

        Returns:
            Serialized message bytes or None
        """
        if not self.our_pubkey:
            self._log("Cannot create batch: no pubkey set", level="warn")
            return None

        # Get our locally-stored profiles (we are the reporter)
        all_profiles = self.db.get_all_traffic_profiles()
        our_profiles = [
            p for p in all_profiles
            if p.get("reporter_id") == self.our_pubkey
        ]

        if not our_profiles:
            return None

        if len(our_profiles) > MAX_PROFILES_IN_BATCH:
            our_profiles = our_profiles[:MAX_PROFILES_IN_BATCH]

        # Build payload profiles list
        profiles_data = []
        for p in our_profiles:
            peak = p.get("peak_hours_utc", "[]")
            quiet = p.get("quiet_hours_utc", "[]")
            profiles_data.append({
                "peer_id": p["peer_id"],
                "profile_type": p.get("profile_type", "mixed"),
                "peak_hours_utc": json.loads(peak) if isinstance(peak, str) else peak,
                "quiet_hours_utc": json.loads(quiet) if isinstance(quiet, str) else quiet,
                "avg_forward_size_sats": p.get("avg_forward_size_sats", 0),
                "daily_volume_sats": p.get("daily_volume_sats", 0),
                "drain_direction": p.get("drain_direction", "balanced"),
                "confidence": p.get("confidence", 0.5),
                "observation_window_hours": p.get("observation_window_hours", 0),
            })

        timestamp = int(time.time())
        payload = {
            "reporter_id": self.our_pubkey,
            "timestamp": timestamp,
            "signature": "",
            "profiles": profiles_data,
        }

        try:
            signing_msg = get_traffic_intelligence_batch_signing_payload(payload)
            sig_result = rpc.signmessage(signing_msg)
            signature = sig_result.get("signature", sig_result.get("zbase", ""))
            payload["signature"] = signature
        except Exception as e:
            self._log(f"Failed to sign batch: {e}", level="error")
            return None

        return create_traffic_intelligence_batch(
            reporter_id=self.our_pubkey,
            timestamp=timestamp,
            signature=signature,
            profiles=profiles_data,
        )

    # ── Gossip: Handle Incoming ────────────────────────────────────

    def handle_traffic_intelligence_batch(
        self,
        sender_id: str,
        payload: Dict[str, Any],
        rpc,
    ) -> Dict[str, Any]:
        """
        Handle incoming TRAFFIC_INTELLIGENCE_BATCH from fleet member.

        Args:
            sender_id: Peer who sent the message
            payload: Message payload
            rpc: RPC proxy for checkmessage

        Returns:
            Dict with success/error status
        """
        # Rate limit
        if not self._check_rate_limit(
            sender_id, self._batch_rate, TRAFFIC_INTELLIGENCE_BATCH_RATE_LIMIT
        ):
            return {"error": "rate_limited"}

        # Reporter must match sender
        reporter_id = payload.get("reporter_id")
        if reporter_id != sender_id:
            return {"error": "reporter_mismatch"}

        # Validate payload structure
        if not validate_traffic_intelligence_batch(payload):
            return {"error": "invalid_payload"}

        # Verify sender is a member
        member = self.db.get_member(reporter_id)
        if not member:
            return {"error": "not_a_member"}

        # Verify signature
        signature = payload.get("signature")
        signing_msg = get_traffic_intelligence_batch_signing_payload(payload)
        try:
            verify = rpc.checkmessage(signing_msg, signature)
            if not verify.get("verified"):
                return {"error": "invalid_signature"}
            if verify.get("pubkey") != reporter_id:
                return {"error": "signature_mismatch"}
        except Exception as e:
            self._log(f"Signature check failed: {e}", level="error")
            return {"error": "verification_failed"}

        # Record for rate limiting
        self._record_message(sender_id, self._batch_rate)

        # Store each profile
        profiles = payload.get("profiles", [])
        timestamp = payload.get("timestamp", int(time.time()))
        stored = 0
        for p in profiles:
            ok = self.db.save_traffic_profile(
                peer_id=p["peer_id"],
                reporter_id=reporter_id,
                profile_type=p.get("profile_type", "mixed"),
                peak_hours_utc=json.dumps(p.get("peak_hours_utc", [])),
                quiet_hours_utc=json.dumps(p.get("quiet_hours_utc", [])),
                avg_forward_size_sats=p.get("avg_forward_size_sats", 0),
                daily_volume_sats=p.get("daily_volume_sats", 0),
                drain_direction=p.get("drain_direction", "balanced"),
                confidence=p.get("confidence", 0.5),
                observation_window_hours=p.get("observation_window_hours", 0),
                received_at=time.time(),
            )
            if ok:
                stored += 1

        self._log(
            f"Stored {stored}/{len(profiles)} profiles from {sender_id[:16]}...",
            level="debug",
        )
        return {"success": True, "profiles_stored": stored}
