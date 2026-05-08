# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

LightGBM-based intraday temperature forecasting for Shanghai Pudong Airport (ZSPD), used to trade Polymarket daily high/low temperature contracts. All times in code are CST (UTC+8); slots are 30-minute (`hour_frac = 0.0, 0.5, ... 23.5`).

## Where the docs live

User-facing documentation is in [`docs/`](docs/):
- **[`docs/STATUS.md`](docs/STATUS.md)** — current system state. **Read this first** when you come back to the project; it tells you what's running, what's gated, and what's open.
- [`docs/operations.md`](docs/operations.md) — daily-runbook, CLI reference, wallet ops, troubleshooting
- [`docs/deployment.md`](docs/deployment.md) — production topology (singapore + valkyrie), systemd units, sync pipeline, infra runbooks
- [`docs/overview.md`](docs/overview.md) / [`docs/weather_strategies.md`](docs/weather_strategies.md) — research / strategy background

If you're touching deploy / infra / SSH / systemd, **read `docs/deployment.md` first** — there's a non-obvious layout convention (`ml.config.ROOT = src/`, so DBs and artifacts both live under `src/`).

## Current production state

(snapshot 2026-05-08; verified by live SSH inventory — see [`docs/STATUS.md`](docs/STATUS.md) for the live numbers)

- **valkyrie** (FI, 204.168.241.66) runs `clob-proxy.service` (active) and `zspd-hot-refresh.timer` (17:30 UTC). `zspd-strategy.service` is `static` and currently INACTIVE — operator-gated. Production wallet funded ($2,021 USDC, FUNDER `0x65917b…dA58`, SIG_TYPE=1).
- **singapore** (SG, 217.15.166.15) runs `zspd-archive.timer` (16:30 UTC) and `zspd-dashboard.service` (currently active on 127.0.0.1:8090, disabled-on-boot). No training. Pushes `daily_labels.db` to valkyrie via dedicated ed25519 key (`SHA256:808lkjRZ…BBhuhvA`).
- **Models** at `valkyrie:/opt/zspd-trader/src/ml/artifacts/v5/` last refreshed 2026-05-08 ~03:25 UTC.
- **Python venvs** are 3.12.3 with `lightgbm 4.6.0`, `pandas 3.0.2`, `numpy 2.4.4`, `pyarrow 24.0.0`, `scikit-learn 1.8.0`, `py-clob-client-v2 1.0.0` on both servers.

## Package layout

```
common/                       shared utilities
├── env.py                    single .env loader (load_env / write_env)
└── orderbook.py              BookLevel / OrderBook / Order dataclasses

strategy_math/                pure-function strategy math (no I/O)
├── certainty.py              _certainty_prob, _event_happened
├── features.py               obs / lag / era5 / calendar feature builders
├── labels.py                 _load_labels (from daily_labels.db)
├── model_edge.py             _model_prob (quantile result → YES probability)
└── sizing.py                 _position_size (tiered)

clients/data/                 data acquisition + persistence
├── wu.py                     Weather Underground half-hour obs
├── open_meteo.py             ERA5 archive + GFS forecast
├── polymarket_meta.py        gamma-api event/market metadata
├── polymarket_prices.py      CLOB 1-min price history (with sqlite migration)
├── polymarket_orderbook.py   live L2 orderbook collector
└── polymarket_search.py      ad-hoc gamma-api keyword search

clients/trading/              order execution
├── base.py                   TradingBackend ABC + result types
├── direct.py                 DirectBackend (py-clob-client-v2 native)
├── proxy.py                  ProxyBackend (HTTP client → server)
├── server.py                 FastAPI proxy server (deployed on valkyrie)
└── dryrun.py                 DryRunBackend (offline stub for tests)

ops/                          operational tooling (importable + CLI)
├── setup_clob_creds.py       derive POLY_API_{KEY,SECRET,PASSPHRASE}
├── rotate_wallet.py          emergency-only EOA rotation
├── deploy_proxy.py           on-chain proxy deploy via factory
└── preflight_clob.py         IP geo + balance/allowance + test order

strategies/                   thin orchestrators
├── modify_str.py             Tier-1 (certainty) + Tier-2 (model edge)
├── plain_as.py               Tier-1 only (NO arbitrage)
├── clob_adapter.py           thin subclass: DirectBackend + print fmt
└── proxy_clob_adapter.py     thin subclass: ProxyBackend + print fmt

ml/                           ML pipeline (training + inference)
├── config.py                 versions / paths / params (single source)
├── features.py               feature column lists (35-WU / 63-WU+ERA5)
├── pipeline.py               update / labels / features / train / eval / predict
├── data/                     build_labels, build_intraday_features (gitignored)
├── models/                   train, evaluate, predict (Predictor class)
└── backtest/backtest.py      backtest engine (uses strategy_math)

scripts/                      shell + thin CLI shims
├── daily_retrain.py          off-host retrain pipeline (local Mac / GPU box)
├── pull_training_data.sh     trainer pulls DBs from singapore archive
├── push_artifacts.sh         trainer pushes artifacts → valkyrie + singapore
└── *.py                      5-line shims that delegate to ops/

tests/                        pytest, all offline (97 tests, <1s)
├── common/                   env loader + orderbook dataclasses
└── clients/                  data parsers + trading backend ABCs / DryRun / Proxy
```

