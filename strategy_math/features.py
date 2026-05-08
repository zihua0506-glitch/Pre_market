"""
strategy_math/features.py

Slot-level feature builders shared by backtest and live strategy. Pure
functions over pandas DataFrames / dicts; no I/O.

Five layers of features, all keyed by `slot_h` (= 0.0 .. 23.5):

  _obs_features          intraday WU obs up to slot_h: tmax/tmin so far,
                         current temp, dew, wind, pressure, count
  _obs_derived_features  v5 derivatives: dew depression, trange so far
  _era5_slot_features    v5 ERA5/GFS reanalysis at slot_h: radiation,
                         clouds, dew depression
  _lag_features          previous N days' settled tmax/tmin + rolling stats
  _calendar_features     day-of-year sin/cos + month + weekday

Plus `_non_nan(v)` — convenience: returns float or None (drops NaN).
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import numpy as np
import pandas as pd


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
        obs_dew_mean      = float(obs["dew_c"].mean()) if obs["dew_c"].notna().any()    else np.nan,
        obs_wind_now      = float(last["wind_kt"])     if pd.notna(last["wind_kt"])     else np.nan,
        obs_pressure_now  = float(last["pressure_hpa"]) if pd.notna(last["pressure_hpa"]) else np.nan,
        obs_count         = int(len(obs)),
    )


def _lag_features(target_date: date, labels_df: pd.DataFrame,
                  version: str = "v0") -> dict:
    """
    Build lag + rolling features from past labels.

    v0/v1/v2/v3 : lag1-7  + roll3/7   (20 features)
    v5          : lag1-14 + roll3/7/14/30   (38 features)
    """
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
    """v5 derived obs features (dew depression, trange so far)."""
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
    ERA5/GFS slot features — radiation, clouds, dew depression — at slot_h.

    Aligned to `build_intraday_features_v5._era5_slot_features`:
      - take obs_hour <= floor(slot_h) for the latest row (no future leak)
      - morning radiation average is over obs_hour ∈ [6, 9] (so meaningful only after 9 CST)
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


def _non_nan(v) -> Optional[float]:
    """Return v if it's a valid float, else None (NaN is treated as None)."""
    if v is None:
        return None
    try:
        f = float(v)
        return None if np.isnan(f) else f
    except (TypeError, ValueError):
        return None
