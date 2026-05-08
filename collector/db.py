"""SQLite schema + connection helpers for collector."""
from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_SCHEMAS = {
    "markets.db": """
        CREATE TABLE IF NOT EXISTS markets (
            market_id      TEXT PRIMARY KEY,
            slug           TEXT UNIQUE NOT NULL,
            question       TEXT,
            end_date       TEXT,
            tag            TEXT,
            city           TEXT,
            contract_type  TEXT,
            threshold_c    REAL,
            threshold_text TEXT,
            token_id_yes   TEXT,
            token_id_no    TEXT,
            first_seen     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen      TIMESTAMP,
            archived       INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_markets_end_date ON markets(end_date);
        CREATE INDEX IF NOT EXISTS idx_markets_city     ON markets(city);
    """,
    "orderbook.db": """
        CREATE TABLE IF NOT EXISTS l2_snapshot (
            ts         TIMESTAMP NOT NULL,
            market_id  TEXT NOT NULL,
            side       TEXT CHECK(side IN ('yes','no')),
            levels     TEXT NOT NULL,
            PRIMARY KEY(ts, market_id, side)
        );
    """,
    "trades.db": """
        CREATE TABLE IF NOT EXISTS trades (
            ts          TIMESTAMP NOT NULL,
            market_id   TEXT NOT NULL,
            side        TEXT,
            price       REAL,
            size        REAL,
            taker_side  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_trades_market_ts ON trades(market_id, ts);
    """,
}


def init(db_dir: Path) -> None:
    db_dir.mkdir(parents=True, exist_ok=True)
    for filename, ddl in _SCHEMAS.items():
        with sqlite3.connect(db_dir / filename) as conn:
            conn.executescript(ddl)


@contextmanager
def connect(db_dir: Path, name: str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_dir / name, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_market(db_dir: Path, **fields) -> None:
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(f":{k}" for k in fields)
    update_set = ", ".join(
        f"{k}=excluded.{k}" for k in fields if k != "market_id"
    )
    sql = (
        f"INSERT INTO markets ({cols}, last_seen) VALUES ({placeholders}, CURRENT_TIMESTAMP) "
        f"ON CONFLICT(market_id) DO UPDATE SET {update_set}, last_seen=CURRENT_TIMESTAMP"
    )
    with connect(db_dir, "markets.db") as conn:
        conn.execute(sql, fields)


def list_active_markets(db_dir: Path) -> list[dict]:
    with connect(db_dir, "markets.db") as conn:
        rows = conn.execute(
            "SELECT * FROM markets WHERE archived=0 ORDER BY end_date"
        ).fetchall()
    return [dict(r) for r in rows]
