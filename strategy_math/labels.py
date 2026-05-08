"""
strategy_math/labels.py

Load historical daily tmax/tmin labels from daily_labels.db. Used by
backtest (full history) and by the live strategy at startup (for lag features).
"""

from __future__ import annotations

import sqlite3

import pandas as pd

from ml.config import LABELS_DB


def _load_labels() -> pd.DataFrame:
    """Load all daily labels (obs_date, tmax_raw, tmin_raw, tmax_settled, tmin_settled)."""
    conn = sqlite3.connect(LABELS_DB)
    df = pd.read_sql(
        """SELECT obs_date, tmax_raw, tmin_raw, tmax_settled, tmin_settled
           FROM daily_labels
           ORDER BY obs_date""",
        conn,
    )
    conn.close()
    df["obs_date"] = pd.to_datetime(df["obs_date"]).dt.date
    return df
