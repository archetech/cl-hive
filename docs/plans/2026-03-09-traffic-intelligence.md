# Traffic Intelligence Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add fleet-shared traffic intelligence with 4 new RPCs, 1 new gossip message type, and a fleet demand forecast to cl-hive.

**Architecture:** New `traffic_intelligence.py` module following the proven `fee_intelligence.py` pattern: RPC ingest → DB store → background loop broadcast → fleet handler → aggregated query. Fleet demand forecast builds on existing AnticipatoryLiquidityManager Kalman predictions.

**Tech Stack:** Python 3.12, SQLite (WAL mode), pyln-client, pytest

---

## Prerequisites

- Working directory: `/home/sat/bin/cl-hive/`
- Test command: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q --deselect tests/test_anticipatory_nnlb_bugs.py::TestHiveBridgeKeyFix`
- Current test count: 2,328 passing
- Design doc: `docs/plans/2026-03-09-traffic-intelligence-design.md`

## Reference Files

| Pattern | File | What to Copy |
|---------|------|--------------|
| Manager class | `modules/fee_intelligence.py` | Constructor, store, create_batch, handle_batch, aggregation |
| Protocol funcs | `modules/protocol.py` | `get_fee_intelligence_snapshot_signing_payload`, `validate_fee_intelligence_snapshot_payload`, `create_fee_intelligence_snapshot` |
| Handler | `modules/protocol_handlers.py:5421-5493` | `handle_fee_intelligence_snapshot` pattern |
| Broadcast | `modules/background_loops.py:1823-1976` | `_broadcast_our_fee_intelligence` pattern |
| RPC method | `cl-hive.py:4087-4171` | `hive-report-fee-observation` pattern |
| DB table | `modules/database.py:728-757` | `fee_intelligence` table + CRUD |

---

### Task 1: Database — fleet_traffic_intelligence Table + CRUD

**Files:**
- Modify: `modules/database.py`
- Test: `tests/test_traffic_intelligence.py` (create)

**Step 1: Write failing tests for DB methods**

Create `tests/test_traffic_intelligence.py` with tests for the 4 DB methods:

```python
"""
Test Suite for Traffic Intelligence.

Tests fleet-shared traffic profiles, temporal conflict detection,
and fleet demand forecasting.
"""

import pytest
import time
import json
import threading
from unittest.mock import Mock, MagicMock, patch

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Mock pyln.client before importing modules
class MockRpcError(Exception):
    pass

mock_pyln = MagicMock()
mock_pyln.Plugin = MagicMock
mock_pyln.RpcError = MockRpcError
sys.modules['pyln'] = mock_pyln
sys.modules['pyln.client'] = mock_pyln

from modules.database import HiveDatabase


@pytest.fixture
def db(tmp_path):
    """Create a temporary database."""
    db_path = str(tmp_path / "test_traffic.db")
    database = HiveDatabase(db_path)
    return database


class TestTrafficIntelligenceDatabase:
    """Test DB operations for fleet_traffic_intelligence table."""

    def test_save_traffic_profile(self, db):
        """save_traffic_profile stores and retrieves a profile."""
        db.save_traffic_profile(
            peer_id="peer_aaa",
            reporter_id="reporter_111",
            profile_type="retail",
            peak_hours_utc=json.dumps([9, 10, 11, 14, 15, 16]),
            quiet_hours_utc=json.dumps([1, 2, 3, 4, 5]),
            avg_forward_size_sats=50000.0,
            daily_volume_sats=5000000.0,
            drain_direction="outbound_heavy",
            confidence=0.85,
            observation_window_hours=168,
            received_at=time.time(),
            ttl_hours=168.0,
        )
        profiles = db.get_traffic_profiles_for_peer("peer_aaa")
        assert len(profiles) == 1
        assert profiles[0]["profile_type"] == "retail"
        assert profiles[0]["reporter_id"] == "reporter_111"

    def test_save_traffic_profile_upsert(self, db):
        """save_traffic_profile overwrites on same (peer_id, reporter_id)."""
        now = time.time()
        db.save_traffic_profile(
            peer_id="peer_aaa", reporter_id="reporter_111",
            profile_type="retail", peak_hours_utc="[]", quiet_hours_utc="[]",
            avg_forward_size_sats=100.0, daily_volume_sats=1000.0,
            drain_direction="balanced", confidence=0.5,
            observation_window_hours=24, received_at=now, ttl_hours=168.0,
        )
        db.save_traffic_profile(
            peer_id="peer_aaa", reporter_id="reporter_111",
            profile_type="wholesale", peak_hours_utc="[]", quiet_hours_utc="[]",
            avg_forward_size_sats=500000.0, daily_volume_sats=50000000.0,
            drain_direction="inbound_heavy", confidence=0.9,
            observation_window_hours=168, received_at=now + 1, ttl_hours=168.0,
        )
        profiles = db.get_traffic_profiles_for_peer("peer_aaa")
        assert len(profiles) == 1
        assert profiles[0]["profile_type"] == "wholesale"

    def test_get_traffic_profiles_for_peer_filters(self, db):
        """get_traffic_profiles_for_peer returns only matching peer."""
        now = time.time()
        for peer in ["peer_aaa", "peer_bbb"]:
            db.save_traffic_profile(
                peer_id=peer, reporter_id="reporter_111",
                profile_type="retail", peak_hours_utc="[]", quiet_hours_utc="[]",
                avg_forward_size_sats=100.0, daily_volume_sats=1000.0,
                drain_direction="balanced", confidence=0.5,
                observation_window_hours=24, received_at=now, ttl_hours=168.0,
            )
        assert len(db.get_traffic_profiles_for_peer("peer_aaa")) == 1
        assert len(db.get_traffic_profiles_for_peer("peer_bbb")) == 1
        assert len(db.get_traffic_profiles_for_peer("peer_ccc")) == 0

    def test_get_all_traffic_profiles(self, db):
        """get_all_traffic_profiles returns all non-expired profiles."""
        now = time.time()
        db.save_traffic_profile(
            peer_id="peer_aaa", reporter_id="reporter_111",
            profile_type="retail", peak_hours_utc="[]", quiet_hours_utc="[]",
            avg_forward_size_sats=100.0, daily_volume_sats=1000.0,
            drain_direction="balanced", confidence=0.5,
            observation_window_hours=24, received_at=now, ttl_hours=168.0,
        )
        db.save_traffic_profile(
            peer_id="peer_bbb", reporter_id="reporter_222",
            profile_type="burst", peak_hours_utc="[]", quiet_hours_utc="[]",
            avg_forward_size_sats=200.0, daily_volume_sats=2000.0,
            drain_direction="balanced", confidence=0.6,
            observation_window_hours=48, received_at=now, ttl_hours=168.0,
        )
        profiles = db.get_all_traffic_profiles()
        assert len(profiles) == 2

    def test_cleanup_expired_traffic_profiles(self, db):
        """cleanup_expired_traffic_profiles removes stale profiles."""
        old_time = time.time() - (200 * 3600)  # 200 hours ago
        db.save_traffic_profile(
            peer_id="peer_old", reporter_id="reporter_111",
            profile_type="retail", peak_hours_utc="[]", quiet_hours_utc="[]",
            avg_forward_size_sats=100.0, daily_volume_sats=1000.0,
            drain_direction="balanced", confidence=0.5,
            observation_window_hours=24, received_at=old_time, ttl_hours=168.0,
        )
        db.save_traffic_profile(
            peer_id="peer_new", reporter_id="reporter_111",
            profile_type="retail", peak_hours_utc="[]", quiet_hours_utc="[]",
            avg_forward_size_sats=100.0, daily_volume_sats=1000.0,
            drain_direction="balanced", confidence=0.5,
            observation_window_hours=24, received_at=time.time(), ttl_hours=168.0,
        )
        deleted = db.cleanup_expired_traffic_profiles()
        assert deleted == 1
        assert len(db.get_all_traffic_profiles()) == 1
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/test_traffic_intelligence.py -x -v`
Expected: FAIL — `save_traffic_profile` does not exist

**Step 3: Implement DB table + CRUD**

Add to `modules/database.py`:

1. In `_init_tables()`, add after the last CREATE TABLE:
```python
# FLEET TRAFFIC INTELLIGENCE TABLE (Phase 15+)
conn.execute("""
    CREATE TABLE IF NOT EXISTS fleet_traffic_intelligence (
        peer_id TEXT NOT NULL,
        reporter_id TEXT NOT NULL,
        profile_type TEXT,
        peak_hours_utc TEXT,
        quiet_hours_utc TEXT,
        avg_forward_size_sats REAL,
        daily_volume_sats REAL,
        drain_direction TEXT,
        confidence REAL,
        observation_window_hours INTEGER,
        received_at REAL,
        ttl_hours REAL DEFAULT 168.0,
        PRIMARY KEY (peer_id, reporter_id)
    )
""")
conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_traffic_intel_peer
    ON fleet_traffic_intelligence(peer_id)
