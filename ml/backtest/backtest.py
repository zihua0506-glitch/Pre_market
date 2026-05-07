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


def _load_labels() -> pd.DataFrame:
    """加载每日结算标签。"""
    conn = sqlite3.connect(LABELS_DB)
    df = pd.read_sql(
        """SELECT obs_date, tmax_raw, tmin_raw, tmax_settled, tmin_settled
           FROM daily_labels
           ORDER BY obs_date""",
        conn,
    )
    conn.close()
    df["obs_date"] = pd.to_datetime(df["obs_date"]).dt.date
    return df


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


# ── Feature builders ───────────────────────────────────────────────────────

def _obs_features(day_obs: pd.DataFrame, hour_cutoff: float) -> dict:
    """Compute intraday observation features from WU obs up to hour_cutoff."""
    mask = day_obs["obs_hour_local"] <= hour_cutoff
    obs  = day_obs[mask]

    nan_row = dict(
        obs_tmax_so_far=np.nan, obs_tmin_so_far=np.nan,
        obs_temp_now=np.nan, obs_temp_prev1h=np.nan, obs_temp_trend_1h=np.nan,
        obs_dew_now=np.nan, obs_dew_mean=np.nan, obs_wind_now=np.nan,
        obs_pressure_now=np.nan, obs_count=0,
    )
    if obs.empty:
        return nan_row

    temps = obs["temp_c"].dropna()
    last  = obs.iloc[-1]
    temp_now = float(last["temp_c"]) if pd.notna(last["temp_c"]) else np.nan

    prev = day_obs[day_obs["obs_hour_local"] <= (hour_cutoff - 1.0)]
    if not prev.empty and pd.notna(prev.iloc[-1]["temp_c"]):
        temp_prev1h = float(prev.iloc[-1]["temp_c"])
        trend       = (temp_now - temp_prev1h) if pd.notna(temp_now) else np.nan
    else:
        temp_prev1h = np.nan
        trend       = np.nan

    return dict(
        obs_tmax_so_far   = float(temps.max()) if not temps.empty else np.nan,
        obs_tmin_so_far   = float(temps.min()) if not temps.empty else np.nan,
        obs_temp_now      = temp_now,
        obs_temp_prev1h   = temp_prev1h,
        obs_temp_trend_1h = trend,
        obs_dew_now       = float(last["dew_c"])       if pd.notna(last["dew_c"])       else np.nan,
        obs_dew_mean      = float(obs["dew_c"].mean()) if obs["dew_c"].notna().any()     else np.nan,
        obs_wind_now      = float(last["wind_kt"])     if pd.notna(last["wind_kt"])      else np.nan,
        obs_pressure_now  = float(last["pressure_hpa"]) if pd.notna(last["pressure_hpa"]) else np.nan,
        obs_count         = int(len(obs)),
    )


def _lag_features(target_date: date, labels_df: pd.DataFrame,
                  version: str = "v0") -> dict:
    """
    从历史标签构建滞后和滚动特征。

    v0/v1/v2/v3 : lag1-7  + roll3/7（共 20 个）
    v5          : lag1-14 + roll3/7/14/30（共 38 个）
    """
    # v5 需要 roll30，提前取 30 行
    n_days = 30 if version == "v5" else 7
    past = (labels_df[labels_df["obs_date"] < target_date]
            .sort_values("obs_date", ascending=False)
            .head(n_days))
    past_list = list(past.itertuples())

    feat: dict = {}
    n_lags = 14 if version == "v5" else 7
    for i in range(n_lags):
        lag = i + 1
        if i < len(past_list):
            feat[f"tmax_lag{lag}"] = past_list[i].tmax_raw
            feat[f"tmin_lag{lag}"] = past_list[i].tmin_raw
        else:
            feat[f"tmax_lag{lag}"] = np.nan
            feat[f"tmin_lag{lag}"] = np.nan

    tmaxs = [r.tmax_raw for r in past_list if r.tmax_raw is not None]
    tmins = [r.tmin_raw for r in past_list if r.tmin_raw is not None]

    feat["tmax_roll3_mean"] = float(np.mean(tmaxs[:3]))  if len(tmaxs) >= 3  else np.nan
    feat["tmax_roll7_mean"] = float(np.mean(tmaxs[:7]))  if len(tmaxs) >= 7  else np.nan
    feat["tmax_roll7_std"]  = float(np.std(tmaxs[:7], ddof=1)) if len(tmaxs) >= 7 else np.nan
    feat["tmin_roll3_mean"] = float(np.mean(tmins[:3]))  if len(tmins) >= 3  else np.nan
    feat["tmin_roll7_mean"] = float(np.mean(tmins[:7]))  if len(tmins) >= 7  else np.nan
    feat["tmin_roll7_std"]  = float(np.std(tmins[:7], ddof=1)) if len(tmins) >= 7 else np.nan

    if version == "v5":
        feat["tmax_roll14_mean"] = float(np.mean(tmaxs[:14])) if len(tmaxs) >= 14 else np.nan
        feat["tmax_roll30_mean"] = float(np.mean(tmaxs[:30])) if len(tmaxs) >= 30 else np.nan
        feat["tmin_roll14_mean"] = float(np.mean(tmins[:14])) if len(tmins) >= 14 else np.nan
        feat["tmin_roll30_mean"] = float(np.mean(tmins[:30])) if len(tmins) >= 30 else np.nan

    return feat


