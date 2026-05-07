"""
strategies/plain_as.py

Live certainty-only NO arbitrage for ZSPD Polymarket contracts.

Logic:
  Every half-hour, fetch the latest temperature from standard.db.
  Track obs_tmax_so_far and obs_tmin_so_far (monotone running max/min).
  When obs_tmax crosses a new settlement boundary S = floor(tmax+0.5):
    → Buy NO on tmax eq/le with threshold <= (S-1)
  When obs_tmin crosses a new settlement boundary S = floor(tmin+0.5):
    → Buy NO on tmin ge/eq with threshold >= (S+1)
  No position limits — eats full NO ask depth.
  No YES signals, no model signals.

Order mechanics:
  Buy NO = place BUY on the NO token (token_meta side='NO').
  Eat the NO token's full ask depth from cheapest upward.

Data flow:
  strategies/config.json  → which bounds / threshold ranges to trade
  data.db (token_meta)    → YES+NO token_ids for today's contracts
  standard.db             → half-hourly WU temperatures
  order_book_fn (inject)  → live NO token order book
  place_order_fn (inject) → sends signed BUY order to CLOB

Usage:
  python -m strategies.plain_as [--date YYYY-MM-DD] [--dry-run]
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

ROOT         = Path(__file__).resolve().parent.parent
CONFIG_PATH  = Path(__file__).resolve().parent / "config.json"
sys.path.insert(0, str(ROOT))

from ml.config import WU_ZSPD_DB

DATA_DB = ROOT / "database" / "data.db"
CST     = timezone(timedelta(hours=8))


# ── Data types ──────────────────────────────────────────────────────────────

@dataclass
class ContractMeta:
    yes_token_id: str
    no_token_id:  str
    kind:         str    # 'highest' (tmax) | 'lowest' (tmin)
    bound:        str    # 'ge' | 'le' | 'eq'
    threshold_c:  int


@dataclass
class BookLevel:
    price: float   # token price ∈ (0, 1)
    size:  float   # contracts available at this price


@dataclass
class OrderBook:
    """Order book for a NO token."""
    bids: list[BookLevel]   # NO buyers, sorted desc (highest bid first)
    asks: list[BookLevel]   # NO sellers, sorted asc  (cheapest ask first) ← we eat these


@dataclass
class Order:
    token_id:    str    # NO token_id
    side:        str    # always 'BUY' in this strategy
    size:        float
    limit_price: float  # max price we pay for NO
    note:        str = ""


# ── Config ───────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


# ── Database helpers ─────────────────────────────────────────────────────────

def _load_contracts(target_date: date, cfg: dict) -> list[ContractMeta]:
    """
    JOIN YES and NO rows from token_meta by condition_id.
    Filter by bounds and threshold ranges from config.json.
    """
    if not DATA_DB.exists():
        print(f"  [warn] data.db not found at {DATA_DB}")
        return []

    c_cfg      = cfg.get("contracts", {})
    tmax_cfg   = c_cfg.get("tmax", {})
    tmin_cfg   = c_cfg.get("tmin", {})
    tmax_bounds = set(tmax_cfg.get("bounds", ["eq", "le"]))
    tmin_bounds = set(tmin_cfg.get("bounds", ["ge", "eq"]))
    tmax_min   = tmax_cfg.get("threshold_min", 0)
    tmax_max   = tmax_cfg.get("threshold_max", 99)
    tmin_min   = tmin_cfg.get("threshold_min", -99)
    tmin_max   = tmin_cfg.get("threshold_max", 99)

    conn = sqlite3.connect(DATA_DB)
    rows = conn.execute(
        """SELECT y.token_id, n.token_id, y.kind, y.bound, y.threshold_c
           FROM token_meta y
           JOIN token_meta n USING(condition_id, kind, bound, threshold_c, day_date)
           WHERE y.side = 'YES' AND n.side = 'NO' AND y.day_date = ?
           ORDER BY y.kind, y.bound, y.threshold_c""",
        (target_date.isoformat(),),
    ).fetchall()
    conn.close()

    result: list[ContractMeta] = []
    for yes_id, no_id, kind, bound, threshold_c in rows:
        if kind == "highest":
            if bound not in tmax_bounds:
                continue
            if not (tmax_min <= threshold_c <= tmax_max):
                continue
        elif kind == "lowest":
            if bound not in tmin_bounds:
                continue
            if not (tmin_min <= threshold_c <= tmin_max):
                continue
        result.append(ContractMeta(yes_id, no_id, kind, bound, int(threshold_c)))

    return result


def _run_wu_update() -> None:
    """Pull latest WU observations into standard.db only."""
    from ml.data.fetch_wu_latest import refresh_today_wu

    rows = refresh_today_wu()
    if rows <= 0:
        raise RuntimeError("WU refresh returned no rows")


def _query_slot_temp(target_date: date, slot_h: float) -> Optional[float]:
    """
    Query standard.db for the observation closest to slot_h (within ±15 min).
    """
    if not WU_ZSPD_DB.exists():
        return None
    midnight_cst  = datetime(target_date.year, target_date.month, target_date.day, tzinfo=CST)
    center_epoch  = int((midnight_cst + timedelta(hours=slot_h)).timestamp())
    conn = sqlite3.connect(WU_ZSPD_DB)
    row  = conn.execute(
        """SELECT temp_c FROM zspd_observations
           WHERE obs_date_local = ?
             AND valid_time_gmt BETWEEN ? AND ?
             AND temp_c IS NOT NULL
           ORDER BY ABS(valid_time_gmt - ?) LIMIT 1""",
        (target_date.isoformat(), center_epoch - 900, center_epoch + 900, center_epoch),
    ).fetchone()
    conn.close()
    return float(row[0]) if row else None


def _load_day_temps(target_date: date, up_to_slot_h: float) -> list[float]:
    """Return all temp_c values observed today up to (and including) slot_h."""
    if not WU_ZSPD_DB.exists():
        return []
    midnight_cst = datetime(target_date.year, target_date.month, target_date.day, tzinfo=CST)
    day_start    = int(midnight_cst.timestamp())
    cutoff       = int((midnight_cst + timedelta(hours=up_to_slot_h + 0.5)).timestamp())
    conn = sqlite3.connect(WU_ZSPD_DB)
    rows = conn.execute(
        """SELECT temp_c FROM zspd_observations
           WHERE obs_date_local = ?
             AND valid_time_gmt BETWEEN ? AND ?
             AND temp_c IS NOT NULL
           ORDER BY valid_time_gmt""",
        (target_date.isoformat(), day_start, cutoff),
    ).fetchall()
    conn.close()
    return [float(r[0]) for r in rows]


# ── Strategy ─────────────────────────────────────────────────────────────────

class PlainArbitrageStrategy:
    """
    Certainty-only NO arbitrage.

    Wakes up at HH:01 and HH:31 CST each day.
    On each wake:
      1. Fetch temperature for the preceding slot from standard.db.
      2. Update running obs_tmax_so_far / obs_tmin_so_far.
      3. If a new settlement boundary is crossed:
           tmax: eat all NO asks for eq/le with threshold <= (S-1)
           tmin: eat all NO asks for ge/eq with threshold >= (S+1)
    Each token is executed at most once per day.
    """

    def __init__(
        self,
        target_date:    date,
        order_book_fn:  Callable[[str], OrderBook],
        place_order_fn: Callable[[Order], None],
        cfg:            Optional[dict] = None,
    ):
        self.target_date = target_date
        self._get_book   = order_book_fn
        self._place      = place_order_fn

        cfg = cfg or _load_config()
        self.contracts   = _load_contracts(target_date, cfg)

        self._tmax_so_far: float = -math.inf
        self._tmin_so_far: float =  math.inf
        self._last_tmax_s: int   = -999   # last settled boundary that fired for tmax
        self._last_tmin_s: int   =  999   # last settled boundary that fired for tmin
        self._fired: set[str]    = set()  # no_token_ids already executed today

        print(f"[plain_as] {target_date}  contracts={len(self.contracts)}")

    # ── Main loop ────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Block until 23:59 CST, waking every half-hour to check for signals."""
        market_close = datetime(
            self.target_date.year, self.target_date.month, self.target_date.day,
            23, 59, tzinfo=CST,
        )
        print(f"  Market closes {market_close.strftime('%H:%M')} CST.\n")

        while True:
            now = datetime.now(CST)
            if now >= market_close:
                print("[plain_as] Market closed. Done.")
                break
            slot_h = self._current_slot_h(now)
            self._process_slot(slot_h)
            self._sleep_to_next(now)

    def _current_slot_h(self, now: datetime) -> float:
        """
        Half-hour slot whose observation is now available.
          HH:01–HH:30  →  HH:00  (slot_h = HH.0)
          HH:31–HH:59  →  HH:30  (slot_h = HH.5)
        """
        return float(now.hour) if now.minute < 31 else now.hour + 0.5

    def _sleep_to_next(self, now: datetime) -> None:
        """Sleep until 1 minute past the next half-hour mark."""
        if now.minute < 31:
            wake_h, wake_m = now.hour, 31
        else:
            wake_h, wake_m = now.hour + 1, 1
        if wake_h >= 24:
            return
        wake = now.replace(hour=wake_h, minute=wake_m, second=5, microsecond=0)
        secs = (wake - datetime.now(CST)).total_seconds()
        if secs > 0:
            print(f"  sleeping {secs:.0f}s → next check {wake.strftime('%H:%M')} CST\n")
            time.sleep(secs)

    # ── Per-slot processing ──────────────────────────────────────────────────

    def _process_slot(self, slot_h: float) -> None:
        h, m = int(slot_h), int((slot_h % 1) * 60)
        print(f"── slot {h:02d}:{m:02d} ───────────────────────────────")

        temp_c = self._fetch_temp(slot_h)
        if temp_c is None:
            print(f"  No temperature for {h:02d}:{m:02d}. Skipping slot.")
            return

        all_temps = _load_day_temps(self.target_date, slot_h)
        if all_temps:
            self._tmax_so_far = max(all_temps)
            self._tmin_so_far = min(all_temps)
        else:
            self._tmax_so_far = max(self._tmax_so_far, temp_c)
            self._tmin_so_far = min(self._tmin_so_far, temp_c)

        print(
            f"  temp={temp_c:+.1f}°C  "
            f"tmax_so_far={self._tmax_so_far:.1f}  "
            f"tmin_so_far={self._tmin_so_far:.1f}"
        )

        self._check_tmax_boundary()
        self._check_tmin_boundary()

    def _fetch_temp(self, slot_h: float) -> Optional[float]:
        """
        1. Query standard.db.
        2. Missing → pull latest WU observations into standard.db and retry.
        3. Still missing → prompt operator for manual input.
        """
        temp = _query_slot_temp(self.target_date, slot_h)
        if temp is not None:
            return temp

        h, m = int(slot_h), int((slot_h % 1) * 60)
        print(f"  [{h:02d}:{m:02d}] Not in DB. Running WU update …")
        try:
            for attempt in range(1, 4):
                _run_wu_update()
                temp = _query_slot_temp(self.target_date, slot_h)
                if temp is not None:
                    print(f"  Updated. temp={temp:.1f}°C (attempt {attempt}/3)")
                    return temp
                if attempt < 3:
                    print(f"  Slot still missing after attempt {attempt}/3. Retrying in 5s ...")
                    time.sleep(5)
            print("  Update succeeded but slot still missing.")
        except Exception as e:
            print(f"  Update error: {e}")

        return self._prompt_manual(slot_h)

    @staticmethod
    def _prompt_manual(slot_h: float) -> Optional[float]:
        h, m = int(slot_h), int((slot_h % 1) * 60)
        print(f"\n  ┌─ MANUAL INPUT REQUIRED ──────────────────────")
        print(f"  │  Temperature at {h:02d}:{m:02d} CST is unavailable.")
        print(f"  │  Format: decimal °C  e.g. 24.5")
        print(f"  │  Press Enter to skip this slot.")
        print(f"  └───────────────────────────────────────────────")
        while True:
            raw = input("  > ").strip()
            if raw == "":
                print("  Slot skipped.")
                return None
            try:
                v = float(raw)
                if -20.0 <= v <= 50.0:
                    print(f"  Accepted: {v:.1f}°C")
                    return v
                print(f"  {v} is outside [-20, 50]. Try again.")
            except ValueError:
                print("  Invalid. Enter a number like '24.5'.")

    # ── Boundary detection ───────────────────────────────────────────────────

    def _check_tmax_boundary(self) -> None:
        if self._tmax_so_far == -math.inf:
            return
        s = math.floor(self._tmax_so_far + 0.5)
        if s <= self._last_tmax_s:
            return
        self._last_tmax_s = s
        threshold = s - 1
        print(f"  ★ tmax settled≥{s}  →  firing NO on tmax thresholds <= {threshold}")
        self._fire_tmax_no(threshold)

    def _check_tmin_boundary(self) -> None:
        if self._tmin_so_far == math.inf:
            return
        s = math.floor(self._tmin_so_far + 0.5)
        if s >= self._last_tmin_s:
            return
        self._last_tmin_s = s
        threshold = s + 1
        print(f"  ★ tmin settled≤{s}  →  firing NO on tmin thresholds >= {threshold}")
        self._fire_tmin_no(threshold)

    # ── Signal execution ─────────────────────────────────────────────────────

    def _fire_tmax_no(self, threshold: int) -> None:
        for tok in self.contracts:
            if tok.kind != "highest" or tok.threshold_c > threshold:
                continue
            if tok.bound not in ("eq", "le") or tok.no_token_id in self._fired:
                continue
            self._eat_no(tok)
            self._fired.add(tok.no_token_id)

    def _fire_tmin_no(self, threshold: int) -> None:
        for tok in self.contracts:
            if tok.kind != "lowest" or tok.threshold_c < threshold:
                continue
            if tok.bound not in ("ge", "eq") or tok.no_token_id in self._fired:
                continue
            self._eat_no(tok)
            self._fired.add(tok.no_token_id)

    def _eat_no(self, tok: ContractMeta) -> None:
        """
        Eat the NO token's entire ask side.
        Each ask level: BUY NO at limit = ask price.
        """
        label = f"{tok.kind} {tok.bound}_{tok.threshold_c}"
        try:
            book = self._get_book(tok.no_token_id)
        except Exception as e:
            print(f"    [{label}] order book error: {e}")
            return

        if not book.asks:
            print(f"    [{label}] no NO asks — skipped")
            return

        best_ask = book.asks[0].price

        total_size = 0.0
        for level in book.asks:
            order = Order(
                token_id    = tok.no_token_id,
                side        = "BUY",
                size        = level.size,
                limit_price = level.price,
                note        = f"{label} eat@NO{level.price:.3f}",
            )
            self._place(order)
            total_size += level.size

        print(
            f"    [{label}]  "
            f"ate {total_size:.1f} contracts  "
            f"best_no_ask={best_ask:.3f}  "
            f"edge≥{1.0 - best_ask:.3f}"
        )


