#!/usr/bin/env python3
"""Print a JSON snapshot of valkyrie's zspd-strategy state. Read-only."""
from __future__ import annotations
import json
import os
import subprocess
import sqlite3
from pathlib import Path

ROOT = Path(os.getenv("ZSPD_ROOT", "/opt/zspd-trader/src"))


def systemd_state(unit: str) -> dict:
    out = subprocess.run(
        ["systemctl", "show", unit, "--property=ActiveState,SubState,MainPID"],
        capture_output=True, text=True, check=False,
    )
    kv = dict(
        line.split("=", 1) for line in out.stdout.strip().splitlines() if "=" in line
    )
    return kv


def positions_today() -> list[dict]:
    db = ROOT / "database" / "data.db"
    if not db.exists():
        return []
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM positions WHERE date(ts)=date('now')"
        ).fetchall() if _has_table(conn, "positions") else []
    return [dict(r) for r in rows]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def main() -> None:
    print(json.dumps({
        "strategy": systemd_state("zspd-strategy.service"),
        "hot_refresh": systemd_state("zspd-hot-refresh.timer"),
        "clob_proxy": systemd_state("clob-proxy.service"),
        "positions_today": positions_today(),
    }, indent=2))


if __name__ == "__main__":
    main()
