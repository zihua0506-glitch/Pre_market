"""
Inference: given a feature row, produce full temperature distribution
and Polymarket bracket probabilities.

Output schema:
  {
    "target_date": "2026-05-01",
    "obs_hour_frac": 14.0,
    "tmax": {
      "distribution": {"q05": float, ..., "q95": float},
      "point_lgbm": float,
      "polymarket": {"ge_10": float, ..., "ge_44": float}
    },
    "tmin": { ... }
  }

Settlement rule: settle(t) = floor(t + 0.5)
  => P(settled >= T) = P(raw >= T - 0.5)

Intraday certainty override:
  If obs_tmax_so_far >= T - 0.5, then P(tmax_settled >= T) = 1.0

Usage:
    from ml.models.predict import Predictor
    p = Predictor()
    result = p.predict(target_date="2026-05-01", feature_row=row, obs_hour_frac=14.0)
"""

from __future__ import annotations

import json
import pickle
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from ml.config import QUANTILES, TMAX_THRESHOLDS, TMIN_THRESHOLDS, get_artifact_dir, settle
from ml.features import ALL_FEATURES


def _load(name: str, artifact_dir: Path):
    path = artifact_dir / f"{name}.pkl"
    if not path.exists():
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


class QuantileDistribution:
    """
    Piecewise-linear CDF from quantile predictions.
    Normal tail extrapolation via IQR / 1.349 sigma outside [Q05, Q95].
    """

    def __init__(self, quantile_map: dict[float, float]):
        qs = sorted(quantile_map)
        vs = [quantile_map[q] for q in qs]

        self._qs = np.array(qs)
        self._vs = np.array(vs, dtype=float)

        q25 = quantile_map.get(0.25, np.nan)
        q75 = quantile_map.get(0.75, np.nan)
        self._sigma = ((q75 - q25) / 1.3490
                       if (not np.isnan(q25) and not np.isnan(q75) and q75 > q25)
                       else 2.5)
        self._mu = quantile_map.get(0.50, float(np.nanmean(vs)))

    def cdf(self, x: float) -> float:
        lo, hi = self._vs[0], self._vs[-1]
        if x < lo or x > hi:
            return float(scipy_stats.norm.cdf(x, loc=self._mu, scale=self._sigma))
        return float(np.interp(x, self._vs, self._qs))

    def survival(self, x: float) -> float:
        return max(0.0, min(1.0, 1.0 - self.cdf(x)))

    def p_settle_ge(self, threshold: int) -> float:
        """P(settle(raw) >= threshold) = P(raw >= threshold - 0.5)."""
        return self.survival(threshold - 0.5)

    def as_dict(self) -> dict:
        return {f"q{int(q*100):02d}": round(float(v), 2)
                for q, v in zip(self._qs, self._vs)}


