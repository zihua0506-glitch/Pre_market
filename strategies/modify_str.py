"""
strategies/modify_str.py

Live v5/v3 strategy: Certainty-First + Model Edge for ZSPD Polymarket contracts.

Signal Tier 1 — Certainty (identical logic to plain_as.py):
  Physics-locked P=0 or P=1 from intraday observations.
  Enter ONCE per token per day (first slot the certainty is established).
  YES (P=1): BUY YES tokens if edge > 0.02
  NO  (P=0): BUY NO  tokens if YES > 0.01  (eat ask depth up to 50 contracts)

Signal Tier 2 — Model Edge (v5/v3 LightGBM quantile distribution):
  edge = model_prob - YES_mid_price  (mid = (best_bid + best_ask) / 2)
  Enter if |edge| >= min_edge (default 0.05).
  cold_start (slot_h < 8): tmax signals disabled.
  ge-YES extra filter: require edge >= 0.30  (tail over-estimation risk).
  Do NOT re-enter tokens that already have a live certainty position.

Position sizing (base_size default $1):
  Certainty YES: |edge| >= 0.50 -> 5x  /  >= 0.20 -> 3x  /  >= 0.02 -> 2x
  Certainty NO : fixed 50 contracts (eat ask depth level by level)
  Model        : |edge| >= 0.20 -> 3x  /  >= 0.10 -> 2x  /  >= 0.05 -> 1x

Data sources:
  standard.db     -- WU half-hourly observations (intraday features)
  weather_om.db   -- ERA5 archive / GFS forecast (v5 ERA5 features)
  daily_labels.db -- historical tmax/tmin labels (lag/roll features)
  data.db         -- today's YES+NO token IDs (token_meta)

Before running, ensure today's GFS forecast is loaded:
  python -m core.fetch_weather --forecast-only

Usage:
  python -m strategies.modify_str --dry-run
  python -m strategies.modify_str --dry-run --version v3
  python -m strategies.modify_str --date 2026-05-07 --version v5 --dry-run
  python -m strategies.modify_str --version v5   # live (requires py-clob-client)
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

ROOT        = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
sys.path.insert(0, str(ROOT))

from ml.config import LABELS_DB, WEATHER_OM_DB, WU_ZSPD_DB
from common.orderbook import BookLevel, Order, OrderBook

DATA_DB = ROOT / "database" / "data.db"
CST     = timezone(timedelta(hours=8))

# Strategy math lives in strategy_math/ — same source-of-truth as backtest.
from strategy_math import (
    _calendar_features,
    _certainty_prob,
    _era5_slot_features,
    _lag_features,
    _load_labels,
    _model_prob,
    _non_nan,
    _obs_derived_features,
    _obs_features,
    _position_size,
)


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class ContractPair:
    yes_token_id: str
    no_token_id:  str
    kind:         str   # 'highest' | 'lowest'
    bound:        str   # 'ge' | 'le' | 'eq'
    threshold_c:  int


# BookLevel / OrderBook / Order moved to common.orderbook (re-imported above).


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


# ── Database helpers ──────────────────────────────────────────────────────────

def _load_contracts(target_date: date, cfg: dict) -> list[ContractPair]:
    """Load YES+NO token pairs for target_date, filtered by config bounds/ranges."""
    if not DATA_DB.exists():
        print(f"  [warn] data.db not found at {DATA_DB}")
        return []

    c_cfg       = cfg.get("contracts", {})
    tmax_cfg    = c_cfg.get("tmax", {})
    tmin_cfg    = c_cfg.get("tmin", {})
    tmax_bounds = set(tmax_cfg.get("bounds", ["ge", "eq", "le"]))
    tmin_bounds = set(tmin_cfg.get("bounds", ["ge", "eq", "le"]))
    tmax_min    = tmax_cfg.get("threshold_min", 0)
    tmax_max    = tmax_cfg.get("threshold_max", 99)
    tmin_min    = tmin_cfg.get("threshold_min", -99)
    tmin_max    = tmin_cfg.get("threshold_max", 99)

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

    result: list[ContractPair] = []
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
        result.append(ContractPair(yes_id, no_id, kind, bound, int(threshold_c)))
    return result


def _load_day_obs(target_date: date) -> pd.DataFrame:
    """Load today's WU obs in the format _obs_features() expects."""
    _empty = pd.DataFrame(columns=[
        "epoch", "obs_date_local", "temp_c",
        "dew_c", "wind_kt", "pressure_hpa", "obs_hour_local",
    ])
    if not WU_ZSPD_DB.exists():
        return _empty
    conn = sqlite3.connect(WU_ZSPD_DB)
    df = pd.read_sql(
        """SELECT valid_time_gmt             AS epoch,
                  obs_date_local,
                  temp_c,
                  dew_point_c               AS dew_c,
                  wind_speed_kmh * 0.539957 AS wind_kt,
                  pressure_hpa
           FROM zspd_observations
           WHERE obs_date_local = ? AND temp_c IS NOT NULL
           ORDER BY valid_time_gmt""",
        conn,
        params=(target_date.isoformat(),),
    )
    conn.close()
    if not df.empty:
        df["obs_hour_local"] = ((df["epoch"] + 8 * 3600) % 86400) / 3600.0
    return df