# ── CLI helpers ──────────────────────────────────────────────────────────────

def _dry_run_book(token_id: str) -> OrderBook:
    """
    Placeholder NO token order book.
    Simulates a contract where YES is ~12% (market still pricing non-zero chance),
    so NO asks are around 0.85–0.92 — profitable for certainty arbitrage.
    """
    return OrderBook(
        bids=[BookLevel(0.83, 200.0), BookLevel(0.80, 400.0)],
        asks=[BookLevel(0.88, 120.0), BookLevel(0.91, 250.0), BookLevel(0.94, 500.0)],
    )


def _dry_run_place(order: Order) -> None:
    print(
        f"      [DRY-RUN] {order.side} NO  "
        f"size={order.size:.1f}  "
        f"limit={order.limit_price:.4f}  "
        f"token={order.token_id[:10]}…  "
        f"({order.note})"
    )


# ── Entry point ──────────────────────────────────────────────────────────────

def main(argv: list[str]) -> None:
    import argparse
    p = argparse.ArgumentParser(description="plain_as — certainty NO arbitrage")
    p.add_argument("--date",    default=None,
                   help="Target date YYYY-MM-DD (default: today CST)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print orders without executing (uses placeholder order book)")
    args = p.parse_args(argv[1:])

    target_date = (date.fromisoformat(args.date) if args.date
                   else datetime.now(CST).date())

    if args.dry_run:
        print("[DRY-RUN] Placeholder NO order book, no real orders.\n")
        book_fn  = _dry_run_book
        place_fn = _dry_run_place
    else:
        raise NotImplementedError(
            "Implement live order_book_fn and place_order_fn using py-clob-client.\n"
            "Load credentials from .env:  POLY_PRIVATE_KEY, POLY_API_KEY, "
            "POLY_API_SECRET, POLY_API_PASSPHRASE."
        )

    cfg = _load_config()
    strategy = PlainArbitrageStrategy(
        target_date    = target_date,
        order_book_fn  = book_fn,
        place_order_fn = place_fn,
        cfg            = cfg,
    )
    strategy.run()


if __name__ == "__main__":
    main(sys.argv)