## Common commands

Everything is invoked via `python -m <package>` from the repo root.
`pip install -r requirements.txt` for the basics; `pip install pytest pytest-mock requests-mock` for the test suite.

```bash
# End-to-end daily refresh (fetch WU + Polymarket → labels → features → train → eval)
python -m ml.pipeline full --version v5

# Individual stages
python -m ml.pipeline update                # WU last 2d + Polymarket tokens + 7d prices
python -m ml.pipeline labels                # rebuild daily_labels.db
python -m ml.pipeline features              # rebuild intraday_features.parquet
python -m ml.pipeline train     --version v5
python -m ml.pipeline evaluate  --version v5
python -m ml.pipeline predict   --version v5  # one-shot intraday inference

# Direct module entrypoints (used by valkyrie hot-refresh.sh + singapore archive.sh)
python -m clients.data.open_meteo --forecast-only       # GFS for today
python -m clients.data.polymarket_meta                  # token.db refresh
python -m clients.data.polymarket_prices                # 1-min price history pull

# Manual half-hour temperature reading (when WU fetch fails)
python -m ml.pipeline --temp 27.8           # current slot
python -m ml.pipeline --temp 27.8 14:30 --retrain --version v5

# Live trading (requires py-clob-client-v2 + funded wallet)
python -m strategies.modify_str --dry-run --version v5
python -m strategies.modify_str --version v5 --backend direct
python -m strategies.modify_str --version v5 --backend proxy   # via SSH tunnel

# Backtest
python -m ml.backtest.backtest --version v5 --start 2026-03-13 --end 2026-05-01

# Scheduled retrain (run on a non-singapore host after market close, ~8–12 min on Mac)
python -m scripts.daily_retrain --version v5

# Operations (rotate emergency / deploy proxy / setup creds / preflight)
python -m ops.preflight_clob --skip-order
python -m ops.setup_clob_creds
python -m ops.rotate_wallet --skip-remote   # local-only
python -m ops.deploy_proxy                  # needs ~$0.05 POL on EOA

# Tests
pytest tests/                  # offline only, ~1s
pytest tests/ -m network       # network-tier (none currently)
```

## Architecture

### Data flow

```
clients/data/*  →  database/*.db        →  ml/data/build_*    →  intraday_features.parquet
                                                                        │
                                                                        ▼
                                                              ml/models/train  →  ml/artifacts/v*/
                                                                        │
                                                                        ▼
                                                  ml/models/predict  +  strategies/modify_str  →  clients/trading  →  Polymarket CLOB
                                                         ↑                                          ↑
                                                         └────────── strategy_math ────────────────┘
                                                                  (shared by backtest + live)
```

