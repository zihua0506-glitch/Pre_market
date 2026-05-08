"""
Evaluate trained models on the held-out test set.

Metrics:
  Point prediction (LightGBM-L2, LightGBM-Q50):
    MAE, RMSE, Bias, Hit@+-1C, Hit@+-2C, Acc(settled)

  Quantile calibration (LightGBM quantile):
    Empirical coverage vs nominal quantile, Pinball loss

  Distribution quality:
    Brier score for P(tmax_settled >= T) at key thresholds

  Time-bucket breakdown:
    Cold-start (00:00-07:30), Morning (08:00-11:30),
    Daytime (12:00-16:00), Evening (16:30-23:30)

Run:
    python -m ml.models.evaluate --version v0
    python -m ml.models.evaluate --version v3
    python -m ml.models.evaluate --version v5
"""

from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from ml.config import (
    INTRADAY_PARQUET,
    QUANTILES,
    TMAX_THRESHOLDS,
    TMIN_THRESHOLDS,
    get_artifact_dir,
    get_date_splits,
    get_feature_list,
    settle,
)
from ml.features import LABEL_TMAX, LABEL_TMIN


def _load_pkl(name: str, artifact_dir: Path):
    path = artifact_dir / f"{name}.pkl"
    if not path.exists():
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


def _load_test(target: str, version: str) -> tuple[pd.DataFrame, pd.Series, pd.Series, pd.Series]:
    df = pd.read_parquet(INTRADAY_PARQUET)
    label_raw     = LABEL_TMAX     if target == "tmax" else LABEL_TMIN
    label_settled = "tmax_settled" if target == "tmax" else "tmin_settled"

    _, test_start = get_date_splits(version)
    feat_list = get_feature_list(version)

    test = df[df["date"] >= str(test_start)].copy()
    test = test[test[label_raw].notna()]

    feat_cols = [c for c in feat_list if c in test.columns]
    return (
        test[feat_cols],
        test[label_raw],
        test[label_settled],
        test["obs_hour_frac"],
    )


def _point_metrics(y_true: pd.Series, y_pred: np.ndarray, name: str) -> dict:
    err  = y_pred - y_true.values
    mae  = float(np.abs(err).mean())
    rmse = float(np.sqrt((err**2).mean()))
    bias = float(err.mean())
    hit1 = float((np.abs(err) <= 1.0).mean())
    hit2 = float((np.abs(err) <= 2.0).mean())
    y_s  = np.array([settle(v) for v in y_true.values])
    yp_s = np.array([settle(v) for v in y_pred])
    acc  = float((y_s == yp_s).mean())
    return {"model": name, "MAE": round(mae, 3), "RMSE": round(rmse, 3),
            "Bias": round(bias, 3), "Hit@1C": round(hit1, 3),
            "Hit@2C": round(hit2, 3), "Acc(settled)": round(acc, 3)}


def _pinball(y: np.ndarray, q_pred: np.ndarray, q: float) -> float:
    e = y - q_pred
    return float(np.mean(np.where(e >= 0, q * e, (q - 1) * e)))


def _brier_from_quantiles(
    y_settled: np.ndarray, q_preds: dict[float, np.ndarray], threshold: int
) -> float:
    qs   = sorted(q_preds)
    vals = [q_preds[q] for q in qs]
    t_raw = threshold - 0.5
    probs = np.clip(
        1.0 - np.array([float(np.interp(t_raw, [v[i] for v in vals], qs))
                        for i in range(len(y_settled))]),
        0.0, 1.0,
    )
    return float(np.mean((probs - (y_settled >= threshold).astype(float)) ** 2))


_BUCKETS = [
    ("Cold-start", 0.0,  7.5),
    ("Morning",    8.0, 11.5),
    ("Daytime",   12.0, 16.0),
    ("Evening",   16.5, 23.5),
]


