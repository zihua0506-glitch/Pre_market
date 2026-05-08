import sqlite3
from pathlib import Path
from collector import db


def test_init_creates_tables(tmp_db_dir: Path):
    db.init(tmp_db_dir)
    conn = sqlite3.connect(tmp_db_dir / "markets.db")
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    assert ("markets",) in rows


def test_init_is_idempotent(tmp_db_dir: Path):
    db.init(tmp_db_dir)
    db.init(tmp_db_dir)
    conn = sqlite3.connect(tmp_db_dir / "orderbook.db")
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    assert ("l2_snapshot",) in rows


def test_upsert_market_round_trip(tmp_db_dir: Path):
    db.init(tmp_db_dir)
    db.upsert_market(
        tmp_db_dir,
        market_id="m1",
        slug="zspd-high-may-7-25c",
        question="Will ZSPD high be ≥25 °C on May 7?",
        end_date="2026-05-07T23:59:00Z",
        tag="weather",
        city="ZSPD",
        contract_type="high_ge",
        threshold_c=25.0,
        threshold_text="≥25",
        token_id_yes="t-yes",
        token_id_no="t-no",
    )
    rows = db.list_active_markets(tmp_db_dir)
    assert len(rows) == 1
    assert rows[0]["slug"] == "zspd-high-may-7-25c"
