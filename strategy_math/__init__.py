"""
strategy_math/

Pure-function library shared by the live strategy (strategies/modify_str.py)
and the backtest engine (ml/backtest/backtest.py). Everything in here is
data-flow only — no I/O, no global state, no threads — so the same code path
that decides what to trade in production decides what would have been traded
in backtest.

  - certainty.py    physics-locked P=0/P=1 from intraday observations
  - features.py     intraday observation, lag, ERA5, calendar features
  - labels.py       daily settled tmax/tmin loaded from daily_labels.db
  - model_edge.py   YES-probability extraction from the quantile predictor
  - sizing.py       tiered position sizing as a function of |edge|
"""

from strategy_math.certainty   import _certainty_prob, _event_happened
from strategy_math.features    import (
    _calendar_features,
    _era5_slot_features,
    _lag_features,
    _non_nan,
    _obs_derived_features,
    _obs_features,
)
from strategy_math.labels      import _load_labels
from strategy_math.model_edge  import _model_prob
from strategy_math.sizing      import _position_size

__all__ = [
    "_certainty_prob",
    "_event_happened",
    "_calendar_features",
    "_era5_slot_features",
    "_lag_features",
    "_non_nan",
    "_obs_derived_features",
    "_obs_features",
    "_load_labels",
    "_model_prob",
    "_position_size",
]