""")
```

2. Add CRUD methods to HiveDatabase class:
```python
def save_traffic_profile(
    self, peer_id: str, reporter_id: str, profile_type: str,
    peak_hours_utc: str, quiet_hours_utc: str,
    avg_forward_size_sats: float, daily_volume_sats: float,
    drain_direction: str, confidence: float,
    observation_window_hours: int, received_at: float,
    ttl_hours: float = 168.0,
) -> bool:
    """Save or update a traffic profile (upsert on peer_id + reporter_id)."""
    try:
        conn = self._get_connection()
        conn.execute("""
            INSERT OR REPLACE INTO fleet_traffic_intelligence (
                peer_id, reporter_id, profile_type, peak_hours_utc,
                quiet_hours_utc, avg_forward_size_sats, daily_volume_sats,
                drain_direction, confidence, observation_window_hours,
                received_at, ttl_hours
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            peer_id, reporter_id, profile_type, peak_hours_utc,
            quiet_hours_utc, avg_forward_size_sats, daily_volume_sats,
            drain_direction, confidence, observation_window_hours,
            received_at, ttl_hours,
        ))
        return True
    except Exception:
        return False

def get_traffic_profiles_for_peer(
    self, peer_id: str
) -> list:
    """Get all traffic profiles for a specific peer."""
    conn = self._get_connection()
    now = time.time()
    rows = conn.execute("""
        SELECT * FROM fleet_traffic_intelligence
        WHERE peer_id = ? AND (received_at + ttl_hours * 3600) > ?
        ORDER BY confidence DESC
    """, (peer_id, now)).fetchall()
    return [dict(row) for row in rows]

def get_all_traffic_profiles(self) -> list:
    """Get all non-expired traffic profiles."""
    conn = self._get_connection()
    now = time.time()
    rows = conn.execute("""
        SELECT * FROM fleet_traffic_intelligence
        WHERE (received_at + ttl_hours * 3600) > ?
        ORDER BY peer_id, confidence DESC
    """, (now,)).fetchall()
    return [dict(row) for row in rows]

def cleanup_expired_traffic_profiles(self) -> int:
    """Remove expired traffic profiles. Returns count deleted."""
    conn = self._get_connection()
    now = time.time()
    cursor = conn.execute("""
        DELETE FROM fleet_traffic_intelligence
        WHERE (received_at + ttl_hours * 3600) <= ?
    """, (now,))
    return cursor.rowcount
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/test_traffic_intelligence.py -x -v`
Expected: All 5 tests PASS

**Step 5: Run full suite**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q --deselect tests/test_anticipatory_nnlb_bugs.py::TestHiveBridgeKeyFix`
Expected: 2,333+ passed

**Step 6: Commit**

```bash
git add modules/database.py tests/test_traffic_intelligence.py
git commit -m "feat(traffic-intel): add fleet_traffic_intelligence table and CRUD methods"
```

---

### Task 2: Protocol — Message Type, Validation, Signing, Serialization

**Files:**
- Modify: `modules/protocol.py`
- Test: `tests/test_traffic_intelligence.py` (append)

**Step 1: Write failing tests for protocol functions**

Append to `tests/test_traffic_intelligence.py`:

```python
from modules.protocol import (
    HiveMessageType,
    validate_traffic_intelligence_batch,
    get_traffic_intelligence_batch_signing_payload,
    create_traffic_intelligence_batch,
    serialize,
    deserialize,
)


class TestTrafficIntelligenceProtocol:
    """Test protocol functions for TRAFFIC_INTELLIGENCE_BATCH."""

    def test_message_type_exists(self):
        """TRAFFIC_INTELLIGENCE_BATCH enum value is 32905."""
        assert HiveMessageType.TRAFFIC_INTELLIGENCE_BATCH == 32905

    def test_signing_payload_deterministic(self):
        """Signing payload is deterministic for same input."""
        payload = {
            "reporter_id": "abc123",
            "timestamp": 1000000,
            "signature": "sig",
            "profiles": [
                {"peer_id": "peer_a", "profile_type": "retail", "confidence": 0.9},
                {"peer_id": "peer_b", "profile_type": "wholesale", "confidence": 0.8},
            ],
        }
        sig1 = get_traffic_intelligence_batch_signing_payload(payload)
        sig2 = get_traffic_intelligence_batch_signing_payload(payload)
        assert sig1 == sig2
        assert "TRAFFIC_INTELLIGENCE_BATCH:" in sig1
        assert "abc123" in sig1

    def test_signing_payload_order_independent(self):
        """Signing payload is the same regardless of profiles order."""
        p1 = {"peer_id": "peer_a", "profile_type": "retail", "confidence": 0.9}
        p2 = {"peer_id": "peer_b", "profile_type": "wholesale", "confidence": 0.8}
        base = {"reporter_id": "abc", "timestamp": 1000, "signature": "s"}
        sig_ab = get_traffic_intelligence_batch_signing_payload({**base, "profiles": [p1, p2]})
        sig_ba = get_traffic_intelligence_batch_signing_payload({**base, "profiles": [p2, p1]})
        assert sig_ab == sig_ba

    def test_validate_valid_payload(self):
        """Valid payload passes validation."""
        payload = {
            "reporter_id": "a" * 66,
            "timestamp": int(time.time()),
            "signature": "validbase64sig",
            "profiles": [
                {
                    "peer_id": "b" * 66,
                    "profile_type": "retail",
                    "peak_hours_utc": [9, 10, 11],
                    "quiet_hours_utc": [1, 2, 3],
                    "avg_forward_size_sats": 50000.0,
                    "daily_volume_sats": 5000000.0,
                    "drain_direction": "outbound_heavy",
                    "confidence": 0.85,
                    "observation_window_hours": 168,
                },
            ],
        }
        assert validate_traffic_intelligence_batch(payload) is True

    def test_validate_rejects_missing_reporter(self):
        """Missing reporter_id fails validation."""
        payload = {
            "timestamp": int(time.time()),
            "signature": "sig",
            "profiles": [],
        }
        assert validate_traffic_intelligence_batch(payload) is False

    def test_validate_rejects_stale_timestamp(self):
        """Timestamp older than 48h fails validation."""
        payload = {
            "reporter_id": "a" * 66,
            "timestamp": int(time.time()) - (49 * 3600),
            "signature": "validbase64sig",
            "profiles": [],
        }
        assert validate_traffic_intelligence_batch(payload) is False

    def test_validate_rejects_bad_profile_type(self):
        """Invalid profile_type fails validation."""
        payload = {
            "reporter_id": "a" * 66,
            "timestamp": int(time.time()),
            "signature": "validbase64sig",
            "profiles": [{
                "peer_id": "b" * 66,
                "profile_type": "INVALID",
                "peak_hours_utc": [],
                "quiet_hours_utc": [],
                "avg_forward_size_sats": 100.0,
                "daily_volume_sats": 1000.0,
                "drain_direction": "balanced",
                "confidence": 0.5,
                "observation_window_hours": 24,
            }],
        }
        assert validate_traffic_intelligence_batch(payload) is False

    def test_validate_rejects_too_many_profiles(self):
        """More than 200 profiles fails validation."""
        payload = {
            "reporter_id": "a" * 66,
            "timestamp": int(time.time()),
            "signature": "validbase64sig",
            "profiles": [{"peer_id": f"peer_{i}", "profile_type": "retail",
                          "peak_hours_utc": [], "quiet_hours_utc": [],
                          "avg_forward_size_sats": 100.0, "daily_volume_sats": 1000.0,
                          "drain_direction": "balanced", "confidence": 0.5,
                          "observation_window_hours": 24} for i in range(201)],
        }
        assert validate_traffic_intelligence_batch(payload) is False

    def test_create_and_deserialize_roundtrip(self):
        """create + deserialize roundtrip preserves data."""
        profiles = [
            {"peer_id": "peer_a", "profile_type": "retail", "confidence": 0.9,
             "peak_hours_utc": [9, 10], "quiet_hours_utc": [1, 2],
             "avg_forward_size_sats": 50000.0, "daily_volume_sats": 5000000.0,
             "drain_direction": "outbound_heavy", "observation_window_hours": 168},
        ]
        msg_bytes = create_traffic_intelligence_batch(
            reporter_id="reporter_abc",
            timestamp=1000000,
            signature="test_sig",
            profiles=profiles,
        )
        assert msg_bytes is not None
        msg_type, payload = deserialize(msg_bytes)
        assert msg_type == HiveMessageType.TRAFFIC_INTELLIGENCE_BATCH
        assert payload["reporter_id"] == "reporter_abc"
        assert len(payload["profiles"]) == 1
        assert payload["profiles"][0]["profile_type"] == "retail"
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/test_traffic_intelligence.py::TestTrafficIntelligenceProtocol -x -v`
Expected: FAIL — imports don't exist

