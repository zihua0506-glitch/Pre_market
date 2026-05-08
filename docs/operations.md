# Operations runbook

Daily checklist for running the ZSPD Polymarket trader. Two contexts:
- **Local dev** (Mac, geo-blocked from trading): used for backtesting, model R&D, dry-run validation. **Also where retraining now happens** (or any non-blocked GPU box).
- **Production**: split across `singapore` (data archive + read-only Polymarket dashboard) and `valkyrie` (inference, live trading). See [deployment.md](deployment.md) for the topology, systemd units, and infra runbooks.

If you're operating the production setup, prefer the systemd units — they handle scheduling and logging. The manual commands below are a fallback / for ad-hoc work.

---

## Production — automatic flow (no human in the loop)

Already wired up via systemd timers — verify with `systemctl list-timers`:

```bash
ssh singapore  'systemctl list-timers zspd-* --no-pager'
ssh valkyrie   'systemctl list-timers zspd-* --no-pager'
```

Daily timeline (UTC; CST = UTC + 8):

| UTC | CST | Where | Unit | What |
|---|---|---|---|---|
| 16:30 | 00:30 | singapore | `zspd-archive.timer` | data refresh (update + labels + features) + push `daily_labels.db` to valkyrie + 14-day DB snapshot |
| 17:30 | 01:30 | valkyrie | `zspd-hot-refresh.timer` | pull today's WU + GFS + Polymarket meta |
| (manual) | (manual) | singapore | `zspd-dashboard.service` | read-only Polymarket viewer on `127.0.0.1:8090` (SSH tunnel; not enabled by default) |
| (manual) | (manual) | local/GPU | retrain off-host | pull DBs from singapore, train, rsync `ml/artifacts/v5/` to valkyrie |
| (manual) | (manual) | valkyrie | `zspd-strategy.service` | live trading (operator-gated, see deployment.md) |

Tail logs:

```bash
ssh singapore 'journalctl -u zspd-archive -f'
ssh singapore 'journalctl -u zspd-dashboard -f'
ssh valkyrie  'journalctl -u zspd-hot-refresh -f'
ssh valkyrie  'journalctl -u zspd-strategy -f'
```

---

## Local dev — manual flow

### Pre-market data refresh (any time before you start a dry-run)

```bash
# Today's GFS forecast (v5 needs sw_radiation / cloud_pct)
python -m clients.data.open_meteo --forecast-only

# Refresh Polymarket token metadata + last 7d prices + last 2d WU obs
python -m ml.pipeline update
```

### Dry-run a strategy slot

```bash
python -m strategies.modify_str --dry-run --version v5
# defaults: --backend proxy   (talks to clob-proxy; needs SSH tunnel to valkyrie)
# add  --backend direct  to call CLOB locally — only works on a non-geo-blocked IP.
```

### One-shot intraday prediction
```bash
python -m ml.pipeline predict --version v5
```

### Backtest
```bash
python -m ml.backtest.backtest --version v5 --start 2026-03-13 --end 2026-05-01
```

---

## Behavior of a running strategy

| Data | Refresh policy | Where |
|---|---|---|
| WU half-hour temperature | auto-pulled per slot via `refresh_today_wu()`; up to 3 retries; manual prompt fallback | strategy main loop |
| ERA5 / GFS forecast | loaded **once at startup** | per-day, cheap |
| Historical lag features | loaded **once at startup** (today's lags don't change) | from `daily_labels.db` |
| Polymarket token metadata | loaded **once at startup** | from `token.db` populated by `fetch_tokens` |
| Polymarket order book | per-token per-slot (live) | injected `order_book_fn` |

WU pull failure behavior:

> If the WU half-hour observation can't be fetched after 3 retries, the strategy prints `MANUAL INPUT REQUIRED` and waits for the operator to type the temperature in degrees Celsius (or Enter to skip the slot). In production we'd rather skip than insert a guess.

---

## Strategy CLI parameters

```bash
python -m strategies.modify_str \
    --version v5 \
    --backend {direct,proxy} \   # default proxy
    --min-edge 0.05 \            # model signal floor
    --size 0.5 \                 # base $ position
    --max-per-slot 1 \           # per-30-min cap
    --date 2026-05-08 \          # override "today"
    --dry-run                    # no real orders
```

Production defaults (from `valkyrie:/etc/systemd/system/zspd-strategy.service`):

```
--backend direct   --version v5
--size 0.5  --max-per-slot 1  --min-edge 0.08
```

These are intentionally conservative. Tune via `systemctl edit zspd-strategy` after watching real fills for a few days.

---

## Manual data patches

### Insert a half-hour temperature reading by hand

```bash
python -m ml.pipeline --temp 27.8                  # current slot
python -m ml.pipeline --temp 27.8 14:30            # specific slot
python -m ml.pipeline --temp 27.8 14:30 --retrain --version v5
```

### Rebuild only one stage

```bash
python -m ml.data.build_labels                    # rebuild daily_labels.db
python -m ml.data.build_intraday_features         # rebuild parquet
python -m ml.models.train --version v5            # train only
python -m ml.models.evaluate --version v5         # evaluate only
```

### Backfill a missing day

```bash
# Refetch WU history for a date range
python -m clients.data.wu 20260331 20260331

# Then rebuild labels + features
python -m ml.pipeline labels && python -m ml.pipeline features
```

---

## Wallet — daily operations

The production wallet is **already bootstrapped, funded, and configured**. See
[STATUS.md](STATUS.md) for the live address book and balance.

