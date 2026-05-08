"""
strategy_math/sizing.py

Tiered position sizing as a function of |edge|.

Certainty signals (P locked by observation):
  |edge| ≥ 0.50  →  5× base   (market badly mispriced vs physics)
  |edge| ≥ 0.20  →  3× base
  |edge| ≥ 0.02  →  2× base

Model edge signals:
  |edge| ≥ 0.20  →  3× base
  |edge| ≥ 0.10  →  2× base
  |edge| <  0.10 →  1× base
"""

from __future__ import annotations


def _position_size(is_certain: bool, edge: float, base: float) -> float:
    ae = abs(edge)
    if is_certain:
        if ae >= 0.50:
            return base * 5.0
        elif ae >= 0.20:
            return base * 3.0
        else:
            return base * 2.0
    else:
        if ae >= 0.20:
            return base * 3.0
        elif ae >= 0.10:
            return base * 2.0
        else:
            return base * 1.0