def _load_era5_today(target_date: date) -> Optional[pd.DataFrame]:
    """Load ERA5/GFS rows for target_date from weather_om.db (archive priority)."""
    if not WEATHER_OM_DB.exists():
        return None
    conn = sqlite3.connect(WEATHER_OM_DB)
    df = pd.read_sql(
        """SELECT obs_date, obs_hour,
                  sw_radiation, direct_rad, diffuse_rad,
                  cloud_pct, cloud_low_pct, cloud_mid_pct,
                  temp_c, dew_point_c, source
           FROM om_hourly
           WHERE obs_date = ?
           ORDER BY obs_hour,
                    CASE WHEN source = 'archive' THEN 0 ELSE 1 END""",
        conn,
        params=(target_date.isoformat(),),
    )
    conn.close()
    if df.empty:
        return None
    df = df.drop_duplicates(subset=["obs_hour"], keep="first")
    return df.sort_values("obs_hour").reset_index(drop=True)


def _query_slot_temp(target_date: date, slot_h: float) -> Optional[float]:
    """Query standard.db for the observation closest to slot_h (within ±15 min)."""
    if not WU_ZSPD_DB.exists():
        return None
    midnight_cst = datetime(target_date.year, target_date.month, target_date.day, tzinfo=CST)
    center_epoch = int((midnight_cst + timedelta(hours=slot_h)).timestamp())
    conn = sqlite3.connect(WU_ZSPD_DB)
    row = conn.execute(
        """SELECT temp_c FROM zspd_observations
           WHERE obs_date_local = ?
             AND valid_time_gmt BETWEEN ? AND ?
             AND temp_c IS NOT NULL
           ORDER BY ABS(valid_time_gmt - ?) LIMIT 1""",
        (target_date.isoformat(), center_epoch - 900, center_epoch + 900, center_epoch),
    ).fetchone()
    conn.close()
    return float(row[0]) if row else None


def _run_wu_update() -> None:
    from ml.data.fetch_wu_latest import refresh_today_wu
    rows = refresh_today_wu()
    if rows <= 0:
        raise RuntimeError("WU refresh returned no rows")


def _book_mid(book: OrderBook) -> Optional[float]:
    """Return (best_bid + best_ask) / 2, or None if either side is empty."""
    if not book.bids or not book.asks:
        return None
    return (book.bids[0].price + book.asks[0].price) / 2.0


def _prompt_manual(slot_h: float) -> Optional[float]:
    h, m = int(slot_h), int((slot_h % 1) * 60)
    # Headless mode (no controlling tty, e.g. systemd / nohup) — skip the slot
    # rather than crash on EOFError. Live trading must never crash-loop.
    if not sys.stdin or not sys.stdin.isatty():
        print(f"  [{h:02d}:{m:02d}] non-interactive (no tty); slot skipped.")
        return None
    print(f"\n  ┌─ MANUAL INPUT REQUIRED ──────────────────────")
    print(f"  │  Temperature at {h:02d}:{m:02d} CST unavailable.")
    print(f"  │  Format: decimal °C  e.g. 24.5  /  Enter to skip.")
    print(f"  └───────────────────────────────────────────────")
    while True:
        try:
            raw = input("  > ").strip()
        except EOFError:
            print("  EOF; slot skipped.")
            return None
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


