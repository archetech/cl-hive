"""
askrene_layers — Manage askrene routing layers from fleet intelligence.

Creates and maintains askrene layers that encode fleet knowledge:
- hive-fleet: Zero-fee fleet channels + actual capacity constraints
- hive-reputation: Peer quality biases + bad-peer blocking

Layers are visible to any plugin on the same CLN instance, enabling
cl-revenue-ops to benefit from fleet intelligence via getroutes.

Degrades gracefully when askrene is unavailable (CLN < 24.11).
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Set


class AskreneLayerManager:
    """Manage askrene layers encoding fleet routing intelligence."""

    FLEET_LAYER = "hive-fleet"
    REPUTATION_LAYER = "hive-reputation"
    CORRIDORS_LAYER = "hive-corridors"
    TRAFFIC_LAYER = "hive-traffic"

    # Reputation thresholds for node bias
    REPUTATION_EXCELLENT = 80   # bias +5
    REPUTATION_GOOD = 60        # bias +2
    REPUTATION_POOR = 30        # bias -3
    REPUTATION_BAD = 15         # bias -5

    # Reputation thresholds for node disabling
    DISABLE_FORCE_CLOSE_THRESHOLD = 2
    DISABLE_HTLC_SUCCESS_THRESHOLD = 0.5

    def __init__(self, plugin, database, peer_reputation_mgr,
                 fee_coordination_mgr=None, traffic_intel_mgr=None):
        """
        Args:
            plugin: CLN plugin reference for RPC + logging
            database: HiveDatabase for member queries
            peer_reputation_mgr: PeerReputationManager for reputation data
            fee_coordination_mgr: FeeCoordinationManager for corridor assignments
            traffic_intel_mgr: TrafficIntelligenceManager for traffic profiles
        """
        self.plugin = plugin
        self.database = database
        self.peer_reputation_mgr = peer_reputation_mgr
        self.fee_coordination_mgr = fee_coordination_mgr
        self.traffic_intel_mgr = traffic_intel_mgr
        self.available: bool = False
        self._our_id: Optional[str] = None
        self._last_refresh: float = 0

    def _log(self, msg: str, level: str = "info") -> None:
        if self.plugin:
            self.plugin.log(f"[AskreneLayerManager] {msg}", level=level)

    def _get_our_id(self) -> Optional[str]:
        if self._our_id:
            return self._our_id
        try:
            info = self.plugin.rpc.getinfo()
            self._our_id = info.get("id")
        except Exception:
            pass
        return self._our_id

    def is_available(self) -> bool:
        """Whether askrene is usable on this CLN version."""
        return self.available

    def refresh_all(self) -> Dict[str, bool]:
        """Refresh all managed layers.

        Returns:
            Dict mapping layer name to success bool.
        """
        results = {}
        results[self.FLEET_LAYER] = self._refresh_fleet_layer()
        results[self.REPUTATION_LAYER] = self._refresh_reputation_layer()
        results[self.CORRIDORS_LAYER] = self._refresh_corridors_layer()
        results[self.TRAFFIC_LAYER] = self._refresh_traffic_layer()
        return results

    # ------------------------------------------------------------------
    # hive-fleet layer
    # ------------------------------------------------------------------

    def _refresh_fleet_layer(self) -> bool:
        """Recreate hive-fleet layer with zero-fee overrides and capacity info.

        For each fleet member channel:
        - askrene-update-channel: fee_base_msat=0, fee_proportional=0
        - askrene-inform-channel: actual capacity in each direction
        - askrene-bias-node: +5 on fleet member nodes
        - askrene-age: decay info older than 15 minutes
        """
        if not self.plugin or not self.database:
            return False

        try:
            # Remove and recreate
            try:
                self.plugin.rpc.call("askrene-remove-layer", {"layer": self.FLEET_LAYER})
            except Exception:
                pass

            self.plugin.rpc.call("askrene-create-layer", {"layer": self.FLEET_LAYER})

            our_id = self._get_our_id()
            if not our_id:
                return False

            # Get fleet members
            members = self.database.get_all_members()
            member_ids: Set[str] = {m.get("peer_id") for m in members if m.get("peer_id")}

            # Get channel data
            channels = self.plugin.rpc.listpeerchannels()
            updated = 0

            for ch in channels.get("channels", []):
                if ch.get("state") != "CHANNELD_NORMAL":
                    continue
                peer_id = ch.get("peer_id", "")
                scid = ch.get("short_channel_id", "")
                if not peer_id or not scid or peer_id not in member_ids:
                    continue

                # Zero-fee overrides (both directions)
                for direction in (0, 1):
                    scid_dir = f"{scid}/{direction}"
                    try:
                        self.plugin.rpc.call("askrene-update-channel", {
                            "layer": self.FLEET_LAYER,
                            "short_channel_id_dir": scid_dir,
                            "fee_base_msat": 0,
                            "fee_proportional_millionths": 0,
                            "cltv_expiry_delta": 6,
                        })
                        updated += 1
                    except Exception:
                        pass

                # Capacity constraints via inform-channel
                to_us_msat = ch.get("to_us_msat", 0)
                total_msat = ch.get("total_msat", 0)
                if isinstance(to_us_msat, str):
                    to_us_msat = int(to_us_msat.rstrip("msat"))
                if isinstance(total_msat, str):
                    total_msat = int(total_msat.rstrip("msat"))
                their_msat = max(0, total_msat - to_us_msat)

                # Direction 0 = us→peer (capacity = our local balance)
                # Direction 1 = peer→us (capacity = their balance)
                for direction, cap_msat in ((0, to_us_msat), (1, their_msat)):
                    if cap_msat > 0:
                        try:
                            self.plugin.rpc.call("askrene-inform-channel", {
                                "layer": self.FLEET_LAYER,
                                "short_channel_id_dir": f"{scid}/{direction}",
                                "amount_msat": cap_msat,
                                "inform": "succeeded",
                            })
                        except Exception:
                            pass

            # Node-level bias for fleet members
            for mid in member_ids:
                for direction in ("in", "out"):
                    try:
                        self.plugin.rpc.call("askrene-bias-node", {
                            "layer": self.FLEET_LAYER,
                            "node": mid,
                            "direction": direction,
                            "bias": 5,
                            "description": "hive fleet preference",
                        })
                    except Exception:
                        pass

            # Age stale information (15 minute cutoff)
            cutoff = int(time.time()) - 900
            try:
                self.plugin.rpc.call("askrene-age", {
                    "layer": self.FLEET_LAYER,
                    "cutoff": cutoff,
                })
            except Exception:
                pass

            self.available = updated > 0
            self._last_refresh = time.time()

            if updated > 0:
                self._log(
                    f"Refreshed {self.FLEET_LAYER} ({updated} channel dirs, "
                    f"{len(member_ids)} fleet nodes)",
                )
            return self.available

        except Exception as e:
            self._log(f"Fleet layer refresh failed: {e}")
            self.available = False
            return False

    # ------------------------------------------------------------------
    # hive-reputation layer
    # ------------------------------------------------------------------

    def _refresh_reputation_layer(self) -> bool:
        """Recreate hive-reputation layer with peer quality biases.

        - disable-node for peers with bad reputation (high force closes, low HTLC success)
        - bias-node scaled by reputation score
        """
        if not self.plugin or not self.peer_reputation_mgr:
            return False

        try:
            try:
                self.plugin.rpc.call("askrene-remove-layer", {"layer": self.REPUTATION_LAYER})
            except Exception:
                pass

            self.plugin.rpc.call("askrene-create-layer", {"layer": self.REPUTATION_LAYER})

            all_reps = self.peer_reputation_mgr.get_all_reputations()
            disabled = 0
            biased = 0

            for peer_id, rep in all_reps.items():
                # Disable nodes with dangerous behavior
                if (rep.total_force_closes >= self.DISABLE_FORCE_CLOSE_THRESHOLD
                        or rep.avg_htlc_success < self.DISABLE_HTLC_SUCCESS_THRESHOLD):
                    try:
                        self.plugin.rpc.call("askrene-disable-node", {
                            "layer": self.REPUTATION_LAYER,
                            "node": peer_id,
                        })
                        disabled += 1
                        self._log(
                            f"Disabled {peer_id[:12]}... "
                            f"(force_closes={rep.total_force_closes}, "
                            f"htlc_success={rep.avg_htlc_success:.2f})",
                        )
                    except Exception:
                        pass
                    continue  # Don't bias a disabled node

                # Bias by reputation score
                score = rep.reputation_score
                if score >= self.REPUTATION_EXCELLENT:
                    bias = 5
                elif score >= self.REPUTATION_GOOD:
                    bias = 2
                elif score < self.REPUTATION_BAD:
                    bias = -5
                elif score < self.REPUTATION_POOR:
                    bias = -3
                else:
                    continue  # Score 30-59: no bias

                for direction in ("in", "out"):
                    try:
                        self.plugin.rpc.call("askrene-bias-node", {
                            "layer": self.REPUTATION_LAYER,
                            "node": peer_id,
                            "direction": direction,
                            "bias": bias,
                            "description": f"reputation score {score}",
                        })
                        biased += 1
                    except Exception:
                        pass

            # Age stale reputation info (1 hour cutoff)
            cutoff = int(time.time()) - 3600
            try:
                self.plugin.rpc.call("askrene-age", {
                    "layer": self.REPUTATION_LAYER,
                    "cutoff": cutoff,
                })
            except Exception:
                pass

            if disabled > 0 or biased > 0:
                self._log(
                    f"Refreshed {self.REPUTATION_LAYER} "
                    f"({disabled} disabled, {biased} biased)",
                )
            return True

        except Exception as e:
            self._log(f"Reputation layer refresh failed: {e}")
            return False

    # ------------------------------------------------------------------
    # hive-corridors layer
    # ------------------------------------------------------------------

    def _refresh_corridors_layer(self) -> bool:
        """Create hive-corridors layer with bias for valuable flow corridors.

        Biases channels serving high-value corridors so getroutes prefers
        routing through them.  Fee overrides apply corridor-optimal fees
        to fleet member channels.
        """
        if not self.plugin or not self.fee_coordination_mgr:
            return False

        try:
            try:
                self.plugin.rpc.call("askrene-remove-layer", {"layer": self.CORRIDORS_LAYER})
            except Exception:
                pass

            self.plugin.rpc.call("askrene-create-layer", {"layer": self.CORRIDORS_LAYER})

            assignments = self.fee_coordination_mgr.corridor_mgr.get_assignments()
            if not assignments:
                return True  # Empty but valid

            biased = 0

            # Get our channel SCIDs mapped to peer_id for corridor matching
            channels = self.plugin.rpc.listpeerchannels()
            peer_to_scids: Dict[str, list] = {}
            for ch in channels.get("channels", []):
                if ch.get("state") != "CHANNELD_NORMAL":
                    continue
                pid = ch.get("peer_id", "")
                scid = ch.get("short_channel_id", "")
                if pid and scid:
                    peer_to_scids.setdefault(pid, []).append(scid)

            for assignment in assignments:
                corridor = assignment.corridor
                volume = corridor.total_volume_sats

                # Score by volume
                if volume > 50_000_000:
                    bias = 8
                elif volume > 20_000_000:
                    bias = 4
                elif volume > 5_000_000:
                    bias = 2
                else:
                    continue  # Not valuable enough to bias

                # Bias channels to corridor source and destination peers
                for peer_id in (corridor.source_peer_id, corridor.destination_peer_id):
                    for scid in peer_to_scids.get(peer_id, []):
                        for direction in (0, 1):
                            try:
                                self.plugin.rpc.call("askrene-bias-channel", {
                                    "layer": self.CORRIDORS_LAYER,
                                    "short_channel_id_dir": f"{scid}/{direction}",
                                    "bias": bias,
                                    "description": f"corridor vol={volume}",
                                })
                                biased += 1
                            except Exception:
                                pass

            if biased > 0:
                self._log(f"Refreshed {self.CORRIDORS_LAYER} ({biased} channel biases)")
            return True

        except Exception as e:
            self._log(f"Corridors layer refresh failed: {e}")
            return False

    # ------------------------------------------------------------------
    # hive-traffic layer
    # ------------------------------------------------------------------

    def _refresh_traffic_layer(self) -> bool:
        """Create hive-traffic layer with drain-direction biases.

        Biases channels in the direction that helps natural rebalancing
        based on observed traffic patterns.
        """
        if not self.plugin or not self.traffic_intel_mgr:
            return False

        try:
            try:
                self.plugin.rpc.call("askrene-remove-layer", {"layer": self.TRAFFIC_LAYER})
            except Exception:
                pass

            self.plugin.rpc.call("askrene-create-layer", {"layer": self.TRAFFIC_LAYER})

            profiles = self.traffic_intel_mgr.get_all_profiles()
            if not profiles:
                return True

            # Get our channels for SCID lookup
            channels = self.plugin.rpc.listpeerchannels()
            peer_to_scids: Dict[str, list] = {}
            for ch in channels.get("channels", []):
                if ch.get("state") != "CHANNELD_NORMAL":
                    continue
                pid = ch.get("peer_id", "")
                scid = ch.get("short_channel_id", "")
                if pid and scid:
                    peer_to_scids.setdefault(pid, []).append(scid)

            biased = 0
            for profile in profiles:
                peer_id = profile.get("peer_id", "")
                drain = profile.get("drain_direction", "balanced")
                confidence = float(profile.get("confidence", 0))

                if drain == "balanced" or confidence < 0.3:
                    continue

                # Base bias scaled by confidence
                base_bias = int(3 * min(1.0, confidence))
                if base_bias < 1:
                    continue

                for scid in peer_to_scids.get(peer_id, []):
                    if drain == "inbound_heavy":
                        # Peer sends us traffic — bias outbound to help rebalance
                        direction = 0  # us→peer
                    else:
                        # outbound_heavy — bias inbound
                        direction = 1  # peer→us

                    try:
                        self.plugin.rpc.call("askrene-bias-channel", {
                            "layer": self.TRAFFIC_LAYER,
                            "short_channel_id_dir": f"{scid}/{direction}",
                            "bias": base_bias,
                            "description": f"drain={drain} conf={confidence:.2f}",
                        })
                        biased += 1
                    except Exception:
                        pass

            # Age stale traffic info (6 hour cutoff)
            cutoff = int(time.time()) - 21600
            try:
                self.plugin.rpc.call("askrene-age", {
                    "layer": self.TRAFFIC_LAYER,
                    "cutoff": cutoff,
                })
            except Exception:
                pass

            if biased > 0:
                self._log(f"Refreshed {self.TRAFFIC_LAYER} ({biased} drain biases)")
            return True

        except Exception as e:
            self._log(f"Traffic layer refresh failed: {e}")
            return False
