"""
strategy_math/model_edge.py

Convert a Polymarket-style quantile prediction result (from ml.models.predict)
into a YES probability for a specific (kind, bound, threshold_c) contract.

The Predictor result has shape:
    {
      "tmax": {"polymarket": {"ge_15": 0.92, "ge_16": 0.78, ...}, ...},
      "tmin": {"polymarket": {"ge_10": 0.55, ...}, ...},
    }

This module is what `model_prob` does — it picks the right "ge_T" entries and
combines them depending on the bound (ge / le / eq).
"""

from __future__ import annotations

from typing import Optional


def _model_prob(result: dict, kind: str, bound: str, threshold_c: int) -> Optional[float]:
    """Extract P(event == 1) from a Predictor result dict for a YES token."""
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
