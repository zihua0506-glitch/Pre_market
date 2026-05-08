"""Daily roll: VM + SQLite -> parquet -> rclone -> cleanup."""
from __future__ import annotations
import asyncio
import logging
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import polars as pl
from archiver import cleanup
from archiver.config import load
from archiver.vm_export import export_day

log = logging.getLogger(__name__)


def _dump_sqlite_to_parquet(db_path: Path, table: str, ts_col: str,
                             day: date, dest: Path) -> int:
    if not db_path.exists():
        return 0
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    end = (datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
           + timedelta(days=1)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE {ts_col} >= ? AND {ts_col} < ?",
            (start, end),
        ).fetchall()
    if not rows:
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([dict(r) for r in rows]).write_parquet(dest, compression="zstd")
    return len(rows)


def _dump_markets_snapshot(db_path: Path, dest: Path) -> int:
    if not db_path.exists():
        return 0
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM markets").fetchall()
    if not rows:
        return 0
    pl.DataFrame([dict(r) for r in rows]).write_parquet(dest, compression="zstd")
    return len(rows)


def _rclone_sync(src: Path, remote: str) -> None:
    day = src.name  # YYYY-MM-DD
    y, mo, d = day.split("-")
    target = f"{remote}/{y}/{mo}/{d}"
    log.info("rclone copy %s -> %s", src, target)
    cmd = ["rclone", "copy", "--checksum", str(src), target]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if res.returncode != 0:
        raise RuntimeError(f"rclone failed: {res.stderr[:500]}")


async def run() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load()
    yesterday = date.today() - timedelta(days=1)
    out_dir = cfg.warm_dir / yesterday.isoformat()

    log.info("export VM ticks for %s", yesterday)
    n_prices = await export_day(cfg.vm_url, yesterday, out_dir / "prices.parquet")
    log.info("  %d price rows", n_prices)

    n_ob = _dump_sqlite_to_parquet(cfg.db_dir / "orderbook.db",
                                   "l2_snapshot", "ts", yesterday, out_dir / "orderbook.parquet")
    log.info("  %d L2 rows", n_ob)
    n_tr = _dump_sqlite_to_parquet(cfg.db_dir / "trades.db",
                                   "trades", "ts", yesterday, out_dir / "trades.parquet")
    log.info("  %d trade rows", n_tr)
    n_mk = _dump_markets_snapshot(cfg.db_dir / "markets.db", out_dir / "markets.parquet")
    log.info("  %d markets snapshot rows", n_mk)

    if n_prices == 0 and n_ob == 0 and n_tr == 0:
        log.warning("no data for %s; aborting before cold sync", yesterday)
        return 0

    _rclone_sync(out_dir, cfg.rclone_remote)

    # cleanup
    for p in cleanup.old_warm_dirs(cfg.warm_dir, cfg.warm_retention_days):
        log.info("removing old warm dir %s", p)
        shutil.rmtree(p)
    n_ob_d = cleanup.delete_old_sqlite_rows(cfg.db_dir / "orderbook.db",
                                            "l2_snapshot", "ts", cfg.hot_retention_days)
    n_tr_d = cleanup.delete_old_sqlite_rows(cfg.db_dir / "trades.db",
                                            "trades", "ts", cfg.hot_retention_days)
    log.info("cleaned %d L2 rows, %d trade rows from hot", n_ob_d, n_tr_d)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
