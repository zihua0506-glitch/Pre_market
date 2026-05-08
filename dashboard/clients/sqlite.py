"""Read helpers for collector SQLite DBs (read-only)."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path


def latest_l2(db_dir: Path, market_id: str) -> dict:
    """Return latest yes/no L2 snapshot dicts."""
    out: dict[str, dict] = {}
    if not (db_dir / "orderbook.db").exists():
        return out
    with sqlite3.connect(db_dir / "orderbook.db") as conn:
        for side in ("yes", "no"):
            row = conn.execute(
                "SELECT levels FROM l2_snapshot WHERE market_id=? AND side=? "
                "ORDER BY ts DESC LIMIT 1", (market_id, side)
            ).fetchone()
            if row:
                out[side] = json.loads(row[0])
    return out


def recent_trades(db_dir: Path, market_id: str, n: int = 50) -> list[dict]:
    if not (db_dir / "trades.db").exists():
        return []
    with sqlite3.connect(db_dir / "trades.db") as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trades WHERE market_id=? ORDER BY ts DESC LIMIT ?",
            (market_id, n),
        ).fetchall()
    return [dict(r) for r in rows]


def market_meta(db_dir: Path, market_id: str) -> dict | None:
    if not (db_dir / "markets.db").exists():
        return None
    with sqlite3.connect(db_dir / "markets.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM markets WHERE market_id=?", (market_id,)
        ).fetchone()
    return dict(row) if row else None
