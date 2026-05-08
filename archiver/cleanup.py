"""Retention cleanup helpers."""
from __future__ import annotations
import sqlite3
from datetime import date
from pathlib import Path

# Whitelist of allowed (table, ts_col) pairs for SQL identifier interpolation.
# Callers must pass only these combinations; anything else raises ValueError.
ALLOWED_TABLES: dict[str, str] = {
    "l2_snapshot": "ts",
    "trades": "ts",
}


def _validated(table: str, ts_col: str) -> tuple[str, str]:
    """Raise ValueError if (table, ts_col) is not in the whitelist."""
    if table not in ALLOWED_TABLES or ALLOWED_TABLES[table] != ts_col:
        raise ValueError(
            f"refusing unknown table/ts_col: {table!r}/{ts_col!r}; "
            f"allowed: {ALLOWED_TABLES}"
        )
    return table, ts_col


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
    table, ts_col = _validated(table, ts_col)
    if not db_path.exists():
        return 0
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            f"DELETE FROM {table} WHERE {ts_col} < datetime('now', ?)",
            (f"-{retention_days} days",),
        )
        return cur.rowcount or 0
