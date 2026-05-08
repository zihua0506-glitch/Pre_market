"""
strategy_math/certainty.py

Physics-locked outcome probabilities from intraday observations.

Once we know obs_tmax_so_far (the day's running maximum) or obs_tmin_so_far
(running minimum), some Polymarket contracts can be resolved with certainty
*before* the day ends:

  ge_T  = 1  when obs_tmax_so_far ≥ T - 0.5   (settle ≥ T is guaranteed)
  eq_T  = 0  when obs_tmax_so_far ≥ T + 0.5   (settle > T → = T impossible)
  le_T  = 0  when obs_tmax_so_far ≥ T + 0.5   (settle > T → ≤ T impossible)

  le_T  = 1  when obs_tmin_so_far <  T + 0.5  (settle ≤ T guaranteed)
  ge_T  = 0  when obs_tmin_so_far <  T - 0.5  (settle < T → ≥ T impossible)
  eq_T  = 0  when obs_tmin_so_far <  T - 0.5  (settle < T → = T impossible)

Bounds use Polymarket's settle = floor(t + 0.5) convention.
"""

from __future__ import annotations

from typing import Optional


def _certainty_prob(
    kind: str,
    bound: str,
    threshold_c: int,
    obs_tmax: Optional[float],
    obs_tmin: Optional[float],
) -> Optional[float]:
    """Return 0.0 / 1.0 if the YES outcome is determined by current obs, else None."""
    T = int(threshold_c)

    if kind == "highest":
        if obs_tmax is None:
            return None
        if bound == "ge":
            return 1.0 if obs_tmax >= T - 0.5 else None
        elif bound == "eq":
            return 0.0 if obs_tmax >= T + 0.5 else None
        elif bound == "le":
            return 0.0 if obs_tmax >= T + 0.5 else None

    elif kind == "lowest":
        if obs_tmin is None:
            return None
        if bound == "le":
            return 1.0 if obs_tmin < T + 0.5 else None
        elif bound == "ge":
            return 0.0 if obs_tmin < T - 0.5 else None
        elif bound == "eq":
            return 0.0 if obs_tmin < T - 0.5 else None

    return None


def _event_happened(
    kind: str,
    bound: str,
    threshold_c: int,
    tmax_settled,
    tmin_settled,
) -> Optional[int]:
    """Return 1/0 for whether the YES token resolves to 1 given final settled values."""
    T = int(threshold_c)
    settled = int(tmax_settled) if kind == "highest" else int(tmin_settled)
    if bound == "ge":
        return int(settled >= T)
    elif bound == "le":
        return int(settled <= T)
    elif bound == "eq":
        return int(settled == T)
    return None