def evaluate(target: str = "tmax", version: str = "v0",
             metrics_out: dict | None = None) -> None:
    _, test_start = get_date_splits(version)
    artifact_dir  = get_artifact_dir(version)
    print(f"\n{'='*60}")
    print(f"Evaluation  --  target: {target}  version: {version}  test from {test_start}")
    print("="*60)

    X_test, y_raw, y_settled, hours = _load_test(target, version)
    if X_test.empty:
        print("No test data.")
        return

    point_rows = []
    q_preds: dict[float, np.ndarray] = {}
    feat_cols = list(X_test.columns)

    # Container for the optional JSON dump.
    out_section: dict = {
        "test_from":   str(test_start),
        "n_test_rows": int(len(X_test)),
        "n_features":  len(feat_cols),
        "point":       {},
        "quantile":    {},
        "brier":       [],
        "bucket_mae":  {},
    }

    # ── LightGBM quantile ──────────────────────────────────────────────────
    lgbm_q_dict = _load_pkl(f"lgbm_quantile_{target}", artifact_dir)
    if lgbm_q_dict:
        for q, m in lgbm_q_dict.items():
            q_preds[q] = m.predict(X_test)
        point_rows.append(_point_metrics(y_raw, q_preds[0.50], "LightGBM-Q50"))

        print("\n  Quantile calibration:")
        print(f"  {'Q':>6}  {'Coverage':>10}  {'Pinball':>10}")
        for q in QUANTILES:
            cov = float((y_raw.values <= q_preds[q]).mean())
            pb  = _pinball(y_raw.values, q_preds[q], q)
            flag = " OK" if abs(cov - q) < 0.05 else " !!"
            print(f"  {q:6.2f}  {cov:10.3f}{flag}  {pb:10.3f}")
            out_section["quantile"][f"{q:.2f}"] = {
                "coverage": round(cov, 4), "pinball": round(pb, 4),
            }

    # ── LightGBM L2 ────────────────────────────────────────────────────────
    lgbm_l2 = _load_pkl(f"lgbm_l2_{target}", artifact_dir)
    if lgbm_l2:
        point_rows.append(_point_metrics(y_raw, lgbm_l2.predict(X_test), "LightGBM-L2"))

    if point_rows:
        print("\n  Point prediction comparison:")
        print(pd.DataFrame(point_rows).set_index("model").to_string())
        for r in point_rows:
            name = r.pop("model")
            out_section["point"][name] = r

    if q_preds:
        thresholds = TMAX_THRESHOLDS if target == "tmax" else TMIN_THRESHOLDS
        rows = []
        for T in thresholds:
            freq = float((y_settled.values >= T).mean())
            if 0.02 <= freq <= 0.98:
                rows.append({"T": T, "freq": round(freq, 3),
                             "brier": round(_brier_from_quantiles(y_settled.values, q_preds, T), 4)})
        if rows:
            print(f"\n  Brier scores P({target}_settled >= T):")
            print(pd.DataFrame(rows).set_index("T").to_string())
            out_section["brier"] = rows

    if lgbm_l2:
        preds = lgbm_l2.predict(X_test)
        print("\n  MAE by time-of-day bucket (LightGBM-L2):")
        print(f"  {'Bucket':>15}  {'N':>6}  {'MAE':>8}")
        for bname, lo, hi in _BUCKETS:
            mask = (hours >= lo) & (hours <= hi)
            if mask.sum() == 0:
                continue
            mae = float(np.abs(preds[mask] - y_raw.values[mask]).mean())
            print(f"  {bname:>15}  {mask.sum():>6}  {mae:>8.3f}")
            out_section["bucket_mae"][bname] = {
                "N": int(mask.sum()), "MAE": round(mae, 4),
            }

    if lgbm_l2 and hasattr(lgbm_l2, "feature_importances_"):
        imp = pd.Series(lgbm_l2.feature_importances_, index=feat_cols)
        print("\n  Top 15 features (LightGBM-L2):")
        top15 = imp.nlargest(15)
        for feat, score in top15.items():
            print(f"    {feat:<40} {score:>8.0f}")
        out_section["top15_features"] = [
            {"feature": str(f), "importance": int(s)} for f, s in top15.items()
        ]

    if metrics_out is not None:
        metrics_out[target] = out_section


def evaluate_all(version: str = "v0", json_path: str | Path | None = None) -> dict:
    """Run tmax + tmin evaluation. If `json_path` is given, write a metrics
    snapshot to that path. The default snapshot location is
    `ml/artifacts/<version>/eval_metrics.json` when json_path == 'auto'.
    """
    import json as _json
    from datetime import datetime as _dt

    metrics: dict = {
        "version": version,
        "evaluated_at": _dt.utcnow().isoformat(timespec="seconds") + "Z",
    }
    evaluate("tmax", version, metrics_out=metrics)
    evaluate("tmin", version, metrics_out=metrics)

    if json_path == "auto":
        json_path = get_artifact_dir(version) / "eval_metrics.json"
    if json_path is not None:
        out = Path(json_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_json.dumps(metrics, indent=2, ensure_ascii=False))
        print(f"\n  metrics → {out}")
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="v0",
                        choices=["v0", "v2", "v3", "v4", "v5"])
    parser.add_argument("--json", dest="json_path", default=None,
                        help="Write metrics JSON to this path. "
                             "Use 'auto' for ml/artifacts/<version>/eval_metrics.json")
    args = parser.parse_args()
    evaluate_all(args.version, json_path=args.json_path)