class Predictor:
    """Loads all model artifacts once and serves intraday predictions."""

    def __init__(self, version: str = "v0"):
        # v4 = v3 base models + seasonal calibration
        base_version = "v3" if version == "v4" else version
        artifact_dir = get_artifact_dir(base_version)
        self.lgbm_q  = {"tmax": _load("lgbm_quantile_tmax", artifact_dir),
                        "tmin": _load("lgbm_quantile_tmin", artifact_dir)}
        self.lgbm_l2 = {"tmax": _load("lgbm_l2_tmax", artifact_dir),
                        "tmin": _load("lgbm_l2_tmin", artifact_dir)}

        feat_path = artifact_dir / "feature_cols.json"
        self.feat_cols: list[str] = (json.loads(feat_path.read_text())
                                     if feat_path.exists() else ALL_FEATURES)

        self._calibration: Optional[dict] = None
        if version == "v4":
            cal_path = get_artifact_dir("v4") / "calibration.json"
            if cal_path.exists():
                self._calibration = json.loads(cal_path.read_text())

    def _to_frame(self, row: pd.Series) -> pd.DataFrame:
        vals = row.reindex(self.feat_cols).values
        return pd.DataFrame([vals], columns=self.feat_cols)

    def _predict_target(self, target: str, X: pd.DataFrame) -> dict:
        out: dict = {}

        if self.lgbm_q[target]:
            raw_map = {q: float(m.predict(X)[0]) for q, m in self.lgbm_q[target].items()}
            # 等排修正：强制分位数单调
            qs = sorted(raw_map)
            vs = [raw_map[q] for q in qs]
            for i in range(1, len(vs)):
                if vs[i] < vs[i - 1]:
                    vs[i] = vs[i - 1]
            q_map = dict(zip(qs, vs))
            dist = QuantileDistribution(q_map)
            out["distribution"] = dist.as_dict()
            thresholds = TMAX_THRESHOLDS if target == "tmax" else TMIN_THRESHOLDS
            out["polymarket"] = {f"ge_{T}": round(dist.p_settle_ge(T), 4) for T in thresholds}

        if self.lgbm_l2[target]:
            out["point_lgbm"] = round(float(self.lgbm_l2[target].predict(X)[0]), 2)

        return out

    def predict(
        self,
        target_date: str | date,
        feature_row: pd.Series,
        obs_hour_frac: float = -1.0,
        obs_tmax_so_far: Optional[float] = None,
        obs_tmin_so_far: Optional[float] = None,
        obs_tmin_now: Optional[float] = None,
        obs_temp_now: Optional[float] = None,
        obs_temp_prev1h: Optional[float] = None,
        obs_dew_now: Optional[float] = None,
        obs_dew_mean: Optional[float] = None,
        obs_wind_now: Optional[float] = None,
        obs_pressure_now: Optional[float] = None,
        obs_count: int = 0,
    ) -> dict:
        """
        统一推断接口。obs_hour_frac >= 0 表示日内模式。
        obs_* 参数覆盖 feature_row 中的对应字段。
        """
        row = feature_row.copy()
        row["obs_hour_frac"] = float(obs_hour_frac)
        row["obs_count"]     = float(obs_count)

        _override = {
            "obs_tmax_so_far":   obs_tmax_so_far,
            "obs_tmin_so_far":   obs_tmin_so_far,
            "obs_temp_now":      obs_temp_now,
            "obs_temp_prev1h":   obs_temp_prev1h,
            "obs_temp_trend_1h": (
                (obs_temp_now - obs_temp_prev1h)
                if obs_temp_now is not None and obs_temp_prev1h is not None
                else None
            ),
            "obs_dew_now":       obs_dew_now,
            "obs_dew_mean":      obs_dew_mean,
            "obs_wind_now":      obs_wind_now,
            "obs_pressure_now":  obs_pressure_now,
        }
        for k, v in _override.items():
            if v is not None:
                row[k] = float(v)

        X = self._to_frame(row)

        result: dict = {
            "target_date":   str(target_date),
            "obs_hour_frac": obs_hour_frac,
            "tmax":          self._predict_target("tmax", X),
            "tmin":          self._predict_target("tmin", X),
        }

        # v4 季节性校准：将分布整体平移 -(monthly_bias + slot_extra_bias)
        if self._calibration is not None:
            td = target_date if not isinstance(target_date, str) else date.fromisoformat(target_date)
            month = td.month
            slot_key = str(float(obs_hour_frac))
            for tgt in ("tmax", "tmin"):
                cal = self._calibration.get(tgt, {})
                bias = float(cal.get("monthly_bias", {}).get(str(month), 0.0))
                bias += float(cal.get("slot_extra_bias", {}).get(slot_key, 0.0))
                if bias == 0.0 or "distribution" not in result[tgt]:
                    continue
                dist = result[tgt]["distribution"]
                q_map = {int(k[1:]) / 100.0: dist[k] - bias for k in dist}
                new_dist = QuantileDistribution(q_map)
                result[tgt]["distribution"] = new_dist.as_dict()
                thresholds = TMAX_THRESHOLDS if tgt == "tmax" else TMIN_THRESHOLDS
                result[tgt]["polymarket"] = {
                    f"ge_{T}": round(new_dist.p_settle_ge(T), 4) for T in thresholds
                }

        # 日内确定性覆盖
        if obs_tmax_so_far is not None and "polymarket" in result["tmax"]:
            for key in result["tmax"]["polymarket"]:
                T = int(key.split("_")[1])
                if obs_tmax_so_far >= T - 0.5:
                    result["tmax"]["polymarket"][key] = 1.0

        if obs_tmin_so_far is not None and "polymarket" in result["tmin"]:
            for key in result["tmin"]["polymarket"]:
                T = int(key.split("_")[1])
                if obs_tmin_so_far < T - 0.5:
                    result["tmin"]["polymarket"][key] = 0.0

        return result


def print_prediction(result: dict) -> None:
    """格式化打印预测结果。"""
    h = result["obs_hour_frac"]
    h_str = f"{int(h):02d}:{int((h % 1) * 60):02d} CST" if h >= 0 else "next-day"
    print(f"\n{'─'*55}")
    print(f"Date: {result['target_date']}  as-of: {h_str}")
    print("─"*55)
    for target in ["tmax", "tmin"]:
        d = result[target]
        label = "TMAX" if target == "tmax" else "TMIN"
        print(f"\n  {label}:")
        if "distribution" in d:
            q = d["distribution"]
            print(f"    Q05/Q25/Q50/Q75/Q95: "
                  f"{q.get('q05')} / {q.get('q25')} / {q.get('q50')} / "
                  f"{q.get('q75')} / {q.get('q95')} degC")
        if "point_lgbm" in d:
            print(f"    point_lgbm        : {d['point_lgbm']:.2f} degC")
        if "polymarket" in d:
            items = sorted(
                [(int(k.split("_")[1]), v) for k, v in d["polymarket"].items()]
            )
            non_trivial = [(T, p) for T, p in items if 0.02 <= p <= 0.98]
            if non_trivial:
                print(f"    Polymarket P(>= T):")
                for T, p in non_trivial:
                    print(f"      >= {T:2d}C : {p:.1%}")