Six SQLite DBs in `database/` (gitignored, created on first run):
- `standard.db` — WU half-hourly observations (primary intraday signal)
- `weather_om.db` — ERA5 archive + GFS forecast (Open-Meteo, v5 only)
- `daily_labels.db` — daily tmax/tmin ground truth
- `iem_asos_zspd.db` — IEM ASOS backup labels (read-only)
- `token.db`, `data.db`, `orderbook.db` — Polymarket contract metadata, 1-min prices, L2 snapshots

### Versioning is first-class

The `--version` flag (`v0|v2|v3|v4|v5`) selects feature list, training window, training method, and LightGBM params. Centralized in `ml/config.py`:
- `get_date_splits(version)` → `(data_start, test_start)`
- `get_feature_list(version)` → 35 (WU-only) or 63 (WU+ERA5) cols from `ml/features.py`
- `get_lgbm_params(version)` → `LGBM_BASE | LGBM_V2 | LGBM_V5`
- `get_artifact_dir(version)` → `ml/artifacts/v*/`

v3/v5 use walk-forward CV; v4 reuses v3 artifacts and applies a seasonal calibration JSON at inference time only (no separate training). v5 is the production version.

#### Per-version resource & accuracy profile

Each version trains 16 LightGBM boosters per day: 2 targets (tmax, tmin) × (7 quantile heads + 1 L2 point head). All training uses `n_jobs=-1` (all CPU cores). Latency below was measured on Apple Silicon (Python 3.11, lightgbm 4.6.0) with single-row prediction across all 16 boosters — this is the realistic per-slot cost in `predict` / `modify_str`.

**Resources & training:**

| Version | Features | Window | Trees (16 boosters) | Leaves | Disk | Inference latency¹ | Training time² |
|---|---|---|---|---|---|---|---|
| v0 | 35 (WU) | 3 yr, simple split | 5,481 | ~170k | 16 MB | 0.78 ms | ~1–2 min |
| v2 | 35 (WU) | 2 yr, simple split | 6,552 | ~261k | ~16 MB | 0.68 ms | ~1–2 min |
| v3 | 35 (WU) | walk-forward CV (2024-03→2026-03) | 5,144 | ~205k | 19 MB | 0.65 ms | ~3–5 min |
| v4 | 35 (WU) | v3 + month-bias calibration | 6,297 | ~195k | (v3 + JSON) | 0.65 ms | inference-only |
| v5 | 63 (WU+ERA5) | walk-forward CV (2024-03→2026-03) | 7,815 | ~488k | 45 MB | 0.6–0.9 min³ |

¹ Single-row, 16-booster forward pass via `booster.predict`. Real strategy latency is bounded by Polymarket API I/O (~hundreds of ms), not the model.
² Reported here is model fit time alone. Full pipeline (`scripts/daily_retrain --version v5`) is documented as 8–12 min once you include WU fetch + label/feature rebuild.
³ v5 fit time: ~6–8 min for full walk-forward CV.

**Evaluate metrics (from `ml/artifacts/v*/eval_metrics.json`, snapshot 2026-05-07):**

| Version | Test window | N | tmax MAE / RMSE / Bias | tmin MAE / RMSE / Bias | tmax Pinball / Brier | tmin Pinball / Brier |
|---|---|---|---|---|---|---|
| v0 | 2025-11-01 → 2026-05-07 | 9,024 | 1.10 / 1.61 / +0.09 | 0.72 / 1.07 / −0.02 | 0.346 / 0.039 | 0.576 / 0.071 |
| v2 | 2026-03-01 → 2026-05-07 | 3,264 | 1.75 / 2.40 / **+1.06** | 0.66 / 0.95 / −0.36 | 0.516 / 0.062 | 0.193 / 0.034 |
| v3 | 2026-03-01 → 2026-05-07 | 3,264 | 1.26 / 1.89 / +0.20 | 0.63 / 0.90 / −0.25 | 0.419 / 0.047 | 0.190 / 0.031 |
| v4 | 2026-03-01 → 2026-05-07 | 3,264 | 1.27 / 1.88 / +0.20 | 0.63 / 0.93 / −0.26 | 0.405 / 0.047 | 0.170 / 0.032 |
| v5 | 2026-03-01 → 2026-05-07 | 3,264 | 1.29 / 1.91 / +0.32 | 0.68 / 0.89 / −0.38 | 0.432 / 0.048 | 0.210 / 0.037 |