def _obs_derived_features(obs_f: dict) -> dict:
    """
    计算 v5 派生观测特征（由 _obs_features 结果直接推导，无额外数据源）。
    """
    t  = obs_f.get("obs_temp_now")
    d  = obs_f.get("obs_dew_now")
    mx = obs_f.get("obs_tmax_so_far")
    mn = obs_f.get("obs_tmin_so_far")

    dew_dep = float(t - d) if (t is not None and d is not None
                               and not np.isnan(t) and not np.isnan(d)) else np.nan
    trange  = float(mx - mn) if (mx is not None and mn is not None
                                 and not np.isnan(mx) and not np.isnan(mn)) else np.nan
    return {
        "obs_dew_depression_now": dew_dep,
        "obs_trange_so_far":      trange,
    }


def _era5_slot_features(era5_day, slot_h: float) -> dict:
    """
    从当日 ERA5 数据提取与 slot_h 对应的气象特征（v5 回测专用）。

    逻辑与 build_intraday_features_v5._era5_slot_features 完全一致：
      - 取 obs_hour <= floor(slot_h) 的最新行（无未来数据泄漏）
      - 晨间辐射均值统计 6-9 时范围（slot<10h 时为 NaN）
    """
    _nan8 = {
        "era5_sw_rad_latest":        np.nan,
        "era5_direct_rad_latest":    np.nan,
        "era5_diffuse_rad_latest":   np.nan,
        "era5_rad_morning_avg":      np.nan,
        "era5_cloud_pct_latest":     np.nan,
        "era5_cloud_low_pct_latest": np.nan,
        "era5_cloud_mid_pct_latest": np.nan,
        "era5_dew_depression":       np.nan,
    }
    if era5_day is None or (hasattr(era5_day, "empty") and era5_day.empty):
        return _nan8

    era5_hour = int(slot_h)
    avail = era5_day[era5_day["obs_hour"] <= era5_hour]
    if avail.empty:
        return _nan8

    latest = avail.iloc[-1]

    morning = era5_day[
        (era5_day["obs_hour"] >= 6) &
        (era5_day["obs_hour"] <= 9) &
        (era5_day["obs_hour"] <= era5_hour)
    ]
    rad_morning = (
        float(morning["sw_radiation"].mean())
        if not morning.empty and morning["sw_radiation"].notna().any()
        else np.nan
    )

    t_e = latest.get("temp_c",      np.nan)
    d_e = latest.get("dew_point_c", np.nan)
    dew_dep = float(t_e - d_e) if (pd.notna(t_e) and pd.notna(d_e)) else np.nan

    def _fv(col):
        v = latest.get(col, np.nan)
        return float(v) if pd.notna(v) else np.nan

    return {
        "era5_sw_rad_latest":        _fv("sw_radiation"),
        "era5_direct_rad_latest":    _fv("direct_rad"),
        "era5_diffuse_rad_latest":   _fv("diffuse_rad"),
        "era5_rad_morning_avg":      rad_morning,
        "era5_cloud_pct_latest":     _fv("cloud_pct"),
        "era5_cloud_low_pct_latest": _fv("cloud_low_pct"),
        "era5_cloud_mid_pct_latest": _fv("cloud_mid_pct"),
        "era5_dew_depression":       dew_dep,
    }


def _calendar_features(d: date) -> dict:
    doy = d.timetuple().tm_yday
    return dict(
        doy_sin = float(np.sin(2 * np.pi * doy / 365.25)),
        doy_cos = float(np.cos(2 * np.pi * doy / 365.25)),
        month   = d.month,
        dow     = d.weekday(),
    )


