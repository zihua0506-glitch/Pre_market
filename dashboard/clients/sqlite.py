"""Read helpers for collector SQLite DBs (read-only)."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path


def _open(path: Path) -> sqlite3.Connection:
    """Open a SQLite DB with WAL mode and a 10s timeout to coexist with collector writers."""
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def latest_l2(db_dir: Path, market_id: str) -> dict:
    """Return latest yes/no L2 snapshot dicts."""
    out: dict[str, dict] = {}
    if not (db_dir / "orderbook.db").exists():
        return out
    with _open(db_dir / "orderbook.db") as conn:
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
    with _open(db_dir / "trades.db") as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE market_id=? ORDER BY ts DESC LIMIT ?",
            (market_id, n),
        ).fetchall()
    return [dict(r) for r in rows]


def market_meta(db_dir: Path, market_id: str) -> dict | None:
    if not (db_dir / "markets.db").exists():
        return None
    with _open(db_dir / "markets.db") as conn:
        row = conn.execute(
            "SELECT * FROM markets WHERE market_id=?", (market_id,)
        ).fetchone()
    return dict(row) if row else None


def all_markets(db_dir: Path) -> dict[str, dict]:
    """Return {market_id: row} for every active market. Empty dict if no DB yet."""
    if not (db_dir / "markets.db").exists():
        return {}
    with _open(db_dir / "markets.db") as conn:
        rows = conn.execute(
            "SELECT * FROM markets WHERE archived=0"
        ).fetchall()
    return {r["market_id"]: dict(r) for r in rows}
