"""
ml/config.py — 上海浦东机场气温预测模型配置

版本说明：
  v0  — WU-only，35 特征，3年窗口，简单切割（基线）
  v2  — WU-only，35 特征，2年窗口，简单切割
  v3  — WU-only，35 特征，walk-forward CV（2024-03-01 → 2026-03-01）
  v4  — v3 artifacts + 季节性校准（仅推断，不重新训练）
  v5  — WU+ERA5，63 特征，walk-forward CV（2024-03-01 → 2026-03-01）

模型：仅 LightGBM（quantile × 7 + L2 点预测）
数据：standard.db（WU）+ daily_labels.db + weather_om.db（ERA5/GFS，v5 专用）
"""

import math
from datetime import date, timedelta
from pathlib import Path

# ── 路径 ───────────────────────────────────────────────────────────────────
ROOT             = Path(__file__).resolve().parent.parent
DB_DIR           = ROOT / "database"
_ARTIFACTS_ROOT  = Path(__file__).resolve().parent / "artifacts"

DB_DIR.mkdir(parents=True, exist_ok=True)
_ARTIFACTS_ROOT.mkdir(parents=True, exist_ok=True)

# ── 数据库路径 ─────────────────────────────────────────────────────────────
WU_ZSPD_DB    = DB_DIR / "standard.db"         # WU 半小时观测（主数据源）
LABELS_DB     = DB_DIR / "daily_labels.db"     # 每日 tmax/tmin 标签
WEATHER_OM_DB = DB_DIR / "weather_om.db"       # ERA5 档案 + GFS 预报（v5）
IEM_ASOS_DB   = DB_DIR / "iem_asos_zspd.db"   # IEM ASOS（标签备用，只读）

# ── 单一特征文件（所有版本共用，63 特征 + 4 标签 + date）───────────────────
INTRADAY_PARQUET = DB_DIR / "intraday_features.parquet"

# ── 站点信息 ────────────────────────────────────────────────────────────────
STATION = "ZSPD"
LAT     = 31.1443
LON     = 121.8083
TZ      = "Asia/Shanghai"

# ── 默认日期范围（v0 基线用）────────────────────────────────────────────────
TODAY      = date.today()
DATA_START = date(TODAY.year - 3, TODAY.month, TODAY.day)
DATA_END   = TODAY

_m6 = TODAY.month - 6
_y6 = TODAY.year
if _m6 <= 0:
    _m6 += 12
    _y6 -= 1
TEST_START = date(_y6, _m6, 1)
TRAIN_END  = TEST_START - timedelta(days=1)


def _first_of_month_offset(months_back: int) -> date:
    m = TODAY.month - months_back
    y = TODAY.year
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


# ── 版本日期分割 ─────────────────────────────────────────────────────────────
def get_date_splits(version: str) -> tuple[date, date]:
    """返回 (data_start, test_start)。CV 版本的 test_start 即 CV 窗口结束日。"""
    if version == "v2":
        try:
            ds = date(TODAY.year - 2, TODAY.month, TODAY.day)
        except ValueError:
            ds = date(TODAY.year - 2, TODAY.month, 1)
        return ds, _first_of_month_offset(2)
    if version in ("v3", "v4", "v5"):
        return date(2024, 3, 1), date(2026, 3, 1)
    # v0 默认：3年窗口，6个月测试集
    return DATA_START, TEST_START


# ── Artifacts 目录 ──────────────────────────────────────────────────────────
def get_artifact_dir(version: str) -> Path:
    d = _ARTIFACTS_ROOT / version
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── 特征列表（按版本选择）────────────────────────────────────────────────────
def get_feature_list(version: str) -> list[str]:
    """返回该版本使用的特征列名列表。
    v5: 63 个特征（WU + ERA5 扩展）
    其他: 35 个特征（WU-only）
    """
    from ml.features import ALL_FEATURES, ALL_FEATURES_V5
    return ALL_FEATURES_V5 if version == "v5" else ALL_FEATURES


# ── 结算规则 ─────────────────────────────────────────────────────────────────
def settle(temp_c: float) -> int:
    """标准四舍五入：settle(27.5)=28，settle(27.4)=27。"""
    return math.floor(temp_c + 0.5)

TMAX_THRESHOLDS = list(range(10, 45))
TMIN_THRESHOLDS = list(range(-5, 35))

# ── 模型输出 ─────────────────────────────────────────────────────────────────
QUANTILES = [0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95]

# ── 训练时间槽（48 个，每 30 分钟一个）────────────────────────────────────────
TRAIN_SLOTS = [i * 0.5 for i in range(48)]   # 00:00, 00:30, ..., 23:30

# ── LightGBM 超参数 ──────────────────────────────────────────────────────────
# v0/v1 基础参数（小数据集）
LGBM_BASE = dict(
    n_estimators=600,
    learning_rate=0.03,
    num_leaves=31,
    min_child_samples=15,
    subsample=0.8,
    subsample_freq=1,
    colsample_bytree=0.8,
    reg_alpha=0.1,
    reg_lambda=0.5,
    random_state=42,
    n_jobs=-1,
    verbose=-1,
)

# v2/v3：2年/CV 窗口，数据量适中
LGBM_V2 = dict(
    n_estimators=800,
    learning_rate=0.03,
    num_leaves=40,
    min_child_samples=20,
    subsample=0.8,
    subsample_freq=1,
    colsample_bytree=0.8,
    reg_alpha=0.05,
    reg_lambda=0.3,
    random_state=42,
    n_jobs=-1,
    verbose=-1,
)

# v5：ERA5 扩展特征（63 个），num_leaves 增大，列采样降低
LGBM_V5 = dict(
    n_estimators=1200,
    learning_rate=0.02,
    num_leaves=63,
    min_child_samples=25,
    subsample=0.8,
    subsample_freq=1,
    colsample_bytree=0.65,
    reg_alpha=0.1,
    reg_lambda=0.5,
    random_state=42,
    n_jobs=-1,
    verbose=-1,
)


def get_lgbm_params(version: str) -> dict:
    if version in ("v2", "v3"):
        return LGBM_V2
    if version == "v5":
        return LGBM_V5
    return LGBM_BASE


# ── 训练控制参数 ─────────────────────────────────────────────────────────────
EARLY_STOPPING_ROUNDS = 50   # LightGBM early stopping
VALID_DAYS            = 30   # 内部验证集长度（天）