# ── Probability mapper ─────────────────────────────────────────────────────

def _non_nan(v) -> Optional[float]:
    """Return v if it's a valid float, else None."""
    if v is None:
        return None
    try:
        f = float(v)
        return None if np.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _model_prob(result: dict, kind: str, bound: str, threshold_c: int) -> Optional[float]:
    """
    Extract P(event == 1) from a prediction result for a YES token.

    kind    : 'highest' → tmax,  'lowest' → tmin
    bound   : 'ge' | 'le' | 'eq'
    threshold_c : integer settlement threshold
    """
    target = "tmax" if kind == "highest" else "tmin"
    pm = result.get(target, {}).get("polymarket")
    if not pm:
        return None

    T = int(threshold_c)
    if bound == "ge":
        return pm.get(f"ge_{T}")
    elif bound == "le":
        # P(settled <= T) = 1 - P(settled >= T+1)
        return 1.0 - pm.get(f"ge_{T + 1}", 0.0)
    elif bound == "eq":
        # P(settled == T) = P(settled >= T) - P(settled >= T+1)
        return max(0.0, pm.get(f"ge_{T}", 0.0) - pm.get(f"ge_{T + 1}", 0.0))
    return None


def _event_happened(kind: str, bound: str, threshold_c: int,
                    tmax_settled, tmin_settled) -> Optional[int]:
    """Return 1/0 for whether the YES token resolves to 1."""
    T = int(threshold_c)
    settled = int(tmax_settled) if kind == "highest" else int(tmin_settled)
    if bound == "ge":
        return int(settled >= T)
    elif bound == "le":
        return int(settled <= T)
    elif bound == "eq":
        return int(settled == T)
    return None


def _certainty_prob(kind: str, bound: str, threshold_c: int,
                    obs_tmax: Optional[float],
                    obs_tmin: Optional[float]) -> Optional[float]:
    """
    Return P(event) = 0.0 or 1.0 if the outcome is determined by current
    observations, else None (uncertain → fall back to model).

    Logic:
      tmax contracts use obs_tmax_so_far (the day's maximum so far).
        ge_T = 1  when obs_tmax ≥ T-0.5   (settle ≥ T is guaranteed)
        eq_T = 0  when obs_tmax ≥ T+0.5   (settle > T, so = T impossible)
        le_T = 0  when obs_tmax ≥ T+0.5   (settle > T, so ≤ T impossible)

      tmin contracts use obs_tmin_so_far (the day's minimum so far).
        le_T = 1  when obs_tmin < T+0.5   (final min ≤ obs_tmin < T+0.5, settle ≤ T)
        ge_T = 0  when obs_tmin < T-0.5   (final min ≤ obs_tmin < T-0.5, settle < T)
        eq_T = 0  when obs_tmin < T-0.5   (same: settle < T, so = T impossible)
    """
    T = int(threshold_c)

    if kind == "highest":
        if obs_tmax is None:
            return None
        if bound == "ge":
            return 1.0 if obs_tmax >= T - 0.5 else None
        elif bound == "eq":
            return 0.0 if obs_tmax >= T + 0.5 else None
        elif bound == "le":
            return 0.0 if obs_tmax >= T + 0.5 else None

    elif kind == "lowest":
        if obs_tmin is None:
            return None
        if bound == "le":
            return 1.0 if obs_tmin < T + 0.5 else None
        elif bound == "ge":
            return 0.0 if obs_tmin < T - 0.5 else None
        elif bound == "eq":
            return 0.0 if obs_tmin < T - 0.5 else None

    return None


def _position_size(is_certain: bool, edge: float, base: float) -> float:
    """
    Tiered position sizing.

    Certainty signals (P locked by observation):
      |edge| ≥ 0.50  →  5× base   (market badly mispriced vs physics)
      |edge| ≥ 0.20  →  3× base
      |edge| ≥ 0.02  →  2× base

    Model edge signals:
      |edge| ≥ 0.20  →  3× base
      |edge| ≥ 0.10  →  2× base
      |edge| <  0.10 →  1× base
    """
    ae = abs(edge)
    if is_certain:
        if ae >= 0.50:
            return base * 5.0
        elif ae >= 0.20:
            return base * 3.0
        else:
            return base * 2.0
    else:
        if ae >= 0.20:
            return base * 3.0
        elif ae >= 0.10:
            return base * 2.0
        else:
            return base * 1.0


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
