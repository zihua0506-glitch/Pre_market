"""
scripts/daily_retrain.py

每日收市后自动重训练流水线。

流程（D 日结束后运行）：
  1. 确认 D 日 WU 观测数据完整（至少 20 条读数）
  2. 重建 daily_labels.db（含 D 日）
  3. 重建 intraday_features.parquet
  4. 重训练模型（默认 v5，可指定）
  5. 更新 v4 季节性校准系数（calibration.json）
  6. 在最近 N 天窗口上运行 backtest（可选），输出简报

典型用法：
  # 当天 23:59 手动触发（或由 Task Scheduler 调用）
  python -m scripts.daily_retrain

  # 补训某历史日期
  python -m scripts.daily_retrain --date 2026-05-06

  # 仅重建 labels + features，不重训练
  python -m scripts.daily_retrain --no-train

  # 指定模型版本（v3 或 v5）
  python -m scripts.daily_retrain --version v5

  # 训练完运行 backtest（最近 30 天）
  python -m scripts.daily_retrain --backtest --backtest-days 30

Windows Task Scheduler 设置参考（每日 00:10 CST = 前日 16:10 UTC）：
  触发器: 每天 16:10 UTC
  操作:   C:\\...\\Pre_market\\.venv\\Scripts\\python.exe
  参数:   -m scripts.daily_retrain --version v5
  起始目录: C:\\...\\Pre_market
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CST = timezone(timedelta(hours=8))

# ── 常量 ──────────────────────────────────────────────────────────────────────

MIN_OBS_ROWS   = 20    # 一天至少需要的 WU 读数才认为数据完整
RETRAIN_TIMEOUT = 3600 # 最多等待 WU 数据 1 小时（秒）
POLL_INTERVAL   = 120  # 每隔 2 分钟检查一次 WU 数据


# ── WU 数据完整性检查 ─────────────────────────────────────────────────────────

def _count_wu_obs(target_date: date) -> int:
    """返回 standard.db 中 target_date 的观测行数。"""
    from ml.config import WU_ZSPD_DB
    if not WU_ZSPD_DB.exists():
        return 0
    conn = sqlite3.connect(WU_ZSPD_DB)
    row = conn.execute(
        "SELECT COUNT(*) FROM zspd_observations WHERE obs_date_local = ?",
        (target_date.isoformat(),),
    ).fetchone()
    conn.close()
    return int(row[0]) if row else 0


def _wait_for_wu(target_date: date, verbose: bool = True) -> bool:
    """
    等待 standard.db 中 target_date 的观测数据达到 MIN_OBS_ROWS 行。
    超过 RETRAIN_TIMEOUT 秒后放弃，返回 False。
    """
    deadline = time.monotonic() + RETRAIN_TIMEOUT
    while time.monotonic() < deadline:
        n = _count_wu_obs(target_date)
        if n >= MIN_OBS_ROWS:
            if verbose:
                print(f"  [WU] {target_date} 已有 {n} 条读数 ✓")
            return True
        remaining = int(deadline - time.monotonic())
        if verbose:
            print(f"  [WU] {target_date} 当前 {n} 条（需 {MIN_OBS_ROWS}），"
                  f"等待中… {remaining}s 剩余")
        time.sleep(POLL_INTERVAL)
    return False


# ── 各流水线步骤 ──────────────────────────────────────────────────────────────

def step_fetch_wu(target_date: date) -> None:
    """拉取最新 WU 数据（两天窗口，保证 target_date 被覆盖）。"""
    from core.fetch_zspd_history import main as _wu_main
    start = (target_date - timedelta(days=1)).strftime("%Y%m%d")
    end   = target_date.strftime("%Y%m%d")
    print(f"\n[1/5] 拉取 WU 数据 {start} → {end} ...")
    _wu_main(["", start, end])


def step_build_labels() -> None:
    """重建 daily_labels.db（全量，幂等）。"""
    from ml.data.build_labels import build_labels
    print("\n[2/5] 重建 daily_labels.db ...")
    build_labels()


def step_build_features() -> None:
    """重建 intraday_features.parquet（全量，幂等）。"""
    from ml.data.build_intraday_features import build_intraday_features
    print("\n[3/5] 重建 intraday_features.parquet ...")
    build_intraday_features()


def step_train(version: str) -> None:
    """重训练指定版本的所有模型（LightGBM × 10 + v4 不重训）。"""
    if version == "v4":
        print("\n[4/5] v4 不重训练（使用 v3 artifacts + 校准），跳过。")
        return
    from ml.models.train import train_all
    print(f"\n[4/5] 重训练模型 [{version}]（walk-forward CV，可能需要数分钟）...")
    t0 = time.monotonic()
    train_all(version)
    elapsed = int(time.monotonic() - t0)
    print(f"  训练完成，耗时 {elapsed}s。")


def step_update_v4_calibration() -> None:
    """
    更新 v4 月度偏差校准系数。
    使用最新 intraday_features.parquet 中 2023-05-01 到 v3 训练截止日之前的数据
    （真正 out-of-sample），加上最近 90 天的 pred_cache 数据（如果存在）。
    """
    import numpy as np
    import pandas as pd
    from ml.config import get_artifact_dir
    from ml.models.predict import Predictor

    print("\n[5/5] 更新 v4 季节性校准系数 ...")

    cal_path = get_artifact_dir("v4") / "calibration.json"
    if not cal_path.exists():
        print("  calibration.json 不存在，跳过（先运行完整 v4 校准脚本）。")
        return

    cal = json.loads(cal_path.read_text())

    # 读取 intraday features（含 tmax_settled 标签）
    parquet_path = ROOT / "database" / "intraday_features.parquet"
    if not parquet_path.exists():
        parquet_path = ROOT / "database" / "intraday_features_v0.parquet"
    if not parquet_path.exists():
        print("  找不到 intraday_features.parquet，跳过。")
        return

    intra = pd.read_parquet(parquet_path)
    intra["date"] = pd.to_datetime(intra["date"]).dt.date

    # OOS 区间：v3 训练前的数据（2023-05-01 ~ 2024-02-28）
    oos = intra[
        (intra["date"] >= date(2023, 5, 1)) &
        (intra["date"] <= date(2024, 2, 28)) &
        (intra["obs_hour_frac"] == 0.0)
    ].copy()

    if oos.empty:
        print("  OOS 数据为空，跳过校准更新。")
        return

    pred = Predictor(version="v3")
    rows = []
    print(f"  对 {len(oos)} 个 OOS 冷启动样本重新推断 ...")
    for _, row in oos.iterrows():
        d = row["date"]
        feat = row.drop(
            labels=[c for c in ["date","tmax_c","tmin_c","tmax_settled","tmin_settled"]
                    if c in row.index],
            errors="ignore"
        )
        try:
            res = pred.predict(target_date=d, feature_row=feat, obs_hour_frac=0.0)
            tmax_q50 = res["tmax"]["distribution"].get("q50", float("nan"))
            tmin_q50 = res["tmin"]["distribution"].get("q50", float("nan"))
            rows.append({
                "month": d.month,
                "tmax_bias": tmax_q50 - int(row["tmax_settled"]),
                "tmin_bias": tmin_q50 - int(row["tmin_settled"]),
            })
        except Exception:
            continue

    if not rows:
        print("  推断失败，跳过。")
        return

    df = pd.DataFrame(rows)

    # 如果 pred_cache 存在，也合并最近的数据
    pred_cache_path = ROOT / "database" / "pred_cache.parquet"
    if pred_cache_path.exists():
        pc = pd.read_parquet(pred_cache_path)
        pc["month"] = pd.to_datetime(pc["date"]).dt.month
        pc_cold = pc[pc["slot_h"] == 0.0].copy()
        pc_cold["tmax_bias"] = pc_cold["tmax_q50"] - pc_cold["tmax_settled"]
        pc_cold["tmin_bias"] = pc_cold["tmin_q50"] - pc_cold["tmin_settled"]
        # pred_cache 覆盖 3-4 月，OOS 覆盖 5-12, 1-2 月
        pc_spring = pc_cold[pc_cold["month"].isin([3, 4])][["month","tmax_bias","tmin_bias"]]
        df = pd.concat([df[~df["month"].isin([3, 4])], pc_spring], ignore_index=True)

    monthly = df.groupby("month").agg(
        tmax=("tmax_bias", "mean"),
        tmin=("tmin_bias", "mean"),
    )

    for m in range(1, 13):
        if m in monthly.index:
            cal["tmax"]["monthly_bias"][str(m)] = round(float(monthly.loc[m, "tmax"]), 3)
            cal["tmin"]["monthly_bias"][str(m)] = round(float(monthly.loc[m, "tmin"]), 3)

    cal["updated_at"] = datetime.now(CST).isoformat()
    cal_path.write_text(json.dumps(cal, indent=2, ensure_ascii=False))
    print("  v4 校准系数已更新：")
    for m in range(1, 13):
        tb = cal["tmax"]["monthly_bias"].get(str(m), 0)
        nb = cal["tmin"]["monthly_bias"].get(str(m), 0)
        print(f"    月份 {m:2d}: tmax_bias={tb:+.3f}°C  tmin_bias={nb:+.3f}°C")


def step_backtest(version: str, days: int) -> None:
    """在最近 days 天内运行 backtest，打印简报。"""
    from ml.backtest.backtest import run_backtest, print_report

    end_date   = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)
    print(f"\n[Backtest] {start_date} → {end_date}（{days} 天，version={version}）")
    trades = run_backtest(
        start_date    = start_date,
        end_date      = end_date,
        version       = version,
        min_edge      = 0.05,
        position_size = 1.0,
        max_per_slot  = 10,
    )
    print_report(trades, position_size=1.0)


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        description="ZSPD 每日收市后重训练流水线"
    )
    parser.add_argument(
        "--date", default=None,
        help="目标日期 YYYY-MM-DD（默认：今日 CST）",
    )
    parser.add_argument(
        "--version", default="v5", choices=["v3", "v4", "v5"],
        help="要重训练的模型版本（默认 v5）",
    )
    parser.add_argument(
        "--no-fetch", action="store_true",
        help="跳过 WU 数据拉取（standard.db 已为最新）",
    )
    parser.add_argument(
        "--no-wait", action="store_true",
        help="不等待 WU 数据完整，直接运行",
    )
    parser.add_argument(
        "--no-train", action="store_true",
        help="只重建 labels + features，不重训练模型",
    )
    parser.add_argument(
        "--no-calibration", action="store_true",
        help="跳过 v4 校准更新",
    )
    parser.add_argument(
        "--backtest", action="store_true",
        help="训练完后运行近期 backtest",
    )
    parser.add_argument(
        "--backtest-days", type=int, default=30,
        help="Backtest 窗口天数（默认 30）",
    )
    args = parser.parse_args(argv[1:])

    # 确定目标日期
    if args.date:
        target_date = date.fromisoformat(args.date)
    else:
        target_date = datetime.now(CST).date()

    print("=" * 60)
    print(f"  每日重训练流水线  target={target_date}  version={args.version}")
    print(f"  启动时间: {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S CST')}")
    print("=" * 60)

    # Step 0: 拉取最新 WU 数据
    if not args.no_fetch:
        try:
            step_fetch_wu(target_date)
        except Exception as e:
            print(f"  [warn] WU 拉取失败: {e}，继续流程（使用已有数据）")

    # Step 0b: 等待 WU 数据完整
    if not args.no_wait:
        print(f"\n[0/5] 检查 {target_date} WU 数据完整性 ...")
        ok = _wait_for_wu(target_date)
        if not ok:
            n = _count_wu_obs(target_date)
            print(f"  [warn] 等待超时，当前仅 {n} 条（< {MIN_OBS_ROWS}）。"
                  "继续执行，但 labels 可能不完整。")
    else:
        n = _count_wu_obs(target_date)
        print(f"\n[0/5] 跳过等待。{target_date} 当前 {n} 条 WU 读数。")

    # Step 1-2: labels + features（始终执行）
    try:
        step_build_labels()
    except Exception as e:
        print(f"  [ERROR] build_labels 失败: {e}")
        sys.exit(1)

    try:
        step_build_features()
    except Exception as e:
        print(f"  [ERROR] build_features 失败: {e}")
        sys.exit(1)

    # Step 3: 模型训练
    if not args.no_train:
        try:
            step_train(args.version)
        except Exception as e:
            print(f"  [ERROR] 训练失败: {e}")
            sys.exit(1)
    else:
        print("\n[4/5] --no-train，跳过模型训练。")

    # Step 4: v4 校准更新
    if not args.no_calibration and not args.no_train:
        try:
            step_update_v4_calibration()
        except Exception as e:
            print(f"  [warn] v4 校准更新失败: {e}")
    else:
        print("\n[5/5] 跳过 v4 校准更新。")

    # Step 5 (可选): Backtest
    if args.backtest:
        try:
            step_backtest(args.version, args.backtest_days)
        except Exception as e:
            print(f"  [warn] Backtest 失败: {e}")

    print("\n" + "=" * 60)
    print(f"  流水线完成  {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S CST')}")
    print("=" * 60)


if __name__ == "__main__":
    main(sys.argv)
