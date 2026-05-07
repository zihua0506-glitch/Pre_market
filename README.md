# Pre_market — ZSPD Temperature Prediction & Polymarket Trading

LightGBM-based intraday temperature forecasting for Shanghai Pudong Airport (ZSPD),
targeting Polymarket daily high/low temperature contracts.

## Architecture

```
core/               Data collection (WU, ERA5, Polymarket)
ml/
  config.py         Version configs, feature lists, LightGBM params
  features.py       Feature definitions (35 WU-only / 63 WU+ERA5)
  pipeline.py       CLI entry point
  data/             Label + feature builders
  models/           Train / evaluate / predict
  backtest/         Strategy backtesting engine
  artifacts/        Trained model pkl files (v0–v5)
strategies/         Live trading strategies
scripts/            Scheduled automation
```

## Versions

| Version | Features | Training | Window |
|---------|----------|----------|--------|
| v0 | 35 (WU) | Simple split | 3yr |
| v2 | 35 (WU) | Simple split | 2yr |
| v3 | 35 (WU) | Walk-forward CV | 2024-03 → 2026-03 |
| v4 | 35 (WU) | v3 + seasonal calibration | — |
| v5 | 63 (WU+ERA5) | Walk-forward CV | 2024-03 → 2026-03 |

## Setup

```bash
pip install -r requirements.txt
```

## Data Sources

| DB | Source | Script |
|----|--------|--------|
| `standard.db` | Weather Underground half-hourly obs | `core/fetch_zspd_history.py` |
| `weather_om.db` | ERA5 archive + GFS forecast (Open-Meteo) | `core/fetch_weather.py` |
| `daily_labels.db` | Daily tmax/tmin ground truth | `ml/data/build_labels.py` |
| `token.db` | Polymarket contract metadata | `core/fetch_tokens.py` |
| `data.db` | Polymarket 1-min price history | `core/fetch_data.py` |
| `orderbook.db` | Live L2 order book snapshots | `core/collect_orderbook.py` |

## Workflow

```bash
# Daily update + retrain
python -m ml.pipeline full --version v5

# Intraday prediction
python -m ml.pipeline predict --version v5

# Collect live order book (1-second snapshots)
python -m core.collect_orderbook

# Backtest
python -m ml.backtest.backtest --version v5 --start 2026-03-13 --end 2026-05-01
```

## Settlement Rule

`settle(t) = floor(t + 0.5)` — standard round-half-up matching Polymarket's source.