For routine ops you only need three things:

### Check current balance + allowance
```bash
ssh valkyrie 'curl -s -H "Authorization: Bearer $(grep PROXY_AUTH_TOKEN /opt/clob-proxy/.env | cut -d= -f2)" http://127.0.0.1:8088/balance'
```

### Top up the Proxy Wallet
Send USDC (Polygon-native, contract `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`)
from any CEX / bridge / on-ramp to the Proxy Wallet address shown in STATUS.md.
**Send to the Proxy Wallet, not the EOA.** Allowances are already ∞; no
configuration changes needed after a top-up.

### Verify .env is in sync across hosts
```bash
LOCAL=$(grep "^POLY_PRIVATE_KEY=" .env | cut -d= -f2)
VALK=$(ssh valkyrie 'grep "^POLY_PRIVATE_KEY=" /opt/zspd-trader/.env | cut -d= -f2')
[ "$LOCAL" = "$VALK" ] && echo "  ✅ in sync" || echo "  ❌ mismatch — re-push .env"
```

### Going live (start the strategy)

```bash
# (optional) tune sizing first — see STATUS.md "Sizing options"
ssh valkyrie 'systemctl edit zspd-strategy'

ssh valkyrie 'systemctl start zspd-strategy'
ssh valkyrie 'journalctl -u zspd-strategy -f'

# panic button
ssh valkyrie 'systemctl stop zspd-strategy'
```

Strategy is operator-gated (the unit is `static`, not `enabled`), so it
won't auto-restart on boot — you control when it runs.

---

## Wallet — emergency rotation (only if private key is leaked)

⚠️ **If the wallet is funded, rotating abandons those funds.** Sweep first.

```bash
python -m scripts.rotate_wallet
# → generates a fresh EOA + creds
# → backs up local .env to .env.rotated-<ts>.bak
# → scp .env to valkyrie:/opt/zspd-trader/.env and /opt/clob-proxy/.env
# → restarts clob-proxy.service
```

Then the new EOA needs a Proxy Wallet:
- **Easiest**: connect the new EOA at polymarket.com from a residential
  (non-datacenter) non-blocked IP. Polymarket UI deploys the proxy and shows
  the address. Send USDC there.
- **If you only have datacenter IPs**: `python -m scripts.deploy_proxy`
  deploys the Polymarket Proxy contract on-chain directly. Needs ~$0.05 of
  POL on the new EOA for gas.

Either path ends with `POLY_FUNDER` + `POLY_SIGNATURE_TYPE=1` written into
`.env` on every host, then `python -m scripts.preflight_clob --skip-order` to
verify.

---

## Training elsewhere (local Mac, GPU server)

Singapore is **not** a training host any more — it's archive + dashboard. Pull the
DBs from singapore, train wherever you have CPU/GPU, push artifacts back.

### Pull the canonical DBs

```bash
# Default: singapore archive → ./src/database/
scripts/pull_training_data.sh

# Custom destination
scripts/pull_training_data.sh /tmp/zspd-train-snapshot

# From a different archive host
ZSPD_ARCHIVE_HOST=my-gpu-box scripts/pull_training_data.sh
```

### Train

```bash
python -m ml.pipeline labels      # rebuild daily_labels.db locally if you want
python -m ml.pipeline features    # rebuild parquet
python -m ml.pipeline train    --version v5
python -m ml.pipeline evaluate --version v5
```

### Push artifacts back to valkyrie + archive

```bash
scripts/push_artifacts.sh                            # v5, both targets
ZSPD_VERSION=v3 scripts/push_artifacts.sh            # different version
ZSPD_SKIP_ARCHIVE=1 scripts/push_artifacts.sh        # trader only, skip archive
```

The trader (valkyrie) starts using the new artifacts on the next half-hour
slot — no restart needed (`Predictor` is reloaded each slot).

Singapore's daily archive job (`zspd-archive.timer` @16:30 UTC) only refreshes
DBs and pushes `daily_labels.db` to valkyrie — it does NOT train, so artifacts
on singapore stay untouched until you push new ones.

---

## Troubleshooting

| Symptom | First thing to check |
|---|---|
| `MANUAL INPUT REQUIRED` repeatedly at the live console | WU API outage; verify `curl https://api.weather.com/...` from the strategy host. Strategy keeps skipping slots until it recovers. |
| Strategy stuck at `[Predictor] loading…` | corrupted `.pkl`; restore from singapore: `rsync /opt/zspd-trainer/src/ml/artifacts/_history/<yesterday>-v5/ valkyrie:/opt/zspd-trader/src/ml/artifacts/v5/` |
| Polymarket `order_version_mismatch` | py-clob-client not on v2; reinstall: `uv pip install py-clob-client-v2` |
| Polymarket `maker address not allowed` | wallet didn't go through Proxy Wallet flow; see deployment.md "Fund the wallet" section |
| Polymarket `Trading restricted in your region` | IP geo-blocked; you're using `--backend direct` from a blocked host. Switch to `--backend proxy` and SSH-tunnel to valkyrie, or run the strategy on valkyrie itself. |
| `lightgbm.basic.LightGBMError: scikit-learn is required` | install sklearn into the venv (`uv pip install scikit-learn`); pickled `LGBMRegressor` objects need it. |
| `sqlite3.OperationalError: views may not be indexed` | old `data.db` schema; `core/fetch_data.py:init_db` already auto-migrates, but if you see this on a stale build, pull latest source. |