Numbers are LightGBM-L2 for MAE/RMSE/Bias (units: °C; Bias = mean(pred − truth)); Pinball is mean over 7 quantiles; Brier is mean over thresholds with empirical frequency in [0.02, 0.98]. **v0's test window is much wider** (6 months including winter cold-start) so its higher absolute MAE is partly a window effect, not pure model degradation.

**MAE by time-of-day bucket** (LightGBM-L2, °C):

| ver | tmax cold-start / morning / daytime / evening | tmin cold-start / morning / daytime / evening |
|---|---|---|
| v0 | 1.85 / 1.21 / 0.57 / 0.55 | 0.93 / 0.68 / 0.62 / 0.56 |
| v2 | 2.78 / 1.99 / 1.01 / 0.96 | 0.85 / 0.65 / 0.56 / 0.54 |
| v3 | 2.21 / 1.45 / 0.56 / 0.56 | 0.81 / 0.64 / 0.55 / 0.49 |
| v4 | 2.20 / 1.44 / 0.58 / 0.58 | 0.84 / 0.62 / 0.53 / 0.49 |
| v5 | 2.13 / 1.52 / 0.65 / 0.66 | 0.84 / 0.66 / 0.59 / 0.56 |

Cold-start (00:00–07:30 CST) tmax MAE is several times higher than evening — that's why the strategy disables tmax model signals when `slot_h < 8`.

**Backtest P&L** (2026-03-13 → 2026-04-28, base_size=$1, from `ml/backtest/eval/v*/summary_*.txt`):

| Version | Trades | Total PnL | Daily Sharpe | Win rate |
|---|---|---|---|---|
| v0 | 5,649 | +$348 | 5.94 | 20.7% |
| v2 | 5,731 | +$362 | 5.47 | 20.3% |
| v3 | 5,379 | +$416 | 6.68 | 22.3% |
| v4 | 4,746 | +$467 | 7.50 | 27.8% |
| v5 | 4,355 | +$462 | 8.32 | 24.9% |

Note: v5 has slightly worse point-prediction MAE on this test window than v3/v4 but produces the best risk-adjusted PnL — the ERA5 features improve tail probability calibration (which drives bet sizing), not the median forecast.

**Regenerating these numbers:** point/quantile/Brier metrics are written to `ml/artifacts/<version>/eval_metrics.json` whenever you run

```bash
python -m ml.pipeline evaluate --version v5     # writes eval_metrics.json automatically
python -m ml.models.evaluate --version v5 --json auto   # same, direct CLI
python -m ml.models.evaluate --version v5 --json /tmp/x.json   # custom path
```

This requires a populated `database/intraday_features.parquet`. To rebuild from scratch:

```bash
python -m clients.data.wu 20230501 <today>            # WU half-hourly obs → standard.db
python -m clients.data.open_meteo --backfill 2023-05-01  # ERA5 archive → weather_om.db
python -m ml.data.build_labels                         # → daily_labels.db
python -m ml.data.build_intraday_features              # → intraday_features.parquet (≈53k rows)
```

### Feature builder

`ml/data/build_intraday_features.py` produces a single `intraday_features.parquet` with all 63 columns + 4 labels + date — every version reads the same file and slices columns via `get_feature_list`. The 48 training slots × multi-day history mean this file is the single source of truth for training and historical inference.

`ml.pipeline.run_predict` re-implements the slot-feature derivation from `build_intraday_features` helpers (`_load_wu`, `_slot_features`, `_slot_derived`, `_load_era5`, `_era5_slot_features`, `_calendar_features`) so live inference matches the training feature definitions exactly. If you change feature engineering, update both.

