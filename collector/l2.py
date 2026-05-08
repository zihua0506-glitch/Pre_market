"""5-min L2 orderbook polling + trade persistence."""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
import httpx
from collector import db
from collector.config import Config

log = logging.getLogger(__name__)


def persist_snapshot(db_dir: Path, *, ts: str, market_id: str, side: str, levels: Any) -> None:
    with db.connect(db_dir, "orderbook.db") as conn:
        conn.execute(
            "INSERT OR REPLACE INTO l2_snapshot(ts, market_id, side, levels) VALUES (?,?,?,?)",
            (ts, market_id, side, json.dumps(levels)),
        )


def persist_trade(db_dir: Path, *, ts: str, market_id: str, side: str,
                  price: float, size: float, taker_side: str) -> None:
    with db.connect(db_dir, "trades.db") as conn:
        conn.execute(
            "INSERT INTO trades(ts, market_id, side, price, size, taker_side) VALUES (?,?,?,?,?,?)",
            (ts, market_id, side, price, size, taker_side),
        )


async def _fetch_book(http: httpx.AsyncClient, clob_host: str, token_id: str) -> dict:
    r = await http.get(f"{clob_host}/book", params={"token_id": token_id}, timeout=15.0)
    r.raise_for_status()
    return r.json()


async def l2_loop(cfg: Config, stop: asyncio.Event) -> None:
    while not stop.is_set():
        markets = db.list_active_markets(cfg.db_dir)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        async with httpx.AsyncClient() as http:
            for m in markets:
                for side, tok in (("yes", m["token_id_yes"]), ("no", m["token_id_no"])):
                    if not tok:
                        continue
                    try:
                        book = await _fetch_book(http, cfg.clob_host, tok)
                    except Exception:
                        log.exception("L2 fetch failed for %s/%s", m["market_id"], side)
                        continue
                    bids = [[float(b["price"]), float(b["size"])] for b in book.get("bids", [])][:10]
                    asks = [[float(a["price"]), float(a["size"])] for a in book.get("asks", [])][:10]
                    persist_snapshot(
                        cfg.db_dir, ts=ts, market_id=m["market_id"], side=side,
                        levels={"bids": bids, "asks": asks},
                    )
        try:
            await asyncio.wait_for(stop.wait(), timeout=cfg.l2_interval_s)
        except asyncio.TimeoutError:
            pass