**Step 3: Implement protocol functions**

Add to `modules/protocol.py`:

1. In `HiveMessageType` enum (after `ARBITRATION_VOTE = 32903`):
```python
# Phase 16: Traffic Intelligence
TRAFFIC_INTELLIGENCE_BATCH = 32905
```

2. Add constants (near other rate limit constants):
```python
# Traffic intelligence bounds
VALID_PROFILE_TYPES = {'retail', 'wholesale', 'burst', 'steady', 'mixed'}
VALID_DRAIN_DIRECTIONS = {'inbound_heavy', 'outbound_heavy', 'balanced'}
MAX_PROFILES_IN_BATCH = 200
TRAFFIC_INTELLIGENCE_MAX_AGE = 48 * 3600  # 48 hours
TRAFFIC_INTELLIGENCE_BATCH_RATE_LIMIT = (1, 6 * 3600)  # 1 per 6 hours per sender
MAX_DAILY_VOLUME_SATS = 1_000_000_000_000  # 10k BTC
MAX_FORWARD_SIZE_SATS = 100_000_000_000  # 1k BTC
MAX_OBSERVATION_WINDOW_HOURS = 720  # 30 days
```

3. Add to `RELIABLE_MESSAGE_TYPES` frozenset:
```python
HiveMessageType.TRAFFIC_INTELLIGENCE_BATCH,
```

4. Add signing function:
```python
def get_traffic_intelligence_batch_signing_payload(payload: Dict[str, Any]) -> str:
    """Get canonical string to sign for TRAFFIC_INTELLIGENCE_BATCH."""
    profiles = payload.get("profiles", [])
    sorted_profiles = sorted(profiles, key=lambda p: p.get("peer_id", ""))
    profiles_json = json.dumps(sorted_profiles, sort_keys=True, separators=(',', ':'))
    profiles_hash = hashlib.sha256(profiles_json.encode()).hexdigest()[:16]
    return (
        f"TRAFFIC_INTELLIGENCE_BATCH:"
        f"{payload.get('reporter_id', '')}:"
        f"{payload.get('timestamp', 0)}:"
        f"{len(profiles)}:"
        f"{profiles_hash}"
    )
```

5. Add validation function:
```python
def validate_traffic_intelligence_batch(payload: Dict[str, Any]) -> bool:
    """Validate a TRAFFIC_INTELLIGENCE_BATCH payload."""
    reporter_id = payload.get("reporter_id")
    if not isinstance(reporter_id, str) or not reporter_id:
        return False

    signature = payload.get("signature")
    if not isinstance(signature, str) or len(signature) < 10:
        return False

    timestamp = payload.get("timestamp", 0)
    if not isinstance(timestamp, (int, float)):
        return False
    now = time.time()
    if timestamp > now + 300:
        return False
    if timestamp < now - TRAFFIC_INTELLIGENCE_MAX_AGE:
        return False

    profiles = payload.get("profiles")
    if not isinstance(profiles, list):
        return False
    if len(profiles) > MAX_PROFILES_IN_BATCH:
        return False

    for p in profiles:
        if not isinstance(p, dict):
            return False
        peer_id = p.get("peer_id")
        if not isinstance(peer_id, str) or not peer_id:
            return False
        if p.get("profile_type") not in VALID_PROFILE_TYPES:
            return False
        if p.get("drain_direction") not in VALID_DRAIN_DIRECTIONS:
            return False
        confidence = p.get("confidence", 0)
        if not isinstance(confidence, (int, float)) or not (0 <= confidence <= 1):
            return False
        avg_size = p.get("avg_forward_size_sats", 0)
        if not isinstance(avg_size, (int, float)) or avg_size < 0 or avg_size > MAX_FORWARD_SIZE_SATS:
            return False
        daily_vol = p.get("daily_volume_sats", 0)
        if not isinstance(daily_vol, (int, float)) or daily_vol < 0 or daily_vol > MAX_DAILY_VOLUME_SATS:
            return False
        obs_window = p.get("observation_window_hours", 0)
        if not isinstance(obs_window, (int, float)) or obs_window < 0 or obs_window > MAX_OBSERVATION_WINDOW_HOURS:
            return False
        peak = p.get("peak_hours_utc")
        if not isinstance(peak, list) or not all(isinstance(h, int) and 0 <= h <= 23 for h in peak):
            return False
        quiet = p.get("quiet_hours_utc")
        if not isinstance(quiet, list) or not all(isinstance(h, int) and 0 <= h <= 23 for h in quiet):
            return False

    return True
```

6. Add creation function:
```python
def create_traffic_intelligence_batch(
    reporter_id: str,
    timestamp: int,
    signature: str,
    profiles: list,
) -> bytes:
    """Create a TRAFFIC_INTELLIGENCE_BATCH message."""
    payload = {
        "reporter_id": reporter_id,
        "timestamp": timestamp,
        "signature": signature,
        "profiles": profiles,
    }
    return serialize(HiveMessageType.TRAFFIC_INTELLIGENCE_BATCH, payload)
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/test_traffic_intelligence.py -x -v`
Expected: All tests PASS

**Step 5: Run full suite**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q --deselect tests/test_anticipatory_nnlb_bugs.py::TestHiveBridgeKeyFix`
Expected: All pass

**Step 6: Commit**

```bash
git add modules/protocol.py tests/test_traffic_intelligence.py
git commit -m "feat(traffic-intel): add TRAFFIC_INTELLIGENCE_BATCH protocol functions"
```

---

### Task 3: TrafficIntelligenceManager — Core Class

**Files:**
- Create: `modules/traffic_intelligence.py`
- Test: `tests/test_traffic_intelligence.py` (append)

**Step 1: Write failing tests for the manager**

Append to `tests/test_traffic_intelligence.py`:

```python
from modules.traffic_intelligence import TrafficIntelligenceManager


