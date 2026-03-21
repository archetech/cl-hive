"""
Routing Intelligence Module (Phase 4 - Cooperative Routing)

Implements collective routing intelligence for the hive:
- Route probe aggregation and analysis
- Best route suggestions based on collective observations
- Path success rate tracking
- Hive-aware route optimization

Security: All route probes require cryptographic signatures.
"""

import heapq
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import network_metrics


# Route quality thresholds
HIGH_SUCCESS_RATE = 0.9     # 90% success rate considered high
LOW_SUCCESS_RATE = 0.5      # Below 50% considered unreliable
MAX_PROBES_PER_PATH = 100   # Cap probe count per path to prevent stat inflation
MAX_CACHED_PATHS = 5000     # Max entries in _path_stats before LRU eviction
PROBE_STALENESS_HOURS = 24  # Probes older than this are stale

# Centrality-aware routing (Use Case 7)
CENTRALITY_WEIGHT_IN_ROUTING = 0.15  # 15% weight for centrality in route score
HIGH_CENTRALITY_ROUTING_BONUS = 1.2  # 20% bonus for paths with high-centrality members


@dataclass
class RouteSuggestion:
    """A suggested route to a destination."""
    destination: str
    path: List[str]
    expected_fee_ppm: int
    expected_latency_ms: int
    success_rate: float
    confidence: float
    last_successful_probe: int
    hive_hop_count: int  # Number of hive members in path
    # Centrality-aware routing (Use Case 7)
    path_centrality_score: float = 0.0  # Average centrality of hive members in path
    is_high_centrality_path: bool = False  # True if path includes high-centrality members


@dataclass
class PathStats:
    """Aggregated statistics for a specific path."""
    path: Tuple[str, ...]  # Immutable path tuple
    destination: str
    probe_count: int = 0
    success_count: int = 0
    total_latency_ms: int = 0
    total_fee_ppm: int = 0
    last_success_time: int = 0
    last_failure_time: int = 0
    last_failure_reason: str = ""
    avg_capacity_sats: int = 0
    reporters: set = field(default_factory=set)


