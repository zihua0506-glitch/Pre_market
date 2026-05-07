"""
Feature definitions for the intraday temperature prediction model.

All features are derived from:
  - Current-day observations (merged half-hourly: WU > IEM > Ogimet)
  - Historical daily labels (tmax/tmin lags and rolling stats)
  - Calendar (day-of-year, month, day-of-week)

No NWP / ERA5 / forecast data is used.
"""

from __future__ import annotations

# ── Feature column groups ──────────────────────────────────────────────────

# Intraday observation features (NaN when no obs yet)
INTRADAY_FEATURES = [
    "obs_hour_frac",        # fractional hour of the prediction slot
    "obs_tmax_so_far",      # running max temp since midnight local
    "obs_tmin_so_far",      # running min temp since midnight local
    "obs_temp_now",         # latest observed temp_c
    "obs_temp_prev1h",      # temp_c ~1 hour ago
    "obs_temp_trend_1h",    # obs_temp_now - obs_temp_prev1h
    "obs_dew_now",          # latest dewpoint
    "obs_dew_mean",         # mean dewpoint so far today
    "obs_wind_now",         # latest wind speed (knots)
    "obs_pressure_now",     # latest QNH pressure (hPa)
    "obs_count",            # number of observations so far today
]

# Historical lag features (from prior days -- no leakage)
LAG_FEATURES = [
    "tmax_lag1", "tmax_lag2", "tmax_lag3",
    "tmax_lag4", "tmax_lag5", "tmax_lag6", "tmax_lag7",
    "tmax_roll3_mean", "tmax_roll7_mean", "tmax_roll7_std",
    "tmin_lag1", "tmin_lag2", "tmin_lag3",
    "tmin_lag4", "tmin_lag5", "tmin_lag6", "tmin_lag7",
    "tmin_roll3_mean", "tmin_roll7_mean", "tmin_roll7_std",
]

# Calendar features
CALENDAR_FEATURES = [
    "doy_sin", "doy_cos",   # smooth annual cycle
    "month",                # 1-12
    "dow",                  # 0=Mon ... 6=Sun
]

# v0/v1/v2/v3 使用的全量特征列表（35个，固定顺序）
ALL_FEATURES = INTRADAY_FEATURES + LAG_FEATURES + CALENDAR_FEATURES

# 标签列
LABEL_TMAX = "tmax_c"
LABEL_TMIN = "tmin_c"
LABEL_TMAX_SETTLED = "tmax_settled"
LABEL_TMIN_SETTLED = "tmin_settled"


# ── v5 新增特征 ─────────────────────────────────────────────────────────────
# （所有 v5 特征均来自 weather_om.db / om_hourly，ERA5 档案数据零缺失）

# 派生观测特征：由现有 WU 观测量显式计算，辅助树模型捕捉交叉效应
V5_DERIVED_FEATURES = [
    "obs_dew_depression_now",  # obs_temp_now - obs_dew_now（正值→干燥→晴天倾向）
    "obs_trange_so_far",       # obs_tmax_so_far - obs_tmin_so_far（当日振幅大→晴）
]

# ERA5 再分析气象特征：训练时使用档案数据，推断时替换为 GFS 预报数据
# 全部在 weather_om.db 中 0% 缺失（wind_speed_80m / visibility_m 除外，已排除）
ERA5_FEATURES = [
    "era5_sw_rad_latest",        # 当前槽前最近整点短波辐射（W/m²，r=+0.68 with tmax）
    "era5_direct_rad_latest",    # 最近整点直接辐射（r=+0.611）
    "era5_diffuse_rad_latest",   # 最近整点散射辐射
    "era5_rad_morning_avg",      # 6-9 时辐射均值（slot≥10h 时非 NaN，r=+0.740，最强新信号）
    "era5_cloud_pct_latest",     # 最近整点总云量（%）
    "era5_cloud_low_pct_latest", # 最近整点低云量（与 tmax 负相关最强，r=-0.29 at 16h）
    "era5_cloud_mid_pct_latest", # 最近整点中云量
    "era5_dew_depression",       # ERA5 气温 - 露点（干燥度，正值→大气稳定→晴）
]

# 扩展滞后特征：lag8-14 捕捉 2 周尺度天气形势，roll14/roll30 提供季节背景
V5_LAG_FEATURES = [
    "tmax_lag8",  "tmax_lag9",  "tmax_lag10", "tmax_lag11",
    "tmax_lag12", "tmax_lag13", "tmax_lag14",
    "tmin_lag8",  "tmin_lag9",  "tmin_lag10", "tmin_lag11",
    "tmin_lag12", "tmin_lag13", "tmin_lag14",
    "tmax_roll14_mean", "tmax_roll30_mean",  # 2周/月均值（季节气候背景）
    "tmin_roll14_mean", "tmin_roll30_mean",
]

# v5 完整特征列表：11 + 2 + 8 + 20 + 18 + 4 = 63 个
ALL_FEATURES_V5 = (
    INTRADAY_FEATURES      # 11: 原有 WU 实时观测特征
    + V5_DERIVED_FEATURES  # 2:  派生观测特征（露点差、当日振幅）
    + ERA5_FEATURES        # 8:  ERA5 气象特征（辐射、云量、露点差）
    + LAG_FEATURES         # 20: 原有 lag1-7 + rolling7
    + V5_LAG_FEATURES      # 18: 扩展 lag8-14 + rolling14/30
    + CALENDAR_FEATURES    # 4:  日历特征
)
