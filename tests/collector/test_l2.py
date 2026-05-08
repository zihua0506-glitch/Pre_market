import json
import sqlite3
from pathlib import Path
from collector import db, l2


def test_persist_l2_snapshot(tmp_db_dir: Path):
    db.init(tmp_db_dir)
    l2.persist_snapshot(
        tmp_db_dir,
        ts="2026-05-08T03:00:00Z",
        market_id="m1",
        side="yes",
        levels={"bids": [[0.42, 1000.0]], "asks": [[0.45, 800.0]]},
    )
    with sqlite3.connect(tmp_db_dir / "orderbook.db") as conn:
        rows = conn.execute("SELECT * FROM l2_snapshot").fetchall()
    assert len(rows) == 1
    parsed = json.loads(rows[0][3])
    assert parsed["bids"] == [[0.42, 1000.0]]


def test_persist_trade(tmp_db_dir: Path):
    db.init(tmp_db_dir)
    l2.persist_trade(
        tmp_db_dir,
        ts="2026-05-08T03:00:01Z",
        market_id="m1",
        side="yes",
        price=0.42,
        size=100.0,
        taker_side="buy",
    )
    with sqlite3.connect(tmp_db_dir / "trades.db") as conn:
        rows = conn.execute("SELECT * FROM trades").fetchall()
    assert len(rows) == 1
    assert rows[0][3] == 0.42  # price