class HiveRoutingMap:
    """
    Collective routing intelligence from all hive members.

    Each member contributes route probe observations; all benefit
    from the aggregated routing knowledge.
    """

    def __init__(
        self,
        database: Any,
        plugin: Any,
        our_pubkey: str
    ):
        """
        Initialize the routing map.

        Args:
            database: HiveDatabase instance
            plugin: Plugin instance for RPC/logging
            our_pubkey: Our node's pubkey
        """
        self.database = database
        self.plugin = plugin
        self.our_pubkey = our_pubkey

        # In-memory path statistics
        # Key: (destination, path_tuple)
        self._path_stats: Dict[Tuple[str, Tuple[str, ...]], PathStats] = {}
        self._lock = threading.Lock()

    def _update_path_stats(
        self,
        destination: str,
        path: Tuple[str, ...],
        success: bool,
        latency_ms: int,
        fee_ppm: int,
        capacity_sats: int,
        reporter_id: str,
        failure_reason: str,
        timestamp: int
    ):
        """Update aggregated statistics for a path."""
        key = (destination, path)

        with self._lock:
            if key not in self._path_stats:
                # Evict least-recently-probed entries if at capacity
                if len(self._path_stats) >= MAX_CACHED_PATHS:
                    self._evict_oldest_locked()

                self._path_stats[key] = PathStats(
                    path=path,
                    destination=destination
                )

            stats = self._path_stats[key]

            # Cap probe count to prevent unbounded stat inflation
            if stats.probe_count >= MAX_PROBES_PER_PATH:
                return

            stats.probe_count += 1
            stats.reporters.add(reporter_id)

            if success:
                stats.success_count += 1
                stats.total_latency_ms += latency_ms
                stats.total_fee_ppm += fee_ppm
                stats.last_success_time = timestamp

                # Update capacity (weighted average)
                if capacity_sats > 0:
                    if stats.avg_capacity_sats == 0:
                        stats.avg_capacity_sats = capacity_sats
                    else:
                        stats.avg_capacity_sats = int(
                            stats.avg_capacity_sats * 0.7 + capacity_sats * 0.3
                        )
            else:
                stats.last_failure_time = timestamp
                stats.last_failure_reason = failure_reason

    def _evict_oldest_locked(self):
        """Evict least-recently-probed entries. Must be called with self._lock held."""
        # Evict 10% of entries with oldest last-probe time
        evict_count = max(1, len(self._path_stats) // 10)
        oldest = heapq.nsmallest(
            evict_count,
            self._path_stats.items(),
            key=lambda kv: max(kv[1].last_success_time, kv[1].last_failure_time)
        )
        for key, _ in oldest:
            del self._path_stats[key]

    def get_path_success_rate(self, path: List[str]) -> float:
        """
        Get the success rate for a specific path.

        Args:
            path: List of hop pubkeys

        Returns:
            Success rate (0.0 to 1.0)
        """
        path_tuple = tuple(path)

        with self._lock:
            items = list(self._path_stats.items())

        # Look for this path to any destination
        for (dest, p), stats in items:
            if p == path_tuple and stats.probe_count > 0:
                return stats.success_count / stats.probe_count

        return 0.5  # Unknown path, return neutral

    @staticmethod
    def _confidence_from_stats(stats, stale_cutoff: float) -> float:
        """Calculate confidence score from a PathStats object.

        Args:
            stats: PathStats instance
            stale_cutoff: Epoch timestamp below which data is stale

        Returns:
            Confidence score (0.0 to 1.0)
        """
        reporter_factor = min(1.0, len(stats.reporters) / 3.0)
        last_probe = max(stats.last_success_time, stats.last_failure_time)
        recency_factor = 0.3 if last_probe < stale_cutoff else 1.0
        count_factor = min(1.0, stats.probe_count / 10.0)
        return reporter_factor * recency_factor * count_factor

    def get_path_confidence(self, path: List[str]) -> float:
        """
        Get confidence level for path data based on reporter count and recency.

        Args:
            path: List of hop pubkeys

        Returns:
            Confidence score (0.0 to 1.0)
        """
        path_tuple = tuple(path)
        now = time.time()
        stale_cutoff = now - (PROBE_STALENESS_HOURS * 3600)

        with self._lock:
            items = list(self._path_stats.items())

        for (dest, p), stats in items:
            if p == path_tuple:
                return self._confidence_from_stats(stats, stale_cutoff)

        return 0.0  # No data

    def _get_path_centrality_score(
        self,
        path: List[str],
        hive_members: set
    ) -> Tuple[float, bool]:
        """
        Calculate centrality score for a routing path.

        Returns:
            Tuple of (average_centrality, is_high_centrality)
        """
        calculator = network_metrics.get_calculator()
        if not calculator:
            return 0.0, False

        centrality_scores = []
        for hop in path:
            if hop in hive_members:
                metrics = calculator.get_member_metrics(hop)
                if metrics:
                    centrality_scores.append(metrics.hive_centrality)

        if not centrality_scores:
            return 0.0, False

        avg_centrality = sum(centrality_scores) / len(centrality_scores)
        is_high = max(centrality_scores) >= 0.6  # High if any hop has centrality >= 0.6

        return avg_centrality, is_high

    def get_best_route_to(
        self,
        destination: str,
        amount_sats: int,
        hive_members: set = None,
        use_centrality_scoring: bool = True
    ) -> Optional[RouteSuggestion]:
        """
        Get best known route to destination based on collective probes.

        Now includes centrality-aware scoring (Use Case 7) to prefer
        paths through well-connected hive members.

        Args:
            destination: Target node pubkey
            amount_sats: Amount to route
            hive_members: Set of hive member pubkeys (for bonus calculation)
            use_centrality_scoring: If True, include centrality in scoring

        Returns:
            RouteSuggestion if found, None otherwise
        """
        if hive_members is None:
            hive_members = set()

        # Collect all paths to this destination
        candidates = []
        stale_cutoff = time.time() - (PROBE_STALENESS_HOURS * 3600)

        with self._lock:
            items = list(self._path_stats.items())

        for (dest, path), stats in items:
            if dest != destination:
                continue

            if stats.probe_count == 0:
                continue

            # Calculate success rate
            success_rate = stats.success_count / stats.probe_count

            # Skip unreliable paths
            if success_rate < LOW_SUCCESS_RATE:
                continue

            # Check capacity
            if stats.avg_capacity_sats > 0 and stats.avg_capacity_sats < amount_sats:
                continue

            # Calculate averages
            if stats.success_count > 0:
                avg_latency = stats.total_latency_ms // stats.success_count
                avg_fee = stats.total_fee_ppm // stats.success_count
            else:
                avg_latency = 0
                avg_fee = 0

            # Calculate hive hop bonus
            hive_hop_count = sum(1 for hop in path if hop in hive_members)

            # Calculate confidence inline from stats (avoids O(n) re-search)
            confidence = self._confidence_from_stats(stats, stale_cutoff)

            # Calculate path centrality (Use Case 7)
            path_centrality, is_high_centrality = self._get_path_centrality_score(
                list(path), hive_members
            )

            candidates.append(RouteSuggestion(
                destination=destination,
                path=list(path),
                expected_fee_ppm=avg_fee,
                expected_latency_ms=avg_latency,
                success_rate=success_rate,
                confidence=confidence,
                last_successful_probe=stats.last_success_time,
                hive_hop_count=hive_hop_count,
                path_centrality_score=path_centrality,
                is_high_centrality_path=is_high_centrality
            ))

        if not candidates:
            return None

        # Score candidates
        def score_route(route: RouteSuggestion) -> float:
            # Higher success rate is better
            success_score = route.success_rate

            # Lower fees are better
            fee_score = 1.0 / (1 + route.expected_fee_ppm / 1000)

            # Prefer paths through hive members (0 fee hops), capped at 3 hops
            hive_bonus = min(0.3, 0.1 * route.hive_hop_count)

            # Centrality bonus (Use Case 7), capped to fill remaining weight
            centrality_bonus = 0.0
            if use_centrality_scoring and route.path_centrality_score > 0:
                centrality_bonus = route.path_centrality_score * CENTRALITY_WEIGHT_IN_ROUTING
                if route.is_high_centrality_path:
                    centrality_bonus *= HIGH_CENTRALITY_ROUTING_BONUS
                centrality_bonus = min(centrality_bonus, 0.15)

            # Confidence multiplier
            confidence_mult = 0.5 + (route.confidence * 0.5)

            # Adjust weights to include centrality
            if use_centrality_scoring:
                return (
                    success_score * 0.35 +
                    fee_score * 0.35 +
                    hive_bonus * 0.15 +
                    centrality_bonus
                ) * confidence_mult
            else:
                return (
                    success_score * 0.4 +
                    fee_score * 0.4 +
                    hive_bonus * 0.2
                ) * confidence_mult

        return max(candidates, key=score_route)

    def get_routes_to(
        self,
        destination: str,
        amount_sats: int = 0,
        limit: int = 5
    ) -> List[RouteSuggestion]:
        """
        Get all known routes to a destination, sorted by quality.

        Args:
            destination: Target node pubkey
            amount_sats: Minimum capacity required (0 for any)
            limit: Maximum routes to return

        Returns:
            List of route suggestions
        """
        candidates = []
        stale_cutoff = time.time() - (PROBE_STALENESS_HOURS * 3600)

        with self._lock:
            items = list(self._path_stats.items())

        for (dest, path), stats in items:
            if dest != destination:
                continue

            if stats.probe_count == 0:
                continue

            success_rate = stats.success_count / stats.probe_count

            # Check capacity if specified
            if amount_sats > 0 and stats.avg_capacity_sats > 0:
                if stats.avg_capacity_sats < amount_sats:
                    continue

            if stats.success_count > 0:
                avg_latency = stats.total_latency_ms // stats.success_count
                avg_fee = stats.total_fee_ppm // stats.success_count
            else:
                avg_latency = 0
                avg_fee = 0

            candidates.append(RouteSuggestion(
                destination=destination,
                path=list(path),
                expected_fee_ppm=avg_fee,
                expected_latency_ms=avg_latency,
                success_rate=success_rate,
                confidence=self._confidence_from_stats(stats, stale_cutoff),
                last_successful_probe=stats.last_success_time,
                hive_hop_count=0
            ))

        # Sort by success rate
        candidates.sort(key=lambda r: r.success_rate, reverse=True)

        return candidates[:limit]

    def get_routing_stats(self) -> Dict[str, Any]:
        """
        Get overall routing intelligence statistics.

        Returns:
            Dict with routing statistics
        """
        with self._lock:
            stats_values = list(self._path_stats.values())
            destinations = {dest for dest, _ in self._path_stats}

        total_paths = len(stats_values)
        total_probes = sum(s.probe_count for s in stats_values)
        total_successes = sum(s.success_count for s in stats_values)

        # High quality paths (>90% success)
        high_quality = sum(
            1 for s in stats_values
            if s.probe_count > 0 and s.success_count / s.probe_count >= HIGH_SUCCESS_RATE
        )

        # Recent activity
        now = time.time()
        recent_cutoff = now - (24 * 3600)
        recent_probes = sum(
            1 for s in stats_values
            if max(s.last_success_time, s.last_failure_time) > recent_cutoff
        )

        return {
            "total_paths": total_paths,
            "total_probes": total_probes,
            "total_successes": total_successes,
            "overall_success_rate": total_successes / total_probes if total_probes > 0 else 0,
            "unique_destinations": len(destinations),
            "high_quality_paths": high_quality,
            "recent_activity_count": recent_probes,
        }

    def cleanup_stale_data(self):
        """Remove stale path statistics."""
        now = time.time()
        stale_cutoff = now - (PROBE_STALENESS_HOURS * 3600)

        with self._lock:
            stale_keys = [
                key for key, stats in self._path_stats.items()
                if max(stats.last_success_time, stats.last_failure_time) < stale_cutoff
            ]

            for key in stale_keys:
                del self._path_stats[key]

        return len(stale_keys)
