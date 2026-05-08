"""
ml/data/build_intraday_features.py

构建统一日内特征矩阵（63 个特征 + 4 个标签 + date/obs_hour_frac）。
所有版本（v0/v2/v3/v5）共用同一 parquet，训练时按版本选取特征子集。

特征构成（共 63 个）：
  WU 观测特征    11 个：实测气温/露点/风速/气压（截止当前时刻）
  派生观测特征    2 个：露点差、当日已观测振幅
  ERA5 气象特征   8 个：辐射、云量、露点差（来自 weather_om.db）
  历史滞后特征   38 个：tmax/tmin lag1-14 + roll3/7/14/30
  日历特征        4 个：doy_sin/cos、month、dow

v0/v2/v3 使用其中 35 个（WU 观测 + lag1-7 + roll3/7 + 日历）。
v5 使用全部 63 个。

输出：database/intraday_features.parquet

运行：
    python -m ml.data.build_intraday_features
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from ml.config import INTRADAY_PARQUET, LABELS_DB, TRAIN_SLOTS, WEATHER_OM_DB, WU_ZSPD_DB


# ── 数据加载 ─────────────────────────────────────────────────────────────────

def _load_wu() -> pd.DataFrame:
    """从 standard.db 加载 WU 半小时观测。"""
    conn = sqlite3.connect(WU_ZSPD_DB)
    df = pd.read_sql(
        """SELECT obs_date_local AS date,
                  ((valid_time_gmt + 28800) % 86400) / 3600.0 AS hour,
                  temp_c,
                  dew_point_c               AS dew_c,
                  wind_speed_kmh * 0.539957 AS wind_kt,
                  pressure_hpa
           FROM zspd_observations
           WHERE temp_c IS NOT NULL
           ORDER BY valid_time_gmt""",
        conn,
    )
    conn.close()
    return df


def _load_era5() -> dict[str, pd.DataFrame]:
    """
    从 weather_om.db 加载所有 om_hourly 行，按 obs_date 分组。
    同一 (obs_date, obs_hour) 有多行时，优先保留 archive（精度更高）。
    """
    if not WEATHER_OM_DB.exists():
        print("  警告：weather_om.db 不存在，ERA5 特征将全部为 NaN")
        return {}
    conn = sqlite3.connect(WEATHER_OM_DB)
    df = pd.read_sql(
        """SELECT obs_date, obs_hour,
                  sw_radiation, direct_rad, diffuse_rad,
                  cloud_pct, cloud_low_pct, cloud_mid_pct,
                  temp_c, dew_point_c, source
           FROM om_hourly
           ORDER BY obs_date, obs_hour,
                    CASE WHEN source = 'archive' THEN 0 ELSE 1 END""",
        conn,
    )
    conn.close()
    df = df.drop_duplicates(subset=["obs_date", "obs_hour"], keep="first")
    result = {
        d: grp.sort_values("obs_hour").reset_index(drop=True)
        for d, grp in df.groupby("obs_date")
    }
    print(f"  ERA5: {len(df):,} 行，{len(result)} 天")
    return result


def _load_labels() -> pd.DataFrame:
    """加载每日最高/最低气温标签。"""
    conn = sqlite3.connect(LABELS_DB)
    df = pd.read_sql(
        """SELECT obs_date AS date,
                  tmax_raw AS tmax_c, tmin_raw AS tmin_c,
                  tmax_settled, tmin_settled
           FROM daily_labels ORDER BY obs_date""",
        conn,
    )
    conn.close()
    return df


# ── 历史滞后特征（扩展版，lag1-14 + roll3/7/14/30）────────────────────────────

def _add_lag_features(labels: pd.DataFrame) -> pd.DataFrame:
    """
    计算扩展滞后特征，全部使用 shift(1) 起点确保无数据泄漏。
    v0/v2/v3 仅用 lag1-7 + roll3/7 子集；v5 用全部 lag1-14 + roll3/7/14/30。
    """
    df = labels.copy().sort_values("date").reset_index(drop=True)

    for lag in range(1, 15):
        df[f"tmax_lag{lag}"] = df["tmax_c"].shift(lag)
        df[f"tmin_lag{lag}"] = df["tmin_c"].shift(lag)

    s_tmax = df["tmax_c"].shift(1)
    s_tmin = df["tmin_c"].shift(1)

    df["tmax_roll3_mean"]  = s_tmax.rolling(3).mean()
    df["tmax_roll7_mean"]  = s_tmax.rolling(7).mean()
    df["tmax_roll7_std"]   = s_tmax.rolling(7).std()
    df["tmax_roll14_mean"] = s_tmax.rolling(14).mean()
    df["tmax_roll30_mean"] = s_tmax.rolling(30).mean()

    df["tmin_roll3_mean"]  = s_tmin.rolling(3).mean()
    df["tmin_roll7_mean"]  = s_tmin.rolling(7).mean()
    df["tmin_roll7_std"]   = s_tmin.rolling(7).std()
    df["tmin_roll14_mean"] = s_tmin.rolling(14).mean()
    df["tmin_roll30_mean"] = s_tmin.rolling(30).mean()

    return df


# ── 时间槽特征提取 ────────────────────────────────────────────────────────────

def _slot_features(day_obs: pd.DataFrame, hour_cutoff: float) -> dict:
    """从截止 hour_cutoff 时刻的 WU 观测中提取特征（11 个）。"""
    mask = day_obs["hour"] <= hour_cutoff
    obs  = day_obs[mask]

    if obs.empty:
        return {
            "obs_tmax_so_far": np.nan, "obs_tmin_so_far": np.nan,
            "obs_temp_now": np.nan, "obs_temp_prev1h": np.nan,
            "obs_temp_trend_1h": np.nan, "obs_dew_now": np.nan,
            "obs_dew_mean": np.nan, "obs_wind_now": np.nan,
            "obs_pressure_now": np.nan, "obs_count": 0,
        }

    temps = obs["temp_c"].dropna()
    last  = obs.iloc[-1]
    temp_now = float(last["temp_c"]) if pd.notna(last["temp_c"]) else np.nan

    prev = day_obs[day_obs["hour"] <= (hour_cutoff - 1.0)]
    if not prev.empty and pd.notna(prev.iloc[-1]["temp_c"]):
        prev1h = float(prev.iloc[-1]["temp_c"])
        trend  = (temp_now - prev1h) if pd.notna(temp_now) else np.nan
    else:
        prev1h = np.nan
        trend  = np.nan

    return {
        "obs_tmax_so_far":   float(temps.max()) if not temps.empty else np.nan,
        "obs_tmin_so_far":   float(temps.min()) if not temps.empty else np.nan,
        "obs_temp_now":      temp_now,
        "obs_temp_prev1h":   prev1h,
        "obs_temp_trend_1h": trend,
        "obs_dew_now":       float(last["dew_c"])        if pd.notna(last["dew_c"])       else np.nan,
        "obs_dew_mean":      float(obs["dew_c"].mean())  if obs["dew_c"].notna().any()    else np.nan,
        "obs_wind_now":      float(last["wind_kt"])      if pd.notna(last["wind_kt"])     else np.nan,
        "obs_pressure_now":  float(last["pressure_hpa"]) if pd.notna(last["pressure_hpa"]) else np.nan,
        "obs_count":         int(len(obs)),
    }


def _slot_derived(obs_feats: dict) -> dict:
    """计算 v5 派生观测特征（2 个），由 _slot_features 结果推导。"""
    t  = obs_feats["obs_temp_now"]
    d  = obs_feats["obs_dew_now"]
    mx = obs_feats["obs_tmax_so_far"]
    mn = obs_feats["obs_tmin_so_far"]
    return {
        "obs_dew_depression_now": float(t - d)   if pd.notna(t) and pd.notna(d)   else np.nan,
        "obs_trange_so_far":      float(mx - mn) if pd.notna(mx) and pd.notna(mn) else np.nan,
    }


def _era5_slot_features(era5_day: pd.DataFrame | None, slot_h: float) -> dict:
    """
    从当日 ERA5 数据提取与 slot_h 对应的气象特征（8 个）。
    取 obs_hour <= floor(slot_h) 的最新行，无未来数据泄漏。
    晨间辐射均值统计 6-9 时范围（slot < 10h 时为 NaN，LightGBM 原生处理）。
    """
    _nan8 = {
        "era5_sw_rad_latest": np.nan, "era5_direct_rad_latest": np.nan,
        "era5_diffuse_rad_latest": np.nan, "era5_rad_morning_avg": np.nan,
        "era5_cloud_pct_latest": np.nan, "era5_cloud_low_pct_latest": np.nan,
        "era5_cloud_mid_pct_latest": np.nan, "era5_dew_depression": np.nan,
    }
    if era5_day is None or era5_day.empty:
        return _nan8

    avail = era5_day[era5_day["obs_hour"] <= int(slot_h)]
    if avail.empty:
        return _nan8

    latest = avail.iloc[-1]

    morning = era5_day[
        (era5_day["obs_hour"] >= 6) &
        (era5_day["obs_hour"] <= 9) &
        (era5_day["obs_hour"] <= int(slot_h))
    ]
    rad_morning = (
        float(morning["sw_radiation"].mean())
        if not morning.empty and morning["sw_radiation"].notna().any()
        else np.nan
    )

    t_e = latest.get("temp_c", np.nan)
    d_e = latest.get("dew_point_c", np.nan)
    dew_dep = float(t_e - d_e) if pd.notna(t_e) and pd.notna(d_e) else np.nan

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
    return {
        "doy_sin": np.sin(2 * np.pi * doy / 365.25),
        "doy_cos": np.cos(2 * np.pi * doy / 365.25),
        "month":   d.month,
        "dow":     d.weekday(),
    }


# ── 主构建函数 ───────────────────────────────────────────────────────────────

def build_intraday_features() -> None:
    """
    构建统一日内特征矩阵并保存为 INTRADAY_PARQUET。

    流程：
      1. 加载 WU 观测（standard.db）
      2. 加载 ERA5 气象数据（weather_om.db，archive 优先于 forecast）
      3. 加载每日标签（daily_labels.db）并计算扩展滞后
      4. 按 (日期 × 48 时间槽) 逐行构建 63 个特征
      5. 保存至 database/intraday_features.parquet
    """
    print("=== 构建统一日内特征矩阵 ===")

    print("加载 WU 观测...")
    wu = _load_wu()
    print(f"  WU: {len(wu):,} 半小时行，{wu['date'].nunique()} 天")

    print("加载 ERA5 气象数据...")
    era5_by_date = _load_era5()

    print("加载每日标签并计算扩展滞后...")
    labels = _load_labels()
    labels_with_lags = _add_lag_features(labels)
    label_idx = labels_with_lags.set_index("date")
    print(f"  标签：{len(labels)} 天，{labels['date'].min()} → {labels['date'].max()}")

    if wu.empty or labels.empty:
        print("数据为空，请先运行数据拉取程序。")
        return

    obs_by_date = {d: grp.sort_values("hour") for d, grp in wu.groupby("date")}

    _lag_keys = [
        "tmax_lag1",  "tmax_lag2",  "tmax_lag3",  "tmax_lag4",
        "tmax_lag5",  "tmax_lag6",  "tmax_lag7",  "tmax_lag8",
        "tmax_lag9",  "tmax_lag10", "tmax_lag11", "tmax_lag12",
        "tmax_lag13", "tmax_lag14",
        "tmax_roll3_mean", "tmax_roll7_mean", "tmax_roll7_std",
        "tmax_roll14_mean", "tmax_roll30_mean",
        "tmin_lag1",  "tmin_lag2",  "tmin_lag3",  "tmin_lag4",
        "tmin_lag5",  "tmin_lag6",  "tmin_lag7",  "tmin_lag8",
        "tmin_lag9",  "tmin_lag10", "tmin_lag11", "tmin_lag12",
        "tmin_lag13", "tmin_lag14",
        "tmin_roll3_mean", "tmin_roll7_mean", "tmin_roll7_std",
        "tmin_roll14_mean", "tmin_roll30_mean",
    ]

    rows: list[dict] = []
    era5_miss = 0

    for d_str in sorted(label_idx.index):
        lbl = label_idx.loc[d_str]
        if pd.isna(lbl["tmax_c"]) or pd.isna(lbl["tmin_c"]):
            continue

        day_obs  = obs_by_date.get(d_str, pd.DataFrame(
            columns=["hour", "temp_c", "dew_c", "wind_kt", "pressure_hpa"]
        ))
        era5_day = era5_by_date.get(d_str, None)
        if era5_day is None:
            era5_miss += 1

        cal = _calendar_features(date.fromisoformat(d_str))

        for slot_h in TRAIN_SLOTS:
            obs_f   = _slot_features(day_obs, slot_h)
            derived = _slot_derived(obs_f)
            era5_f  = _era5_slot_features(era5_day, slot_h)

            row = {
                "date":          d_str,
                "obs_hour_frac": float(slot_h),
                **obs_f,
                **derived,
                **era5_f,
                **{k: lbl.get(k, np.nan) for k in _lag_keys},
                **cal,
                "tmax_c":       float(lbl["tmax_c"]),
                "tmin_c":       float(lbl["tmin_c"]),
                "tmax_settled": int(lbl["tmax_settled"]),
                "tmin_settled": int(lbl["tmin_settled"]),
            }
            rows.append(row)

    df = pd.DataFrame(rows)
    print(f"\n构建完成：{len(df):,} 行（{df['date'].nunique()} 天 × {len(TRAIN_SLOTS)} 槽）")

    if era5_miss > 0:
        print(f"  警告：{era5_miss} 天无 ERA5 数据（era5_* 特征为 NaN，LightGBM 原生处理）")

    leakage = (df["obs_tmax_so_far"] > df["tmax_c"] + 0.05).sum()
    if leakage > 0:
        print(f"  警告：{leakage} 行 obs_tmax_so_far > tmax_c（数据源精度差异，可忽略）")

    df.to_parquet(INTRADAY_PARQUET, index=False)
    print(f"已保存 → {INTRADAY_PARQUET}")
    print(f"  列数：{len(df.columns)}（特征 63 + 标签 4 + date/obs_hour_frac）")
    print(f"  日期范围：{df['date'].min()} → {df['date'].max()}")

    era5_cols = [c for c in df.columns if c.startswith("era5_")]
    if era5_cols:
        null_rates = df[era5_cols].isnull().mean().mul(100).round(1)
        print("\nERA5 特征缺失率（%）：")
        print(null_rates.to_string())


if __name__ == "__main__":
    build_intraday_features()