# ── Strategy ──────────────────────────────────────────────────────────────────

class ModelArbitrageStrategy:
    """
    Live v5/v3 strategy combining physics certainty arbitrage with model edge.

    Wakes up at HH:01 and HH:31 CST.
    Each slot:
      1. Fetch WU temperature (auto-retry + manual fallback).
      2. Tier 1: certainty check — enter once per token/day if physics locks outcome.
      3. Tier 2: model edge — run v5 quantile prediction, enter top-N signals.
    """

    _CERTAINTY_MIN_EDGE = 0.02
    _CERTAINTY_NO_SIZE  = 50.0   # max contracts for certainty NO (matches backtest)

    def __init__(
        self,
        target_date:    date,
        order_book_fn:  Callable[[str], OrderBook],
        place_order_fn: Callable[[Order], None],
        version:        str   = "v5",
        min_edge:       float = 0.05,
        base_size:      float = 1.0,
        max_per_slot:   int   = 10,
        cfg:            Optional[dict] = None,
    ):
        from ml.models.predict import Predictor

        self.target_date   = target_date
        self._get_book     = order_book_fn
        self._place        = place_order_fn
        self.version       = version
        self.min_edge      = min_edge
        self.base_size     = base_size
        self.max_per_slot  = max_per_slot

        cfg = cfg or _load_config()
        self.contracts = _load_contracts(target_date, cfg)

        # Load heavy data once — reused across all slots
        self.labels_df = _load_labels()
        self.era5_day  = _load_era5_today(target_date)
        self.lag_feat  = _lag_features(target_date, self.labels_df, version)
        self.cal_feat  = _calendar_features(target_date)
        self.predictor = Predictor(version=version)

        # Daily state: track tokens with live certainty position
        self._certainty_entered: set[str] = set()   # yes_token_ids

        if self.era5_day is None and version == "v5":
            print(f"  [warn] No ERA5/GFS data for {target_date}. "
                  "Run: python -m core.fetch_weather --forecast-only")

        print(
            f"[modify_str] {target_date}  version={version}  "
            f"contracts={len(self.contracts)}  "
            f"era5={'loaded' if self.era5_day is not None else 'MISSING'}  "
            f"min_edge={min_edge}  base_size={base_size}"
        )

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        market_close = datetime(
            self.target_date.year, self.target_date.month, self.target_date.day,
            23, 59, tzinfo=CST,
        )
        print(f"  Market closes {market_close.strftime('%H:%M')} CST.\n")

        while True:
            now = datetime.now(CST)
            if now >= market_close:
                print("[modify_str] Market closed. Done.")
                break
            slot_h = self._current_slot_h(now)
            self._process_slot(slot_h)
            self._sleep_to_next(now)

    def _current_slot_h(self, now: datetime) -> float:
        return float(now.hour) if now.minute < 31 else now.hour + 0.5

    def _sleep_to_next(self, now: datetime) -> None:
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

    # ── Per-slot processing ───────────────────────────────────────────────────

    def _process_slot(self, slot_h: float) -> None:
        h, m = int(slot_h), int((slot_h % 1) * 60)
        print(f"\n── slot {h:02d}:{m:02d} ─────────────────────────────────────────")

        temp = self._fetch_temp(slot_h)
        if temp is None:
            print(f"  No temperature for {h:02d}:{m:02d}. Skipping slot.")
            return

        day_obs  = _load_day_obs(self.target_date)
        obs_f    = _obs_features(day_obs, slot_h)
        obs_tmax = _non_nan(obs_f["obs_tmax_so_far"])
        obs_tmin = _non_nan(obs_f["obs_tmin_so_far"])

        print(
            f"  temp={temp:+.1f}°C  "
            f"tmax_so_far={f'{obs_tmax:.1f}' if obs_tmax is not None else '?'}  "
            f"tmin_so_far={f'{obs_tmin:.1f}' if obs_tmin is not None else '?'}"
        )

        n_cert  = self._run_certainty(obs_tmax, obs_tmin)
        n_model = self._run_model_edge(slot_h, obs_f, obs_tmax, obs_tmin)
        print(f"  → slot signals: certainty={n_cert}  model={n_model}")

    def _fetch_temp(self, slot_h: float) -> Optional[float]:
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
                    print(f"  Still missing (attempt {attempt}/3). Retrying in 5s …")
                    time.sleep(5)
            print("  Update succeeded but slot still missing.")
        except Exception as e:
            print(f"  Update error: {e}")

        return _prompt_manual(slot_h)

    # ── Tier 1: Certainty signals ─────────────────────────────────────────────

    def _run_certainty(
        self,
        obs_tmax: Optional[float],
        obs_tmin: Optional[float],
    ) -> int:
        n = 0
        for pair in self.contracts:
            if pair.yes_token_id in self._certainty_entered:
                continue

            cert_p = _certainty_prob(
                pair.kind, pair.bound, pair.threshold_c, obs_tmax, obs_tmin
            )
            if cert_p is None:
                continue

            yes_book = self._safe_book(pair.yes_token_id)
            if yes_book is None:
                continue
            yes_mid = _book_mid(yes_book)
            if yes_mid is None:
                continue

            edge  = cert_p - yes_mid
            label = f"{pair.kind} {pair.bound}_{pair.threshold_c}"

            if cert_p == 0.0:
                # Certainty NO: market still prices YES > 0.01 → BUY NO
                if yes_mid <= 0.01:
                    self._certainty_entered.add(pair.yes_token_id)
                    continue
                if self._eat_no(pair, label, cert_p, yes_mid, edge, certain=True):
                    self._certainty_entered.add(pair.yes_token_id)
                    n += 1

            else:  # cert_p == 1.0  — certainty YES
                if abs(edge) < self._CERTAINTY_MIN_EDGE:
                    self._certainty_entered.add(pair.yes_token_id)
                    continue
                size = _position_size(True, edge, self.base_size)
                if self._buy_yes(pair, label, cert_p, yes_mid, edge, size, certain=True):
                    self._certainty_entered.add(pair.yes_token_id)
                    n += 1

        return n

    # ── Tier 2: Model edge signals ────────────────────────────────────────────

    def _run_model_edge(
        self,
        slot_h:   float,
        obs_f:    dict,
        obs_tmax: Optional[float],
        obs_tmin: Optional[float],
    ) -> int:
        is_cold_start = slot_h < 8.0

        derived_f = _obs_derived_features(obs_f) if self.version == "v5" else {}
        era5_f    = _era5_slot_features(self.era5_day, slot_h) if self.version == "v5" else {}

        feat_row = pd.Series({
            **obs_f, **derived_f, **era5_f,
            **self.lag_feat, **self.cal_feat,
            "obs_hour_frac": float(slot_h),
        })

        result = self.predictor.predict(
            target_date      = self.target_date,
            feature_row      = feat_row,
            obs_hour_frac    = slot_h,
            obs_tmax_so_far  = obs_tmax,
            obs_tmin_so_far  = obs_tmin,
            obs_temp_now     = _non_nan(obs_f["obs_temp_now"]),
            obs_temp_prev1h  = _non_nan(obs_f["obs_temp_prev1h"]),
            obs_dew_now      = _non_nan(obs_f["obs_dew_now"]),
            obs_dew_mean     = _non_nan(obs_f["obs_dew_mean"]),
            obs_wind_now     = _non_nan(obs_f["obs_wind_now"]),
            obs_pressure_now = _non_nan(obs_f["obs_pressure_now"]),
            obs_count        = int(obs_f["obs_count"]),
        )

        # Print model distribution summary
        for tgt in ("tmax", "tmin"):
            d = result.get(tgt, {})
            if "distribution" in d:
                q = d["distribution"]
                print(
                    f"  {tgt.upper()} Q05/Q50/Q95: "
                    f"{q.get('q05')} / {q.get('q50')} / {q.get('q95')} °C"
                )

        signals: list[tuple] = []
        for pair in self.contracts:
            if is_cold_start and pair.kind == "highest":
                continue
            if pair.yes_token_id in self._certainty_entered:
                continue

            mp = _model_prob(result, pair.kind, pair.bound, pair.threshold_c)
            if mp is None:
                continue

            yes_book = self._safe_book(pair.yes_token_id)
            if yes_book is None:
                continue
            yes_mid = _book_mid(yes_book)
            if yes_mid is None:
                continue

            edge      = mp - yes_mid
            direction = "YES" if edge > 0 else "NO"

            if abs(edge) < self.min_edge:
                continue
            # ge-YES: require large edge due to tail over-estimation risk
            if pair.bound == "ge" and direction == "YES" and edge < 0.30:
                continue

            signals.append((abs(edge), edge, mp, yes_mid, pair, direction))

        signals.sort(key=lambda x: -x[0])
        n = 0
        for _, edge, mp, yes_mid, pair, direction in signals[:self.max_per_slot]:
            label = f"{pair.kind} {pair.bound}_{pair.threshold_c}"
            size  = _position_size(False, edge, self.base_size)

            if direction == "YES":
                if self._buy_yes(pair, label, mp, yes_mid, edge, size, certain=False):
                    n += 1
            else:
                if self._eat_no(pair, label, mp, yes_mid, edge, certain=False, size=size):
                    n += 1

        return n

    # ── Order execution ───────────────────────────────────────────────────────

    def _buy_yes(
        self,
        pair:         ContractPair,
        label:        str,
        model_prob:   float,
        market_price: float,
        edge:         float,
        size:         float,
        certain:      bool,
    ) -> bool:
        """Place a single BUY limit order on the YES token at best ask."""
        yes_book = self._safe_book(pair.yes_token_id)
        if yes_book is None or not yes_book.asks:
            print(f"    [{'cert' if certain else 'model'}] [{label}] no YES asks — skipped")
            return False

        ask = yes_book.asks[0].price
        self._place(Order(
            token_id    = pair.yes_token_id,
            side        = "BUY",
            size        = size,
            limit_price = ask,
            note        = (
                f"{'CERTAIN' if certain else 'MODEL'} YES  "
                f"model={model_prob:.3f}  mid={market_price:.3f}  "
                f"edge={edge:+.3f}  {label}"
            ),
        ))
        print(
            f"    [{'cert' if certain else 'model'}] [{label}]  "
            f"BUY YES  size={size:.1f}  ask={ask:.3f}  "
            f"model={model_prob:.3f}  edge={edge:+.3f}"
        )
        return True

    def _eat_no(
        self,
        pair:         ContractPair,
        label:        str,
        model_prob:   float,
        market_price: float,
        edge:         float,
        certain:      bool,
        size:         Optional[float] = None,
    ) -> bool:
        """
        Buy NO tokens.
        certain=True: eat ask depth up to _CERTAINTY_NO_SIZE contracts.
        certain=False (model): place single order at best ask for `size` contracts.
        """
        no_book = self._safe_book(pair.no_token_id)
        if no_book is None or not no_book.asks:
            print(f"    [{'cert' if certain else 'model'}] [{label}] no NO asks — skipped")
            return False

        best_ask  = no_book.asks[0].price
        total_buy = 0.0

        if certain:
            remaining = self._CERTAINTY_NO_SIZE
            for level in no_book.asks:
                if remaining <= 0:
                    break
                fill = min(level.size, remaining)
                self._place(Order(
                    token_id    = pair.no_token_id,
                    side        = "BUY",
                    size        = fill,
                    limit_price = level.price,
                    note        = f"CERTAIN NO  {label}  eat@{level.price:.3f}",
                ))
                total_buy += fill
                remaining -= fill
        else:
            assert size is not None
            self._place(Order(
                token_id    = pair.no_token_id,
                side        = "BUY",
                size        = size,
                limit_price = best_ask,
                note        = (
                    f"MODEL NO  model={model_prob:.3f}  mid={market_price:.3f}  "
                    f"edge={edge:+.3f}  {label}"
                ),
            ))
            total_buy = size

        print(
            f"    [{'cert' if certain else 'model'}] [{label}]  "
            f"BUY NO  size={total_buy:.1f}  best_ask={best_ask:.3f}  "
            f"model_yes={model_prob:.3f}  mid_yes={market_price:.3f}  edge={edge:+.3f}"
        )
        return True

    def _safe_book(self, token_id: str) -> Optional[OrderBook]:
        try:
            return self._get_book(token_id)
        except Exception as e:
            print(f"    [warn] book({token_id[:12]}…) error: {e}")
            return None


