"""
ZSPD temperature model pipeline.

Usage:
  python -m ml.pipeline update
      Pull the latest WU observations into standard.db (last 2 days),
      refresh Polymarket token metadata (token.db),
      and incrementally update price series for the last 7 days (data.db).

  python -m ml.pipeline --temp FLOAT [HH:MM] [--retrain] [--version v0|v2|v3|v5]
      Record a manual temperature reading for the given time slot (CST).
      HH:MM is optional; omit it to use the most recent past half-hour mark.
      --retrain  rebuild labels → features → retrain all models → evaluate

  python -m ml.pipeline labels
      Rebuild daily labels (daily_labels.db).

  python -m ml.pipeline features
      Rebuild intraday feature matrix (intraday_features.parquet, unified).

  python -m ml.pipeline train [--version v0|v2|v3|v5]
      Train all models for the given version.
      v3/v5 use walk-forward CV.

  python -m ml.pipeline evaluate [--version v0|v2|v3|v5]
      Evaluate all models on the test set.

  python -m ml.pipeline build [--version v0|v2|v3|v5]
      labels → features → train → evaluate

  python -m ml.pipeline full [--version v0|v2|v3|v5]
      update → labels → features → train → evaluate

  python -m ml.pipeline predict [--version v0|v2|v3|v5]
      Run intraday inference and print the prediction.

版本说明：
  v0  — WU-only，35 特征，3年窗口（传统基线）
  v2  — WU-only，35 特征，2年窗口
  v3  — WU-only，35 特征，walk-forward CV（2024-03-01 → 2026-03-01）
  v4  — v3 artifacts + 季节性校准（仅推断，不重新训练）
  v5  — WU+ERA5 扩展，63 特征，walk-forward CV（2024-03-01 → 2026-03-01）

Examples:
  python -m ml.pipeline update
  python -m ml.pipeline --temp 27.8
  python -m ml.pipeline --temp 27.8 14:30
  python -m ml.pipeline --temp 27.8 14:30 --retrain
  python -m ml.pipeline features
  python -m ml.pipeline train --version v5
  python -m ml.pipeline evaluate --version v3
  python -m ml.pipeline build --version v5
  python -m ml.pipeline predict --version v5
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ml.config import DATA_END, DATA_START

SHANGHAI_OFFSET = timedelta(hours=8)


# ── helpers ───────────────────────────────────────────────────────────────

def _parse_version(args: list[str], default: str = "v0") -> str:
    """从命令行参数中提取 --version v0|v2|v3|v5。"""
    for i, a in enumerate(args):
        if a == "--version" and i + 1 < len(args):
            return args[i + 1]
        if a in ("--v0", "--v2", "--v3", "--v5"):
            return a.lstrip("-")
    return default


# ── 1. Update standard.db ─────────────────────────────────────────────────

def update_db(start: Optional[date] = None, end: Optional[date] = None) -> None:
    from core.fetch_zspd_history import main as _wu_main
    s = (start or DATA_START).strftime("%Y%m%d")
    e = (end   or DATA_END  ).strftime("%Y%m%d")
    print(f"\n[UpdateDB] Fetching WU ZSPD {s} → {e} ...")
    _wu_main(["", s, e])
    print("[UpdateDB] Done.")


def update_market(days: int = 7) -> None:
    from core.fetch_tokens import main as _fetch_tokens
    from core.fetch_data import update_recent as _update_recent

    print("\n[UpdateMarket] 刷新 token 元数据 (gamma-api) ...")
    _fetch_tokens()

    print(f"\n[UpdateMarket] 增量拉取近 {days} 天价格序列 ...")
    _update_recent(days=days)


# ── 2. Manual temperature input ───────────────────────────────────────────

def _nearest_past_30min(dt_utc: datetime) -> datetime:
    epoch   = int(dt_utc.timestamp())
    floored = (epoch // 1800) * 1800
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def _cst_hhmm_to_utc(hhmm: str, reference_utc: datetime) -> datetime:
    h, m = int(hhmm.split(":")[0]), int(hhmm.split(":")[1])
    ref_cst  = reference_utc + SHANGHAI_OFFSET
    slot_cst = ref_cst.replace(hour=h, minute=m, second=0, microsecond=0)
    return slot_cst - SHANGHAI_OFFSET


def record_temp(temp_c: float, slot_cst: Optional[str] = None) -> None:
    from ml.data.fetch_wu_latest import insert_observation

    now_utc = datetime.now(timezone.utc)
    if slot_cst:
        slot_utc = _cst_hhmm_to_utc(slot_cst, now_utc)
        epoch    = int(slot_utc.timestamp())
        slot_utc = datetime.fromtimestamp((epoch // 1800) * 1800, tz=timezone.utc)
    else:
        slot_utc = _nearest_past_30min(now_utc)

    slot_label = (slot_utc + SHANGHAI_OFFSET).strftime("%Y-%m-%d %H:%M CST")
    obs = {"slot_utc": slot_utc, "temp_c": temp_c, "source": "manual"}
    insert_observation(obs)
    print(f"[RecordTemp] {slot_label}  temp={temp_c}°C  → written to standard.db")


# ── 3. Individual pipeline steps ──────────────────────────────────────────

def run_labels() -> None:
    from ml.data.build_labels import build_labels
    print("\n[Labels] Rebuilding daily labels ...")
    build_labels()
    print("[Labels] Done.")


def run_features() -> None:
    from ml.data.build_intraday_features import build_intraday_features
    print("\n[Features] 重建统一日内特征矩阵（63 个特征）...")
    build_intraday_features()
    print("[Features] 完成。")


def run_train(version: str = "v0") -> None:
    from ml.models.train import train_all
    print(f"\n[Train] Training all models [{version}] ...")
    train_all(version)
    print("[Train] Done.")


def run_evaluate(version: str = "v0") -> None:
    from ml.models.evaluate import evaluate_all
    print(f"\n[Evaluate] Evaluating on test set [{version}] ...")
    evaluate_all(version)
    print("[Evaluate] Done.")


def run_predict(version: str = "v0") -> None:
    """从 standard.db 当前状态运行日内推断。v5 同时读取 ERA5 预报特征。"""
    from ml.data.build_intraday_features import (
        _calendar_features, _era5_slot_features, _load_era5,
        _load_wu, _slot_derived, _slot_features,
    )
    from ml.models.predict import Predictor, print_prediction

    now_utc   = datetime.now(timezone.utc)
    now_cst   = now_utc + SHANGHAI_OFFSET
    target_dt = now_cst.date()
    hour_frac = now_cst.hour + now_cst.minute / 60.0

    print(f"\n[Predict] target={target_dt}  as-of {now_cst.strftime('%H:%M CST')} ...")

    wu = _load_wu()
    today_obs = wu[wu["date"] == str(target_dt)].copy()

    obs_f   = _slot_features(today_obs, hour_frac)
    derived = _slot_derived(obs_f) if version == "v5" else {}

    # v5：从 weather_om.db 读取当日 ERA5/GFS 预报特征
    era5_f = {}
    if version == "v5":
        era5_by_date = _load_era5()
        era5_day = era5_by_date.get(str(target_dt), None)
        era5_f = _era5_slot_features(era5_day, hour_frac)

    cal = _calendar_features(target_dt)

    # 历史滞后特征（从 parquet 中读取当日行或重新构建简版）
    import pandas as pd
    from ml.config import INTRADAY_PARQUET
    feat_row = pd.Series({**obs_f, **derived, **era5_f, **cal,
                          "obs_hour_frac": float(hour_frac)})
    if INTRADAY_PARQUET.exists():
        df = pd.read_parquet(INTRADAY_PARQUET)
        recent = df[df["date"] < str(target_dt)].tail(1)
        if not recent.empty:
            lag_cols = [c for c in recent.columns if "lag" in c or "roll" in c]
            for c in lag_cols:
                feat_row[c] = recent.iloc[0][c]

    p      = Predictor(version=version)
    result = p.predict(
        target_date=str(target_dt),
        feature_row=feat_row,
        obs_hour_frac=hour_frac,
    )
    print_prediction(result)


# ── 4. Composite pipelines ────────────────────────────────────────────────

def retrain(version: str = "v0") -> None:
    """完整重训流程：labels → features → train → evaluate。"""
    run_labels()
    run_features()
    run_train(version)
    run_evaluate(version)


def run_full(version: str = "v0") -> None:
    """update（WU + Polymarket）→ retrain。"""
    today        = (datetime.now(timezone.utc) + SHANGHAI_OFFSET).date()
    two_days_ago = today - timedelta(days=1)
    update_db(start=two_days_ago, end=today)
    update_market(days=7)
    retrain(version)


# ── CLI ───────────────────────────────────────────────────────────────────

_USAGE = __doc__


def main(argv: list[str]) -> None:
    args = argv[1:]

    if not args:
        print(_USAGE)
        return

    cmd = args[0]

    if cmd == "update":
        today        = (datetime.now(timezone.utc) + SHANGHAI_OFFSET).date()
        two_days_ago = today - timedelta(days=1)
        update_db(start=two_days_ago, end=today)
        update_market(days=7)
        return

    if cmd == "labels":
        run_labels()
        return

    if cmd == "features":
        run_features()
        return

    if cmd == "train":
        ver = _parse_version(args[1:])
        run_train(ver)
        return

    if cmd in ("evaluate", "eval"):
        ver = _parse_version(args[1:])
        run_evaluate(ver)
        return

    if cmd == "build":
        ver = _parse_version(args[1:])
        retrain(ver)
        return

    if cmd == "full":
        ver = _parse_version(args[1:])
        run_full(ver)
        return

    if cmd in ("predict", "intraday"):
        ver = _parse_version(args[1:])
        run_predict(ver)
        return

    if cmd == "--temp":
        if len(args) < 2:
            print("Error: --temp requires a temperature value (e.g. --temp 27.8)")
            return
        try:
            temp_c = float(args[1])
        except ValueError:
            print(f"Error: invalid temperature '{args[1]}'")
            return

        slot_cst: Optional[str] = None
        remaining = args[2:]
        if remaining and not remaining[0].startswith("--"):
            slot_cst  = remaining[0]
            remaining = remaining[1:]

        do_retrain = "--retrain" in remaining
        ver        = _parse_version(remaining)

        record_temp(temp_c, slot_cst)

        if do_retrain:
            retrain(ver)
        return

    print(f"Unknown argument: '{cmd}'")
    print(_USAGE)


if __name__ == "__main__":
    main(sys.argv)
