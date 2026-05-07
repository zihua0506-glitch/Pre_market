"""
ml/models/train.py

统一 LightGBM 训练入口，支持所有版本：

  v0/v2 — 简单时间切割训练（WU-only 特征子集，35 个）
  v3/v5 — Walk-forward CV（21 折，3+1 月，步长 1 月，FINAL_SCALE=1.1）

仅训练 LightGBM 模型（分位数 × 7 + L2 点预测）。
特征从 intraday_features.parquet 读取，按版本选取子集。

运行：
    python -m ml.models.train --version v0
    python -m ml.models.train --version v2
    python -m ml.models.train --version v3
    python -m ml.models.train --version v5
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
import warnings
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from ml.config import (
    EARLY_STOPPING_ROUNDS,
    INTRADAY_PARQUET,
    QUANTILES,
    TRAIN_SLOTS,
    VALID_DAYS,
    get_artifact_dir,
    get_date_splits,
    get_feature_list,
    get_lgbm_params,
)
from ml.features import LABEL_TMAX, LABEL_TMIN

warnings.filterwarnings("ignore")

# ── CV 参数 ──────────────────────────────────────────────────────────────────
FOLD_TRAIN_MONTHS = 3
FOLD_STEP_MONTHS  = 1
FINAL_SCALE       = 1.1   # 最终模型用完整窗口训练，数据更多，适当放大迭代次数

_CV_VERSIONS = {"v3", "v5"}


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _save(obj, name: str, artifact_dir: Path) -> None:
    path = artifact_dir / f"{name}.pkl"
    with open(path, "wb") as f:
        pickle.dump(obj, f, protocol=5)
    print(f"  已保存 {path.name}")


def _add_months(d: date, n: int) -> date:
    """日期向后移动 n 个月，始终返回该月 1 日。"""
    m = d.month + n - 1
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, 1)


def _generate_folds(cv_start: date, cv_end: date) -> list[tuple[date, date, date]]:
    """生成 walk-forward 折叠列表：[(train_start, val_start, val_end), ...]。"""
    folds = []
    fold_start = cv_start
    while True:
        val_start = _add_months(fold_start, FOLD_TRAIN_MONTHS)
        val_end   = _add_months(val_start, 1)
        if val_end > cv_end:
            break
        folds.append((fold_start, val_start, val_end))
        fold_start = _add_months(fold_start, FOLD_STEP_MONTHS)
    return folds


def _es_split(X: pd.DataFrame, y: pd.Series):
    """从训练集末尾切出 early-stop 内部验证集（不暴露给 holdout）。"""
    n_val = min(VALID_DAYS * len(TRAIN_SLOTS), len(X) // 5)
    return (
        X.iloc[:-n_val], y.iloc[:-n_val],
        X.iloc[-n_val:], y.iloc[-n_val:],
    )


# ── LightGBM 训练（early-stop 版，返回 best_iteration）────────────────────────

def _lgbm_q_early_stop(X_tr, y_tr, X_es, y_es,
                       lgbm_params: dict) -> dict[float, int]:
    """训练分位数模型，返回各分位 best_iteration。"""
    from lightgbm import LGBMRegressor, early_stopping, log_evaluation
    best: dict[float, int] = {}
    for q in QUANTILES:
        m = LGBMRegressor(**{**lgbm_params, "objective": "quantile", "alpha": q})
        m.fit(X_tr, y_tr, eval_set=[(X_es, y_es)],
              callbacks=[early_stopping(EARLY_STOPPING_ROUNDS, verbose=False),
                         log_evaluation(-1)])
        best[q] = m.best_iteration_
    return best


def _lgbm_l2_early_stop(X_tr, y_tr, X_es, y_es, lgbm_params: dict) -> int:
    """训练 L2 模型，返回 best_iteration。"""
    from lightgbm import LGBMRegressor, early_stopping, log_evaluation
    m = LGBMRegressor(**{**lgbm_params, "objective": "regression"})
    m.fit(X_tr, y_tr, eval_set=[(X_es, y_es)],
          callbacks=[early_stopping(EARLY_STOPPING_ROUNDS, verbose=False),
                     log_evaluation(-1)])
    return m.best_iteration_


# ── LightGBM 训练（固定迭代版，全量数据）────────────────────────────────────

def _lgbm_q_final(X: pd.DataFrame, y: pd.Series,
                  lgbm_params: dict, n_iters: dict[float, int]) -> dict:
    """用固定 n_estimators 在全量数据上训练最终分位数模型。"""
    from lightgbm import LGBMRegressor
    models = {}
    for q in QUANTILES:
        m = LGBMRegressor(**{**lgbm_params, "objective": "quantile", "alpha": q,
                              "n_estimators": n_iters[q]})
        m.fit(X, y)
        models[q] = m
        print(f"    q{int(q*100):02d}: n_estimators={n_iters[q]}")
    return models


def _lgbm_l2_final(X: pd.DataFrame, y: pd.Series,
                   lgbm_params: dict, n_iter: int):
    """用固定 n_estimators 在全量数据上训练最终 L2 模型。"""
    from lightgbm import LGBMRegressor
    m = LGBMRegressor(**{**lgbm_params, "objective": "regression",
                          "n_estimators": n_iter})
    m.fit(X, y)
    print(f"    LightGBM L2: n_estimators={n_iter}")
    return m


# ── 简单时间切割训练（v0/v2）────────────────────────────────────────────────

def _train_simple(version: str) -> None:
    """v0/v2：固定时间切割，early-stop 内部验证集取训练集末尾。"""
    start_time   = datetime.now()
    lgbm_params  = get_lgbm_params(version)
    artifact_dir = get_artifact_dir(version)
    feat_list    = get_feature_list(version)
    data_start, test_start = get_date_splits(version)

    df = pd.read_parquet(INTRADAY_PARQUET)
    feat_cols = [c for c in feat_list if c in df.columns]

    (artifact_dir / "feature_cols.json").write_text(json.dumps(feat_cols, indent=2))
    print(f"[{version}] 特征列已保存（{len(feat_cols)} 个）")

    for target in ["tmax", "tmin"]:
        label_col = LABEL_TMAX if target == "tmax" else LABEL_TMIN
        print(f"\n{'='*50}\n目标变量: {target}  [{version}]\n{'='*50}")

        sub   = df[df[label_col].notna() & (df["date"] >= str(data_start))].copy()
        train = sub[sub["date"] < str(test_start)]

        X_tr = train[feat_cols]
        y_tr = train[label_col]
        test_rows = sub[sub["date"] >= str(test_start)]
        print(f"  train={len(X_tr):,} rows ({train['date'].nunique()} days)  "
              f"test={len(test_rows):,} rows ({test_rows['date'].nunique()} days)  "
              f"features={len(feat_cols)}")

        X_tr_es, y_tr_es, X_es, y_es = _es_split(X_tr, y_tr)

        print("  训练 LightGBM 分位数 ...")
        q_iters = _lgbm_q_early_stop(X_tr_es, y_tr_es, X_es, y_es, lgbm_params)
        _save(_lgbm_q_final(X_tr, y_tr, lgbm_params, q_iters),
              f"lgbm_quantile_{target}", artifact_dir)

        print("  训练 LightGBM L2 ...")
        l2_iter = _lgbm_l2_early_stop(X_tr_es, y_tr_es, X_es, y_es, lgbm_params)
        _save(_lgbm_l2_final(X_tr, y_tr, lgbm_params, l2_iter),
              f"lgbm_l2_{target}", artifact_dir)

    elapsed = (datetime.now() - start_time).seconds
    print(f"\n[{version}] 全部完成，耗时 {elapsed}s。Artifacts → {artifact_dir}")


# ── Walk-forward CV 训练（v3/v5）────────────────────────────────────────────

def _train_cv_target(target: str, version: str,
                     df_cv: pd.DataFrame, feat_cols: list[str],
                     cv_start: date, cv_end: date) -> None:
    """对单个预测目标运行 walk-forward CV + 最终模型训练。"""
    from lightgbm import LGBMRegressor
    lgbm_params = get_lgbm_params(version)
    folds       = _generate_folds(cv_start, cv_end)
    label_col   = LABEL_TMAX if target == "tmax" else LABEL_TMIN

    print(f"\n{'='*60}")
    print(f"  {version.upper()} Walk-forward CV  |  target={target}  |  {len(folds)} 折")
    print(f"  CV 窗口：{cv_start} → {cv_end}，{len(feat_cols)} 个特征")
    print("="*60)

    q_iters:   dict[float, list[int]] = {q: [] for q in QUANTILES}
    l2_iters:  list[int]   = []
    fold_maes: list[float] = []

    sub = df_cv[df_cv[label_col].notna()].copy()

    for i, (train_start, val_start, val_end) in enumerate(folds):
        fold_tr  = sub[(sub["date"] >= str(train_start)) & (sub["date"] < str(val_start))]
        fold_val = sub[(sub["date"] >= str(val_start))   & (sub["date"] < str(val_end))]

        if len(fold_tr) < 100 or len(fold_val) < 10:
            print(f"  折叠 {i+1:02d} 跳过（数据不足）")
            continue

        X_tr  = fold_tr[feat_cols]
        y_tr  = fold_tr[label_col]
        X_val = fold_val[feat_cols]
        y_val = fold_val[label_col]

        X_tr_es, y_tr_es, X_es, y_es = _es_split(X_tr, y_tr)

        q_best = _lgbm_q_early_stop(X_tr_es, y_tr_es, X_es, y_es, lgbm_params)
        for q, it in q_best.items():
            q_iters[q].append(it)

        l2_it = _lgbm_l2_early_stop(X_tr_es, y_tr_es, X_es, y_es, lgbm_params)
        l2_iters.append(l2_it)

        # holdout MAE：用找到的 n_iter 在完整训练集上重新拟合后评估
        _m = LGBMRegressor(**{**lgbm_params, "objective": "regression",
                               "n_estimators": l2_it, "verbose": -1})
        _m.fit(X_tr, y_tr)
        val_mae = float(np.abs(_m.predict(X_val) - y_val.values).mean())
        fold_maes.append(val_mae)

        print(f"  折叠 {i+1:02d}  [{train_start}→{val_start})  "
              f"holdout[{val_start}→{val_end})  "
              f"lgbm_l2={l2_it}  MAE={val_mae:.3f}", flush=True)

    if not l2_iters:
        print("错误：无有效折叠，无法训练。")
        return

    avg_mae = float(np.mean(fold_maes))
    print(f"\n  CV 汇总（{len(l2_iters)} 折）：")
    print(f"    平均 holdout MAE          : {avg_mae:.3f}")
    print(f"    LightGBM L2 平均最佳迭代  : {np.mean(l2_iters):.0f}")

    final_q_iters = {
        q: max(1, round(np.mean(iters) * FINAL_SCALE))
        for q, iters in q_iters.items() if iters
    }
    final_l2_iter = max(1, round(np.mean(l2_iters) * FINAL_SCALE))

    print(f"\n  最终 n_estimators（×{FINAL_SCALE}）：")
    print(f"    LightGBM L2 : {final_l2_iter}")
    for q, it in sorted(final_q_iters.items()):
        print(f"    q{int(q*100):02d}         : {it}")

    print(f"\n  训练最终模型：{cv_start} → {cv_end} ...")
    X_full = sub[feat_cols]
    y_full = sub[label_col]
    artifact_dir = get_artifact_dir(version)

    print("  最终 LightGBM 分位数 ...")
    _save(_lgbm_q_final(X_full, y_full, lgbm_params, final_q_iters),
          f"lgbm_quantile_{target}", artifact_dir)

    print("  最终 LightGBM L2 ...")
    _save(_lgbm_l2_final(X_full, y_full, lgbm_params, final_l2_iter),
          f"lgbm_l2_{target}", artifact_dir)

    meta = {
        "version":            version,
        "cv_start":           str(cv_start),
        "cv_end":             str(cv_end),
        "n_folds":            len(l2_iters),
        "avg_val_mae":        round(avg_mae, 4),
        "final_lgbm_l2_iter": final_l2_iter,
        "final_lgbm_q_iters": {str(k): v for k, v in final_q_iters.items()},
    }
    meta_path = artifact_dir / f"cv_meta_{target}.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  CV 元数据已保存 → {meta_path.name}")


def _train_cv(version: str) -> None:
    """v3/v5：walk-forward CV 训练（tmax + tmin）。"""
    start_time   = datetime.now()
    artifact_dir = get_artifact_dir(version)
    feat_list    = get_feature_list(version)
    cv_start, cv_end = get_date_splits(version)

    if not INTRADAY_PARQUET.exists():
        print(f"[{version}] 错误：{INTRADAY_PARQUET} 不存在。")
        print("     请先运行：python -m ml.data.build_intraday_features")
        return

    df = pd.read_parquet(INTRADAY_PARQUET)
    feat_cols = [c for c in feat_list if c in df.columns]

    (artifact_dir / "feature_cols.json").write_text(
        json.dumps(feat_cols, indent=2), encoding="utf-8"
    )
    print(f"[{version}] 特征列已保存（{len(feat_cols)} 个）")

    df_cv = df[(df["date"] >= str(cv_start)) & (df["date"] < str(cv_end))].copy()
    print(f"[{version}] CV 窗口数据：{len(df_cv):,} 行，{df_cv['date'].nunique()} 天")

    _train_cv_target("tmax", version, df_cv, feat_cols, cv_start, cv_end)
    _train_cv_target("tmin", version, df_cv, feat_cols, cv_start, cv_end)

    elapsed = (datetime.now() - start_time).seconds
    print(f"\n[{version}] 全部完成，耗时 {elapsed}s。Artifacts → {artifact_dir}")


# ── 公共入口 ──────────────────────────────────────────────────────────────────

def train_all(version: str = "v0") -> None:
    if version in _CV_VERSIONS:
        _train_cv(version)
    else:
        _train_simple(version)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="v0",
                        choices=["v0", "v2", "v3", "v5"])
    args = parser.parse_args()
    train_all(args.version)