# ── Dry-run stubs ─────────────────────────────────────────────────────────────

def _dry_run_book_yes(_token_id: str) -> OrderBook:
    """Simulate a YES token at ~25% probability (mid ≈ 0.25)."""
    return OrderBook(
        bids=[BookLevel(0.23, 300.0), BookLevel(0.20, 600.0)],
        asks=[BookLevel(0.27, 200.0), BookLevel(0.30, 400.0)],
    )


def _dry_run_book_no(_token_id: str) -> OrderBook:
    """Simulate a NO token (≈ 1 - 0.25 = 0.75)."""
    return OrderBook(
        bids=[BookLevel(0.72, 300.0), BookLevel(0.70, 600.0)],
        asks=[BookLevel(0.76, 200.0), BookLevel(0.80, 400.0)],
    )


def _dry_run_book(token_id: str) -> OrderBook:
    """Return a reasonable dummy book for any token (YES or NO)."""
    return _dry_run_book_yes(token_id)


def _dry_run_place(order: Order) -> None:
    print(
        f"      [DRY-RUN] {order.side}  "
        f"size={order.size:.1f}  limit={order.limit_price:.4f}  "
        f"token={order.token_id[:12]}…  ({order.note})"
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main(argv: list[str]) -> None:
    import argparse
    p = argparse.ArgumentParser(
        description="modify_str — v5 Certainty + Model Edge live strategy"
    )
    p.add_argument("--date",         default=None,
                   help="Target date YYYY-MM-DD (default: today CST)")
    p.add_argument("--version",      default="v5", choices=["v3", "v4", "v5"],
                   help="Model version (default: v5)")
    p.add_argument("--dry-run",      action="store_true",
                   help="Print orders without executing (placeholder order book)")
    p.add_argument("--min-edge",     type=float, default=0.05,
                   help="Minimum |model edge| to enter a trade (default 0.05)")
    p.add_argument("--size",         type=float, default=1.0,
                   help="Base position size in $ (default 1.0)")
    p.add_argument("--max-per-slot", type=int,   default=10,
                   help="Max model signals per half-hour slot (default 10)")
    p.add_argument("--backend",      default="proxy", choices=["direct", "proxy"],
                   help="direct = py-clob-client locally (needs unblocked IP); "
                        "proxy = HTTP to clob-proxy server (default)")
    args = p.parse_args(argv[1:])

    target_date = (date.fromisoformat(args.date) if args.date
                   else datetime.now(CST).date())

    if args.dry_run:
        print("[DRY-RUN] Placeholder order books, no real orders.\n")
        book_fn  = _dry_run_book
        place_fn = _dry_run_place
    else:
        if args.backend == "direct":
            from strategies.clob_adapter import LiveClobBackend
            backend = LiveClobBackend()
        else:
            from strategies.proxy_clob_adapter import ProxyClobBackend
            backend = ProxyClobBackend()

        print("┌─ LIVE MODE — REAL ORDERS WILL BE PLACED ─────────")
        print(f"│  date={target_date}  version={args.version}  "
              f"size=${args.size}  min_edge={args.min_edge}")
        print(f"│  backend={args.backend}  "
              f"{'url=' + backend.url if args.backend == 'proxy' else ''}")
        print(f"│  ctrl-C now to abort. Starting in 5s ...")
        print("└──────────────────────────────────────────────────")
        time.sleep(5)

        book_fn  = backend.get_book
        place_fn = backend.place_order

    cfg = _load_config()
    strategy = ModelArbitrageStrategy(
        target_date    = target_date,
        order_book_fn  = book_fn,
        place_order_fn = place_fn,
        version        = args.version,
        min_edge       = args.min_edge,
        base_size      = args.size,
        max_per_slot   = args.max_per_slot,
        cfg            = cfg,
    )
    strategy.run()


if __name__ == "__main__":
    main(sys.argv)
