"""
Build daily ground-truth labels aligned with Polymarket settlement rules.

Label sources (preferred order):
  1. Wunderground ZSPD (database/standard.db) — half-hourly, highest fidelity,
     matches Polymarket's settlement source exactly.
  2. IEM ASOS ZSPD (database/iem_asos_zspd.db) — hourly, same station,
     ~1h lag; fills historical dates not yet in WU.

Settlement rule:
  tmax_settled = round(MAX(temp_c) over obs_date_local)  [standard rounding]
  tmin_settled = round(MIN(temp_c) over obs_date_local)

  "round" here is standard round-half-up: 27.5 → 28, 27.4 → 27.
  This matches how Wunderground displays integer temperatures on its website
  and how Polymarket compares against integer thresholds.

Output: database/daily_labels.db  (table: daily_labels)

Run:
    python -m ml.data.build_labels
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from ml.config import (
    DATA_END, DATA_START, IEM_ASOS_DB, LABELS_DB,
    WU_ZSPD_DB, settle,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_labels (
    obs_date        TEXT PRIMARY KEY,   -- YYYY-MM-DD (Asia/Shanghai)
    source          TEXT NOT NULL,      -- 'wu_zspd' | 'iem_asos' | 'both'
    tmax_raw        REAL NOT NULL,      -- max temp °C (raw, decimal precision)
    tmin_raw        REAL NOT NULL,      -- min temp °C
    tmax_settled    INTEGER NOT NULL,   -- round(tmax_raw) per Polymarket rule
    tmin_settled    INTEGER NOT NULL,
    n_obs           INTEGER,            -- number of sub-daily readings
    fetched_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_labels_date ON daily_labels(obs_date);
"""


def _load_wu_daily() -> dict[str, dict]:
    """Aggregate WU half-hourly → daily tmax/tmin keyed by obs_date."""
    if not WU_ZSPD_DB.exists():
        return {}
    conn = sqlite3.connect(WU_ZSPD_DB)
    rows = conn.execute(
        """
        SELECT obs_date_local,
               MAX(temp_c) AS tmax,
               MIN(temp_c) AS tmin,
               COUNT(*)    AS n_obs
        FROM zspd_observations
        WHERE temp_c IS NOT NULL
          AND obs_date_local BETWEEN ? AND ?
        GROUP BY obs_date_local
        """,
        (DATA_START.isoformat(), DATA_END.isoformat()),
    ).fetchall()
    conn.close()
    return {
        r[0]: {"tmax": r[1], "tmin": r[2], "n_obs": r[3], "source": "wu_zspd"}
        for r in rows
        if r[1] is not None and r[2] is not None
    }


def _load_iem_daily() -> dict[str, dict]:
    """Load IEM pre-aggregated daily stats."""
    if not IEM_ASOS_DB.exists():
        return {}
    conn = sqlite3.connect(IEM_ASOS_DB)
    # iem_daily_stats is built by fetch_iem_asos._rebuild_daily_stats
    try:
        rows = conn.execute(
            """
            SELECT obs_date, tmax_c, tmin_c, n_obs
            FROM iem_daily_stats
            WHERE tmax_c IS NOT NULL AND tmin_c IS NOT NULL
              AND obs_date BETWEEN ? AND ?
            """,
            (DATA_START.isoformat(), DATA_END.isoformat()),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return {
        r[0]: {"tmax": r[1], "tmin": r[2], "n_obs": r[3], "source": "iem_asos"}
        for r in rows
    }


def build_labels() -> int:
    """Merge WU + IEM, apply settlement rounding, write to LABELS_DB."""
    wu  = _load_wu_daily()
    iem = _load_iem_daily()

    all_dates = sorted(set(wu) | set(iem))
    now_iso = datetime.utcnow().isoformat()

    records = []
    for d in all_dates:
        if d in wu and d in iem:
            # Prefer WU for tmax/tmin but note both available
            entry  = wu[d]
            source = "both"
        elif d in wu:
            entry  = wu[d]
            source = "wu_zspd"
        else:
            entry  = iem[d]
            source = "iem_asos"

        tmax_raw = entry["tmax"]
        tmin_raw = entry["tmin"]

        # Guard: sanity check (ZSPD plausible range −10…45°C)
        if not (-10.0 <= tmax_raw <= 50.0) or not (-15.0 <= tmin_raw <= 45.0):
            continue
        if tmax_raw < tmin_raw:
            continue

        records.append((
            d,
            source,
            round(tmax_raw, 2),
            round(tmin_raw, 2),
            settle(tmax_raw),
            settle(tmin_raw),
            entry.get("n_obs"),
            now_iso,
        ))

    conn = sqlite3.connect(LABELS_DB)
    conn.executescript(SCHEMA)
    conn.executemany(
        """INSERT INTO daily_labels
               (obs_date, source, tmax_raw, tmin_raw,
                tmax_settled, tmin_settled, n_obs, fetched_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(obs_date) DO UPDATE SET
               source=excluded.source,
               tmax_raw=excluded.tmax_raw,
               tmin_raw=excluded.tmin_raw,
               tmax_settled=excluded.tmax_settled,
               tmin_settled=excluded.tmin_settled,
               n_obs=excluded.n_obs,
               fetched_at=excluded.fetched_at""",
        records,
    )
    conn.commit()

    # Summary
    src_counts = {}
    for r in records:
        src_counts[r[1]] = src_counts.get(r[1], 0) + 1

    print(f"daily_labels: {len(records)} days written to {LABELS_DB}")
    for src, cnt in sorted(src_counts.items()):
        print(f"  {src}: {cnt} days")

    # Coverage check
    wu_only_dates  = sorted(d for d, e in {**iem, **wu}.items() if d not in iem or d not in wu)
    if wu_only_dates:
        print(f"  Note: {len(wu_only_dates)} dates have only one source")

    conn.close()
    return len(records)


def load_labels_df():
    """Return daily_labels as pandas DataFrame indexed by obs_date."""
    import pandas as pd
    conn = sqlite3.connect(LABELS_DB)
    df = pd.read_sql(
        "SELECT * FROM daily_labels ORDER BY obs_date",
        conn,
        index_col="obs_date",
        parse_dates=["obs_date"],
    )
    conn.close()
    return df


if __name__ == "__main__":
    n = build_labels()
    print(f"Done. {n} label rows.")
