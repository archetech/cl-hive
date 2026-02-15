#!/usr/bin/env python3
"""advisor.db maintenance: bounded retention + WAL hygiene.

Keeps the advisor DB useful for learning while preventing unbounded growth.

Default policy (tunable via env):
- channel_history_days: 45
- hourly_snapshots_days: 14  (fleet_snapshots where snapshot_type='hourly')
- action_outcomes_days: 180
- ai_decisions_days: 365
- alert_history_resolved_days: 90

Notes:
- Uses DELETEs + WAL checkpoint (TRUNCATE). Does NOT VACUUM by default.
- For file size shrink, run VACUUM separately during low-usage windows.

Usage:
  ADVISOR_DB_PATH=... ./advisor_db_maintenance.py
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Policy:
    channel_history_days: int = int(os.environ.get("ADVISOR_RETENTION_CHANNEL_HISTORY_DAYS", "45"))
    hourly_snapshots_days: int = int(os.environ.get("ADVISOR_RETENTION_HOURLY_SNAPSHOTS_DAYS", "14"))
    action_outcomes_days: int = int(os.environ.get("ADVISOR_RETENTION_ACTION_OUTCOMES_DAYS", "180"))
    ai_decisions_days: int = int(os.environ.get("ADVISOR_RETENTION_AI_DECISIONS_DAYS", "365"))
    alert_history_resolved_days: int = int(os.environ.get("ADVISOR_RETENTION_ALERT_RESOLVED_DAYS", "90"))


def _cutoff_ts(days: int) -> int:
    return int(time.time()) - int(days) * 86400


def main() -> int:
    db_path = os.environ.get(
        "ADVISOR_DB_PATH",
        str(Path.home() / "bin" / "cl-hive" / "production" / "data" / "advisor.db"),
    )

    p = Policy()

    if not db_path:
        print("ERROR: ADVISOR_DB_PATH not set")
        return 2

    if not Path(db_path).exists():
        print(f"ERROR: advisor db not found at {db_path}")
        return 2

    # Use a short timeout; if the advisor is writing, we'll retry next run.
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    cur = conn.cursor()

    stats = {}

    try:
        # 1) Channel history (high volume)
        ch_cutoff = _cutoff_ts(p.channel_history_days)
        cur.execute("DELETE FROM channel_history WHERE timestamp < ?", (ch_cutoff,))
        stats["channel_history_deleted"] = cur.rowcount

        # 2) Fleet snapshots: prune old hourly only (keep daily/manual longer)
        fs_cutoff = _cutoff_ts(p.hourly_snapshots_days)
        cur.execute(
            "DELETE FROM fleet_snapshots WHERE snapshot_type='hourly' AND timestamp < ?",
            (fs_cutoff,),
        )
        stats["fleet_snapshots_hourly_deleted"] = cur.rowcount

        # 3) Action outcomes (learning signal, but can grow large)
        ao_cutoff = _cutoff_ts(p.action_outcomes_days)
        cur.execute("DELETE FROM action_outcomes WHERE measured_at < ?", (ao_cutoff,))
        stats["action_outcomes_deleted"] = cur.rowcount

        # 4) AI decisions (keep longer; never delete pending/recommended)
        ad_cutoff = _cutoff_ts(p.ai_decisions_days)
        cur.execute(
            "DELETE FROM ai_decisions WHERE timestamp < ? AND status NOT IN ('recommended')",
            (ad_cutoff,),
        )
        stats["ai_decisions_deleted"] = cur.rowcount

        # 5) Alert history (resolved alerts can be pruned)
        ah_cutoff = _cutoff_ts(p.alert_history_resolved_days)
        cur.execute(
            "DELETE FROM alert_history WHERE resolved=1 AND resolved_at IS NOT NULL AND resolved_at < ?",
            (ah_cutoff,),
        )
        stats["alert_history_resolved_deleted"] = cur.rowcount

        # Hygiene
        conn.commit()

        # WAL checkpoint to keep WAL from growing without needing VACUUM
        cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        chk = cur.fetchone()
        stats["wal_checkpoint"] = chk

        # Update planner stats
        cur.execute("ANALYZE")
        conn.commit()

        print("advisor_db_maintenance: ok")
        for k, v in stats.items():
            print(f"- {k}: {v}")
        return 0

    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
