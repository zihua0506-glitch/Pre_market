"""
core/collect_orderbook.py

每秒并发抓取当日所有上海气温 Polymarket 合约的订单薄快照，
写入 database/orderbook.db。

存储内容（每快照一行）：
  token_id, ts_ms（API时间戳）, captured_at（本地时间）,
  best_bid / best_ask / spread / mid（顶档摘要）,
  last_trade_price,
  bids_json / asks_json（完整档位 JSON，限 top_n 档）

Run:
  python -m core.collect_orderbook                     # 今日合约，运行至 23:59 CST
  python -m core.collect_orderbook --date 2026-05-07
  python -m core.collect_orderbook --top-n 5           # 每侧最多存 5 档（默认 10）
  python -m core.collect_orderbook --interval 1.0      # 轮询间隔秒数（默认 1.0）
  python -m core.collect_orderbook --dry-run           # 打印快照，不写库
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TOKEN_DB   = ROOT / "database" / "token.db"
OB_DB      = ROOT / "database" / "orderbook.db"
CLOB_API   = "https://clob.polymarket.com"
CST        = timezone(timedelta(hours=8))

SCHEMA = """
CREATE TABLE IF NOT EXISTS book_snapshot (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id     TEXT    NOT NULL,
    ts_ms        INTEGER NOT NULL,   -- API 服务端时间戳（毫秒）
    captured_at  INTEGER NOT NULL,   -- 本地抓取时间（毫秒）
    day_date     TEXT    NOT NULL,   -- YYYY-MM-DD（CST）
    kind         TEXT,               -- 'highest' / 'lowest'
    side         TEXT,               -- 'YES' / 'NO'
    bound        TEXT,               -- 'ge' / 'le' / 'eq'
    threshold_c  INTEGER,
    hash         TEXT,
    best_bid     REAL,
    best_ask     REAL,
    best_bid_sz  REAL,
    best_ask_sz  REAL,
    mid          REAL,
    spread       REAL,
    last_trade   REAL,
    bids_json    TEXT,
    asks_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_ob_token_ts   ON book_snapshot(token_id, ts_ms);
CREATE INDEX IF NOT EXISTS idx_ob_captured   ON book_snapshot(captured_at);
CREATE INDEX IF NOT EXISTS idx_ob_day        ON book_snapshot(day_date);
CREATE INDEX IF NOT EXISTS idx_ob_thresh     ON book_snapshot(day_date, kind, threshold_c);
"""


# ── DB helpers ────────────────────────────────────────────────────────────────

def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _insert_batch(conn: sqlite3.Connection, rows: list[dict]) -> None:
    if not rows:
        return
    conn.executemany(
        """INSERT INTO book_snapshot
           (token_id, ts_ms, captured_at, day_date, kind, side, bound, threshold_c,
            hash, best_bid, best_ask, best_bid_sz, best_ask_sz, mid, spread,
            last_trade, bids_json, asks_json)
           VALUES
           (:token_id, :ts_ms, :captured_at, :day_date, :kind, :side, :bound, :threshold_c,
            :hash, :best_bid, :best_ask, :best_bid_sz, :best_ask_sz, :mid, :spread,
            :last_trade, :bids_json, :asks_json)""",
        rows,
    )
    conn.commit()


# ── Token loading ─────────────────────────────────────────────────────────────

def load_tokens_for_date(target_date: date) -> list[dict]:
    """
    从 token.db 加载 target_date 当日的所有 YES+NO token 元数据。
    返回 list[dict]，每条对应一个 token_id（YES 和 NO 分开）。
    """
    if not TOKEN_DB.exists():
        raise FileNotFoundError(f"{TOKEN_DB} not found — run core/fetch_tokens.py first")

    conn = sqlite3.connect(TOKEN_DB)
    sql = """
    SELECT m.yes_token_id, m.no_token_id,
           m.kind, m.bound, m.threshold_c, e.day_date
    FROM markets m
    JOIN events e ON e.id = m.event_id
    WHERE e.day_date = ?
      AND m.yes_token_id IS NOT NULL
      AND m.no_token_id  IS NOT NULL
    ORDER BY m.kind, m.bound, m.threshold_c
    """
    rows = conn.execute(sql, (target_date.isoformat(),)).fetchall()
    conn.close()

    tokens: list[dict] = []
    for yes_id, no_id, kind, bound, threshold_c, day_date in rows:
        for token_id, side in [(yes_id, "YES"), (no_id, "NO")]:
            tokens.append({
                "token_id":    token_id,
                "side":        side,
                "kind":        kind,
                "bound":       bound,
                "threshold_c": threshold_c,
                "day_date":    day_date,
            })
    return tokens


# ── Async fetch ───────────────────────────────────────────────────────────────

async def _fetch_book(session, token_meta: dict, top_n: int) -> Optional[dict]:
    """Fetch /book for one token_id, return parsed snapshot dict or None on error."""
    token_id = token_meta["token_id"]
    url      = f"{CLOB_API}/book"
    captured_ms = int(time.time() * 1000)
    try:
        async with session.get(url, params={"token_id": token_id}, timeout=5) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
    except Exception:
        return None

    bids = data.get("bids", [])
    asks = data.get("asks", [])

    # sort: bids desc (highest first), asks asc (lowest first)
    bids_s = sorted(bids, key=lambda x: float(x["price"]), reverse=True)
    asks_s = sorted(asks, key=lambda x: float(x["price"]))

    best_bid    = float(bids_s[0]["price"]) if bids_s else None
    best_bid_sz = float(bids_s[0]["size"])  if bids_s else None
    best_ask    = float(asks_s[0]["price"]) if asks_s else None
    best_ask_sz = float(asks_s[0]["size"])  if asks_s else None

    mid    = round((best_bid + best_ask) / 2, 5) if best_bid and best_ask else None
    spread = round(best_ask - best_bid, 5)        if best_bid and best_ask else None

    ltp_raw = data.get("last_trade_price")
    last_trade = float(ltp_raw) if ltp_raw not in (None, "", "0") else None

    ts_raw = data.get("timestamp")
    ts_ms  = int(ts_raw) if ts_raw else captured_ms

    return {
        **token_meta,
        "ts_ms":       ts_ms,
        "captured_at": captured_ms,
        "hash":        data.get("hash"),
        "best_bid":    best_bid,
        "best_ask":    best_ask,
        "best_bid_sz": best_bid_sz,
        "best_ask_sz": best_ask_sz,
        "mid":         mid,
        "spread":      spread,
        "last_trade":  last_trade,
        "bids_json":   json.dumps(bids_s[:top_n]),
        "asks_json":   json.dumps(asks_s[:top_n]),
    }


async def fetch_all_books(tokens: list[dict], top_n: int) -> list[dict]:
    """Fetch order books for all tokens concurrently."""
    import aiohttp
    connector = aiohttp.TCPConnector(limit=50, ssl=False)
    headers   = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        tasks   = [_fetch_book(session, tok, top_n) for tok in tokens]
        results = await asyncio.gather(*tasks)
    return [r for r in results if r is not None]


# ── Collector loop ────────────────────────────────────────────────────────────

class OrderBookCollector:

    def __init__(
        self,
        target_date: date,
        interval:    float = 1.0,
        top_n:       int   = 10,
        dry_run:     bool  = False,
    ):
        self.target_date = target_date
        self.interval    = interval
        self.top_n       = top_n
        self.dry_run     = dry_run

        self.tokens = load_tokens_for_date(target_date)
        if not self.tokens:
            raise RuntimeError(
                f"No tokens found for {target_date}. "
                "Run: python -m core.fetch_tokens"
            )

        self.conn = None if dry_run else init_db(OB_DB)
        self._n_snapshots  = 0
        self._n_cycles     = 0
        self._n_errors     = 0
        self._t_start      = time.time()

        print(
            f"[collect_orderbook] {target_date}  "
            f"tokens={len(self.tokens)}  "
            f"interval={interval}s  top_n={top_n}  "
            f"{'DRY-RUN' if dry_run else f'=> {OB_DB.name}'}"
        )

    def _print_cycle(self, rows: list[dict], elapsed_ms: float) -> None:
        """Print a brief summary line each cycle."""
        n_ok = len(rows)
        # Pick one representative token for display (highest ge with best data)
        sample = next((r for r in rows if r["kind"] == "highest" and r["side"] == "YES"
                       and r["mid"] is not None), None)
        sample_str = ""
        if sample:
            sample_str = (
                f"  eg.highest{sample['threshold_c']}YES "
                f"bid={sample['best_bid']:.3f} ask={sample['best_ask']:.3f} "
                f"mid={sample['mid']:.3f}"
            )
        ts = datetime.now(CST).strftime("%H:%M:%S")
        print(f"  [{ts}] cycle={self._n_cycles:5d}  ok={n_ok}/{len(self.tokens)}  "
              f"fetch={elapsed_ms:.0f}ms{sample_str}")

    async def run_async(self) -> None:
        market_close = datetime(
            self.target_date.year, self.target_date.month, self.target_date.day,
            23, 59, tzinfo=CST,
        )
        print(f"  Running until {market_close.strftime('%H:%M')} CST. Ctrl+C to stop.\n")

        stats_every = max(1, int(60 / self.interval))  # print stats every ~60s

        while datetime.now(CST) < market_close:
            t0 = time.perf_counter()

            rows = await fetch_all_books(self.tokens, self.top_n)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            self._n_cycles    += 1
            self._n_snapshots += len(rows)
            self._n_errors    += len(self.tokens) - len(rows)

            if self.dry_run:
                self._print_dry(rows)
            else:
                _insert_batch(self.conn, rows)

            if self._n_cycles % stats_every == 0:
                self._print_cycle(rows, elapsed_ms)

            # Sleep the remainder of the interval
            sleep_s = max(0.0, self.interval - (time.perf_counter() - t0))
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)

        self._print_summary()

    def _print_dry(self, rows: list[dict]) -> None:
        ts = datetime.now(CST).strftime("%H:%M:%S")
        print(f"\n[{ts}] {len(rows)} snapshots:")
        for r in rows[:4]:
            print(
                f"  {r['kind']:7s} {r['bound']:2s}{r['threshold_c']:3d}C "
                f"{r['side']:3s}  "
                f"bid={r['best_bid']}  ask={r['best_ask']}  "
                f"mid={r['mid']}  last={r['last_trade']}"
            )
        if len(rows) > 4:
            print(f"  ... +{len(rows)-4} more")

    def _print_summary(self) -> None:
        elapsed = time.time() - self._t_start
        rate    = self._n_snapshots / elapsed if elapsed > 0 else 0
        print(
            f"\n[collect_orderbook] Done. "
            f"cycles={self._n_cycles}  "
            f"snapshots={self._n_snapshots}  "
            f"errors={self._n_errors}  "
            f"elapsed={elapsed:.0f}s  "
            f"rate={rate:.1f} snaps/s"
        )
        if self.conn:
            self.conn.close()

    def run(self) -> None:
        try:
            asyncio.run(self.run_async())
        except KeyboardInterrupt:
            print("\n[collect_orderbook] Interrupted.")
            self._print_summary()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(description="Collect Polymarket ZSPD order book snapshots")
    p.add_argument("--date",     default=None,
                   help="Target date YYYY-MM-DD (default: today CST)")
    p.add_argument("--interval", type=float, default=1.0,
                   help="Polling interval in seconds (default: 1.0)")
    p.add_argument("--top-n",    type=int,   default=10,
                   help="Max order book levels to store per side (default: 10)")
    p.add_argument("--dry-run",  action="store_true",
                   help="Print snapshots without writing to DB")
    args = p.parse_args(argv[1:])

    target_date = (date.fromisoformat(args.date) if args.date
                   else datetime.now(CST).date())

    collector = OrderBookCollector(
        target_date = target_date,
        interval    = args.interval,
        top_n       = args.top_n,
        dry_run     = args.dry_run,
    )
    collector.run()


if __name__ == "__main__":
    main(sys.argv)
