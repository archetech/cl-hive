"""
Database module for cl-hive

Handles SQLite persistence for:
- Hive membership registry
- Intent locks for conflict resolution
- Hive state (HiveMap) cache
- Contribution ledger (anti-leech tracking)
- Ban list (distributed immunity)

Thread Safety:
- Uses threading.local() to provide each thread with its own SQLite connection
- Prevents race conditions during concurrent writes
"""

import sqlite3
import os
import time
import json
import threading
import hashlib
from contextlib import contextmanager
from typing import Dict, List, Optional, Any, Tuple, Generator


class HiveDatabase:
    """
    SQLite database manager for the Hive plugin.
    
    Provides persistence for:
    - Member registry (peer_id, contribution, uptime)
    - Intent locks (conflict resolution)
    - Hive state cache (fleet topology view)
    - Contribution ledger (forwarding stats)
    - Ban list (shared immunity)
    
    Thread Safety:
    - Each thread gets its own isolated SQLite connection via threading.local()
    - WAL mode enabled for better concurrent read/write performance
    """
    
    def __init__(self, db_path: str, plugin):
        """
        Initialize the database manager.
        
        Args:
            db_path: Path to SQLite database file
            plugin: Reference to the pyln Plugin (or proxy) for logging
        """
        self.db_path = os.path.expanduser(db_path)
        self.plugin = plugin
        # Thread-local storage for connections
        self._local = threading.local()
        
    def _get_connection(self) -> sqlite3.Connection:
        """
        Get or create a thread-local database connection.
        
        Each thread gets its own isolated connection to prevent race conditions
        during concurrent database operations.
        
        Returns:
            sqlite3.Connection: Thread-local database connection
        """
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            
            # Create new connection for this thread
            # Use isolation_level=None (autocommit mode) - each statement commits immediately.
            # This prevents long-running implicit transactions from holding locks.
            # For explicit transactions, use BEGIN/COMMIT/ROLLBACK directly.
            self._local.conn = sqlite3.connect(
                self.db_path,
                isolation_level=None,  # Autocommit mode - critical for multi-threaded access
                timeout=30.0  # Wait up to 30s for locks instead of failing immediately
            )
            self._local.conn.row_factory = sqlite3.Row

            # Enable Write-Ahead Logging for better multi-thread concurrency
            self._local.conn.execute("PRAGMA journal_mode=WAL;")
            # Enable foreign key enforcement (required per-connection in SQLite)
            self._local.conn.execute("PRAGMA foreign_keys=ON;")
            
            self.plugin.log(
                f"HiveDatabase: Created thread-local connection (thread={threading.current_thread().name})",
                level='debug'
            )
        return self._local.conn

    def close_connection(self):
        """Close the thread-local connection if it exists."""
        conn = getattr(self._local, 'conn', None)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """
        Context manager for atomic database transactions.

        Use this for multi-step operations that must succeed or fail together.

        Example:
            with self.transaction() as conn:
                conn.execute("INSERT INTO table1 ...")
                conn.execute("INSERT INTO table2 ...")
            # Both inserts committed, or both rolled back on error

        Yields:
            sqlite3.Connection: The thread-local connection in transaction mode
        """
        conn = self._get_connection()
        try:
            # BEGIN IMMEDIATE acquires write lock immediately, preventing deadlocks
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass  # Don't mask the original exception
            raise

    def _table_create_sql(self, conn: sqlite3.Connection, table_name: str) -> str:
        """Return CREATE TABLE SQL for table_name (empty string if missing)."""
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        if not row:
            return ""
        return str(row["sql"] or "")
    def initialize(self):
        """Create database tables if they don't exist."""
        conn = self._get_connection()
        
        # =====================================================================
        # HIVE MEMBERS TABLE
        # =====================================================================
        # Core membership registry tracking tier, contribution, and uptime
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hive_members (
                peer_id TEXT PRIMARY KEY,
                tier TEXT NOT NULL DEFAULT 'member',
                joined_at INTEGER NOT NULL,
                contribution_ratio REAL DEFAULT 0.0,
                uptime_pct REAL DEFAULT 0.0,
                last_seen INTEGER,
                metadata TEXT,
                addresses TEXT
            )
        """)
        # Add addresses column if upgrading from older schema
        try:
            conn.execute(
                "ALTER TABLE hive_members ADD COLUMN addresses TEXT"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists

        # =====================================================================
        # INTENT LOCKS TABLE
        # =====================================================================
        # Tracks Intent Lock protocol state for conflict resolution
        conn.execute("""
            CREATE TABLE IF NOT EXISTS intent_locks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_type TEXT NOT NULL,
                target TEXT NOT NULL,
                initiator TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                signature TEXT
            )
        """)
        
        # Index for quick lookup of active intents by target
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_intent_locks_target
            ON intent_locks(target, status)
        """)

        # Add reason column for audit trail if upgrading from older schema
        try:
            conn.execute(
                "ALTER TABLE intent_locks ADD COLUMN reason TEXT"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists

        # =====================================================================
        # HIVE STATE TABLE
        # =====================================================================
        # Local cache of fleet state (HiveMap)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hive_state (
                peer_id TEXT PRIMARY KEY,
                capacity_sats INTEGER,
                available_sats INTEGER,
                fee_policy TEXT,
                topology TEXT,
                last_gossip INTEGER,
                state_hash TEXT,
                version INTEGER DEFAULT 0
            )
        """)
        
        # =====================================================================
        # CONTRIBUTION LEDGER TABLE
        # =====================================================================
        # Tracks forwarding events for contribution ratio calculation
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contribution_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                peer_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                amount_sats INTEGER NOT NULL,
                timestamp INTEGER NOT NULL
            )
        """)
        
        # Index for efficient ratio calculation
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_contribution_peer_time 
            ON contribution_ledger(peer_id, timestamp)
        """)

        # =====================================================================
        # MEMBERSHIP AUDIT LOG TABLE
        # =====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS membership_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                peer_id TEXT NOT NULL,
                actor_peer_id TEXT,
                reason TEXT,
                timestamp INTEGER NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_membership_audit_peer "
            "ON membership_audit_log(peer_id)"
        )

        # =====================================================================
        # MEMBERSHIP TOMBSTONES TABLE
        # =====================================================================
        # Durable deletion feed for membership convergence and replay safety
        conn.execute("""
            CREATE TABLE IF NOT EXISTS membership_tombstones (
                event_id TEXT PRIMARY KEY,
                peer_id TEXT NOT NULL,
                event TEXT NOT NULL,
                actor_peer_id TEXT,
                reason TEXT,
                timestamp INTEGER NOT NULL,
                joined_at_cutoff INTEGER NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_membership_tombstones_peer "
            "ON membership_tombstones(peer_id, timestamp DESC)"
        )

        # =====================================================================
        # LOCAL FEE TRACKING TABLE (Settlement Phase)
        # =====================================================================
        # Persists fee tracking state across restarts to prevent revenue loss
        conn.execute("""
            CREATE TABLE IF NOT EXISTS local_fee_tracking (
                id INTEGER PRIMARY KEY DEFAULT 1,
                earned_sats INTEGER NOT NULL DEFAULT 0,
                forward_count INTEGER NOT NULL DEFAULT 0,
                period_start_ts INTEGER NOT NULL DEFAULT 0,
                last_broadcast_ts INTEGER NOT NULL DEFAULT 0,
                last_broadcast_amount INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0
            )
        """)

        # =====================================================================
        # CONTRIBUTION RATE LIMITS TABLE
        # =====================================================================
        # Persists rate limit state across restarts to prevent bypass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contribution_rate_limits (
                peer_id TEXT PRIMARY KEY,
                window_start INTEGER NOT NULL,
                event_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rate_limits_window "
            "ON contribution_rate_limits(window_start)"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS contribution_daily_stats (
                id INTEGER PRIMARY KEY DEFAULT 1,
                window_start_ts INTEGER NOT NULL DEFAULT 0,
                event_count INTEGER NOT NULL DEFAULT 0
            )
        """)

        # =====================================================================
        # PEER PRESENCE TABLE
        # =====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS peer_presence (
                peer_id TEXT PRIMARY KEY,
                last_change_ts INTEGER NOT NULL,
                is_online INTEGER NOT NULL,
                online_seconds_rolling INTEGER NOT NULL,
                window_start_ts INTEGER NOT NULL
            )
        """)
        
        # =====================================================================
        # HIVE BANS TABLE
        # =====================================================================
        # Shared ban list for distributed immunity
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hive_bans (
                peer_id TEXT PRIMARY KEY,
                reason TEXT,
                reporter TEXT NOT NULL,
                signature TEXT,
                banned_at INTEGER NOT NULL,
                expires_at INTEGER
            )
        """)

        # =====================================================================
        # PLANNER LOG TABLE (Phase 6)
        # =====================================================================
        # Audit log for automated planner decisions
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hive_planner_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                target TEXT,
                result TEXT NOT NULL,
                details TEXT
            )
        """)

        # =====================================================================
        # PLANNER IGNORED PEERS TABLE
        # =====================================================================
        # Persistent storage for manually ignored peers (prevents planner from
        # opening channels to these peers until released)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS planner_ignored_peers (
                peer_id TEXT PRIMARY KEY,
                ignored_at INTEGER NOT NULL,
                reason TEXT,
                expires_at INTEGER
            )
        """)

        # =====================================================================
        # FEE INTELLIGENCE TABLE (Phase 7 - Cooperative Fee Coordination)
        # =====================================================================
        # Stores fee intelligence reports from hive members
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fee_intelligence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id TEXT NOT NULL,
                target_peer_id TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                our_fee_ppm INTEGER,
                their_fee_ppm INTEGER,
                forward_count INTEGER,
                forward_volume_sats INTEGER,
                revenue_sats INTEGER,
                flow_direction TEXT,
                utilization_pct REAL,
                last_fee_change_ppm INTEGER,
                volume_delta_pct REAL,
                days_observed INTEGER,
                signature TEXT NOT NULL
            )
        """)

        # Index for querying by target peer
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_fee_intel_target
            ON fee_intelligence(target_peer_id)
        """)

        # Index for querying by reporter
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_fee_intel_reporter
            ON fee_intelligence(reporter_id)
        """)

        # =====================================================================
        # MEMBER HEALTH TABLE (Phase 7 - NNLB Health Tracking)
        # =====================================================================
        # Stores health reports from hive members for NNLB coordination
        conn.execute("""
            CREATE TABLE IF NOT EXISTS member_health (
                peer_id TEXT PRIMARY KEY,
                timestamp INTEGER NOT NULL,
                overall_health INTEGER,
                capacity_score INTEGER,
                revenue_score INTEGER,
                connectivity_score INTEGER,
                tier TEXT,
                needs_help INTEGER DEFAULT 0,
                can_help_others INTEGER DEFAULT 0,
                needs_inbound INTEGER DEFAULT 0,
                needs_outbound INTEGER DEFAULT 0,
                needs_channels INTEGER DEFAULT 0,
                assistance_budget_sats INTEGER DEFAULT 0
            )
        """)

        # =====================================================================
        # PEER FEE PROFILES TABLE (Phase 7 - Aggregated Fee Intelligence)
        # =====================================================================
        # Stores aggregated fee profiles for external peers
        conn.execute("""
            CREATE TABLE IF NOT EXISTS peer_fee_profiles (
                peer_id TEXT PRIMARY KEY,
                reporter_count INTEGER DEFAULT 0,
                avg_fee_charged REAL DEFAULT 0,
                min_fee_charged INTEGER DEFAULT 0,
                max_fee_charged INTEGER DEFAULT 0,
                total_hive_volume INTEGER DEFAULT 0,
                total_hive_revenue INTEGER DEFAULT 0,
                avg_utilization REAL DEFAULT 0,
                estimated_elasticity REAL DEFAULT 0,
                optimal_fee_estimate INTEGER DEFAULT 0,
                last_update INTEGER NOT NULL,
                confidence REAL DEFAULT 0
            )
        """)

        # =====================================================================
        # PEER REPUTATION TABLE (Phase 5 - Advanced Cooperation)
        # =====================================================================
        # Stores reputation reports about external peers from hive members
        conn.execute("""
            CREATE TABLE IF NOT EXISTS peer_reputation (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id TEXT NOT NULL,
                peer_id TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                uptime_pct REAL DEFAULT 1.0,
                response_time_ms INTEGER DEFAULT 0,
                force_close_count INTEGER DEFAULT 0,
                fee_stability REAL DEFAULT 1.0,
                htlc_success_rate REAL DEFAULT 1.0,
                channel_age_days INTEGER DEFAULT 0,
                total_routed_sats INTEGER DEFAULT 0,
                warnings TEXT DEFAULT '[]',
                observation_days INTEGER DEFAULT 7
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_peer_reputation_peer_id "
            "ON peer_reputation(peer_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_peer_reputation_timestamp "
            "ON peer_reputation(timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_peer_reputation_reporter "
            "ON peer_reputation(reporter_id)"
        )

        # =====================================================================
        # FLOW SAMPLES TABLE (Phase 7.1 - Anticipatory Liquidity)
        # =====================================================================
        # Stores hourly flow samples for temporal pattern detection
        conn.execute("""
            CREATE TABLE IF NOT EXISTS flow_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL,
                hour INTEGER NOT NULL,
                day_of_week INTEGER NOT NULL,
                inbound_sats INTEGER NOT NULL DEFAULT 0,
                outbound_sats INTEGER NOT NULL DEFAULT 0,
                net_flow_sats INTEGER NOT NULL DEFAULT 0,
                timestamp INTEGER NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_flow_samples_channel_ts "
            "ON flow_samples(channel_id, timestamp DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_flow_samples_hour "
            "ON flow_samples(hour)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_flow_samples_day "
            "ON flow_samples(day_of_week)"
        )

        # =====================================================================
        # TEMPORAL PATTERNS TABLE (Phase 7.1 - Anticipatory Liquidity)
        # =====================================================================
        # Stores detected temporal patterns for liquidity prediction
        conn.execute("""
            CREATE TABLE IF NOT EXISTS temporal_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL,
                hour_of_day INTEGER,
                day_of_week INTEGER,
                direction TEXT NOT NULL,
                intensity REAL NOT NULL DEFAULT 1.0,
                confidence REAL NOT NULL DEFAULT 0.5,
                samples INTEGER NOT NULL DEFAULT 0,
                avg_flow_sats INTEGER NOT NULL DEFAULT 0,
                detected_at INTEGER NOT NULL,
                UNIQUE(channel_id, hour_of_day, day_of_week)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_temporal_patterns_channel "
            "ON temporal_patterns(channel_id)"
        )

        # =====================================================================
        # PEER CAPABILITIES TABLE (Phase B - Version Tolerance)
        # =====================================================================
        # Stores peer feature sets and max supported protocol version
        conn.execute("""
            CREATE TABLE IF NOT EXISTS peer_capabilities (
                peer_id TEXT PRIMARY KEY,
                features TEXT NOT NULL DEFAULT '[]',
                max_protocol_version INTEGER NOT NULL DEFAULT 1,
                plugin_version TEXT DEFAULT '',
                updated_at INTEGER NOT NULL
            )
        """)

        # =====================================================================
        # PROTO EVENTS TABLE (Phase C - Deterministic Idempotency)
        # =====================================================================
        # Persistent dedup for state-changing protocol messages
        conn.execute("""
            CREATE TABLE IF NOT EXISTS proto_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                received_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_proto_events_created
            ON proto_events(created_at)
        """)

        # =====================================================================
        # PROTO OUTBOX TABLE (Phase D - Reliable Delivery)
        # =====================================================================
        # Per-peer message delivery tracking with retry and backoff
        conn.execute("""
            CREATE TABLE IF NOT EXISTS proto_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id TEXT NOT NULL,
                peer_id TEXT NOT NULL,
                msg_type INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at INTEGER NOT NULL,
                sent_at INTEGER,
                next_retry_at INTEGER NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                expires_at INTEGER NOT NULL,
                last_error TEXT,
                acked_at INTEGER,
                UNIQUE(msg_id, peer_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_proto_outbox_retry
            ON proto_outbox(status, next_retry_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_proto_outbox_peer
            ON proto_outbox(peer_id, status)
        """)

        # =====================================================================
        # TRAFFIC PROFILES TABLE (Traffic Intelligence)
        # =====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS traffic_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id TEXT NOT NULL,
                peer_id TEXT NOT NULL,
                profile_type TEXT DEFAULT 'unknown',
                peak_hours_utc TEXT DEFAULT '[]',
                quiet_hours_utc TEXT DEFAULT '[]',
                avg_forward_size_sats REAL DEFAULT 0,
                daily_volume_sats REAL DEFAULT 0,
                drain_direction TEXT DEFAULT 'balanced',
                confidence REAL DEFAULT 0.0,
                observation_window_hours INTEGER DEFAULT 24,
                received_at REAL NOT NULL,
                UNIQUE(reporter_id, peer_id)
            )
        """)

        # =====================================================================
        # LIQUIDITY NEEDS TABLE (Liquidity Coordination)
        # =====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS liquidity_needs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id TEXT NOT NULL,
                need_type TEXT NOT NULL,
                target_peer_id TEXT NOT NULL,
                amount_sats INTEGER DEFAULT 0,
                urgency TEXT DEFAULT 'low',
                max_fee_ppm INTEGER DEFAULT 0,
                reason TEXT DEFAULT '',
                current_balance_pct REAL DEFAULT 0.5,
                timestamp INTEGER NOT NULL,
                UNIQUE(reporter_id, target_peer_id, need_type)
            )
        """)

        # =====================================================================
        # LEECH FLAGS TABLE (Contribution / Anti-Leech)
        # =====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leech_flags (
                peer_id TEXT PRIMARY KEY,
                low_since_ts INTEGER NOT NULL,
                ban_triggered INTEGER DEFAULT 0
            )
        """)

        # =====================================================================
        # PEER EVENTS TABLE (Quality Scorer / Peer History)
        # =====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS peer_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                peer_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                details TEXT DEFAULT '{}',
                reporter_id TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_peer_events_peer
            ON peer_events(peer_id, timestamp)
        """)

        # =====================================================================
        # MEMBER LIQUIDITY STATE TABLE (Liquidity Coordination)
        # =====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS member_liquidity_state (
                member_id TEXT PRIMARY KEY,
                depleted_count INTEGER DEFAULT 0,
                saturated_count INTEGER DEFAULT 0,
                rebalancing_active INTEGER DEFAULT 0,
                rebalancing_peers TEXT DEFAULT '[]',
                timestamp INTEGER NOT NULL
            )
        """)

    def add_member(self, peer_id: str, tier: str = 'member',
                   joined_at: Optional[int] = None) -> bool:
        """
        Add a new member to the Hive.

        Args:
            peer_id: 66-character hex public key
            joined_at: Unix timestamp (defaults to now)

        Returns:
            True if successful, False if member already exists
        """
        conn = self._get_connection()
        now = int(time.time())

        try:
            conn.execute("""
                INSERT INTO hive_members (peer_id, tier, joined_at, last_seen)
                VALUES (?, ?, ?, ?)
            """, (peer_id, tier, joined_at or now, now))
            return True
        except sqlite3.IntegrityError:
            return False  # Already exists
    
    def get_member(self, peer_id: str) -> Optional[Dict[str, Any]]:
        """Get member info by peer_id."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM hive_members WHERE peer_id = ?",
            (peer_id,)
        ).fetchone()
        return dict(row) if row else None
    
    def get_all_members(self) -> List[Dict[str, Any]]:
        """Get all Hive members."""
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM hive_members ORDER BY joined_at LIMIT 1000"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_membership_hash(self) -> str:
        """
        Calculate deterministic hash of membership state.

        Includes peer_id and tier for each member, sorted by peer_id.
        Used to detect membership divergence between nodes and trigger
        FULL_SYNC when membership lists differ.

        Returns:
            Hex-encoded SHA256 hash of membership state
        """
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT peer_id, tier FROM hive_members ORDER BY peer_id"
        ).fetchall()

        # Build list of (peer_id, tier) tuples
        member_tuples = [(row['peer_id'], row['tier']) for row in rows]

        # Serialize to canonical JSON
        json_str = json.dumps(member_tuples, sort_keys=True, separators=(',', ':'))

        # Calculate SHA256
        hash_hex = hashlib.sha256(json_str.encode('utf-8')).hexdigest()

        return hash_hex

    def update_member(self, peer_id: str, **kwargs) -> bool:
        """
        Update member fields.

        Allowed fields: tier, contribution_ratio, uptime_pct,
                       last_seen, metadata, addresses
        """
        allowed = {'tier', 'contribution_ratio', 'uptime_pct',
                   'last_seen', 'metadata', 'addresses'}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        
        if not updates:
            return False
        
        conn = self._get_connection()
        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [peer_id]
        
        result = conn.execute(
            f"UPDATE hive_members SET {set_clause} WHERE peer_id = ?",
            values
        )
        return result.rowcount > 0
    
    def remove_member(self, peer_id: str) -> bool:
        """Remove a member from the Hive."""
        conn = self._get_connection()
        result = conn.execute(
            "DELETE FROM hive_members WHERE peer_id = ?",
            (peer_id,)
        )
        return result.rowcount > 0
    
    # =========================================================================
    # INTENT LOCK OPERATIONS
    # =========================================================================
    
    def create_intent(self, intent_type: str, target: str, initiator: str,
                      expires_seconds: int = 300,
                      timestamp: Optional[int] = None) -> int:
        """
        Create a new Intent lock.

        Args:
            intent_type: 'channel_open', 'rebalance', 'ban_peer'
            target: Target peer_id or identifier
            initiator: Our node pubkey
            expires_seconds: Lock TTL
            timestamp: Creation timestamp (uses current time if None)

        Returns:
            Intent ID
        """
        conn = self._get_connection()
        now = timestamp if timestamp is not None else int(time.time())
        expires = now + expires_seconds

        cursor = conn.execute("""
            INSERT INTO intent_locks (intent_type, target, initiator, timestamp, expires_at, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
        """, (intent_type, target, initiator, now, expires))

        return cursor.lastrowid

    def create_intent_if_no_conflict(self, intent_type: str, target: str,
                                      initiator: str, expires_seconds: int = 300,
                                      timestamp: Optional[int] = None) -> Optional[int]:
        """
        Atomically check for conflicting intents and create a new one.

        Uses BEGIN IMMEDIATE to prevent TOCTOU race between the conflict
        check and the insert.

        Returns:
            Intent ID if created, None if a conflicting intent already exists.
        """
        conn = self._get_connection()
        now = timestamp if timestamp is not None else int(time.time())
        expires = now + expires_seconds

        try:
            conn.execute("BEGIN IMMEDIATE")
            # Check ALL initiators for conflicts (not just self)
            rows = conn.execute("""
                SELECT id FROM intent_locks
                WHERE target = ? AND intent_type = ?
                  AND status = 'pending' AND expires_at > ?
            """, (target, intent_type, now)).fetchall()
            if rows:
                conn.execute("ROLLBACK")
                return None

            cursor = conn.execute("""
                INSERT INTO intent_locks (intent_type, target, initiator, timestamp, expires_at, status)
                VALUES (?, ?, ?, ?, ?, 'pending')
            """, (intent_type, target, initiator, now, expires))
            intent_id = cursor.lastrowid
            conn.execute("COMMIT")
            return intent_id
        except Exception as e:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: create_intent_if_no_conflict error: {e}",
                    level='error'
                )
            return None

    def get_conflicting_intents(self, target: str, intent_type: str) -> List[Dict]:
        """Get active intents for the same target."""
        conn = self._get_connection()
        now = int(time.time())
        
        rows = conn.execute("""
            SELECT * FROM intent_locks 
            WHERE target = ? AND intent_type = ? AND status = 'pending' AND expires_at > ?
        """, (target, intent_type, now)).fetchall()
        
        return [dict(row) for row in rows]
    
    def update_intent_status(self, intent_id: int, status: str,
                             expected_status: str = None, reason: str = None) -> bool:
        """Update Intent status with optional CAS guard and reason for audit trail.

        Args:
            intent_id: Intent lock ID
            status: New status to set
            expected_status: If provided, UPDATE only succeeds if current status matches (CAS guard)
            reason: Optional reason string for audit trail

        Returns:
            True if row was updated, False if not found or expected_status mismatch
        """
        conn = self._get_connection()
        if expected_status:
            if reason:
                result = conn.execute(
                    "UPDATE intent_locks SET status = ?, reason = ? WHERE id = ? AND status = ?",
                    (status, reason, intent_id, expected_status)
                )
            else:
                result = conn.execute(
                    "UPDATE intent_locks SET status = ? WHERE id = ? AND status = ?",
                    (status, intent_id, expected_status)
                )
        else:
            if reason:
                result = conn.execute(
                    "UPDATE intent_locks SET status = ?, reason = ? WHERE id = ?",
                    (status, reason, intent_id)
                )
            else:
                result = conn.execute(
                    "UPDATE intent_locks SET status = ? WHERE id = ?",
                    (status, intent_id)
                )
        return result.rowcount > 0
    
    def cleanup_expired_intents(self) -> int:
        """Soft-delete expired intents, then purge terminal intents after 24h.

        Phase 1: Mark pending expired intents as 'expired' (preserves audit trail).
        Phase 2: Hard-delete terminal intents (expired/aborted/failed) older than 24h.

        Returns:
            Total number of intents affected (soft-deleted + purged)
        """
        conn = self._get_connection()
        now = int(time.time())

        # D2 FIX: Wrap multi-statement cleanup in transaction for atomicity
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Phase 1: Soft-delete - mark pending expired intents
            r1 = conn.execute(
                "UPDATE intent_locks SET status = 'expired', reason = 'ttl_expired' "
                "WHERE status = 'pending' AND expires_at < ?",
                (now,)
            )

            # Phase 2: Purge terminal intents older than 24 hours
            purge_cutoff = now - 86400
            r2 = conn.execute(
                "DELETE FROM intent_locks "
                "WHERE status IN ('expired', 'aborted', 'failed') AND expires_at < ?",
                (purge_cutoff,)
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

        return r1.rowcount + r2.rowcount
    
    def get_pending_intents_ready(self, hold_seconds: int) -> List[Dict]:
        """
        Get pending intents where hold period has elapsed.

        Args:
            hold_seconds: The hold period that must have passed

        Returns:
            List of intent rows ready to commit
        """
        conn = self._get_connection()
        now = int(time.time())
        cutoff = now - hold_seconds

        rows = conn.execute("""
            SELECT * FROM intent_locks
            WHERE status = 'pending' AND timestamp <= ? AND expires_at > ?
            ORDER BY timestamp
        """, (cutoff, now)).fetchall()

        return [dict(row) for row in rows]

    def get_pending_intents(self) -> List[Dict]:
        """
        Get all active pending intents.

        Returns:
            List of pending intent rows that haven't expired
        """
        conn = self._get_connection()
        now = int(time.time())

        rows = conn.execute("""
            SELECT * FROM intent_locks
            WHERE status = 'pending' AND expires_at > ?
            ORDER BY timestamp
        """, (now,)).fetchall()

        return [dict(row) for row in rows]

    def recover_stuck_intents(self, max_age_seconds: int = 300) -> int:
        """
        Mark intents stuck in 'committed' state as 'failed'.

        Intents that remain in 'committed' for longer than max_age_seconds
        are assumed to have failed execution and are freed for retry.

        Args:
            max_age_seconds: Max age in seconds before marking as failed

        Returns:
            Number of intents recovered
        """
        conn = self._get_connection()
        cutoff = int(time.time()) - max_age_seconds
        result = conn.execute(
            "UPDATE intent_locks SET status = 'failed', reason = 'stuck_recovery' "
            "WHERE status = 'committed' AND timestamp < ?",
            (cutoff,)
        )
        return result.rowcount

    def get_intent_by_id(self, intent_id: int) -> Optional[Dict]:
        """Get a specific intent by ID."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM intent_locks WHERE id = ?",
            (intent_id,)
        ).fetchone()
        return dict(row) if row else None
    
    # =========================================================================
    # HIVE STATE OPERATIONS
    # =========================================================================
    
    def update_hive_state(self, peer_id: str, capacity_sats: int,
                          available_sats: int, fee_policy: Dict,
                          topology: List[str], state_hash: str,
                          version: Optional[int] = None,
                          last_update_ts: Optional[int] = None) -> None:
        """Update local cache of a peer's Hive state.

        Uses version-guarded writes: only writes if the new version is
        higher than what's already in the DB, preventing late-arriving
        writes from overwriting newer state after concurrent updates.
        """
        conn = self._get_connection()
        stored_ts = last_update_ts if last_update_ts is not None else int(time.time())

        fee_json = json.dumps(fee_policy)
        topo_json = json.dumps(topology)

        if version is not None:
            # Insert if new, or update only if our version is higher
            conn.execute("""
                INSERT INTO hive_state
                (peer_id, capacity_sats, available_sats, fee_policy, topology,
                 last_gossip, state_hash, version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(peer_id) DO UPDATE SET
                    capacity_sats = excluded.capacity_sats,
                    available_sats = excluded.available_sats,
                    fee_policy = excluded.fee_policy,
                    topology = excluded.topology,
                    last_gossip = excluded.last_gossip,
                    state_hash = excluded.state_hash,
                    version = excluded.version
                WHERE excluded.version > hive_state.version
            """, (
                peer_id, capacity_sats, available_sats,
                fee_json, topo_json,
                stored_ts, state_hash, version
            ))
        else:
            # Auto-increment for backward compatibility
            conn.execute("""
                INSERT INTO hive_state
                (peer_id, capacity_sats, available_sats, fee_policy, topology,
                 last_gossip, state_hash, version)
                VALUES (?, ?, ?, ?, ?, ?, ?,
                        COALESCE((SELECT version FROM hive_state WHERE peer_id = ?), 0) + 1)
                ON CONFLICT(peer_id) DO UPDATE SET
                    capacity_sats = excluded.capacity_sats,
                    available_sats = excluded.available_sats,
                    fee_policy = excluded.fee_policy,
                    topology = excluded.topology,
                    last_gossip = excluded.last_gossip,
                    state_hash = excluded.state_hash,
                    version = COALESCE((SELECT version FROM hive_state WHERE peer_id = ?), 0) + 1
            """, (
                peer_id, capacity_sats, available_sats,
                fee_json, topo_json,
                stored_ts, state_hash, peer_id, peer_id
            ))
    
    def get_all_hive_states(self) -> List[Dict]:
        """Get cached state for all Hive peers."""
        conn = self._get_connection()
        rows = conn.execute("SELECT * FROM hive_state").fetchall()

        results = []
        for row in rows:
            result = dict(row)
            result['fee_policy'] = json.loads(result['fee_policy'] or '{}')
            result['topology'] = json.loads(result['topology'] or '[]')
            results.append(result)
        return results

    def delete_hive_state_if_stale(self, peer_id: str, cutoff_timestamp: int) -> bool:
        """Delete a peer's state only if it's still stale (last_gossip < cutoff).

        Prevents race where a fresh gossip re-inserts state between
        in-memory removal and DB deletion.

        Returns:
            True if a row was deleted.
        """
        conn = self._get_connection()
        result = conn.execute(
            "DELETE FROM hive_state WHERE peer_id = ? AND last_gossip < ?",
            (peer_id, cutoff_timestamp)
        )
        return result.rowcount > 0

    # =========================================================================
    # CONTRIBUTION TRACKING
    # =========================================================================

    # Absolute cap on contribution ledger rows to prevent unbounded DB growth
    MAX_CONTRIBUTION_ROWS = 500000

    # Absolute caps on protocol tables to prevent unbounded DB growth
    MAX_PROTO_EVENT_ROWS = 500000
    MAX_PROTO_OUTBOX_ROWS = 100000

    # Ring-buffer cap on planner log rows
    MAX_PLANNER_LOG_ROWS = 10000

    def record_contribution(self, peer_id: str, direction: str,
                            amount_sats: int) -> bool:
        """
        Record a forwarding event for contribution tracking.

        P5-03: Rejects inserts if ledger exceeds MAX_CONTRIBUTION_ROWS.

        Args:
            peer_id: The Hive peer involved
            direction: 'forwarded' (we routed for them) or 'received' (they routed for us)
            amount_sats: Amount in satoshis

        Returns:
            True if recorded, False if rejected due to DB cap
        """
        conn = self._get_connection()
        now = int(time.time())

        try:
            # Atomic check-and-insert under BEGIN IMMEDIATE to prevent
            # concurrent threads from both passing the cap check.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT COUNT(*) as cnt FROM contribution_ledger").fetchone()
            if row and row['cnt'] >= self.MAX_CONTRIBUTION_ROWS:
                conn.execute("ROLLBACK")
                self.plugin.log(
                    f"HiveDatabase: Contribution ledger at cap ({self.MAX_CONTRIBUTION_ROWS}), rejecting insert",
                    level='warn'
                )
                return False

            conn.execute("""
                INSERT INTO contribution_ledger (peer_id, direction, amount_sats, timestamp)
                VALUES (?, ?, ?, ?)
            """, (peer_id, direction, amount_sats, now))
            conn.execute("COMMIT")
            return True
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def get_contribution_stats(self, peer_id: str, window_days: int = 30) -> Dict[str, int]:
        """
        Get contribution totals within the window.
        
        Returns:
            Dict with forwarded and received totals in sats
        """
        conn = self._get_connection()
        cutoff = int(time.time()) - (window_days * 86400)
        
        rows = conn.execute("""
            SELECT direction, SUM(amount_sats) as total
            FROM contribution_ledger
            WHERE peer_id = ? AND timestamp > ?
            GROUP BY direction
        """, (peer_id, cutoff)).fetchall()
        
        forwarded = 0
        received = 0
        for row in rows:
            if row['direction'] == 'forwarded':
                forwarded = row['total'] or 0
            elif row['direction'] == 'received':
                received = row['total'] or 0
        
        return {"forwarded": forwarded, "received": received}
    
    def get_contribution_ratio(self, peer_id: str, window_days: int = 30) -> float:
        """
        Calculate contribution ratio: forwarded / received.
        
        A ratio > 1.0 means the peer contributes more than they take.
        A ratio < 1.0 means the peer is a net consumer (potential leech).
        
        Args:
            peer_id: Hive peer to check
            window_days: Lookback period
            
        Returns:
            Contribution ratio (default 1.0 if no data)
        """
        conn = self._get_connection()
        cutoff = int(time.time()) - (window_days * 86400)
        
        rows = conn.execute("""
            SELECT direction, SUM(amount_sats) as total
            FROM contribution_ledger
            WHERE peer_id = ? AND timestamp > ?
            GROUP BY direction
        """, (peer_id, cutoff)).fetchall()
        
        forwarded = 0
        received = 0
        for row in rows:
            if row['direction'] == 'forwarded':
                forwarded = row['total'] or 0
            elif row['direction'] == 'received':
                received = row['total'] or 0
        
        if received == 0:
            # Cap at high ratio instead of inf to avoid propagating infinity
            return 1.0 if forwarded == 0 else 100.0
        
        return forwarded / received
    
    def prune_old_contributions(self, older_than_days: int = 45) -> int:
        """Remove contribution records older than specified days."""
        conn = self._get_connection()
        cutoff = int(time.time()) - (older_than_days * 86400)
        result = conn.execute(
            "DELETE FROM contribution_ledger WHERE timestamp < ?",
            (cutoff,)
        )
        return result.rowcount

    # =========================================================================
    # MEMBERSHIP AUDIT LOG
    # =========================================================================

    def log_membership_event(self, event: str, peer_id: str,
                              actor_peer_id: str = None,
                              reason: str = None) -> bool:
        """
        Record a membership lifecycle event.

        Args:
            event: Event type (joined, approved, banned, removed, left)
            peer_id: The member affected
            actor_peer_id: Who initiated the action (None for self-actions)
            reason: Optional reason string

        Returns:
            True if recorded
        """
        conn = self._get_connection()
        now = int(time.time())
        conn.execute("""
            INSERT INTO membership_audit_log (event, peer_id, actor_peer_id, reason, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """, (event, peer_id, actor_peer_id, reason, now))
        return True

    def get_membership_audit_log(self, peer_id: str = None,
                                  limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get membership audit log entries.

        Args:
            peer_id: Filter by peer (None for all)
            limit: Max entries to return

        Returns:
            List of audit log entries, newest first
        """
        conn = self._get_connection()
        if peer_id:
            rows = conn.execute(
                "SELECT * FROM membership_audit_log WHERE peer_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (peer_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM membership_audit_log "
                "ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def record_membership_tombstone(self, event_id: str, peer_id: str, event: str,
                                    actor_peer_id: str = None, reason: str = None,
                                    timestamp: int = None,
                                    joined_at_cutoff: int = 0) -> bool:
        """
        Persist a membership deletion tombstone for convergence and replay safety.

        Returns:
            True if inserted, False if the tombstone already exists.
        """
        conn = self._get_connection()
        ts = int(time.time()) if timestamp is None else int(timestamp)
        cutoff = int(joined_at_cutoff or 0)

        try:
            conn.execute(
                """INSERT INTO membership_tombstones
                   (event_id, peer_id, event, actor_peer_id, reason, timestamp, joined_at_cutoff)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (event_id, peer_id, event, actor_peer_id, reason, ts, cutoff)
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def get_membership_tombstones(self, limit: int = 500) -> List[Dict[str, Any]]:
        """
        Return durable membership tombstones, newest first.
        """
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM membership_tombstones ORDER BY timestamp DESC, event_id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    # =========================================================================
    # PEER PRESENCE
    # =========================================================================

    def get_presence(self, peer_id: str) -> Optional[Dict[str, Any]]:
        """Get presence record for a peer."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM peer_presence WHERE peer_id = ?",
            (peer_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_presence(self, peer_id: str, is_online: bool, now_ts: int,
                        window_seconds: int) -> None:
        """
        Update presence using a rolling accumulator.

        Wrapped in a transaction to prevent TOCTOU race between the
        existence check and the subsequent INSERT/UPDATE.
        """
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM peer_presence WHERE peer_id = ?",
                (peer_id,)
            ).fetchone()

            if not existing:
                conn.execute("""
                    INSERT INTO peer_presence
                    (peer_id, last_change_ts, is_online, online_seconds_rolling, window_start_ts)
                    VALUES (?, ?, ?, ?, ?)
                """, (peer_id, now_ts, 1 if is_online else 0, 0, now_ts))
                return

            last_change_ts = existing["last_change_ts"]
            online_seconds = existing["online_seconds_rolling"]
            window_start_ts = existing["window_start_ts"]
            was_online = bool(existing["is_online"])

            if was_online:
                online_seconds += max(0, now_ts - last_change_ts)

            if now_ts - window_start_ts > window_seconds:
                window_start_ts = now_ts - window_seconds
                if online_seconds > window_seconds:
                    online_seconds = window_seconds

            conn.execute("""
                UPDATE peer_presence
                SET last_change_ts = ?, is_online = ?, online_seconds_rolling = ?, window_start_ts = ?
                WHERE peer_id = ?
            """, (now_ts, 1 if is_online else 0, online_seconds, window_start_ts, peer_id))

    def prune_presence(self, window_seconds: int) -> int:
        """Clamp rolling windows to the configured window length."""
        conn = self._get_connection()
        now = int(time.time())
        cutoff = now - window_seconds
        result = conn.execute("""
            UPDATE peer_presence
            SET window_start_ts = ?, 
                online_seconds_rolling = CASE
                    WHEN online_seconds_rolling > ? THEN ?
                    ELSE online_seconds_rolling
                END
            WHERE window_start_ts < ?
        """, (cutoff, window_seconds, window_seconds, cutoff))
        return result.rowcount

    def sync_uptime_from_presence(self, window_seconds: int = 30 * 86400) -> int:
        """
        Calculate uptime percentage from peer_presence and update hive_members.

        Uses a single JOIN query instead of N+1 individual lookups.

        For each member with presence data, calculates:
        uptime_pct = online_seconds_rolling / elapsed_window_time

        Args:
            window_seconds: Rolling window size (default 30 days)

        Returns:
            Number of members updated
        """
        conn = self._get_connection()
        now = int(time.time())

        # Single JOIN query: members with their presence data
        rows = conn.execute("""
            SELECT m.peer_id, p.online_seconds_rolling, p.window_start_ts,
                   p.is_online, p.last_change_ts
            FROM hive_members m
            JOIN peer_presence p ON m.peer_id = p.peer_id
        """).fetchall()

        updated = 0
        with self.transaction() as tx_conn:
            for row in rows:
                online_seconds = row['online_seconds_rolling']

                # If currently online, add time since last state change
                if row['is_online']:
                    online_seconds += max(0, now - row['last_change_ts'])

                # Calculate window elapsed time
                elapsed = max(1, now - row['window_start_ts'])

                # Cap at window size
                if elapsed > window_seconds:
                    elapsed = window_seconds
                if online_seconds > elapsed:
                    online_seconds = elapsed

                uptime_pct = online_seconds / elapsed

                tx_conn.execute(
                    "UPDATE hive_members SET uptime_pct = ? WHERE peer_id = ?",
                    (uptime_pct, row['peer_id'])
                )
                updated += 1

        return updated
    def add_ban(self, peer_id: str, reason: str, reporter: str,
                signature: Optional[str] = None, 
                expires_days: Optional[int] = None) -> bool:
        """
        Add a peer to the ban list.
        
        Args:
            peer_id: Peer to ban
            reason: Human-readable reason
            reporter: Node that reported the ban
            signature: Cryptographic proof (optional)
            expires_days: Ban duration (None = permanent)
            
        Returns:
            True if added, False if already banned
        """
        conn = self._get_connection()
        now = int(time.time())
        expires = now + (expires_days * 86400) if expires_days else None
        
        try:
            conn.execute("""
                INSERT INTO hive_bans (peer_id, reason, reporter, signature, banned_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (peer_id, reason, reporter, signature, now, expires))
            return True
        except sqlite3.IntegrityError:
            return False
    
    def is_banned(self, peer_id: str) -> bool:
        """Check if a peer is banned."""
        conn = self._get_connection()
        now = int(time.time())
        
        row = conn.execute("""
            SELECT 1 FROM hive_bans 
            WHERE peer_id = ? AND (expires_at IS NULL OR expires_at > ?)
        """, (peer_id, now)).fetchone()
        
        return row is not None
    
    def get_ban_info(self, peer_id: str) -> Optional[Dict]:
        """Get ban details for a peer."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM hive_bans WHERE peer_id = ?",
            (peer_id,)
        ).fetchone()
        return dict(row) if row else None
    def count_outbox_pending(self) -> int:
        """
        Count outbox entries ready for sending or retry.

        More efficient than get_outbox_pending() when only a count is needed.

        Returns:
            Count of pending entries.
        """
        conn = self._get_connection()
        now = int(time.time())
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM proto_outbox
               WHERE status IN ('queued', 'sent')
                 AND next_retry_at <= ?
                 AND expires_at > ?""",
            (now, now)
        ).fetchone()
        return row['cnt'] if row else 0
    def log_planner_action(self, action_type: str, result: str,
                           target: Optional[str] = None,
                           details: Optional[Dict[str, Any]] = None) -> None:
        """
        Log a decision made by the Planner.

        Implements ring-buffer behavior: when MAX_PLANNER_LOG_ROWS is exceeded,
        oldest 10% of entries are pruned to make room.

        Wrapped in a transaction so the COUNT + DELETE + INSERT are atomic.

        Args:
            action_type: What the planner did (e.g., 'saturation_check', 'expansion')
            result: Outcome ('success', 'skipped', 'failed', 'proposed')
            target: Target peer related to the action
            details: Additional context as dict
        """
        now = int(time.time())
        details_json = json.dumps(details) if details else None

        with self.transaction() as conn:
            # Check row count and prune if at cap (ring-buffer behavior)
            row = conn.execute("SELECT COUNT(*) as cnt FROM hive_planner_log").fetchone()
            if row and row['cnt'] >= self.MAX_PLANNER_LOG_ROWS:
                # Delete oldest 10% to make room
                prune_count = self.MAX_PLANNER_LOG_ROWS // 10
                conn.execute("""
                    DELETE FROM hive_planner_log WHERE id IN (
                        SELECT id FROM hive_planner_log ORDER BY timestamp ASC LIMIT ?
                    )
                """, (prune_count,))
                self.plugin.log(
                    f"HiveDatabase: Planner log at cap ({self.MAX_PLANNER_LOG_ROWS}), pruned {prune_count} oldest entries",
                    level='debug'
                )

            conn.execute("""
                INSERT INTO hive_planner_log (timestamp, action_type, target, result, details)
                VALUES (?, ?, ?, ?, ?)
            """, (now, action_type, target, result, details_json))

    def get_planner_logs(self, limit: int = 50) -> List[Dict]:
        """Get recent planner logs."""
        conn = self._get_connection()
        rows = conn.execute("""
            SELECT * FROM hive_planner_log
            ORDER BY timestamp DESC LIMIT ?
        """, (limit,)).fetchall()

        results = []
        for row in rows:
            result = dict(row)
            if result['details']:
                try:
                    result['details'] = json.loads(result['details'])
                except json.JSONDecodeError:
                    pass
            results.append(result)
        return results

    # =========================================================================
    # PLANNER IGNORED PEERS
    # =========================================================================

    def add_ignored_peer(self, peer_id: str, reason: str = "manual",
                         duration_hours: Optional[int] = None) -> bool:
        """
        Add a peer to the planner ignore list.

        Ignored peers will not be selected as expansion targets until
        the ignore is released or expires.

        Args:
            peer_id: Pubkey of peer to ignore
            reason: Reason for ignoring (e.g., "manual", "connection_failed")
            duration_hours: Optional expiration in hours (None = permanent until released)

        Returns:
            True if added, False if already ignored
        """
        conn = self._get_connection()
        now = int(time.time())
        expires_at = now + (duration_hours * 3600) if duration_hours else None

        try:
            conn.execute("""
                INSERT OR REPLACE INTO planner_ignored_peers
                (peer_id, ignored_at, reason, expires_at)
                VALUES (?, ?, ?, ?)
            """, (peer_id, now, reason, expires_at))
            return True
        except Exception as e:
            self.plugin.log(f"HiveDatabase: Failed to add ignored peer: {e}", level='warning')
            return False

    def remove_ignored_peer(self, peer_id: str) -> bool:
        """
        Remove a peer from the planner ignore list.

        Args:
            peer_id: Pubkey of peer to unignore

        Returns:
            True if removed, False if not found
        """
        conn = self._get_connection()
        result = conn.execute(
            "DELETE FROM planner_ignored_peers WHERE peer_id = ?",
            (peer_id,)
        )
        return result.rowcount > 0

    def get_ignored_peers(self, include_expired: bool = False) -> List[Dict]:
        """
        Get list of currently ignored peers.

        Args:
            include_expired: If True, include expired ignores (default: False)

        Returns:
            List of ignored peer records
        """
        conn = self._get_connection()
        now = int(time.time())

        if include_expired:
            rows = conn.execute("""
                SELECT * FROM planner_ignored_peers
                ORDER BY ignored_at DESC
            """).fetchall()
        else:
            # Only return non-expired ignores
            rows = conn.execute("""
                SELECT * FROM planner_ignored_peers
                WHERE expires_at IS NULL OR expires_at > ?
                ORDER BY ignored_at DESC
            """, (now,)).fetchall()

        return [dict(row) for row in rows]

    def is_peer_ignored(self, peer_id: str) -> bool:
        """
        Check if a peer is currently ignored.

        Args:
            peer_id: Pubkey to check

        Returns:
            True if peer is ignored (and not expired)
        """
        conn = self._get_connection()
        now = int(time.time())
        row = conn.execute("""
            SELECT 1 FROM planner_ignored_peers
            WHERE peer_id = ? AND (expires_at IS NULL OR expires_at > ?)
        """, (peer_id, now)).fetchone()
        return row is not None

    def cleanup_expired_ignores(self) -> int:
        """
        Remove expired ignore entries.

        Returns:
            Number of expired ignores removed
        """
        conn = self._get_connection()
        now = int(time.time())
        result = conn.execute(
            "DELETE FROM planner_ignored_peers WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,)
        )
        return result.rowcount

    def prune_planner_logs(self, older_than_days: int = 30) -> int:
        """
        Remove planner logs older than specified days.

        Args:
            older_than_days: Delete logs older than this many days (default: 30)

        Returns:
            Number of records deleted
        """
        conn = self._get_connection()
        cutoff = int(time.time()) - (older_than_days * 86400)
        result = conn.execute(
            "DELETE FROM hive_planner_log WHERE timestamp < ?",
            (cutoff,)
        )
        return result.rowcount
    def store_fee_intelligence(
        self,
        reporter_id: str,
        target_peer_id: str,
        timestamp: int,
        our_fee_ppm: int,
        their_fee_ppm: int,
        forward_count: int,
        forward_volume_sats: int,
        revenue_sats: int,
        flow_direction: str,
        utilization_pct: float,
        signature: str,
        last_fee_change_ppm: int = 0,
        volume_delta_pct: float = 0.0,
        days_observed: int = 1
    ) -> int:
        """
        Store a fee intelligence report.

        Args:
            reporter_id: Hive member who reported this
            target_peer_id: External peer being reported on
            timestamp: Unix timestamp of the report
            our_fee_ppm: Fee charged to the peer
            their_fee_ppm: Fee the peer charges us
            forward_count: Number of forwards
            forward_volume_sats: Total volume routed
            revenue_sats: Fees earned from this peer
            flow_direction: 'source', 'sink', or 'balanced'
            utilization_pct: Channel utilization (0.0-1.0)
            signature: Cryptographic signature of the report
            last_fee_change_ppm: Previous fee rate
            volume_delta_pct: Volume change after fee change
            days_observed: How long peer has been observed

        Returns:
            ID of the inserted record
        """
        conn = self._get_connection()
        cursor = conn.execute("""
            INSERT INTO fee_intelligence (
                reporter_id, target_peer_id, timestamp, our_fee_ppm, their_fee_ppm,
                forward_count, forward_volume_sats, revenue_sats, flow_direction,
                utilization_pct, last_fee_change_ppm, volume_delta_pct, days_observed,
                signature
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            reporter_id, target_peer_id, timestamp, our_fee_ppm, their_fee_ppm,
            forward_count, forward_volume_sats, revenue_sats, flow_direction,
            utilization_pct, last_fee_change_ppm, volume_delta_pct, days_observed,
            signature
        ))
        return cursor.lastrowid

    def get_fee_intelligence_for_peer(
        self,
        target_peer_id: str,
        max_age_hours: int = 24
    ) -> List[Dict[str, Any]]:
        """
        Get all fee intelligence reports for a specific external peer.

        Args:
            target_peer_id: External peer to get reports for
            max_age_hours: Maximum age of reports in hours

        Returns:
            List of fee intelligence reports
        """
        conn = self._get_connection()
        cutoff = int(time.time()) - (max_age_hours * 3600)
        rows = conn.execute("""
            SELECT * FROM fee_intelligence
            WHERE target_peer_id = ? AND timestamp >= ?
            ORDER BY timestamp DESC
        """, (target_peer_id, cutoff)).fetchall()
        return [dict(row) for row in rows]

    def get_all_fee_intelligence(
        self,
        max_age_hours: int = 24
    ) -> List[Dict[str, Any]]:
        """
        Get all recent fee intelligence reports.

        Args:
            max_age_hours: Maximum age of reports in hours

        Returns:
            List of fee intelligence reports
        """
        conn = self._get_connection()
        cutoff = int(time.time()) - (max_age_hours * 3600)
        rows = conn.execute("""
            SELECT * FROM fee_intelligence
            WHERE timestamp >= ?
            ORDER BY target_peer_id, timestamp DESC
            LIMIT 10000
        """, (cutoff,)).fetchall()
        return [dict(row) for row in rows]

    def cleanup_old_fee_intelligence(self, max_age_hours: int = 168) -> int:
        """
        Remove old fee intelligence records.

        Args:
            max_age_hours: Maximum age to keep (default 7 days)

        Returns:
            Number of records deleted
        """
        conn = self._get_connection()
        cutoff = int(time.time()) - (max_age_hours * 3600)
        cursor = conn.execute("""
            DELETE FROM fee_intelligence WHERE timestamp < ?
        """, (cutoff,))
        return cursor.rowcount

    # =========================================================================
    # PEER FEE PROFILES OPERATIONS (Phase 7)
    # =========================================================================

    def update_peer_fee_profile(
        self,
        peer_id: str,
        reporter_count: int,
        avg_fee_charged: float,
        min_fee_charged: int,
        max_fee_charged: int,
        total_hive_volume: int,
        total_hive_revenue: int,
        avg_utilization: float,
        estimated_elasticity: float = 0.0,
        optimal_fee_estimate: int = 0,
        confidence: float = 0.5
    ) -> None:
        """
        Update or insert aggregated fee profile for an external peer.

        Args:
            peer_id: External peer ID
            reporter_count: Number of hive members reporting on this peer
            avg_fee_charged: Average fee charged by hive to this peer
            min_fee_charged: Minimum fee charged
            max_fee_charged: Maximum fee charged
            total_hive_volume: Total volume hive routes through this peer
            total_hive_revenue: Total revenue from this peer
            avg_utilization: Average channel utilization
            estimated_elasticity: Estimated price elasticity (-1 to 1)
            optimal_fee_estimate: Recommended optimal fee
            confidence: Confidence score (0-1)
        """
        conn = self._get_connection()
        now = int(time.time())
        conn.execute("""
            INSERT INTO peer_fee_profiles (
                peer_id, reporter_count, avg_fee_charged, min_fee_charged,
                max_fee_charged, total_hive_volume, total_hive_revenue,
                avg_utilization, estimated_elasticity, optimal_fee_estimate,
                last_update, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(peer_id) DO UPDATE SET
                reporter_count = excluded.reporter_count,
                avg_fee_charged = excluded.avg_fee_charged,
                min_fee_charged = excluded.min_fee_charged,
                max_fee_charged = excluded.max_fee_charged,
                total_hive_volume = excluded.total_hive_volume,
                total_hive_revenue = excluded.total_hive_revenue,
                avg_utilization = excluded.avg_utilization,
                estimated_elasticity = excluded.estimated_elasticity,
                optimal_fee_estimate = excluded.optimal_fee_estimate,
                last_update = excluded.last_update,
                confidence = excluded.confidence
        """, (
            peer_id, reporter_count, avg_fee_charged, min_fee_charged,
            max_fee_charged, total_hive_volume, total_hive_revenue,
            avg_utilization, estimated_elasticity, optimal_fee_estimate,
            now, confidence
        ))

    def get_peer_fee_profile(self, peer_id: str) -> Optional[Dict[str, Any]]:
        """
        Get aggregated fee profile for an external peer.

        Args:
            peer_id: External peer ID

        Returns:
            Fee profile dict or None if not found
        """
        conn = self._get_connection()
        row = conn.execute("""
            SELECT * FROM peer_fee_profiles WHERE peer_id = ?
        """, (peer_id,)).fetchone()
        return dict(row) if row else None

    def get_all_peer_fee_profiles(self, limit: int = 500) -> List[Dict[str, Any]]:
        """
        Get all aggregated fee profiles.

        Args:
            limit: Maximum number of profiles to return (default 500)

        Returns:
            List of fee profile dicts
        """
        conn = self._get_connection()
        rows = conn.execute("""
            SELECT * FROM peer_fee_profiles ORDER BY reporter_count DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(row) for row in rows]

    # =========================================================================
    # MEMBER HEALTH OPERATIONS (Phase 7 - NNLB)
    # =========================================================================

    def update_member_health(
        self,
        peer_id: str,
        overall_health: int,
        capacity_score: int,
        revenue_score: int,
        connectivity_score: int,
        tier: str = 'stable',
        needs_help: bool = False,
        can_help_others: bool = False,
        needs_inbound: bool = False,
        needs_outbound: bool = False,
        needs_channels: bool = False,
        assistance_budget_sats: int = 0
    ) -> None:
        """
        Update health record for a hive member.

        Args:
            peer_id: Hive member peer ID
            overall_health: Overall health score (0-100)
            capacity_score: Capacity score (0-100)
            revenue_score: Revenue score (0-100)
            connectivity_score: Connectivity score (0-100)
            tier: 'struggling', 'vulnerable', 'stable', or 'thriving'
            needs_help: Whether member needs assistance
            can_help_others: Whether member can provide assistance
            needs_inbound: Whether member needs inbound liquidity
            needs_outbound: Whether member needs outbound liquidity
            needs_channels: Whether member needs more channels
            assistance_budget_sats: How much member can spend helping
        """
        conn = self._get_connection()
        now = int(time.time())
        conn.execute("""
            INSERT INTO member_health (
                peer_id, timestamp, overall_health, capacity_score,
                revenue_score, connectivity_score, tier, needs_help,
                can_help_others, needs_inbound, needs_outbound,
                needs_channels, assistance_budget_sats
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(peer_id) DO UPDATE SET
                timestamp = excluded.timestamp,
                overall_health = excluded.overall_health,
                capacity_score = excluded.capacity_score,
                revenue_score = excluded.revenue_score,
                connectivity_score = excluded.connectivity_score,
                tier = excluded.tier,
                needs_help = excluded.needs_help,
                can_help_others = excluded.can_help_others,
                needs_inbound = excluded.needs_inbound,
                needs_outbound = excluded.needs_outbound,
                needs_channels = excluded.needs_channels,
                assistance_budget_sats = excluded.assistance_budget_sats
        """, (
            peer_id, now, overall_health, capacity_score,
            revenue_score, connectivity_score, tier,
            1 if needs_help else 0,
            1 if can_help_others else 0,
            1 if needs_inbound else 0,
            1 if needs_outbound else 0,
            1 if needs_channels else 0,
            assistance_budget_sats
        ))

    def get_member_health(self, peer_id: str) -> Optional[Dict[str, Any]]:
        """
        Get health record for a hive member.

        Args:
            peer_id: Hive member peer ID

        Returns:
            Health record dict or None if not found
        """
        conn = self._get_connection()
        row = conn.execute("""
            SELECT * FROM member_health WHERE peer_id = ?
        """, (peer_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        # Convert integer flags to booleans
        result['needs_help'] = bool(result.get('needs_help', 0))
        result['can_help_others'] = bool(result.get('can_help_others', 0))
        result['needs_inbound'] = bool(result.get('needs_inbound', 0))
        result['needs_outbound'] = bool(result.get('needs_outbound', 0))
        result['needs_channels'] = bool(result.get('needs_channels', 0))
        return result

    def get_all_member_health(self) -> List[Dict[str, Any]]:
        """
        Get health records for all hive members.

        Returns:
            List of health record dicts
        """
        conn = self._get_connection()
        rows = conn.execute("""
            SELECT * FROM member_health ORDER BY overall_health ASC LIMIT 1000
        """).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            result['needs_help'] = bool(result.get('needs_help', 0))
            result['can_help_others'] = bool(result.get('can_help_others', 0))
            result['needs_inbound'] = bool(result.get('needs_inbound', 0))
            result['needs_outbound'] = bool(result.get('needs_outbound', 0))
            result['needs_channels'] = bool(result.get('needs_channels', 0))
            results.append(result)
        return results

    def get_struggling_members(self, threshold: int = 20) -> List[Dict[str, Any]]:
        """
        Get members with health below threshold (NNLB candidates).

        Args:
            threshold: Health score threshold (default 20, relaxed 2026-02-12)

        Returns:
            List of health records for struggling members
        """
        conn = self._get_connection()
        rows = conn.execute("""
            SELECT * FROM member_health
            WHERE overall_health < ? OR needs_help = 1
            ORDER BY overall_health ASC
        """, (threshold,)).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            result['needs_help'] = bool(result.get('needs_help', 0))
            result['can_help_others'] = bool(result.get('can_help_others', 0))
            result['needs_inbound'] = bool(result.get('needs_inbound', 0))
            result['needs_outbound'] = bool(result.get('needs_outbound', 0))
            result['needs_channels'] = bool(result.get('needs_channels', 0))
            results.append(result)
        return results

    def get_helping_members(self) -> List[Dict[str, Any]]:
        """
        Get members who can provide assistance to others.

        Returns:
            List of health records for members who can help
        """
        conn = self._get_connection()
        rows = conn.execute("""
            SELECT * FROM member_health
            WHERE can_help_others = 1
            ORDER BY overall_health DESC
        """).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            result['needs_help'] = bool(result.get('needs_help', 0))
            result['can_help_others'] = bool(result.get('can_help_others', 0))
            result['needs_inbound'] = bool(result.get('needs_inbound', 0))
            result['needs_outbound'] = bool(result.get('needs_outbound', 0))
            result['needs_channels'] = bool(result.get('needs_channels', 0))
            results.append(result)
        return results
    def store_peer_reputation(
        self,
        reporter_id: str,
        peer_id: str,
        timestamp: int,
        uptime_pct: float = 1.0,
        response_time_ms: int = 0,
        force_close_count: int = 0,
        fee_stability: float = 1.0,
        htlc_success_rate: float = 1.0,
        channel_age_days: int = 0,
        total_routed_sats: int = 0,
        warnings: list = None,
        observation_days: int = 7
    ):
        """
        Store a peer reputation report.

        Args:
            reporter_id: Hive member reporting
            peer_id: External peer being reported on
            timestamp: Report timestamp
            uptime_pct: Peer uptime (0-1)
            response_time_ms: Average HTLC response time
            force_close_count: Force closes by peer
            fee_stability: Fee stability (0-1)
            htlc_success_rate: HTLC success rate (0-1)
            channel_age_days: Channel age
            total_routed_sats: Total volume routed
            warnings: List of warning codes
            observation_days: Days covered by report
        """
        conn = self._get_connection()
        warnings_json = json.dumps(warnings or [])

        conn.execute("""
            INSERT INTO peer_reputation (
                reporter_id, peer_id, timestamp, uptime_pct, response_time_ms,
                force_close_count, fee_stability, htlc_success_rate,
                channel_age_days, total_routed_sats, warnings, observation_days
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            reporter_id, peer_id, timestamp, uptime_pct, response_time_ms,
            force_close_count, fee_stability, htlc_success_rate,
            channel_age_days, total_routed_sats, warnings_json, observation_days
        ))

    def get_peer_reputation_reports(
        self,
        peer_id: str,
        max_age_hours: int = 168
    ) -> list:
        """
        Get all reputation reports for a specific peer.

        Args:
            peer_id: External peer pubkey
            max_age_hours: Maximum age of reports to include

        Returns:
            List of reputation report dicts
        """
        conn = self._get_connection()
        cutoff = int(time.time()) - (max_age_hours * 3600)

        rows = conn.execute("""
            SELECT * FROM peer_reputation
            WHERE peer_id = ? AND timestamp > ?
            ORDER BY timestamp DESC
        """, (peer_id, cutoff)).fetchall()

        reports = []
        for row in rows:
            report = dict(row)
            # Parse warnings JSON
            report["warnings"] = json.loads(report.get("warnings", "[]"))
            reports.append(report)

        return reports

    def get_all_peer_reputation_reports(
        self,
        max_age_hours: int = 168
    ) -> list:
        """
        Get all reputation reports.

        Args:
            max_age_hours: Maximum age of reports to include

        Returns:
            List of all reputation report dicts
        """
        conn = self._get_connection()
        cutoff = int(time.time()) - (max_age_hours * 3600)

        rows = conn.execute("""
            SELECT * FROM peer_reputation
            WHERE timestamp > ?
            ORDER BY timestamp DESC
            LIMIT 10000
        """, (cutoff,)).fetchall()

        reports = []
        for row in rows:
            report = dict(row)
            report["warnings"] = json.loads(report.get("warnings", "[]"))
            reports.append(report)

        return reports

    def cleanup_old_peer_reputation(self, max_age_hours: int = 168) -> int:
        """
        Remove old peer reputation records.

        Args:
            max_age_hours: Maximum age to keep (default 7 days)

        Returns:
            Number of records deleted
        """
        conn = self._get_connection()
        cutoff = int(time.time()) - (max_age_hours * 3600)
        cursor = conn.execute("""
            DELETE FROM peer_reputation WHERE timestamp < ?
        """, (cutoff,))
        return cursor.rowcount
    def record_flow_sample(
        self,
        channel_id: str,
        hour: int,
        day_of_week: int,
        inbound_sats: int,
        outbound_sats: int,
        net_flow_sats: int,
        timestamp: int
    ) -> bool:
        """
        Record a flow sample for pattern analysis.

        Args:
            channel_id: Channel SCID
            hour: Hour of day (0-23)
            day_of_week: Day of week (0=Monday, 6=Sunday)
            inbound_sats: Satoshis received
            outbound_sats: Satoshis sent
            net_flow_sats: Net flow (inbound - outbound)
            timestamp: Unix timestamp

        Returns:
            True if recorded successfully
        """
        conn = self._get_connection()
        try:
            conn.execute("""
                INSERT INTO flow_samples
                (channel_id, hour, day_of_week, inbound_sats, outbound_sats,
                 net_flow_sats, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (channel_id, hour, day_of_week, inbound_sats, outbound_sats,
                  net_flow_sats, timestamp))
            return True
        except Exception as e:
            self.plugin.log(
                f"Failed to record flow sample: {e}",
                level="debug"
            )
            return False

    def get_flow_samples(
        self,
        channel_id: str,
        days: int = 14
    ) -> List[Dict[str, Any]]:
        """
        Get flow samples for a channel.

        Args:
            channel_id: Channel SCID
            days: Number of days of history to retrieve

        Returns:
            List of flow sample dicts
        """
        conn = self._get_connection()
        cutoff = int(time.time()) - (days * 24 * 3600)

        rows = conn.execute("""
            SELECT * FROM flow_samples
            WHERE channel_id = ? AND timestamp > ?
            ORDER BY timestamp DESC
            LIMIT 10000
        """, (channel_id, cutoff)).fetchall()

        return [dict(row) for row in rows]

    def prune_old_flow_samples(self, days_to_keep: int = 30) -> int:
        """
        Remove old flow samples to limit database growth.

        Args:
            days_to_keep: Days of samples to retain

        Returns:
            Number of rows deleted
        """
        conn = self._get_connection()
        cutoff = int(time.time()) - (days_to_keep * 24 * 3600)

        result = conn.execute("""
            DELETE FROM flow_samples
            WHERE timestamp < ?
        """, (cutoff,))

        deleted = result.rowcount
        if deleted > 0:
            self.plugin.log(
                f"Pruned {deleted} old flow samples",
                level="debug"
            )
        return deleted

    # =========================================================================
    # LOCAL FEE TRACKING OPERATIONS
    # =========================================================================

    def save_local_fee_tracking(self, earned_sats: int, forward_count: int,
                                 period_start_ts: int, last_broadcast_ts: int,
                                 last_broadcast_amount: int) -> bool:
        """
        Persist local fee tracking state to survive restarts.

        Args:
            earned_sats: Total fees earned in current period
            forward_count: Number of forwards in current period
            period_start_ts: Period start timestamp
            last_broadcast_ts: Timestamp of last fee broadcast
            last_broadcast_amount: Fees at last broadcast

        Returns:
            True if saved successfully
        """
        conn = self._get_connection()
        now = int(time.time())

        try:
            conn.execute("""
                INSERT OR REPLACE INTO local_fee_tracking
                (id, earned_sats, forward_count, period_start_ts,
                 last_broadcast_ts, last_broadcast_amount, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?)
            """, (earned_sats, forward_count, period_start_ts,
                  last_broadcast_ts, last_broadcast_amount, now))
            return True
        except Exception:
            return False

    def load_local_fee_tracking(self) -> Optional[Dict[str, int]]:
        """
        Load persisted fee tracking state.

        Returns:
            Dict with earned_sats, forward_count, period_start_ts,
            last_broadcast_ts, last_broadcast_amount, or None if not found
        """
        conn = self._get_connection()

        row = conn.execute("""
            SELECT earned_sats, forward_count, period_start_ts,
                   last_broadcast_ts, last_broadcast_amount
            FROM local_fee_tracking WHERE id = 1
        """).fetchone()

        if not row:
            return None

        return {
            "earned_sats": row["earned_sats"],
            "forward_count": row["forward_count"],
            "period_start_ts": row["period_start_ts"],
            "last_broadcast_ts": row["last_broadcast_ts"],
            "last_broadcast_amount": row["last_broadcast_amount"]
        }

    # =========================================================================
    # CONTRIBUTION RATE LIMIT OPERATIONS
    # =========================================================================

    def save_contribution_rate_limit(self, peer_id: str, window_start: int,
                                      event_count: int) -> bool:
        """
        Persist per-peer contribution rate limit state.

        Args:
            peer_id: Peer's public key
            window_start: Window start timestamp
            event_count: Events in current window

        Returns:
            True if saved successfully
        """
        conn = self._get_connection()

        try:
            conn.execute("""
                INSERT OR REPLACE INTO contribution_rate_limits
                (peer_id, window_start, event_count)
                VALUES (?, ?, ?)
            """, (peer_id, window_start, event_count))
            return True
        except Exception:
            return False

    def load_contribution_rate_limits(self) -> Dict[str, Tuple[int, int]]:
        """
        Load all persisted contribution rate limits.

        Returns:
            Dict mapping peer_id to (window_start, event_count)
        """
        conn = self._get_connection()

        rows = conn.execute("""
            SELECT peer_id, window_start, event_count
            FROM contribution_rate_limits
        """).fetchall()

        return {
            row["peer_id"]: (row["window_start"], row["event_count"])
            for row in rows
        }

    def save_contribution_daily_stats(self, window_start_ts: int,
                                       event_count: int) -> bool:
        """
        Persist global daily contribution stats.

        Args:
            window_start_ts: Daily window start timestamp
            event_count: Total events in current window

        Returns:
            True if saved successfully
        """
        conn = self._get_connection()

        try:
            conn.execute("""
                INSERT OR REPLACE INTO contribution_daily_stats
                (id, window_start_ts, event_count)
                VALUES (1, ?, ?)
            """, (window_start_ts, event_count))
            return True
        except Exception:
            return False

    def load_contribution_daily_stats(self) -> Optional[Dict[str, int]]:
        """
        Load persisted global daily contribution stats.

        Returns:
            Dict with window_start_ts and event_count, or None if not found
        """
        conn = self._get_connection()

        row = conn.execute("""
            SELECT window_start_ts, event_count
            FROM contribution_daily_stats WHERE id = 1
        """).fetchone()

        if not row:
            return None

        return {
            "window_start_ts": row["window_start_ts"],
            "event_count": row["event_count"]
        }

    # =========================================================================
    # SPLICE SESSION OPERATIONS (Phase 11)
    # =========================================================================

    def save_peer_capabilities(self, peer_id: str, features: list) -> bool:
        """
        Save or update a peer's advertised capabilities.

        Parses 'proto-vN' from the features list to populate max_protocol_version.

        Args:
            peer_id: Peer's public key
            features: List of feature strings from ATTEST manifest

        Returns:
            True if saved successfully
        """
        if not isinstance(features, list):
            return False

        max_proto = 1
        plugin_version = ''
        for f in features:
            if isinstance(f, str) and f.startswith('proto-v'):
                try:
                    v = int(f[7:])
                    max_proto = max(max_proto, v)
                except ValueError:
                    pass
            if isinstance(f, str) and f.startswith('cl-hive'):
                plugin_version = f

        conn = self._get_connection()
        try:
            conn.execute(
                """INSERT INTO peer_capabilities
                   (peer_id, features, max_protocol_version, plugin_version, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(peer_id) DO UPDATE SET
                       features = excluded.features,
                       max_protocol_version = excluded.max_protocol_version,
                       plugin_version = excluded.plugin_version,
                       updated_at = excluded.updated_at""",
                (peer_id, json.dumps(features), max_proto, plugin_version, int(time.time()))
            )
            return True
        except Exception as e:
            self.plugin.log(f"HiveDatabase: save_peer_capabilities error: {e}", level='warn')
            return False

    def get_peer_capabilities(self, peer_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a peer's capabilities record.

        Returns:
            Dict with features, max_protocol_version, plugin_version, updated_at
            or None if not found.
        """
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM peer_capabilities WHERE peer_id = ?",
            (peer_id,)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result['features'] = json.loads(result.get('features', '[]'))
        except (json.JSONDecodeError, TypeError):
            result['features'] = []
        return result

    def get_peer_max_protocol_version(self, peer_id: str) -> int:
        """
        Get the max protocol version a peer supports.

        Returns:
            Integer version (defaults to 1 if unknown).
        """
        conn = self._get_connection()
        row = conn.execute(
            "SELECT max_protocol_version FROM peer_capabilities WHERE peer_id = ?",
            (peer_id,)
        ).fetchone()
        return row['max_protocol_version'] if row else 1

    # =========================================================================
    # PROTO EVENTS (Phase C - Deterministic Idempotency)
    # =========================================================================

    def record_proto_event(self, event_id: str, event_type: str, actor_id: str) -> bool:
        """
        Record a protocol event for idempotency.

        Uses INSERT OR IGNORE so duplicate event_ids are silently skipped.
        Rejects inserts if proto_events exceeds MAX_PROTO_EVENT_ROWS.

        Args:
            event_id: SHA256-based unique event identifier
            event_type: Message type name (e.g. 'MEMBER_LEFT')
            actor_id: Peer that originated the event

        Returns:
            True if this is a new event (inserted), False if duplicate or at cap.
        """
        conn = self._get_connection()
        now = int(time.time())
        try:
            # Atomic check-and-insert to prevent TOCTOU race on row cap
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT COUNT(*) AS cnt FROM proto_events").fetchone()
            if row and row['cnt'] >= self.MAX_PROTO_EVENT_ROWS:
                conn.execute("ROLLBACK")
                self.plugin.log(
                    f"HiveDatabase: proto_events at cap ({self.MAX_PROTO_EVENT_ROWS}), rejecting insert",
                    level='warn'
                )
                return False
            result = conn.execute(
                """INSERT OR IGNORE INTO proto_events
                   (event_id, event_type, actor_id, created_at, received_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (event_id, event_type, actor_id, now, now)
            )
            conn.execute("COMMIT")
            return result.rowcount > 0
        except Exception as e:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            self.plugin.log(f"HiveDatabase: record_proto_event error: {e}", level='warn')
            return False

    def has_proto_event(self, event_id: str) -> bool:
        """Check if a protocol event has already been recorded."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT 1 FROM proto_events WHERE event_id = ?",
            (event_id,)
        ).fetchone()
        return row is not None

    def cleanup_proto_events(self, max_age_seconds: int = 30 * 86400) -> int:
        """
        Remove proto_events older than max_age_seconds.

        Args:
            max_age_seconds: Maximum age in seconds (default 30 days)

        Returns:
            Number of rows pruned.
        """
        conn = self._get_connection()
        cutoff = int(time.time()) - max_age_seconds
        result = conn.execute(
            "DELETE FROM proto_events WHERE created_at < ?",
            (cutoff,)
        )
        return result.rowcount

    # =========================================================================
    # PROTO OUTBOX OPERATIONS (Phase D - Reliable Delivery)
    # =========================================================================

    def enqueue_outbox(self, msg_id: str, peer_id: str, msg_type: int,
                       payload_json: str, expires_at: int) -> bool:
        """
        Enqueue a message for reliable delivery to a specific peer.

        Uses INSERT OR IGNORE for idempotent enqueue (same msg_id+peer_id
        is silently ignored). Rejects inserts if proto_outbox exceeds
        MAX_PROTO_OUTBOX_ROWS.

        Args:
            msg_id: Unique message identifier
            peer_id: Target peer pubkey
            msg_type: HiveMessageType integer value
            payload_json: JSON-serialized payload
            expires_at: Unix timestamp when message expires

        Returns:
            True if inserted, False if duplicate, at cap, or error.
        """
        conn = self._get_connection()
        now = int(time.time())
        try:
            # Atomic check-and-insert to prevent TOCTOU race on row cap
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT COUNT(*) AS cnt FROM proto_outbox").fetchone()
            if row and row['cnt'] >= self.MAX_PROTO_OUTBOX_ROWS:
                conn.execute("ROLLBACK")
                self.plugin.log(
                    f"HiveDatabase: proto_outbox at cap ({self.MAX_PROTO_OUTBOX_ROWS}), rejecting enqueue",
                    level='warn'
                )
                return False
            result = conn.execute(
                """INSERT OR IGNORE INTO proto_outbox
                   (msg_id, peer_id, msg_type, payload_json, status,
                    created_at, next_retry_at, expires_at)
                   VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)""",
                (msg_id, peer_id, msg_type, payload_json, now, now, expires_at)
            )
            conn.execute("COMMIT")
            return result.rowcount > 0
        except Exception as e:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            self.plugin.log(f"enqueue_outbox error: {e}", level='warn')
            return False

    def get_outbox_pending(self, limit: int = 50) -> list:
        """
        Get outbox entries ready for sending or retry.

        Returns entries where:
        - status is 'queued' or 'sent' (pending ack)
        - next_retry_at <= now (ready to retry)
        - expires_at > now (not expired)

        Args:
            limit: Maximum entries to return (default 50)

        Returns:
            List of dicts with outbox entry fields.
        """
        conn = self._get_connection()
        now = int(time.time())
        rows = conn.execute(
            """SELECT id, msg_id, peer_id, msg_type, payload_json, status,
                      created_at, sent_at, next_retry_at, retry_count,
                      expires_at, last_error
               FROM proto_outbox
               WHERE status IN ('queued', 'sent')
                 AND next_retry_at <= ?
                 AND expires_at > ?
               ORDER BY next_retry_at ASC
               LIMIT ?""",
            (now, now, limit)
        ).fetchall()
        return [dict(row) for row in rows]

    def update_outbox_sent(self, msg_id: str, peer_id: str,
                           next_retry_at: int) -> bool:
        """
        Mark an outbox entry as sent and schedule next retry.

        Args:
            msg_id: Message identifier
            peer_id: Target peer pubkey
            next_retry_at: Unix timestamp for next retry attempt

        Returns:
            True if updated, False otherwise.
        """
        conn = self._get_connection()
        now = int(time.time())
        result = conn.execute(
            """UPDATE proto_outbox
               SET status = 'sent', sent_at = ?, retry_count = retry_count + 1,
                   next_retry_at = ?
               WHERE msg_id = ? AND peer_id = ?
                 AND status IN ('queued', 'sent')""",
            (now, next_retry_at, msg_id, peer_id)
        )
        return result.rowcount > 0

    def update_outbox_retry(self, msg_id: str, peer_id: str,
                            next_retry_at: int) -> bool:
        """
        Schedule next retry for a failed send WITHOUT incrementing retry_count.

        Used when send_fn fails (peer unreachable) — the message was never
        transmitted, so retry budget should not be consumed.

        Args:
            msg_id: Message identifier
            peer_id: Target peer pubkey
            next_retry_at: Unix timestamp for next retry attempt

        Returns:
            True if updated, False otherwise.
        """
        conn = self._get_connection()
        result = conn.execute(
            """UPDATE proto_outbox
               SET next_retry_at = ?
               WHERE msg_id = ? AND peer_id = ?
                 AND status IN ('queued', 'sent')""",
            (next_retry_at, msg_id, peer_id)
        )
        return result.rowcount > 0

    def ack_outbox(self, msg_id: str, peer_id: str) -> bool:
        """
        Mark an outbox entry as acknowledged.

        Args:
            msg_id: Message identifier (the _event_id)
            peer_id: Peer that acknowledged

        Returns:
            True if updated, False otherwise.
        """
        conn = self._get_connection()
        now = int(time.time())
        result = conn.execute(
            """UPDATE proto_outbox
               SET status = 'acked', acked_at = ?
               WHERE msg_id = ? AND peer_id = ?
                 AND status IN ('queued', 'sent')""",
            (now, msg_id, peer_id)
        )
        return result.rowcount > 0

    def ack_outbox_by_type(self, peer_id: str, msg_type: int,
                           match_field: str, match_value: str) -> int:
        """
        Acknowledge outbox entries by type and payload field match.

        Used for implicit acks: a domain response clears the
        corresponding request outbox entries for that peer.

        Args:
            peer_id: Peer that implicitly acknowledged
            msg_type: The original message type integer to match
            match_field: JSON field name to match in payload
            match_value: Expected value of the field

        Returns:
            Number of entries acknowledged.
        """
        conn = self._get_connection()
        now = int(time.time())
        # Use json_extract for matching payload fields
        # Fallback to LIKE for SQLite versions without json_extract
        try:
            result = conn.execute(
                """UPDATE proto_outbox
                   SET status = 'acked', acked_at = ?
                   WHERE peer_id = ? AND msg_type = ?
                     AND status IN ('queued', 'sent')
                     AND json_extract(payload_json, ?) = ?""",
                (now, peer_id, msg_type, f'$.{match_field}', match_value)
            )
            return result.rowcount
        except Exception:
            # Fallback: match using LIKE pattern for older SQLite
            # Escape LIKE metacharacters in match_value to prevent over-matching
            safe_value = match_value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            pattern = f'"{match_field}":"{safe_value}"'
            try:
                result = conn.execute(
                    """UPDATE proto_outbox
                       SET status = 'acked', acked_at = ?
                       WHERE peer_id = ? AND msg_type = ?
                         AND status IN ('queued', 'sent')
                         AND payload_json LIKE ? ESCAPE '\\'""",
                    (now, peer_id, msg_type, f'%{pattern}%')
                )
                return result.rowcount
            except Exception as e:
                self.plugin.log(f"ack_outbox_by_type error: {e}", level='warn')
                return 0

    def fail_outbox(self, msg_id: str, peer_id: str, error: str) -> bool:
        """
        Mark an outbox entry as permanently failed.

        Args:
            msg_id: Message identifier
            peer_id: Target peer pubkey
            error: Error description

        Returns:
            True if updated, False otherwise.
        """
        conn = self._get_connection()
        result = conn.execute(
            """UPDATE proto_outbox
               SET status = 'failed', last_error = ?
               WHERE msg_id = ? AND peer_id = ?
                 AND status IN ('queued', 'sent')""",
            (error[:500], msg_id, peer_id)
        )
        return result.rowcount > 0

    def expire_outbox(self) -> int:
        """
        Mark expired outbox entries.

        Returns:
            Number of entries expired.
        """
        conn = self._get_connection()
        now = int(time.time())
        result = conn.execute(
            """UPDATE proto_outbox
               SET status = 'expired'
               WHERE expires_at <= ? AND status IN ('queued', 'sent')""",
            (now,)
        )
        return result.rowcount

    def cleanup_outbox(self, max_age_seconds: int = 7 * 86400) -> int:
        """
        Delete terminal outbox entries (acked/failed/expired) older than threshold.

        Args:
            max_age_seconds: Maximum age in seconds (default 7 days)

        Returns:
            Number of entries cleaned up.
        """
        conn = self._get_connection()
        cutoff = int(time.time()) - max_age_seconds
        result = conn.execute(
            """DELETE FROM proto_outbox
               WHERE status IN ('acked', 'failed', 'expired')
                 AND created_at < ?""",
            (cutoff,)
        )
        return result.rowcount

    def count_inflight_for_peer(self, peer_id: str) -> int:
        """
        Count active (queued or sent) outbox entries for a peer.

        Used for backpressure: reject new enqueues when too many are inflight.

        Args:
            peer_id: Target peer pubkey

        Returns:
            Count of inflight entries.
        """
        conn = self._get_connection()
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM proto_outbox
               WHERE peer_id = ? AND status IN ('queued', 'sent')""",
            (peer_id,)
        ).fetchone()
        return row['cnt'] if row else 0

    # =========================================================================
    # TRAFFIC PROFILE OPERATIONS (Traffic Intelligence)
    # =========================================================================

    def save_traffic_profile(
        self,
        peer_id: str,
        reporter_id: str,
        profile_type: str = 'unknown',
        peak_hours_utc: str = '[]',
        quiet_hours_utc: str = '[]',
        avg_forward_size_sats: float = 0,
        daily_volume_sats: float = 0,
        drain_direction: str = 'balanced',
        confidence: float = 0.0,
        observation_window_hours: int = 24,
        received_at: float = 0,
    ) -> bool:
        """
        Save or update a traffic profile for a peer.

        Uses INSERT OR REPLACE keyed on (reporter_id, peer_id).

        Args:
            peer_id: External peer being profiled
            reporter_id: Hive member who reported this
            profile_type: retail | wholesale | burst | steady | mixed
            peak_hours_utc: JSON array of peak hours
            quiet_hours_utc: JSON array of quiet hours
            avg_forward_size_sats: Average forward size
            daily_volume_sats: Average daily volume
            drain_direction: inbound_heavy | outbound_heavy | balanced
            confidence: Profile confidence (0-1)
            observation_window_hours: How long peer was observed
            received_at: Unix timestamp when received

        Returns:
            True if stored successfully, False on error
        """
        conn = self._get_connection()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO traffic_profiles
                    (reporter_id, peer_id, profile_type, peak_hours_utc,
                     quiet_hours_utc, avg_forward_size_sats, daily_volume_sats,
                     drain_direction, confidence, observation_window_hours, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (reporter_id, peer_id, profile_type, peak_hours_utc,
                  quiet_hours_utc, avg_forward_size_sats, daily_volume_sats,
                  drain_direction, confidence, observation_window_hours,
                  received_at or time.time()))
            return True
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: save_traffic_profile error: {e}",
                    level='error'
                )
            return False

    def get_traffic_profiles_for_peer(self, peer_id: str) -> List[Dict[str, Any]]:
        """
        Get all traffic profiles for a specific peer.

        Args:
            peer_id: Peer to look up

        Returns:
            List of profile dicts
        """
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM traffic_profiles WHERE peer_id = ? ORDER BY received_at DESC",
                (peer_id,)
            ).fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: get_traffic_profiles_for_peer error: {e}",
                    level='error'
                )
            return []

    def get_all_traffic_profiles(self) -> List[Dict[str, Any]]:
        """
        Get all stored traffic profiles.

        Returns:
            List of profile dicts
        """
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM traffic_profiles ORDER BY received_at DESC LIMIT 5000"
            ).fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: get_all_traffic_profiles error: {e}",
                    level='error'
                )
            return []

    def cleanup_expired_traffic_profiles(self, max_age_hours: int = 168) -> int:
        """
        Remove traffic profiles older than max_age_hours.

        Args:
            max_age_hours: Max age in hours (default: 168 = 7 days)

        Returns:
            Number of profiles deleted
        """
        conn = self._get_connection()
        try:
            cutoff = time.time() - (max_age_hours * 3600)
            result = conn.execute(
                "DELETE FROM traffic_profiles WHERE received_at < ?",
                (cutoff,)
            )
            return result.rowcount
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: cleanup_expired_traffic_profiles error: {e}",
                    level='error'
                )
            return 0

    # =========================================================================
    # LIQUIDITY NEEDS OPERATIONS (Liquidity Coordination)
    # =========================================================================

    def store_liquidity_need(
        self,
        reporter_id: str,
        need_type: str,
        target_peer_id: str,
        amount_sats: int = 0,
        urgency: str = 'low',
        max_fee_ppm: int = 0,
        reason: str = '',
        current_balance_pct: float = 0.5,
        timestamp: int = 0,
    ) -> bool:
        """
        Store a liquidity need report.

        Uses INSERT OR REPLACE keyed on (reporter_id, target_peer_id, need_type).

        Args:
            reporter_id: Hive member reporting the need
            need_type: Type of need (e.g. 'inbound', 'outbound')
            target_peer_id: Peer the need relates to
            amount_sats: Amount needed
            urgency: low | medium | high | critical
            max_fee_ppm: Maximum fee willing to pay
            reason: Human-readable reason
            current_balance_pct: Current balance percentage
            timestamp: Unix timestamp

        Returns:
            True if stored, False on error
        """
        conn = self._get_connection()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO liquidity_needs
                    (reporter_id, need_type, target_peer_id, amount_sats,
                     urgency, max_fee_ppm, reason, current_balance_pct, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (reporter_id, need_type, target_peer_id, amount_sats,
                  urgency, max_fee_ppm, reason, current_balance_pct,
                  timestamp or int(time.time())))
            return True
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: store_liquidity_need error: {e}",
                    level='error'
                )
            return False

    def get_all_liquidity_needs(self, max_age_hours: int = 24) -> List[Dict[str, Any]]:
        """
        Get all liquidity needs within the age window.

        Args:
            max_age_hours: Only return needs newer than this (default: 24h)

        Returns:
            List of need dicts
        """
        conn = self._get_connection()
        try:
            cutoff = int(time.time()) - (max_age_hours * 3600)
            rows = conn.execute(
                "SELECT * FROM liquidity_needs WHERE timestamp > ? ORDER BY timestamp DESC",
                (cutoff,)
            ).fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: get_all_liquidity_needs error: {e}",
                    level='error'
                )
            return []

    def get_liquidity_needs_for_reporter(self, reporter_id: str) -> List[Dict[str, Any]]:
        """
        Get liquidity needs from a specific reporter.

        Args:
            reporter_id: Reporter peer ID to filter by

        Returns:
            List of need dicts
        """
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM liquidity_needs WHERE reporter_id = ? ORDER BY timestamp DESC",
                (reporter_id,)
            ).fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: get_liquidity_needs_for_reporter error: {e}",
                    level='error'
                )
            return []

    def cleanup_old_liquidity_needs(self, max_age_hours: int = 24) -> int:
        """
        Remove liquidity needs older than max_age_hours.

        Args:
            max_age_hours: Max age in hours (default: 24)

        Returns:
            Number of needs deleted
        """
        conn = self._get_connection()
        try:
            cutoff = int(time.time()) - (max_age_hours * 3600)
            result = conn.execute(
                "DELETE FROM liquidity_needs WHERE timestamp < ?",
                (cutoff,)
            )
            return result.rowcount
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: cleanup_old_liquidity_needs error: {e}",
                    level='error'
                )
            return 0

    def update_member_liquidity_state(
        self,
        member_id: str,
        depleted_count: int = 0,
        saturated_count: int = 0,
        rebalancing_active: bool = False,
        rebalancing_peers: list = None,
        timestamp: int = 0,
    ) -> bool:
        """
        Store or update a member's liquidity state.

        Full overwrite of the member's liquidity state row.

        Args:
            member_id: Member peer ID
            depleted_count: Number of depleted channels
            saturated_count: Number of saturated channels
            rebalancing_active: Whether member is currently rebalancing
            rebalancing_peers: List of peers being rebalanced through
            timestamp: Unix timestamp

        Returns:
            True if stored, False on error
        """
        conn = self._get_connection()
        try:
            peers_json = json.dumps(rebalancing_peers or [])
            conn.execute("""
                INSERT OR REPLACE INTO member_liquidity_state
                    (member_id, depleted_count, saturated_count,
                     rebalancing_active, rebalancing_peers, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (member_id, depleted_count, saturated_count,
                  1 if rebalancing_active else 0, peers_json,
                  timestamp or int(time.time())))
            return True
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: update_member_liquidity_state error: {e}",
                    level='error'
                )
            return False

    def update_rebalancing_activity(
        self,
        member_id: str,
        rebalancing_active: bool = False,
        rebalancing_peers: list = None,
    ) -> bool:
        """
        Targeted update of rebalancing fields only, preserving depleted/saturated counts.

        If no row exists yet, creates one with zero counts.

        Args:
            member_id: Member peer ID
            rebalancing_active: Whether member is currently rebalancing
            rebalancing_peers: List of peers being rebalanced through

        Returns:
            True if updated, False on error
        """
        conn = self._get_connection()
        try:
            peers_json = json.dumps(rebalancing_peers or [])
            now = int(time.time())

            # Try update first (preserves depleted/saturated counts)
            result = conn.execute("""
                UPDATE member_liquidity_state
                SET rebalancing_active = ?, rebalancing_peers = ?, timestamp = ?
                WHERE member_id = ?
            """, (1 if rebalancing_active else 0, peers_json, now, member_id))

            if result.rowcount == 0:
                # No existing row — create with zero counts
                conn.execute("""
                    INSERT INTO member_liquidity_state
                        (member_id, depleted_count, saturated_count,
                         rebalancing_active, rebalancing_peers, timestamp)
                    VALUES (?, 0, 0, ?, ?, ?)
                """, (member_id, 1 if rebalancing_active else 0, peers_json, now))

            return True
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: update_rebalancing_activity error: {e}",
                    level='error'
                )
            return False

    # =========================================================================
    # LEECH FLAG OPERATIONS (Contribution / Anti-Leech)
    # =========================================================================

    def get_leech_flag(self, peer_id: str) -> Optional[Dict[str, Any]]:
        """
        Get leech flag for a peer.

        Args:
            peer_id: Peer to look up

        Returns:
            Dict with low_since_ts and ban_triggered, or None if no flag
        """
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM leech_flags WHERE peer_id = ?",
                (peer_id,)
            ).fetchone()
            return dict(row) if row else None
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: get_leech_flag error: {e}",
                    level='error'
                )
            return None

    def set_leech_flag(self, peer_id: str, low_since_ts: int,
                       ban_triggered: bool) -> None:
        """
        Set or update leech flag for a peer.

        Args:
            peer_id: Peer to flag
            low_since_ts: Unix timestamp when low contribution was first detected
            ban_triggered: Whether a ban has been triggered
        """
        conn = self._get_connection()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO leech_flags (peer_id, low_since_ts, ban_triggered)
                VALUES (?, ?, ?)
            """, (peer_id, low_since_ts, 1 if ban_triggered else 0))
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: set_leech_flag error: {e}",
                    level='error'
                )

    def clear_leech_flag(self, peer_id: str) -> None:
        """
        Remove leech flag for a peer.

        Args:
            peer_id: Peer to clear flag for
        """
        conn = self._get_connection()
        try:
            conn.execute(
                "DELETE FROM leech_flags WHERE peer_id = ?",
                (peer_id,)
            )
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: clear_leech_flag error: {e}",
                    level='error'
                )

    # =========================================================================
    # PEER EVENT OPERATIONS (Quality Scorer / Peer History)
    # =========================================================================

    def get_peer_event_summary(self, peer_id: str, days: int = 90) -> Dict[str, Any]:
        """
        Get aggregated event summary for a peer.

        Args:
            peer_id: Peer to summarize
            days: Look-back window in days (default: 90)

        Returns:
            Dict with event counts by type
        """
        conn = self._get_connection()
        try:
            cutoff = int(time.time()) - (days * 86400)
            rows = conn.execute(
                "SELECT event_type, COUNT(*) as cnt FROM peer_events "
                "WHERE peer_id = ? AND timestamp > ? GROUP BY event_type",
                (peer_id, cutoff)
            ).fetchall()

            summary = {
                "total_events": 0,
                "channel_opens": 0,
                "channel_closes": 0,
                "remote_closes": 0,
                "local_closes": 0,
                "forwards": 0,
                "remote_open_count": 0,
                "days_covered": days,
            }

            type_mapping = {
                "channel_open": "channel_opens",
                "channel_close": "channel_closes",
                "remote_close": "remote_closes",
                "local_close": "local_closes",
                "forward": "forwards",
                "remote_open": "remote_open_count",
            }

            for row in rows:
                count = row['cnt']
                summary["total_events"] += count
                mapped_key = type_mapping.get(row['event_type'])
                if mapped_key:
                    summary[mapped_key] = count

            return summary
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: get_peer_event_summary error: {e}",
                    level='error'
                )
            return {
                "total_events": 0, "channel_opens": 0, "channel_closes": 0,
                "remote_closes": 0, "local_closes": 0, "forwards": 0,
                "remote_open_count": 0, "days_covered": days,
            }

    def get_peer_events(
        self,
        peer_id: str = None,
        event_type: str = None,
        reporter_id: str = None,
        days: int = 90,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Get peer events with optional filters.

        Args:
            peer_id: Filter by peer (optional)
            event_type: Filter by event type (optional)
            reporter_id: Filter by reporter (optional)
            days: Look-back window in days (default: 90)
            limit: Maximum results (default: 100)

        Returns:
            List of event dicts
        """
        conn = self._get_connection()
        try:
            cutoff = int(time.time()) - (days * 86400)
            conditions = ["timestamp > ?"]
            params: list = [cutoff]

            if peer_id:
                conditions.append("peer_id = ?")
                params.append(peer_id)
            if event_type:
                conditions.append("event_type = ?")
                params.append(event_type)
            if reporter_id:
                conditions.append("reporter_id = ?")
                params.append(reporter_id)

            where = " AND ".join(conditions)
            params.append(limit)

            rows = conn.execute(
                f"SELECT * FROM peer_events WHERE {where} "
                f"ORDER BY timestamp DESC LIMIT ?",
                params
            ).fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: get_peer_events error: {e}",
                    level='error'
                )
            return []

    def get_peers_with_events(self, days: int = 90) -> List[str]:
        """
        Get list of unique peer IDs that have events within the time window.

        Args:
            days: Look-back window in days (default: 90)

        Returns:
            List of peer_id strings
        """
        conn = self._get_connection()
        try:
            cutoff = int(time.time()) - (days * 86400)
            rows = conn.execute(
                "SELECT DISTINCT peer_id FROM peer_events WHERE timestamp > ? "
                "ORDER BY peer_id LIMIT 5000",
                (cutoff,)
            ).fetchall()
            return [row['peer_id'] for row in rows]
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: get_peers_with_events error: {e}",
                    level='error'
                )
            return []

    # =========================================================================
    # FEE REPORT OPERATIONS (Fee Coordination)
    # =========================================================================

    def get_fee_reports_for_period(self, period: str) -> List[Dict[str, Any]]:
        """
        Get fee intelligence reports matching an ISO week period string.

        Queries the existing fee_intelligence table and maps columns to the
        report format expected by callers.

        Args:
            period: ISO week string like "2026-W12"

        Returns:
            List of report dicts with keys: peer_id, fees_earned_sats,
            forward_count, period, received_at
        """
        conn = self._get_connection()
        try:
            # Parse ISO week period (e.g. "2026-W12") into timestamp range
            import datetime
            year_str, week_str = period.split('-W')
            year = int(year_str)
            week = int(week_str)
            # Monday of the given ISO week
            week_start = datetime.datetime.strptime(
                f"{year}-W{week:02d}-1", "%G-W%V-%u"
            )
            start_ts = int(week_start.timestamp())
            end_ts = start_ts + (7 * 86400)

            rows = conn.execute(
                "SELECT target_peer_id, revenue_sats, forward_count, timestamp "
                "FROM fee_intelligence "
                "WHERE timestamp >= ? AND timestamp < ? "
                "ORDER BY timestamp DESC",
                (start_ts, end_ts)
            ).fetchall()

            return [
                {
                    "peer_id": row['target_peer_id'],
                    "fees_earned_sats": row['revenue_sats'] or 0,
                    "forward_count": row['forward_count'] or 0,
                    "period": period,
                    "received_at": row['timestamp'],
                }
                for row in rows
            ]
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: get_fee_reports_for_period error: {e}",
                    level='error'
                )
            return []

    def get_latest_fee_reports(self, limit: int = 500) -> List[Dict[str, Any]]:
        """
        Get the most recent fee intelligence report for each peer.

        Queries the existing fee_intelligence table and returns the latest
        report per target_peer_id.

        Args:
            limit: Maximum number of peers to return (default: 500)

        Returns:
            List of report dicts with keys: peer_id, fees_earned_sats,
            forward_count, period, received_at
        """
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT target_peer_id, revenue_sats, forward_count, timestamp "
                "FROM fee_intelligence fi "
                "WHERE fi.timestamp = ("
                "  SELECT MAX(fi2.timestamp) FROM fee_intelligence fi2 "
                "  WHERE fi2.target_peer_id = fi.target_peer_id"
                ") "
                "ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            ).fetchall()

            return [
                {
                    "peer_id": row['target_peer_id'],
                    "fees_earned_sats": row['revenue_sats'] or 0,
                    "forward_count": row['forward_count'] or 0,
                    "period": "latest",
                    "received_at": row['timestamp'],
                }
                for row in rows
            ]
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: get_latest_fee_reports error: {e}",
                    level='error'
                )
            return []

    # =========================================================================
    # PLANNER BUDGET OPERATIONS
    # =========================================================================

    def get_available_budget(self, daily_budget_sats: int = 0) -> int:
        """
        Get available expansion budget remaining for today.

        Calculates remaining budget by subtracting today's spend (from planner
        logs) from the daily budget.

        Args:
            daily_budget_sats: Daily budget cap in sats

        Returns:
            Remaining budget in sats
        """
        conn = self._get_connection()
        try:
            today_start = int(time.time()) - (int(time.time()) % 86400)
            row = conn.execute(
                "SELECT COALESCE(SUM(CAST("
                "  json_extract(details, '$.amount_sats') AS INTEGER"
                ")), 0) as spent "
                "FROM hive_planner_log "
                "WHERE action_type = 'channel_open' AND result = 'success' "
                "AND timestamp >= ?",
                (today_start,)
            ).fetchone()

            spent = row['spent'] if row else 0
            return max(0, daily_budget_sats - spent)
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: get_available_budget error: {e}",
                    level='error'
                )
            return daily_budget_sats

    def get_budget_summary(self, daily_budget_sats: int = 0,
                           days: int = 1) -> Dict[str, Any]:
        """
        Get a budget summary for the given period.

        Args:
            daily_budget_sats: Daily budget cap in sats
            days: Number of days to summarize (default: 1)

        Returns:
            Dict with budget info
        """
        conn = self._get_connection()
        try:
            cutoff = int(time.time()) - (days * 86400)
            row = conn.execute(
                "SELECT COALESCE(SUM(CAST("
                "  json_extract(details, '$.amount_sats') AS INTEGER"
                ")), 0) as spent, "
                "COUNT(*) as open_count "
                "FROM hive_planner_log "
                "WHERE action_type = 'channel_open' AND result = 'success' "
                "AND timestamp >= ?",
                (cutoff,)
            ).fetchone()

            spent = row['spent'] if row else 0
            opens = row['open_count'] if row else 0

            return {
                "daily_budget_sats": daily_budget_sats,
                "spent_sats": spent,
                "remaining_sats": max(0, daily_budget_sats - spent),
                "channel_opens": opens,
                "days": days,
            }
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: get_budget_summary error: {e}",
                    level='error'
                )
            return {
                "daily_budget_sats": daily_budget_sats,
                "spent_sats": 0,
                "remaining_sats": daily_budget_sats,
                "channel_opens": 0,
                "days": days,
            }

    def get_channel_history(self, channel_id: str,
                            hours: int = 48) -> List[Dict[str, Any]]:
        """
        Get channel event history from peer_events for flow velocity calculation.

        Args:
            channel_id: Channel ID (short_channel_id or peer_id)
            hours: Look-back window in hours (default: 48)

        Returns:
            List of event dicts with timestamp and details
        """
        conn = self._get_connection()
        try:
            cutoff = int(time.time()) - (hours * 3600)
            rows = conn.execute(
                "SELECT * FROM peer_events "
                "WHERE peer_id = ? AND timestamp > ? "
                "ORDER BY timestamp ASC LIMIT 1000",
                (channel_id, cutoff)
            ).fetchall()

            results = []
            for row in rows:
                entry = dict(row)
                # Parse JSON details if present
                details_str = entry.get('details', '{}')
                try:
                    details = json.loads(details_str) if details_str else {}
                except (json.JSONDecodeError, TypeError):
                    details = {}
                entry.update(details)
                results.append(entry)

            return results
        except Exception as e:
            if self.plugin:
                self.plugin.log(
                    f"HiveDatabase: get_channel_history error: {e}",
                    level='error'
                )
            return []
