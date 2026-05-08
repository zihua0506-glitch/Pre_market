"""
ml/backtest/backtest.py

STRATEGY: Certainty-First + Model Edge, Tiered Position Sizing
═══════════════════════════════════════════════════════════════

SIGNAL TIER 1 — Certainty (observation-locked, physics-derived)
  Once a temperature is observed, some contracts are guaranteed to settle 0 or 1.
  These signals are entered exactly ONCE per token per day (first slot they fire).

  From obs_tmax_so_far (max temperature observed today so far):
    ge_T  P=1.0  if obs_tmax >= T-0.5   → BUY YES  (tmax will settle ≥ T for certain)
    eq_T  P=0.0  if obs_tmax >= T+0.5   → BUY NO   (tmax has passed T, can't settle =T)
    le_T  P=0.0  if obs_tmax >= T+0.5   → BUY NO   (tmax has passed T, can't settle ≤T)

  From obs_tmin_so_far (min temperature observed today so far):
    le_T  P=1.0  if obs_tmin <  T+0.5   → BUY YES  (tmin already below T+0.5, settles ≤T)
    ge_T  P=0.0  if obs_tmin <  T-0.5   → BUY NO   (tmin already below T-0.5, can't settle ≥T)
    eq_T  P=0.0  if obs_tmin <  T-0.5   → BUY NO   (same reason)

  Example: obs_tmax = 28.0
    ge_28 YES: floor(28+0.5)=28 ≥ 28 ✓ → P=1, buy YES
    eq_27 NO:  tmax already ≥ 28 > 27.5, settle ≥ 28 ≠ 27 → P=0, buy NO
    le_27 NO:  settle ≥ 28 > 27, tmax ≤ 27 impossible → P=0, buy NO
；
  Certainty YES entry filter: |edge| > 0.02 (skip if market already priced it in)
  Certainty NO entry filter : skip only if YES price ≤ 0.01 (NO ≥ 0.99, too expensive)
  Certainty NO position size: fixed 50 contracts (no edge scaling)

SIGNAL TIER 2 — Model Edge (quantile distribution, entered every slot)
  edge = model_prob − market_price
  Enter if |edge| > min_edge (default 0.05)
  Do NOT re-enter a token that already has a live certainty position this day.
  cold_start (slot_h < 8): tmax model signals DISABLED (no intraday obs yet);
             tmin model signals still active (nighttime = tmin formation period).

POSITION SIZING (base_size = $1 default)
  Certainty + |edge| ≥ 0.50  →  5 × base   (heavy: market badly mispriced)
  Certainty + |edge| ≥ 0.20  →  3 × base   (medium-heavy)
  Certainty + |edge| ≥ 0.02  →  2 × base   (medium)
  Model     + |edge| ≥ 0.20  →  3 × base
  Model     + |edge| ≥ 0.10  →  2 × base
  Model     + |edge| ≥ 0.05  →  1 × base

TIMELINE
  HH:00/HH:30 CST  WU observation arrives
  HH:01 CST        temperature known, inference computed
  HH:02 CST        execution at first 1-min price bar at/after signal_epoch
  23:59 CST        market closes, settled at 0 or 1

Run:
    python -m ml.backtest.backtest --version v0
    python -m ml.backtest.backtest --version v0 --start 2026-03-13 --end 2026-04-28
    python -m ml.backtest.backtest --version v1 --min-edge 0.05 --size 1.0 --max-per-slot 10
    python -m ml.backtest.backtest --version v0 --no-save
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# Silence sklearn "feature names" warning that fires when passing numpy arrays
# to estimators trained with DataFrames (fixed in predict.py; kept here as safety net)
warnings.filterwarnings("ignore", message="X does not have valid feature names")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from ml.config import LABELS_DB, WEATHER_OM_DB, WU_ZSPD_DB

DATA_DB = ROOT / "database" / "data.db"
SHANGHAI_OFFSET = timedelta(hours=8)


# ── Data loaders ───────────────────────────────────────────────────────────

def _load_token_meta() -> pd.DataFrame:
    """Load YES-side token metadata from data.db."""
    conn = sqlite3.connect(DATA_DB)
    df = pd.read_sql(
        """SELECT token_id, kind, bound, threshold_c,
                  day_date, ts_start, ts_end
           FROM token_meta
           WHERE side = 'YES'
           ORDER BY day_date, kind, bound, threshold_c""",
        conn,
    )
    conn.close()
    df["day_date"] = pd.to_datetime(df["day_date"]).dt.date
    return df


def _load_price_minute(token_ids: list[str],
                       start_epoch: int, end_epoch: int) -> pd.DataFrame:
    """Load price_minute rows for the given tokens and epoch range."""
    conn = sqlite3.connect(DATA_DB)
    placeholders = ",".join("?" * len(token_ids))
    df = pd.read_sql(
        f"""SELECT token_id, ts, price
            FROM price_minute
            WHERE token_id IN ({placeholders})
              AND ts BETWEEN ? AND ?
            ORDER BY token_id, ts""",
        conn,
        params=token_ids + [start_epoch, end_epoch],
    )
    conn.close()
    return df


def _load_wu_obs(start_date: date, end_date: date) -> pd.DataFrame:
    """Load WU half-hourly obs for the backtest window."""
    if not WU_ZSPD_DB.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(WU_ZSPD_DB)
    df = pd.read_sql(
        """SELECT valid_time_gmt AS epoch,
                  obs_date_local,
                  temp_c,
                  dew_point_c      AS dew_c,
                  wind_speed_kmh * 0.539957 AS wind_kt,
                  pressure_hpa
           FROM zspd_observations
           WHERE obs_date_local BETWEEN ? AND ?
             AND temp_c IS NOT NULL
           ORDER BY valid_time_gmt""",
        conn,
        params=(start_date.isoformat(), end_date.isoformat()),
    )
    conn.close()
    df["obs_hour_local"] = ((df["epoch"] + 8 * 3600) % 86400) / 3600.0
    return df


# _load_labels moved to strategy_math.labels (re-imported below for backwards
# compat with code that does `from ml.backtest.backtest import _load_labels`).


def _load_era5_obs(start_date: date, end_date: date) -> dict[str, pd.DataFrame]:
    """
    加载回测窗口内的 ERA5 气象数据（v5 回测专用）。

    从 weather_om.db / om_hourly 读取，archive 优先于 forecast（精度更高）。
    返回 {obs_date_str: DataFrame（按 obs_hour 升序）} 供 slot 级特征提取使用。
    """
    if not WEATHER_OM_DB.exists():
        print("  [v5] 警告：weather_om.db 不存在，ERA5 特征将全部为 NaN")
        return {}
    conn = sqlite3.connect(WEATHER_OM_DB)
    df = pd.read_sql(
        """SELECT obs_date, obs_hour,
                  sw_radiation, direct_rad, diffuse_rad,
                  cloud_pct, cloud_low_pct, cloud_mid_pct,
                  temp_c, dew_point_c, source
           FROM om_hourly
           WHERE obs_date BETWEEN ? AND ?
           ORDER BY obs_date, obs_hour,
                    CASE WHEN source = 'archive' THEN 0 ELSE 1 END""",
        conn,
        params=(start_date.isoformat(), end_date.isoformat()),
    )
    conn.close()
    # 同一 (obs_date, obs_hour) 保留最优先行（archive > forecast）
    df = df.drop_duplicates(subset=["obs_date", "obs_hour"], keep="first")
    return {
        d: grp.sort_values("obs_hour").reset_index(drop=True)
        for d, grp in df.groupby("obs_date")
    }


# ── Feature builders moved to strategy_math.features ──────────────────────


# All strategy math (feature builders, certainty probs, model_prob,
# position_size, label loader) lives in the strategy_math/ package and is
# shared with the live strategy in strategies/modify_str.py. Re-imported
# here under the original private names for backwards compat.
from strategy_math import (
    _calendar_features,
    _certainty_prob,
    _era5_slot_features,
    _event_happened,
    _lag_features,
    _load_labels,
    _model_prob,
    _non_nan,
    _obs_derived_features,
    _obs_features,
    _position_size,
)
# ── Main backtest engine ───────────────────────────────────────────────────

def run_backtest(
    start_date: date,
    end_date: date,
    min_edge: float = 0.05,
    position_size: float = 1.0,
    max_per_slot: int = 10,
    version: str = "v0",
) -> pd.DataFrame:
    """
    Run the full backtest and return a DataFrame of all trades.

    Columns: date, slot_h, bucket, token_id, kind, bound, threshold_c,
             direction, model_prob, market_price, edge,
             exec_epoch, exec_price, event_happened, pnl
    """
    from ml.models.predict import Predictor

    print(f"加载数据（{start_date} → {end_date}，version={version}）...")
    token_meta = _load_token_meta()
    labels_df  = _load_labels()
    wu_obs     = _load_wu_obs(start_date, end_date)

    # v5：额外加载 ERA5 气象数据（archive 优先，供 slot 级特征提取）
    era5_by_date: dict = {}
    if version == "v5":
        era5_by_date = _load_era5_obs(start_date, end_date)
        print(f"  ERA5 days: {len(era5_by_date)}")

    # Filter token_meta to backtest window
    token_meta = token_meta[
        (token_meta["day_date"] >= start_date) &
        (token_meta["day_date"] <= end_date)
    ].copy()

    # Load price_minute for all relevant tokens in one shot
    start_epoch = int(datetime(start_date.year, start_date.month, start_date.day,
                               tzinfo=timezone.utc).timestamp()) - 8 * 3600
    end_epoch   = int(datetime(end_date.year, end_date.month, end_date.day, 23, 59,
                               tzinfo=timezone.utc).timestamp())
    token_ids = token_meta["token_id"].tolist()
    prices_df = _load_price_minute(token_ids, start_epoch, end_epoch)

    print(f"  合约数   : {len(token_meta):,}")
    print(f"  价格行数 : {len(prices_df):,}")
    print(f"  WU 观测  : {len(wu_obs):,}")
    print(f"  标签天数 : {len(labels_df)}")

    # Build per-token price index {token_id → DataFrame indexed by ts}
    price_idx: dict[str, pd.Series] = {
        tid: grp.set_index("ts")["price"]
        for tid, grp in prices_df.groupby("token_id")
    }

    # Build per-date WU obs index
    wu_by_date = {d: grp for d, grp in wu_obs.groupby("obs_date_local")}

    # Labels indexed by obs_date
    labels_idx = labels_df.set_index("obs_date")

    predictor = Predictor(version=version)

    trades: list[dict] = []
    all_dates = sorted(token_meta["day_date"].unique())
    target_date = None

    CERTAINTY_MIN_EDGE = 0.02   # enter certainty bets if market hasn't fully priced them

    try:
        for target_date in all_dates:
            d_str = target_date.isoformat()

            if target_date not in labels_idx.index:
                continue
            lbl = labels_idx.loc[target_date]
            if pd.isna(lbl["tmax_settled"]) or pd.isna(lbl["tmin_settled"]):
                continue
            tmax_settled = int(lbl["tmax_settled"])
            tmin_settled = int(lbl["tmin_settled"])

            day_tokens = token_meta[token_meta["day_date"] == target_date]

            day_midnight_utc = int(
                datetime(target_date.year, target_date.month, target_date.day,
                         tzinfo=timezone.utc).timestamp()
            ) - 8 * 3600
            market_close_epoch = day_midnight_utc + 23 * 3600 + 59 * 60

            day_obs = wu_by_date.get(d_str, pd.DataFrame(
                columns=["epoch", "obs_date_local", "temp_c", "dew_c",
                         "wind_kt", "pressure_hpa", "obs_hour_local"]
            ))

            # v5：获取当日 ERA5 数据（无数据时返回 None，特征将为 NaN）
            era5_day = era5_by_date.get(d_str, None) if version == "v5" else None

            # 每日固定特征（滞后 + 日历），v5 需要更多历史行
            lag_feat = _lag_features(target_date, labels_df, version)
            cal_feat = _calendar_features(target_date)

            # Track which tokens already have a live certainty position today
            certainty_entered: set[str] = set()
            n_day_trades = 0

            for slot_idx in range(48):
                slot_h = slot_idx * 0.5
                signal_epoch = day_midnight_utc + int((slot_h + 2.0 / 60.0) * 3600)
                if signal_epoch >= market_close_epoch:
                    break

                obs_f    = _obs_features(day_obs, slot_h)
                obs_tmax = _non_nan(obs_f["obs_tmax_so_far"])
                obs_tmin = _non_nan(obs_f["obs_tmin_so_far"])
                exec_after = signal_epoch

                # v5：每半小时更新 ERA5 特征和派生观测特征
                era5_f   = _era5_slot_features(era5_day, slot_h) if version == "v5" else {}
                derived_f = _obs_derived_features(obs_f)         if version == "v5" else {}

                # ── Tier 1: Certainty signals ──────────────────────────────
                # Enter once per token per day (first slot certainty is established).
                for tok in day_tokens.itertuples(index=False):
                    if tok.token_id in certainty_entered:
                        continue

                    cert_p = _certainty_prob(
                        tok.kind, tok.bound, tok.threshold_c, obs_tmax, obs_tmin
                    )
                    if cert_p is None:
                        continue   # not yet certain

                    tok_prices = price_idx.get(tok.token_id)
                    if tok_prices is None or tok_prices.empty:
                        continue
                    pre = tok_prices[tok_prices.index <= signal_epoch]
                    if pre.empty:
                        continue
                    market_price = float(pre.iloc[-1])

                    edge = cert_p - market_price

                    if cert_p == 0.0:  # certainty NO
                        if market_price <= 0.01:  # NO already ≥ 0.99, skip
                            certainty_entered.add(tok.token_id)
                            continue
                        direction = "NO"
                        size = 50.0
                    else:  # certainty YES (cert_p == 1.0)
                        if abs(edge) < CERTAINTY_MIN_EDGE:
                            certainty_entered.add(tok.token_id)
                            continue
                        direction = "YES"
                        size = _position_size(True, edge, position_size)

                    post = tok_prices[
                        (tok_prices.index >= exec_after) &
                        (tok_prices.index < market_close_epoch)
                    ]
                    if post.empty:
                        continue
                    exec_price = float(post.iloc[0])
                    exec_epoch = int(post.index[0])

                    ev = _event_happened(
                        tok.kind, tok.bound, tok.threshold_c, tmax_settled, tmin_settled
                    )
                    if ev is None:
                        continue

                    pnl = (ev - exec_price) * size if direction == "YES" \
                          else (exec_price - ev) * size

                    trades.append(_trade_row(
                        d_str, slot_h, tok, direction,
                        cert_p, market_price, edge, exec_epoch, exec_price,
                        ev, size, pnl, signal_type="certain",
                    ))
                    certainty_entered.add(tok.token_id)
                    n_day_trades += 1

                # ── Tier 2: Model edge signals ─────────────────────────────
                # v5：在标准特征基础上加入 ERA5 + 派生特征（共 63 个）
                # v0/v3：仅 WU 观测 + 滞后 + 日历（35 个）
                feat_row = pd.Series({
                    **obs_f, **derived_f, **era5_f, **lag_feat, **cal_feat,
                    "obs_hour_frac": float(slot_h),
                })
                result = predictor.predict(
                    target_date      = target_date,
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

                is_cold_start = slot_h < 8.0

                slot_signals: list[tuple] = []
                for tok in day_tokens.itertuples(index=False):
                    # cold_start: skip tmax model signals (no intraday info yet)
                    if is_cold_start and tok.kind == "highest":
                        continue
                    mp = _model_prob(result, tok.kind, tok.bound, tok.threshold_c)
                    if mp is None:
                        continue
                    tok_prices = price_idx.get(tok.token_id)
                    if tok_prices is None or tok_prices.empty:
                        continue
                    pre = tok_prices[tok_prices.index <= signal_epoch]
                    if pre.empty:
                        continue
                    market_price = float(pre.iloc[-1])
                    edge = mp - market_price
                    if abs(edge) < min_edge:
                        continue
                    direction = "YES" if edge > 0 else "NO"
                    # ge YES: tail over-estimation risk, require large edge
                    if tok.bound == "ge" and direction == "YES" and edge < 0.3:
                        continue
                    slot_signals.append(
                        (abs(edge), edge, mp, market_price, tok, direction)
                    )

                slot_signals.sort(key=lambda x: -x[0])

                for _, edge, mp, market_price, tok, direction in slot_signals[:max_per_slot]:
                    tok_prices = price_idx.get(tok.token_id)
                    post = tok_prices[
                        (tok_prices.index >= exec_after) &
                        (tok_prices.index < market_close_epoch)
                    ]
                    if post.empty:
                        continue
                    exec_price = float(post.iloc[0])
                    exec_epoch = int(post.index[0])

                    ev = _event_happened(
                        tok.kind, tok.bound, tok.threshold_c, tmax_settled, tmin_settled
                    )
                    if ev is None:
                        continue

                    size = _position_size(False, edge, position_size)
                    pnl  = (ev - exec_price) * size if direction == "YES" \
                           else (exec_price - ev) * size

                    trades.append(_trade_row(
                        d_str, slot_h, tok, direction,
                        mp, market_price, edge, exec_epoch, exec_price,
                        ev, size, pnl, signal_type="model",
                    ))
                    n_day_trades += 1

            print(f"  {d_str}: tmax={tmax_settled}°C  tmin={tmin_settled}°C  "
                  f"trades={n_day_trades}", flush=True)

    except KeyboardInterrupt:
        n = len(trades)
        last = target_date.isoformat() if target_date else "?"
        print(f"\n[Interrupted after {last}] Showing {n} partial trades …", flush=True)

    return pd.DataFrame(trades)


def _trade_row(date_str, slot_h, tok, direction,
               model_prob, market_price, edge,
               exec_epoch, exec_price, event_happened, size, pnl,
               signal_type: str) -> dict:
    """Assemble a trade record dict."""
    if slot_h < 8:
        bucket = "cold_start"
    elif slot_h < 12:
        bucket = "morning"
    elif slot_h < 16:
        bucket = "daytime"
    else:
        bucket = "evening"
    return dict(
        date           = date_str,
        slot_h         = slot_h,
        bucket         = bucket,
        signal_type    = signal_type,
        token_id       = tok.token_id,
        kind           = tok.kind,
        bound          = tok.bound,
        threshold_c    = tok.threshold_c,
        direction      = direction,
        model_prob     = round(model_prob, 4),
        market_price   = round(market_price, 4),
        edge           = round(edge, 4),
        exec_epoch     = exec_epoch,
        exec_price     = round(exec_price, 4),
        event_happened = event_happened,
        size           = round(size, 4),
        pnl            = round(pnl, 6),
    )


# ── Reporting ──────────────────────────────────────────────────────────────



def print_report(trades: pd.DataFrame, position_size: float) -> None:
    if trades.empty:
        print("\nNo trades generated.")
        return
    _write_report(trades, position_size)


# ── Eval file output ───────────────────────────────────────────────────────

_EVAL_ROOT = Path(__file__).resolve().parent / "eval"


def save_eval(trades: pd.DataFrame, position_size: float, version: str = "v0") -> None:
    """
    Write evaluation artefacts to ml/backtest/eval/{version}/:
      trades_YYYYMMDD-YYYYMMDD.csv  — one row per trade
      daily_YYYYMMDD-YYYYMMDD.csv   — one row per day (aggregate)
      summary_YYYYMMDD-YYYYMMDD.txt — human-readable report
    """
    if trades.empty:
        print("No trades to save.")
        return

    eval_dir = _EVAL_ROOT / version
    eval_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{trades['date'].min().replace('-','')}_{trades['date'].max().replace('-','')}"

    trades_path = eval_dir / f"trades_{tag}.csv"
    trades.to_csv(trades_path, index=False)

    daily = trades.groupby("date").agg(
        n_trades   = ("pnl", "count"),
        pnl        = ("pnl", "sum"),
        win_rate   = ("pnl", lambda x: (x > 0).mean()),
        avg_edge   = ("edge", lambda x: x.abs().mean()),
        yes_trades = ("direction", lambda x: (x == "YES").sum()),
        no_trades  = ("direction", lambda x: (x == "NO").sum()),
        tmax_trades= ("kind", lambda x: (x == "highest").sum()),
        tmin_trades= ("kind", lambda x: (x == "lowest").sum()),
    ).reset_index()
    daily["cum_pnl"] = daily["pnl"].cumsum()
    daily_path = eval_dir / f"daily_{tag}.csv"
    daily.to_csv(daily_path, index=False)

    import io
    buf = io.StringIO()
    _write_report(trades, position_size, buf)
    summary_path = eval_dir / f"summary_{tag}.txt"
    summary_path.write_text(buf.getvalue(), encoding="utf-8")

    print(f"\nEval files saved to {eval_dir}/")
    print(f"  {trades_path.name}")
    print(f"  {daily_path.name}")
    print(f"  {summary_path.name}")


def _write_report(trades: pd.DataFrame, position_size: float,
                  out=None) -> None:
    """Write the full report to `out` (file-like). Defaults to stdout."""
    import sys as _sys
    sink = out if out is not None else _sys.stdout

    def w(s=""):
        sink.write(s + "\n")

    n         = len(trades)
    total_pnl = trades["pnl"].sum()
    win_rate  = (trades["pnl"] > 0).mean()
    avg_pnl   = trades["pnl"].mean()
    avg_edge  = trades["edge"].abs().mean()
    edge_real = avg_pnl / position_size

    daily  = trades.groupby("date")["pnl"].sum()
    sharpe = (daily.mean() / daily.std() * np.sqrt(252)
              if len(daily) > 1 and daily.std() > 0 else float("nan"))

    w()
    w("=" * 58)
    w(f"  BACKTEST RESULTS  {trades['date'].min()} → {trades['date'].max()}")
    w("=" * 58)
    w(f"  Total trades     : {n:,}")
    w(f"  Trading days     : {trades['date'].nunique()}")
    w(f"  Total PnL        : ${total_pnl:+.2f}")
    w(f"  Avg PnL/trade    : ${avg_pnl:+.5f}")
    w(f"  Win rate         : {win_rate:.1%}")
    w(f"  Avg |model edge| : {avg_edge:.4f}")
    w(f"  Realised edge    : {edge_real:+.4f}  (PnL per $1 bet)")
    w(f"  Daily Sharpe     : {sharpe:.2f}")
    w(f"  Max drawdown day : ${daily.min():+.2f}")

    w("\nBy direction:")
    for d in ["YES", "NO"]:
        s = trades[trades["direction"] == d]
        if s.empty:
            continue
        w(f"  {d:<3}  n={len(s):>4}  PnL={s['pnl'].sum():+7.2f}  "
          f"WR={(s['pnl']>0).mean():.1%}")

    w("\nBy contract type:")
    for b in ["ge", "le", "eq"]:
        s = trades[trades["bound"] == b]
        if s.empty:
            continue
        w(f"  {b:<3}  n={len(s):>4}  PnL={s['pnl'].sum():+7.2f}  "
          f"WR={(s['pnl']>0).mean():.1%}")

    w("\nBy kind (tmax/tmin):")
    for k, label in [("highest", "tmax"), ("lowest", "tmin")]:
        s = trades[trades["kind"] == k]
        if s.empty:
            continue
        w(f"  {label}  n={len(s):>4}  PnL={s['pnl'].sum():+7.2f}  "
          f"WR={(s['pnl']>0).mean():.1%}")

    w("\nBy time bucket:")
    for b in ["cold_start", "morning", "daytime", "evening"]:
        s = trades[trades["bucket"] == b]
        if s.empty:
            continue
        w(f"  {b:<12}  n={len(s):>4}  PnL={s['pnl'].sum():+7.2f}  "
          f"WR={(s['pnl']>0).mean():.1%}  avgEdge={s['edge'].abs().mean():.3f}")

    if "signal_type" in trades.columns:
        w("\nBy signal type:")
        for st in ["certain", "model"]:
            s = trades[trades["signal_type"] == st]
            if s.empty:
                continue
            w(f"  {st:<8}  n={len(s):>4}  PnL={s['pnl'].sum():+7.2f}  "
              f"WR={(s['pnl']>0).mean():.1%}  "
              f"avgSize={s['size'].mean():.2f}  avgEdge={s['edge'].abs().mean():.3f}")

    w()
    w("Daily PnL summary:")
    w(f"  mean={daily.mean():+.3f}  std={daily.std():.3f}  "
      f"min={daily.min():+.3f}  max={daily.max():+.3f}")
    w("=" * 58)


# ── CLI ────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        description="ZSPD Polymarket temperature strategy backtest"
    )
    parser.add_argument("--version",      default="v0",
                        choices=["v0", "v1", "v2", "v3", "v4", "v5"],
                        help="模型版本：v0/v1/v2/v3=WU观测特征，v5=WU+ERA5扩展特征（63个）")
    parser.add_argument("--start",        default="2026-03-13",
                        help="Backtest start date YYYY-MM-DD")
    parser.add_argument("--end",          default="2026-04-28",
                        help="Backtest end date YYYY-MM-DD")
    parser.add_argument("--min-edge",     type=float, default=0.05,
                        help="Minimum edge threshold to enter (default 0.05)")
    parser.add_argument("--size",         type=float, default=1.0,
                        help="Position size per trade in $ (default 1.0)")
    parser.add_argument("--max-per-slot", type=int,   default=10,
                        help="Max trades per half-hour slot (default 10)")
    parser.add_argument("--no-save",      action="store_true",
                        help="Skip writing eval files to ml/backtest/eval/{version}/")
    args = parser.parse_args(argv[1:])

    start = date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end)

    trades = run_backtest(
        start_date    = start,
        end_date      = end,
        min_edge      = args.min_edge,
        position_size = args.size,
        max_per_slot  = args.max_per_slot,
        version       = args.version,
    )

    print_report(trades, position_size=args.size)

    if not args.no_save:
        save_eval(trades, position_size=args.size, version=args.version)


if __name__ == "__main__":
    main(sys.argv)
