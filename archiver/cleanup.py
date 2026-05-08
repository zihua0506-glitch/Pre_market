"""Retention cleanup helpers."""
from __future__ import annotations
import sqlite3
from datetime import date
from pathlib import Path


def old_warm_dirs(warm_root: Path, retention_days: int) -> list[Path]:
    today = date.today()
    out: list[Path] = []
    if not warm_root.exists():
        return out
    for p in warm_root.iterdir():
        if not p.is_dir():
            continue
        try:
            d = date.fromisoformat(p.name)
        except ValueError:
            continue
        if (today - d).days > retention_days:
            out.append(p)
    return out


def delete_old_sqlite_rows(db_path: Path, table: str, ts_col: str, retention_days: int) -> int:
    if not db_path.exists():
        return 0
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            f"DELETE FROM {table} WHERE {ts_col} < datetime('now', ?)",
            (f"-{retention_days} days",),
        )
        return cur.rowcount or 0