@pytest.fixture
def traffic_mgr(db):
    """Create a TrafficIntelligenceManager with test database."""
    plugin = Mock()
    plugin.log = Mock()
    plugin.rpc = MagicMock()
    mgr = TrafficIntelligenceManager(
        database=db,
        plugin=plugin,
        our_pubkey="our_node_pubkey_abc123",
    )
    return mgr


class TestTrafficIntelligenceManager:
    """Test TrafficIntelligenceManager core methods."""

    def test_store_local_profile(self, traffic_mgr, db):
        """store_local_profile saves to database."""
        result = traffic_mgr.store_local_profile(
            peer_id="peer_aaa",
            profile_type="retail",
            peak_hours_utc=[9, 10, 11, 14, 15, 16],
            quiet_hours_utc=[1, 2, 3, 4, 5],
            avg_forward_size_sats=50000.0,
            daily_volume_sats=5000000.0,
            drain_direction="outbound_heavy",
            confidence=0.85,
            observation_window_hours=168,
        )
        assert result is True
        profiles = db.get_traffic_profiles_for_peer("peer_aaa")
        assert len(profiles) == 1
        assert profiles[0]["profile_type"] == "retail"
        assert profiles[0]["reporter_id"] == "our_node_pubkey_abc123"

    def test_store_local_profile_rejects_invalid_type(self, traffic_mgr):
        """store_local_profile rejects invalid profile_type."""
        result = traffic_mgr.store_local_profile(
            peer_id="peer_aaa", profile_type="INVALID",
            peak_hours_utc=[], quiet_hours_utc=[],
            avg_forward_size_sats=100.0, daily_volume_sats=1000.0,
            drain_direction="balanced", confidence=0.5,
            observation_window_hours=24,
        )
        assert result is False

    def test_get_aggregated_profile_single_reporter(self, traffic_mgr):
        """get_aggregated_profile with one reporter returns its data."""
        traffic_mgr.store_local_profile(
            peer_id="peer_aaa", profile_type="retail",
            peak_hours_utc=[9, 10, 11], quiet_hours_utc=[1, 2, 3],
            avg_forward_size_sats=50000.0, daily_volume_sats=5000000.0,
            drain_direction="outbound_heavy", confidence=0.85,
            observation_window_hours=168,
        )
        agg = traffic_mgr.get_aggregated_profile("peer_aaa")
        assert agg is not None
        assert agg["profile_type"] == "retail"
        assert agg["confidence"] == 0.85
        assert 9 in agg["peak_hours_utc"]

    def test_get_aggregated_profile_multiple_reporters(self, traffic_mgr, db):
        """get_aggregated_profile merges multiple reporters."""
        now = time.time()
        # Our report
        traffic_mgr.store_local_profile(
            peer_id="peer_aaa", profile_type="retail",
            peak_hours_utc=[9, 10, 11], quiet_hours_utc=[1, 2, 3],
            avg_forward_size_sats=50000.0, daily_volume_sats=5000000.0,
            drain_direction="outbound_heavy", confidence=0.9,
            observation_window_hours=168,
        )
        # Remote report with different peak hours
        db.save_traffic_profile(
            peer_id="peer_aaa", reporter_id="remote_node_xyz",
            profile_type="wholesale", peak_hours_utc=json.dumps([14, 15, 16]),
            quiet_hours_utc=json.dumps([4, 5, 6]),
            avg_forward_size_sats=200000.0, daily_volume_sats=20000000.0,
            drain_direction="inbound_heavy", confidence=0.7,
            observation_window_hours=168, received_at=now, ttl_hours=168.0,
        )
        agg = traffic_mgr.get_aggregated_profile("peer_aaa")
        assert agg is not None
        # Highest confidence reporter's profile_type wins
        assert agg["profile_type"] == "retail"
        # Peak hours are union of both reporters
        assert 9 in agg["peak_hours_utc"]
        assert 14 in agg["peak_hours_utc"]

    def test_get_aggregated_profile_nonexistent_peer(self, traffic_mgr):
        """get_aggregated_profile returns None for unknown peer."""
        assert traffic_mgr.get_aggregated_profile("unknown_peer") is None

    def test_get_all_profiles_no_filter(self, traffic_mgr):
        """get_all_profiles returns all stored profiles."""
        for peer in ["peer_aaa", "peer_bbb"]:
            traffic_mgr.store_local_profile(
                peer_id=peer, profile_type="retail",
                peak_hours_utc=[], quiet_hours_utc=[],
                avg_forward_size_sats=100.0, daily_volume_sats=1000.0,
                drain_direction="balanced", confidence=0.5,
                observation_window_hours=24,
            )
        profiles = traffic_mgr.get_all_profiles()
        assert len(profiles) == 2

    def test_get_all_profiles_filter_by_type(self, traffic_mgr):
        """get_all_profiles filters by profile_type."""
        traffic_mgr.store_local_profile(
            peer_id="peer_aaa", profile_type="retail",
            peak_hours_utc=[], quiet_hours_utc=[],
            avg_forward_size_sats=100.0, daily_volume_sats=1000.0,
            drain_direction="balanced", confidence=0.5,
            observation_window_hours=24,
        )
        traffic_mgr.store_local_profile(
            peer_id="peer_bbb", profile_type="wholesale",
            peak_hours_utc=[], quiet_hours_utc=[],
            avg_forward_size_sats=500000.0, daily_volume_sats=50000000.0,
            drain_direction="balanced", confidence=0.5,
            observation_window_hours=24,
        )
        retail = traffic_mgr.get_all_profiles(profile_type="retail")
        assert len(retail) == 1
        assert retail[0]["profile_type"] == "retail"

    def test_cleanup_expired(self, traffic_mgr, db):
        """cleanup_expired_profiles delegates to database."""
        old_time = time.time() - (200 * 3600)
        db.save_traffic_profile(
            peer_id="peer_old", reporter_id="reporter_111",
            profile_type="retail", peak_hours_utc="[]", quiet_hours_utc="[]",
            avg_forward_size_sats=100.0, daily_volume_sats=1000.0,
            drain_direction="balanced", confidence=0.5,
            observation_window_hours=24, received_at=old_time, ttl_hours=168.0,
        )
        deleted = traffic_mgr.cleanup_expired_profiles()
        assert deleted == 1
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/test_traffic_intelligence.py::TestTrafficIntelligenceManager -x -v`
Expected: FAIL — module does not exist

**Step 3: Create modules/traffic_intelligence.py**

```python
"""
Traffic Intelligence Manager for cl-hive.

Manages fleet-shared traffic profiles for peer channels. Follows the
fee_intelligence.py pattern: RPC ingest → DB store → gossip broadcast →
fleet handler → aggregated query.

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

    # ── Rate Limiting ──────────────────────────────────────────────

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

    # ── Local Profile Storage ──────────────────────────────────────

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

    # ── Aggregation ────────────────────────────────────────────────

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

        # Sort by confidence descending — first entry is highest
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
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/test_traffic_intelligence.py -x -v`
Expected: All tests PASS

**Step 5: Run full suite + commit**

```bash
cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q --deselect tests/test_anticipatory_nnlb_bugs.py::TestHiveBridgeKeyFix
git add modules/traffic_intelligence.py tests/test_traffic_intelligence.py
git commit -m "feat(traffic-intel): add TrafficIntelligenceManager core class"
```

---

### Task 4: Gossip — Create Batch Message + Handle Incoming

**Files:**
- Modify: `modules/traffic_intelligence.py`
- Test: `tests/test_traffic_intelligence.py` (append)

**Step 1: Write failing tests for gossip methods**

Append to `tests/test_traffic_intelligence.py`:

```python
class TestTrafficIntelligenceGossip:
    """Test gossip creation and handling."""

    def test_create_batch_message(self, traffic_mgr):
        """create_traffic_intelligence_batch_message creates signed bytes."""
        traffic_mgr.store_local_profile(
            peer_id="peer_aaa", profile_type="retail",
            peak_hours_utc=[9, 10], quiet_hours_utc=[1, 2],
            avg_forward_size_sats=50000.0, daily_volume_sats=5000000.0,
            drain_direction="outbound_heavy", confidence=0.85,
            observation_window_hours=168,
        )
        rpc = MagicMock()
        rpc.signmessage.return_value = {"zbase": "fakesig123abc"}
        msg = traffic_mgr.create_traffic_intelligence_batch_message(rpc)
        assert msg is not None
        rpc.signmessage.assert_called_once()

    def test_create_batch_message_no_profiles(self, traffic_mgr):
        """create_traffic_intelligence_batch_message returns None with no data."""
        rpc = MagicMock()
        msg = traffic_mgr.create_traffic_intelligence_batch_message(rpc)
        assert msg is None

    def test_handle_batch_valid(self, traffic_mgr, db):
        """handle_traffic_intelligence_batch stores remote profiles."""
        sender = "remote_node_xyz"
        db.update_member(sender, tier="full")
        payload = {
            "reporter_id": sender,
            "timestamp": int(time.time()),
            "signature": "valid_sig",
            "profiles": [{
                "peer_id": "peer_ext",
                "profile_type": "wholesale",
                "peak_hours_utc": [14, 15, 16],
                "quiet_hours_utc": [2, 3, 4],
                "avg_forward_size_sats": 200000.0,
                "daily_volume_sats": 20000000.0,
                "drain_direction": "inbound_heavy",
                "confidence": 0.8,
                "observation_window_hours": 168,
            }],
        }
        rpc = MagicMock()
        rpc.checkmessage.return_value = {"verified": True, "pubkey": sender}
        result = traffic_mgr.handle_traffic_intelligence_batch(sender, payload, rpc)
        assert result.get("success") is True
        assert result.get("profiles_stored") == 1
        profiles = db.get_traffic_profiles_for_peer("peer_ext")
        assert len(profiles) == 1

    def test_handle_batch_rejects_nonmember(self, traffic_mgr):
        """handle_traffic_intelligence_batch rejects non-member."""
        payload = {
            "reporter_id": "stranger",
            "timestamp": int(time.time()),
            "signature": "sig",
            "profiles": [],
        }
        rpc = MagicMock()
        result = traffic_mgr.handle_traffic_intelligence_batch("stranger", payload, rpc)
        assert "error" in result

    def test_handle_batch_rejects_bad_signature(self, traffic_mgr, db):
        """handle_traffic_intelligence_batch rejects invalid signature."""
        sender = "remote_node_xyz"
        db.update_member(sender, tier="full")
        payload = {
            "reporter_id": sender,
            "timestamp": int(time.time()),
            "signature": "bad_sig",
            "profiles": [],
        }
        rpc = MagicMock()
        rpc.checkmessage.return_value = {"verified": False}
        result = traffic_mgr.handle_traffic_intelligence_batch(sender, payload, rpc)
        assert result.get("error") == "invalid_signature"

    def test_handle_batch_rejects_reporter_mismatch(self, traffic_mgr, db):
        """handle_traffic_intelligence_batch rejects if reporter != sender."""
        sender = "real_sender"
        db.update_member(sender, tier="full")
        payload = {
            "reporter_id": "impersonator",
            "timestamp": int(time.time()),
            "signature": "sig",
            "profiles": [],
        }
        rpc = MagicMock()
        result = traffic_mgr.handle_traffic_intelligence_batch(sender, payload, rpc)
        assert result.get("error") == "reporter_mismatch"
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/test_traffic_intelligence.py::TestTrafficIntelligenceGossip -x -v`
Expected: FAIL — methods don't exist

**Step 3: Add gossip methods to TrafficIntelligenceManager**

Add to `modules/traffic_intelligence.py`:

```python
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
```

**Step 4: Run tests**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/test_traffic_intelligence.py -x -v`
Expected: All tests PASS

**Step 5: Full suite + commit**

```bash
cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q --deselect tests/test_anticipatory_nnlb_bugs.py::TestHiveBridgeKeyFix
git add modules/traffic_intelligence.py tests/test_traffic_intelligence.py
git commit -m "feat(traffic-intel): add gossip create/handle for TRAFFIC_INTELLIGENCE_BATCH"
```

---

### Task 5: Rebalance Conflict Check + Fleet Demand Forecast

**Files:**
- Modify: `modules/traffic_intelligence.py`
- Test: `tests/test_traffic_intelligence.py` (append)

**Step 1: Write failing tests**

Append to `tests/test_traffic_intelligence.py`:

```python
class TestRebalanceConflictCheck:
    """Test temporal rebalance conflict detection."""

    def test_no_conflict_no_data(self, traffic_mgr):
        """No conflict when no traffic data exists."""
        result = traffic_mgr.check_rebalance_conflict(
            peer_id="unknown_peer",
            direction="outbound",
            amount_sats=100000,
        )
        assert result["conflict"] is False
        assert result["peer_in_peak_hours"] is False

    def test_peak_hour_detection(self, traffic_mgr):
        """Detects when peer is in peak hours."""
        current_hour = datetime.now(timezone.utc).hour
        traffic_mgr.store_local_profile(
            peer_id="peer_aaa", profile_type="retail",
            peak_hours_utc=[current_hour],
            quiet_hours_utc=[(current_hour + 12) % 24],
            avg_forward_size_sats=50000.0, daily_volume_sats=5000000.0,
            drain_direction="outbound_heavy", confidence=0.85,
            observation_window_hours=168,
        )
        result = traffic_mgr.check_rebalance_conflict(
            peer_id="peer_aaa", direction="outbound", amount_sats=100000,
        )
        assert result["peer_in_peak_hours"] is True

    def test_suggested_window_from_quiet_hours(self, traffic_mgr):
        """Suggests rebalance window from quiet hours."""
        current_hour = datetime.now(timezone.utc).hour
        quiet = [(current_hour + 6) % 24, (current_hour + 7) % 24]
        traffic_mgr.store_local_profile(
            peer_id="peer_aaa", profile_type="retail",
            peak_hours_utc=[current_hour],
            quiet_hours_utc=quiet,
            avg_forward_size_sats=50000.0, daily_volume_sats=5000000.0,
            drain_direction="outbound_heavy", confidence=0.85,
            observation_window_hours=168,
        )
        result = traffic_mgr.check_rebalance_conflict(
            peer_id="peer_aaa", direction="outbound", amount_sats=100000,
        )
        assert result["suggested_window_utc"] is not None
        assert len(result["suggested_window_utc"]) == 2

    def test_conflict_response_structure(self, traffic_mgr):
        """Response has all expected fields."""
        result = traffic_mgr.check_rebalance_conflict(
            peer_id="any_peer", direction="outbound", amount_sats=100000,
        )
        assert "conflict" in result
        assert "conflicting_member" in result
        assert "peer_in_peak_hours" in result
        assert "suggested_window_utc" in result
        assert "fleet_drain_forecast_sats" in result


class TestFleetDemandForecast:
    """Test fleet demand forecasting."""

    def test_forecast_no_data(self, traffic_mgr):
        """Forecast returns empty structure when no data."""
        forecast = traffic_mgr.get_fleet_demand_forecast(hours_ahead=6)
        assert "members" in forecast
        assert isinstance(forecast["members"], list)

    def test_forecast_structure(self, traffic_mgr):
        """Forecast response has expected top-level fields."""
        forecast = traffic_mgr.get_fleet_demand_forecast(hours_ahead=6)
        assert "members" in forecast
        assert "generated_at" in forecast
        assert "hours_ahead" in forecast
```

**Step 2: Run tests to verify they fail**

**Step 3: Implement conflict check and forecast**

Add to `modules/traffic_intelligence.py`:

```python
    # ── Rebalance Conflict Check ───────────────────────────────────

    def check_rebalance_conflict(
        self,
        peer_id: str,
        direction: str,
        amount_sats: int,
    ) -> Dict[str, Any]:
        """
        Check if rebalancing through a peer would conflict with fleet activity.

        Checks:
        1. Is any fleet member actively rebalancing through this peer? (MCF)
        2. Is this peer currently in peak traffic hours?
        3. What is the fleet's combined drain forecast for this peer?

        Args:
            peer_id: External peer to rebalance through
            direction: inbound or outbound
            amount_sats: Rebalance amount

        Returns:
            Conflict assessment dict
        """
        result = {
            "conflict": False,
            "conflicting_member": None,
            "peer_in_peak_hours": False,
            "suggested_window_utc": None,
            "fleet_drain_forecast_sats": 0,
        }

        # Check active MCF assignments for this peer
        if self.liquidity_coordinator:
            try:
                mcf_status = self.liquidity_coordinator.get_mcf_status()
                active = mcf_status.get("active_assignments", [])
                for a in active:
                    if peer_id in (a.get("from_channel", ""), a.get("to_channel", "")):
                        result["conflict"] = True
                        result["conflicting_member"] = a.get("member_id")
                        break
            except Exception:
                pass

        # Check fleet traffic intelligence for peak hours
        agg = self.get_aggregated_profile(peer_id)
        if agg:
            now_utc = datetime.now(timezone.utc).hour
            peak_hours = agg.get("peak_hours_utc", [])
            quiet_hours = agg.get("quiet_hours_utc", [])

            if now_utc in peak_hours:
                result["peer_in_peak_hours"] = True

            # Suggest window from quiet hours
            if quiet_hours:
                # Find the next quiet hour block
                start = None
                for h in sorted(quiet_hours):
                    if h > now_utc:
                        start = h
                        break
                if start is None and quiet_hours:
                    start = quiet_hours[0]  # Wrap to tomorrow

                if start is not None:
                    # Find contiguous block
                    end = start
                    for h in sorted(quiet_hours):
                        if h == end + 1:
                            end = h
                    result["suggested_window_utc"] = [start, end + 1]

            # Estimate drain forecast
            daily_vol = agg.get("daily_volume_sats", 0)
            drain_dir = agg.get("drain_direction", "balanced")
            if drain_dir == "outbound_heavy":
                result["fleet_drain_forecast_sats"] = int(daily_vol * 0.3)
            elif drain_dir == "inbound_heavy":
                result["fleet_drain_forecast_sats"] = int(-daily_vol * 0.3)

        return result

    # ── Fleet Demand Forecast ──────────────────────────────────────

    def get_fleet_demand_forecast(
        self, hours_ahead: int = 6
    ) -> Dict[str, Any]:
        """
        Generate fleet-wide demand forecast combining Kalman predictions
        with traffic intelligence.

        Args:
            hours_ahead: Prediction horizon in hours

        Returns:
            Forecast dict with per-member predictions
        """
        forecast = {
            "members": [],
            "generated_at": int(time.time()),
            "hours_ahead": hours_ahead,
        }

        # Get Kalman predictions from anticipatory liquidity manager
        if not self.anticipatory_mgr:
            return forecast

        try:
            predictions = self.anticipatory_mgr.get_all_predictions()
        except Exception:
            predictions = {}

        if not predictions:
            return forecast

        # Get all traffic profiles for enrichment
        all_profiles = self.db.get_all_traffic_profiles()
        profile_by_peer = {}
        for p in all_profiles:
            pid = p.get("peer_id")
            if pid not in profile_by_peer:
                profile_by_peer[pid] = []
            profile_by_peer[pid].append(p)

        # Build per-member forecast
        now = time.time()
        now_utc = datetime.now(timezone.utc).hour

        for channel_id, pred in predictions.items():
            if not isinstance(pred, dict):
                continue

            peer_id = pred.get("peer_id", "")
            predicted_pct = pred.get("predicted_local_pct")
            velocity = pred.get("velocity_pct_per_hour", 0)

            if predicted_pct is None:
                continue

            current_pct = pred.get("current_local_pct", 50)
            hours_to_depletion = None
            hours_to_saturation = None

            if velocity < 0 and current_pct > 0:
                hours_to_depletion = current_pct / abs(velocity)
            elif velocity > 0 and current_pct < 100:
                hours_to_saturation = (100 - current_pct) / velocity

            # Enrich with traffic intelligence
            optimal_window = None
            traffic_profiles = profile_by_peer.get(peer_id, [])
            if traffic_profiles:
                best = max(traffic_profiles, key=lambda p: p.get("confidence", 0))
                quiet_str = best.get("quiet_hours_utc", "[]")
                quiet = json.loads(quiet_str) if isinstance(quiet_str, str) else quiet_str
                if quiet:
                    next_quiet = None
                    for h in sorted(quiet):
                        if h > now_utc:
                            next_quiet = h
                            break
                    if next_quiet is None and quiet:
                        next_quiet = quiet[0]
                    if next_quiet is not None:
                        optimal_window = next_quiet

            entry = {
                "channel_id": channel_id,
                "peer_id": peer_id,
                "current_local_pct": current_pct,
                "velocity_pct_per_hour": velocity,
                "hours_to_depletion": hours_to_depletion,
                "hours_to_saturation": hours_to_saturation,
                "optimal_rebalance_hour_utc": optimal_window,
            }

            if hours_to_depletion is not None and hours_to_depletion <= hours_ahead:
                entry["action"] = "depleting"
            elif hours_to_saturation is not None and hours_to_saturation <= hours_ahead:
                entry["action"] = "saturating"
            else:
                entry["action"] = "stable"

            forecast["members"].append(entry)

        return forecast
```

**Step 4: Run tests**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/test_traffic_intelligence.py -x -v`
Expected: All tests PASS

**Step 5: Full suite + commit**

```bash
cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q --deselect tests/test_anticipatory_nnlb_bugs.py::TestHiveBridgeKeyFix
git add modules/traffic_intelligence.py tests/test_traffic_intelligence.py
git commit -m "feat(traffic-intel): add rebalance conflict check and fleet demand forecast"
```

---

### Task 6: Protocol Handler + Background Loop Broadcast

**Files:**
- Modify: `modules/protocol_handlers.py`
- Modify: `modules/background_loops.py`
- Test: `tests/test_traffic_intelligence.py` (append)

**Step 1: Write failing test for handler**

Append to `tests/test_traffic_intelligence.py`:

```python
from modules import protocol_handlers


class TestTrafficIntelligenceHandler:
    """Test protocol handler for TRAFFIC_INTELLIGENCE_BATCH."""

    def test_handler_exists(self):
        """handle_traffic_intelligence_batch function exists."""
        assert hasattr(protocol_handlers, 'handle_traffic_intelligence_batch')

    def test_handler_returns_continue_when_no_manager(self):
        """Handler returns continue when traffic_intel_mgr is None."""
        # Save and clear the global
        original = getattr(protocol_handlers, 'traffic_intel_mgr', None)
        protocol_handlers.traffic_intel_mgr = None
        try:
            result = protocol_handlers.handle_traffic_intelligence_batch(
                "peer_id", {}, Mock()
            )
            assert result == {"result": "continue"}
        finally:
            protocol_handlers.traffic_intel_mgr = original
```

**Step 2: Run test to verify it fails**

**Step 3: Implement handler in protocol_handlers.py**

Add to `modules/protocol_handlers.py` (near the other Phase 14+ handlers):

```python
def handle_traffic_intelligence_batch(peer_id: str, payload: Dict, plugin: Plugin) -> Dict:
    """
    Handle TRAFFIC_INTELLIGENCE_BATCH message from a hive member.

    RELAY: Supports multi-hop relay for non-mesh topologies.
    """
    if not traffic_intel_mgr or not database:
        return {"result": "continue"}

    # RELAY: Check deduplication
    if not _should_process_message(payload):
        return {"result": "continue"}

    reporter_id = payload.get("reporter_id", peer_id)
    is_relayed = _is_relayed_message(payload)

    # Verify sender is a member and not banned
    sender = database.get_member(reporter_id)
    if not sender or database.is_banned(reporter_id):
        plugin.log(f"cl-hive: TRAFFIC_INTELLIGENCE_BATCH from non-member {reporter_id[:16]}...", level='debug')
        return {"result": "continue"}

    # Identity binding for direct messages
    if not is_relayed and reporter_id != peer_id:
        plugin.log(f"cl-hive: TRAFFIC_INTELLIGENCE_BATCH reporter mismatch", level='debug')
        return {"result": "continue"}

    # Timestamp freshness
    if not _check_timestamp_freshness(payload, 48 * 3600, "TRAFFIC_INTELLIGENCE_BATCH"):
        return {"result": "continue"}

    # Signature verification
    signature = payload.get("signature")
    if not signature:
        plugin.log(f"cl-hive: TRAFFIC_INTELLIGENCE_BATCH missing signature", level='warn')
        return {"result": "continue"}

    from modules.protocol import get_traffic_intelligence_batch_signing_payload
    signing_payload = get_traffic_intelligence_batch_signing_payload(payload)
    try:
        verify_result = plugin.rpc.checkmessage(signing_payload, signature)
        if not verify_result.get("verified") or verify_result.get("pubkey") != reporter_id:
            plugin.log(f"cl-hive: TRAFFIC_INTELLIGENCE_BATCH invalid signature", level='warn')
            return {"result": "continue"}
    except Exception as e:
        plugin.log(f"cl-hive: TRAFFIC_INTELLIGENCE_BATCH signature check failed: {e}", level='warn')
        return {"result": "continue"}

    # Delegate to manager
    result = traffic_intel_mgr.handle_traffic_intelligence_batch(reporter_id, payload, plugin.rpc)

    if result.get("success"):
        relay_info = " (relayed)" if is_relayed else ""
        plugin.log(
            f"cl-hive: Stored traffic intelligence from {reporter_id[:16]}...{relay_info} "
            f"with {result.get('profiles_stored', 0)} profiles",
            level='debug'
        )
        from modules.protocol import HiveMessageType
        relay_count = _relay_message(HiveMessageType.TRAFFIC_INTELLIGENCE_BATCH, payload, peer_id)
        if relay_count > 0:
            plugin.log(f"cl-hive: TRAFFIC_INTELLIGENCE_BATCH relayed to {relay_count} members", level='debug')

    return {"result": "continue"}
```

**Step 4: Add broadcast helper to background_loops.py**

Add to `modules/background_loops.py`:

```python
def _broadcast_our_traffic_intelligence():
    """
    Broadcast our traffic intelligence profiles to the fleet.

    Called every 6 hours by the intelligence broadcast loop.
    Collects locally-stored traffic profiles and sends a
    TRAFFIC_INTELLIGENCE_BATCH message.
    """
    if not traffic_intel_mgr or not plugin or not outbox_mgr:
        return

    try:
        msg = traffic_intel_mgr.create_traffic_intelligence_batch_message(plugin.rpc)
        if msg:
            outbox_mgr.broadcast(msg)
            plugin.log("cl-hive: Broadcast traffic intelligence to fleet", level='debug')
    except Exception as e:
        plugin.log(f"cl-hive: Traffic intelligence broadcast error: {e}", level='warn')
```

**Step 5: Run tests + commit**

```bash
cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q --deselect tests/test_anticipatory_nnlb_bugs.py::TestHiveBridgeKeyFix
git add modules/protocol_handlers.py modules/background_loops.py tests/test_traffic_intelligence.py
git commit -m "feat(traffic-intel): add protocol handler and broadcast loop"
```

---

### Task 7: RPC Commands — All 4 New Methods

**Files:**
- Modify: `modules/rpc_commands.py`
- Test: `tests/test_traffic_intelligence.py` (append)

**Step 1: Write failing tests for RPC functions**

Append to `tests/test_traffic_intelligence.py`:

```python
from modules.rpc_commands import (
    report_traffic_profile,
    get_traffic_intelligence,
    check_rebalance_conflict,
    get_fleet_demand_forecast,
)


class TestTrafficIntelligenceRPCs:
    """Test RPC command implementations."""

    @pytest.fixture
    def ctx(self, db, traffic_mgr):
        """Create a mock HiveContext."""
        ctx = Mock()
        ctx.database = db
        ctx.traffic_intel_mgr = traffic_mgr
        ctx.safe_plugin = Mock()
        ctx.safe_plugin.rpc = MagicMock()
        return ctx

    def test_report_traffic_profile_success(self, ctx):
        """report_traffic_profile stores profile and returns accepted."""
        result = report_traffic_profile(
            ctx,
            peer_id="peer_aaa",
            profile_type="retail",
            peak_hours_utc=[9, 10, 11],
            quiet_hours_utc=[1, 2, 3],
            avg_forward_size_sats=50000.0,
            daily_volume_sats=5000000.0,
            drain_direction="outbound_heavy",
            confidence=0.85,
            observation_window_hours=168,
        )
        assert result["status"] == "accepted"

    def test_report_traffic_profile_missing_peer(self, ctx):
        """report_traffic_profile returns error for missing peer_id."""
        result = report_traffic_profile(ctx, peer_id="")
        assert "error" in result

    def test_get_traffic_intelligence_all(self, ctx):
        """get_traffic_intelligence returns all profiles."""
        ctx.traffic_intel_mgr.store_local_profile(
            peer_id="peer_aaa", profile_type="retail",
            peak_hours_utc=[], quiet_hours_utc=[],
            avg_forward_size_sats=100.0, daily_volume_sats=1000.0,
            drain_direction="balanced", confidence=0.5,
            observation_window_hours=24,
        )
        result = get_traffic_intelligence(ctx)
        assert "profiles" in result
        assert len(result["profiles"]) >= 1

    def test_check_rebalance_conflict_returns_assessment(self, ctx):
        """check_rebalance_conflict returns conflict assessment."""
        result = check_rebalance_conflict(
            ctx, peer_id="peer_aaa", direction="outbound", amount_sats=100000,
        )
        assert "conflict" in result
        assert "peer_in_peak_hours" in result

    def test_get_fleet_demand_forecast_returns_forecast(self, ctx):
        """get_fleet_demand_forecast returns forecast structure."""
        result = get_fleet_demand_forecast(ctx, hours_ahead=6)
        assert "members" in result
        assert "hours_ahead" in result
```

**Step 2: Run tests to verify they fail**

**Step 3: Implement RPC functions in rpc_commands.py**

Add to `modules/rpc_commands.py`:

```python
def report_traffic_profile(
    ctx,
    peer_id: str = "",
    profile_type: str = "mixed",
    peak_hours_utc: list = None,
    quiet_hours_utc: list = None,
    avg_forward_size_sats: float = 0.0,
    daily_volume_sats: float = 0.0,
    drain_direction: str = "balanced",
    confidence: float = 0.5,
    observation_window_hours: int = 24,
):
    """
    Receive traffic profile from cl-revenue-ops.

    Permission: None (local integration)
    """
    if not ctx.database or not ctx.traffic_intel_mgr:
        return {"error": "Traffic intelligence not initialized"}

    if not peer_id:
        return {"error": "peer_id is required"}

    try:
        ok = ctx.traffic_intel_mgr.store_local_profile(
            peer_id=peer_id,
            profile_type=profile_type,
            peak_hours_utc=peak_hours_utc or [],
            quiet_hours_utc=quiet_hours_utc or [],
            avg_forward_size_sats=avg_forward_size_sats,
            daily_volume_sats=daily_volume_sats,
            drain_direction=drain_direction,
            confidence=confidence,
            observation_window_hours=observation_window_hours,
        )
        if ok:
            return {"status": "accepted", "peer_id": peer_id}
        else:
            return {"error": "Failed to store profile (validation failed)"}
    except Exception as e:
        return {"error": f"Failed to store profile: {e}"}


def get_traffic_intelligence(
    ctx,
    peer_id: str = None,
    profile_type: str = None,
):
    """
    Query aggregated fleet traffic intelligence.

    Permission: None (local query)
    """
    if not ctx.traffic_intel_mgr:
        return {"error": "Traffic intelligence not initialized"}

    try:
        if peer_id:
            agg = ctx.traffic_intel_mgr.get_aggregated_profile(peer_id)
            if agg:
                return {"profiles": [agg]}
            return {"profiles": []}
        else:
            profiles = ctx.traffic_intel_mgr.get_all_profiles(
                profile_type=profile_type,
            )
            return {"profiles": profiles}
    except Exception as e:
        return {"error": f"Query failed: {e}"}


def check_rebalance_conflict(
    ctx,
    peer_id: str = "",
    direction: str = "outbound",
    amount_sats: int = 0,
):
    """
    Check if rebalancing through a peer conflicts with fleet activity.

    Permission: None (local query)
    """
    if not ctx.traffic_intel_mgr:
        return {"error": "Traffic intelligence not initialized"}

    if not peer_id:
        return {"error": "peer_id is required"}

    try:
        return ctx.traffic_intel_mgr.check_rebalance_conflict(
            peer_id=peer_id,
            direction=direction,
            amount_sats=amount_sats,
        )
    except Exception as e:
        return {"error": f"Conflict check failed: {e}"}


def get_fleet_demand_forecast(ctx, hours_ahead: int = 6):
    """
    Get fleet-wide demand forecast.

    Permission: None (local query)
    """
    if not ctx.traffic_intel_mgr:
        return {"error": "Traffic intelligence not initialized"}

    hours_ahead = max(1, min(hours_ahead, 168))

    try:
        return ctx.traffic_intel_mgr.get_fleet_demand_forecast(
            hours_ahead=hours_ahead,
        )
    except Exception as e:
        return {"error": f"Forecast failed: {e}"}
```

**Step 4: Run tests + commit**

```bash
cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q --deselect tests/test_anticipatory_nnlb_bugs.py::TestHiveBridgeKeyFix
git add modules/rpc_commands.py tests/test_traffic_intelligence.py
git commit -m "feat(traffic-intel): add 4 RPC command implementations"
```

---

### Task 8: Wire Everything in cl-hive.py

**Files:**
- Modify: `cl-hive.py`
- Test: Run full suite

This task wires the new module into the plugin entry point:

**Step 1: Add imports**

Near the existing imports from rpc_commands (around line 132-266), add:
```python
from modules.rpc_commands import (
    report_traffic_profile,
    get_traffic_intelligence,
    check_rebalance_conflict,
    get_fleet_demand_forecast,
)
```

Near existing module imports, add:
```python
from modules.traffic_intelligence import TrafficIntelligenceManager
```

**Step 2: Add global variable**

Near other manager globals:
```python
traffic_intel_mgr = None
```

**Step 3: Instantiate manager**

Near where `fee_intel_mgr` is created (around line 1122):
```python
traffic_intel_mgr = TrafficIntelligenceManager(
    database=database,
    plugin=plugin,
    our_pubkey=our_pubkey,
    anticipatory_mgr=anticipatory_liquidity_mgr,
    liquidity_coordinator=liquidity_coord,
    membership_mgr=membership_mgr,
)
plugin.log("cl-hive: Traffic intelligence manager initialized")
```

**Step 4: Register 4 RPC methods**

Near the other @plugin.method registrations:
```python
@plugin.method("hive-report-traffic-profile")
def hive_report_traffic_profile(
    plugin: Plugin,
    peer_id: str = "",
    profile_type: str = "mixed",
    peak_hours_utc: list = None,
    quiet_hours_utc: list = None,
    avg_forward_size_sats: float = 0.0,
    daily_volume_sats: float = 0.0,
    drain_direction: str = "balanced",
    confidence: float = 0.5,
    observation_window_hours: int = 24,
):
    """Receive traffic profile from cl-revenue-ops."""
    return report_traffic_profile(
        ctx, peer_id=peer_id, profile_type=profile_type,
        peak_hours_utc=peak_hours_utc, quiet_hours_utc=quiet_hours_utc,
        avg_forward_size_sats=avg_forward_size_sats,
        daily_volume_sats=daily_volume_sats,
        drain_direction=drain_direction, confidence=confidence,
        observation_window_hours=observation_window_hours,
    )


@plugin.method("hive-traffic-intelligence")
def hive_traffic_intelligence(
    plugin: Plugin,
    peer_id: str = None,
    profile_type: str = None,
):
    """Query aggregated fleet traffic intelligence."""
    return get_traffic_intelligence(ctx, peer_id=peer_id, profile_type=profile_type)


@plugin.method("hive-check-rebalance-conflict")
def hive_check_rebalance_conflict(
    plugin: Plugin,
    peer_id: str = "",
    direction: str = "outbound",
    amount_sats: int = 0,
):
    """Check rebalance conflict with fleet activity."""
    return check_rebalance_conflict(
        ctx, peer_id=peer_id, direction=direction, amount_sats=amount_sats,
    )


@plugin.method("hive-fleet-demand-forecast")
def hive_fleet_demand_forecast(plugin: Plugin, hours_ahead: int = 6):
    """Get fleet-wide demand forecast."""
    return get_fleet_demand_forecast(ctx, hours_ahead=hours_ahead)
```

**Step 5: Add dispatch entry**

In `_dispatch_hive_message()`, add after the last Phase 15 (MCF) entry:
```python
# Phase 16: Traffic Intelligence
elif msg_type == HiveMessageType.TRAFFIC_INTELLIGENCE_BATCH:
    protocol_handlers.handle_traffic_intelligence_batch(peer_id, msg_payload, plugin)
```

**Step 6: Inject into protocol_handlers and background_loops**

In `init_protocol_handlers()` deps dict, add:
```python
"traffic_intel_mgr": traffic_intel_mgr,
```

In `init_background_loops()` deps dict, add:
```python
"traffic_intel_mgr": traffic_intel_mgr,
```

**Step 7: Wire broadcast into background loop cycle**

In background_loops.py, add a call to `_broadcast_our_traffic_intelligence()` inside the existing intelligence broadcast loop (the one that calls `_broadcast_our_fee_intelligence`). It should be called every 6 hours (use a counter or timestamp check).

**Step 8: Run full suite + commit**

```bash
cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q --deselect tests/test_anticipatory_nnlb_bugs.py::TestHiveBridgeKeyFix
git add cl-hive.py modules/protocol_handlers.py modules/background_loops.py
git commit -m "feat(traffic-intel): wire TrafficIntelligenceManager into cl-hive.py"
```

---

## Success Criteria

- All existing 2,328+ tests pass
- New test file `tests/test_traffic_intelligence.py` passes (~25-30 new tests)
- 4 new RPC methods registered and functional
- 1 new gossip message type (32905) with handler
- 1 new DB table with CRUD
- 1 new module (`traffic_intelligence.py`)
- ~8 commits total