**Note:** the `ml/data/` package is gitignored (the root `.gitignore` has a generic `data/` rule that matches it). The builder modules are required for the pipeline to run; if they're missing locally they need to be restored from elsewhere.

### Models

For each of `tmax` and `tmin` (`ml/models/train.py`):
- 7 quantile LightGBM models (`QUANTILES = [0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95]`)
- 1 L2 point-prediction LightGBM
- Saved as `lgbm_quantile_*.pkl` + `lgbm_l2_*.pkl` per artifact dir
- `feature_cols.json` records the exact column order to reload at inference

### Strategy: two-tier signals

`strategies/modify_str.py` and `ml/backtest/backtest.py` share the same math by importing from `strategy_math/`:

- **Tier 1 — Certainty:** physics-locked P=0/P=1 from intraday observations. Once `obs_tmax ≥ T-0.5`, `ge_T` settles YES with certainty. Entered at most once per token per day. Filters: YES needs `edge > 0.02`; NO needs `YES_market > 0.01`.
- **Tier 2 — Model edge:** `edge = model_prob − YES_mid`, enter when `|edge| ≥ min_edge` (default 0.05). Special rules: `slot_h < 8` disables tmax model signals (cold start); `ge YES` requires `edge ≥ 0.30` (tmax model systematically over-estimates ge contracts by 15–26%).

Position sizing is tiered by `|edge|` (`strategy_math.sizing._position_size`). The settlement convention is `settle(t) = floor(t + 0.5)` (`ml.config.settle`).

### Where strategy math lives

The math is the same code path for backtest and live trading. Single source of truth in `strategy_math/`:

- `certainty.py`  — `_certainty_prob`, `_event_happened`
- `features.py`   — `_obs_features`, `_obs_derived_features`, `_era5_slot_features`, `_lag_features`, `_calendar_features`, `_non_nan`
- `labels.py`     — `_load_labels`
- `model_edge.py` — `_model_prob`
- `sizing.py`     — `_position_size`

Both `strategies/modify_str.py` and `ml/backtest/backtest.py` import from `strategy_math` (the latter also re-exports the names for any legacy callers). **Don't add a fork of any of these in either consumer — change them in `strategy_math/` and both stay in sync.**

### Trading backend abstraction

`clients/trading/base.TradingBackend` is the single contract for getting books / placing orders / cancelling / reading balance. Three implementations:
- `DirectBackend` — `py-clob-client-v2` native (use on valkyrie / non-blocked hosts)
- `ProxyBackend` — HTTP to `clients/trading/server.py` running on a non-blocked host
- `DryRunBackend` — offline stub for tests / dry-run

Strategies use the abstraction via thin formatter subclasses in `strategies/{clob,proxy_clob}_adapter.py`. New strategy code should target `TradingBackend` directly.

### Testing

Offline pytest suite at `tests/`. 97 tests, all-pass in <1s. Covers `common.env`, the orderbook dataclasses, `clients.trading` (full DryRun + ABC + Proxy via `requests-mock`), and `clients.data` parsers / sqlite migrations. Network-tier tests are gated behind `pytest -m network` (none currently — placeholder for cassette-based fetcher tests).

```bash
pytest tests/         # routinely, after any clients/* or common/* change
pytest tests/ -v -k <name>   # focus on one
```

## Conventions

- All timestamps in code are timezone-aware. CST = `timezone(timedelta(hours=8))`. WU "slot_utc" is UTC; `hour_frac` is in CST.
- Polymarket contract bounds: tmax uses `eq`/`le`, tmin uses `ge`/`eq` (see `strategies/config.json`).
- Don't add a model variant by branching inside trainer code — add a new version key in `ml/config.py` and route through the existing `get_*` selectors.
- Pipeline outputs are deliberately verbose with `[Stage] ...` prefix lines; preserve that style if adding new stages.
