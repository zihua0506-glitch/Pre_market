# DEPLOYMENT

How the ZSPD Polymarket trader runs in production.

## Topology

```
┌─────────── SINGAPORE (archive + 看盘) ──────────────┐
│ root@217.15.166.15  AS141995 Contabo SG              │
│ 6 cores AMD EPYC, 11 GB RAM, 193 GB disk             │
│ Geo-blocked by Polymarket (POST /order → 403);       │
│ public GET endpoints (gamma, clob /book) reachable.  │
│                                                      │
│ /opt/zspd-trainer/                                   │
│   ├─ src/         (full repo, data prep only)        │
│   ├─ src/database/  authoritative historical DBs     │
│   ├─ src/ml/artifacts/v*/  legacy snapshot (frozen,  │
│   │                    not refreshed here — train    │
│   │                    moved to local/GPU box)       │
│   ├─ .venv/       data-prep deps (lightgbm installed │
│   │               but not invoked here)              │
│   ├─ archive.sh   daily data refresh + push labels   │
│   └─ snapshots/YYYY-MM-DD/  rolling 14-day backup    │
│                  of src/database/ via rsync          │
│                  --link-dest                         │
│                                                      │
│ /opt/zspd-dashboard/  read-only Polymarket viewer    │
│   ├─ app.py       FastAPI (skeleton)                 │
│   └─ .venv/       fastapi/uvicorn/httpx (no ML)      │
│                                                      │
│ systemd:                                             │
│   zspd-archive.timer    @ 16:30 UTC daily            │
│   zspd-dashboard.service  manual start, 127.0.0.1:8090│
└──────────────────────────┬──────────────────────────┘
                           │ rsync over SSH (daily_labels.db only)
                           │ singapore → valkyrie (one-way push)
                           ▼
┌────────────── VALKYRIE (hot + 推理 + 交易) ──────────┐
│ root@204.168.241.66  AS24940 Hetzner FI              │
│ 2 cores Intel Skylake, 3.7 GB RAM, 38 GB disk        │
│ NOT geo-blocked by Polymarket                        │
│                                                      │
│ /opt/clob-proxy/    (legacy proxy from earlier work; │
│                      kept as fallback)               │
│                                                      │
│ /opt/zspd-trader/                                    │
│   ├─ src/             (strategy/inference subset)    │
│   ├─ src/database/    今天 hot DB (self-fetched)     │
│   ├─ src/ml/artifacts/v5/  ← rsync target from       │
│   │                    off-host trainer (Mac/GPU)    │
│   ├─ .venv/           minimal trading deps           │
│   └─ .env             POLY_* creds + PROXY_AUTH_TOKEN│
│                                                      │
│ systemd:                                             │
│   zspd-strategy.service     live trading             │
│   zspd-hot-refresh.timer    pre-market WU/GFS pull   │
│   clob-proxy.service        (kept; alt access path)  │
└──────────────────────────────────────────────────────┘
```

## Servers

### `valkyrie` (host alias in `~/.ssh/config`)
- **HostName**: 204.168.241.66
- **User**: root
- **Country**: 🇫🇮 Finland (Helsinki, Hetzner)
- **Role**: live trading. Polymarket reachable, accepts POST /order.
- **Resources**: 2 cores, 3.7 GB RAM (no swap by default — we add 2 GB), 38 GB disk
- **Existing services we share with**: `clob-proxy.service` (port 8088), `polysim-dashboard.service` (port 8000). Both bind 127.0.0.1.

### `singapore` (host alias in `~/.ssh/config`)
- **HostName**: 217.15.166.15
- **User**: root
- **Country**: 🇸🇬 Singapore (Contabo)
- **Role**: data archive + daily data refresh + read-only Polymarket dashboard. **Geo-blocked from trading**, but public Polymarket GET endpoints are reachable. **No longer trains models** — training moved off-host (local Mac or GPU box pulls DBs from here, trains, pushes artifacts to valkyrie).
- **Resources**: 6 cores AMD EPYC, 11 GB RAM, 193 GB disk
- **Existing services we share with**: containerd / Docker stacks (untouched).
- **Note**: `/opt/zspd-trainer/.venv` still contains scikit-learn / scipy from the old retrain pipeline. They're unused now (archive.sh only needs lightgbm + pandas + pyarrow + requests + aiohttp + numpy for the labels/features build). Safe to leave; if disk pressure appears, prune later.

### Local Mac (this dev machine)
- **Country**: 🇬🇧 UK (Jisc, geo-blocked)
- **Role**: development only. Runs dry-run, edits code, ssh into the two servers. No live trading from here.

## Daily lifecycle (CST = UTC+8)

| UTC | CST | Where | What |
|---|---|---|---|
| 15:59 | 23:59 | valkyrie | Market close. `zspd-strategy.service` exits or idles. |
| 16:30 | 00:30 | singapore | `zspd-archive.timer` fires → `archive.sh` |
| 16:30→16:32 | 00:30→00:32 | singapore | `pipeline update` (WU + Polymarket + ERA5) |
| 16:32→16:34 | 00:32→00:34 | singapore | `pipeline labels` + `features` |
| 16:34→16:35 | 00:34→00:35 | singapore→valkyrie | rsync `daily_labels.db` (no artifacts: training is off-host) |
| 16:35→16:36 | 00:35→00:36 | singapore | rolling snapshot of `src/database/` → `snapshots/YYYY-MM-DD/` (14-day retention via `--link-dest`) |
| (off-host) | (off-host) | local/GPU | manual: pull DBs from singapore, train, rsync `ml/artifacts/v5/` to valkyrie |
| 17:30 | 01:30 | valkyrie | `zspd-hot-refresh.timer` → today's WU/GFS/Polymarket meta |
| 00:01 | 08:01 | valkyrie | `zspd-strategy.service` starts (per-day instance) |
| 00:01→15:59 | 08:01→23:59 | valkyrie | strategy main loop, half-hour slots |

(Strategy actually wakes at HH:01 / HH:31 each half-hour. The first slot of "the day" depends on when you choose to enable trading; for cold-start safety the strategy itself disables tmax model signals when `slot_h < 8`.)

## Sync pipeline

**Direction**: singapore → valkyrie only. Valkyrie never pushes to singapore.

**Auth**: dedicated ed25519 deploy key on singapore, public half whitelisted in `valkyrie:/root/.ssh/authorized_keys` with `from="217.15.166.15"` to limit source.

**Payload (one rsync per file/tree)**:
- `database/daily_labels.db` (≈ 140 KB)  — historical labels for lag features (pushed by `archive.sh`)
- `ml/artifacts/v5/` (≈ 45 MB)  — production model (no longer auto-pushed; training is off-host. Operator manually rsyncs after retrain.)

**Gating**: rsync of `daily_labels.db` runs only if `archive.sh` data stages exited 0. Failed archive ⇒ valkyrie keeps yesterday's labels.

## Operational runbooks

### Trigger an unscheduled archive (data refresh + label push)
```bash
ssh singapore 'systemctl start zspd-archive.service'        # one-shot
ssh singapore 'journalctl -u zspd-archive -f'               # tail logs
```

### Trigger an unscheduled retrain (off-host)
Training no longer runs on singapore. Pull DBs, train locally, push artifacts:
```bash
# from local Mac or GPU box
rsync -az singapore:/opt/zspd-trainer/src/database/ ./src/database/
python -m ml.pipeline train --version v5
python -m ml.pipeline evaluate --version v5
rsync -az --delete src/ml/artifacts/v5/ valkyrie:/opt/zspd-trader/src/ml/artifacts/v5/
```

### Open the singapore dashboard (read-only Polymarket viewer)
The dashboard is bound to `127.0.0.1:8090` and is **not** publicly exposed.
```bash
# 1. SSH-tunnel from your laptop
ssh -L 8090:127.0.0.1:8090 singapore
# 2. browse  http://127.0.0.1:8090
# 3. for /api/* the bearer token lives in singapore:/opt/zspd-dashboard/.env (DASHBOARD_AUTH_TOKEN)
```
Manual control:
```bash
ssh singapore 'systemctl start zspd-dashboard'      # service is NOT enabled by default
ssh singapore 'systemctl status zspd-dashboard'
ssh singapore 'journalctl -u zspd-dashboard -f'
```

### Tail strategy logs
```bash
ssh valkyrie 'journalctl -u zspd-strategy -f'
```

### Stop trading immediately
```bash
ssh valkyrie 'systemctl stop zspd-strategy'
```

### See what artifacts valkyrie is using right now
```bash
ssh valkyrie 'ls -la /opt/zspd-trader/ml/artifacts/v5/ | head -10'
ssh valkyrie 'cat /opt/zspd-trader/ml/artifacts/v5/eval_metrics.json | python3 -m json.tool | head -20'
```

### Top up the Proxy Wallet (when balance gets low)
Send USDC (Polygon-native, contract `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`)
to the Proxy Wallet `0x65917b5e41485B8c62814344184aC70c05CbdA58` from any
source (CEX withdraw, bridge, on-ramp). No further configuration is needed —
`POLY_FUNDER` + `POLY_SIGNATURE_TYPE=1` are already in `.env` on every host.

⚠️ Send to the **Proxy Wallet** (`0x65917b...`), NOT to the EOA (`0x398112...`).
The EOA is a Magic-managed signing key only and holds no funds.

### Bootstrap a brand-new wallet (only if the existing key is compromised)
This abandons existing USDC. Sweep first if needed.

1. Run `python -m scripts.rotate_wallet` (generates a fresh EOA + creds, pushes
   `.env` to valkyrie, restarts clob-proxy).
2. From a non-blocked, **non-datacenter** IP (residential), browse polymarket.com
   and connect the new EOA. Polymarket auto-deploys a Proxy Wallet.
3. The new Proxy address appears in Settings → Wallet → Deposit address.
4. Send USDC to the new Proxy.
5. `ssh valkyrie 'cat >> /opt/zspd-trader/.env <<EOF
   POLY_FUNDER=0x<new proxy>
   POLY_SIGNATURE_TYPE=1
   EOF'`
6. `ssh valkyrie 'systemctl restart clob-proxy'`
7. `python -m scripts.preflight_clob --skip-order` to verify.

If the new EOA can't reach the Polymarket UI deposit page (e.g. you only have
datacenter IPs), `scripts/deploy_proxy.py` can deploy the Proxy contract on-chain
directly — needs ~$0.05 worth of POL on the EOA for gas.

### Recover artifact corruption
Production artifacts on valkyrie come from off-host training (local Mac or GPU
box). To roll back, re-train on a known-good DB snapshot or restore a previous
artifacts/v5/ directory from your trainer host's git/backup.

The legacy `singapore:/opt/zspd-trainer/ml/artifacts/` is a frozen snapshot
from before the singapore role change — it's not refreshed and shouldn't be
used as a recovery source for the latest model.

If singapore's daily DBs themselves get corrupted (rare), restore from
`singapore:/opt/zspd-trainer/snapshots/YYYY-MM-DD/` (14-day rolling backup
maintained by `archive.sh` via `rsync --link-dest`).

### Add Python deps after deploy
```bash
# singapore
ssh singapore 'cd /opt/zspd-trainer && /root/.local/bin/uv pip install --python .venv/bin/python <pkg>'

# valkyrie
ssh valkyrie 'cd /opt/zspd-trader  && /root/.local/bin/uv pip install --python .venv/bin/python <pkg>'
```

### Push code changes from local Mac
After editing strategy/inference code locally:

```bash
# To valkyrie (hot path: most strategy edits go here)
rsync -avz --exclude-from=/tmp/zspd-rsync-exclude.txt \
    --exclude='database/' --exclude='ml/artifacts/' \
    . valkyrie:/opt/zspd-trader/src/
ssh valkyrie 'systemctl restart zspd-strategy'   # only if it was running

# To singapore (training-side edits, e.g. ml/data/, ml/models/train.py)
rsync -avz --exclude-from=/tmp/zspd-rsync-exclude.txt \
    --exclude='database/' --exclude='ml/artifacts/' \
    . singapore:/opt/zspd-trainer/src/
```

The `/tmp/zspd-rsync-exclude.txt` content (build it once locally):
```
__pycache__/
*.pyc
.git/
.DS_Store
.venv/
node_modules/
ml/artifacts/_history/
database/orderbook.db
```

### Inspect the rolling artifact history (last 7 days)
```bash
ssh singapore 'ls -la /opt/zspd-trainer/ml/artifacts/_history/'
# To roll back to a known-good day:
ssh singapore 'rsync -av /opt/zspd-trainer/ml/artifacts/_history/20260507-v5/ valkyrie:/opt/zspd-trader/ml/artifacts/v5/'
```

## What's deployed (state at first deploy)

### Singapore — `/opt/zspd-trainer/` (verified 2026-05-08)
```
.
├── src/                              full repo (data prep only; training is off-host)
│   ├── database/                     ← archive.sh reads/writes here (~163 MB total)
│   │   ├── standard.db (7.4M)   weather_om.db (4.7M)   daily_labels.db (140K)
│   │   ├── data.db (146M)   iem_asos_zspd.db (4.1M)   intraday_features.parquet (840K)
│   │   └── token.db (492K)
│   └── ml/artifacts/                 ← legacy snapshot, no longer refreshed here
│       ├── v0/ v2/ v3/ v4/ v5/        (frozen, do not use as recovery source)
│       └── _history/20260508-v5/      (last off-host re-training snapshot)
├── archive.sh             daily data pipeline + push daily_labels.db
├── retrain.sh.disabled    legacy retrain script (kept for reference; not invoked)
├── snapshots/             14-day rolling rsync --link-dest backup of src/database/
│   └── YYYY-MM-DD/        (1 day present so far; rolls out to 14)
├── ml/                    empty top-level dir (artefact of older layout, ignored)
└── .venv/                 data-prep deps (sklearn/scipy still installed but unused)

Top-level venv (Python 3.12.3, key versions):
  lightgbm 4.6.0   pandas 3.0.2     numpy 2.4.4    pyarrow 24.0.0
  scikit-learn 1.8.0 scipy 1.17.1   (unused; legacy from retrain era)
  fastapi 0.136.1  uvicorn 0.46.0   httpx 0.28.1   aiohttp 3.13.5
  py-clob-client-v2 1.0.0   py-order-utils 0.3.2   requests 2.33.1
  pydantic 2.13.4
```

### Singapore — `/opt/zspd-dashboard/` (verified 2026-05-08)
```
.
├── app.py             FastAPI skeleton; live routes:
│                        GET /health
│                        GET /
│                        GET /api/markets/today    (auth-gated, proxies gamma /events)
│                        GET /api/book/{token_id}  (auth-gated, proxies clob /book)
│                        GET /api/labels/recent    (auth-gated, reads daily_labels.db)
├── templates/         Jinja2 (base.html + index.html placeholder)
├── static/            empty
├── .env               DASHBOARD_HOST, DASHBOARD_PORT, DASHBOARD_AUTH_TOKEN, LABELS_DB
└── .venv/             Python 3.12.3:
                        fastapi 0.136.1  uvicorn 0.46.0  jinja2 3.1.6
                        httpx 0.28.1     requests 2.33.1  pydantic 2.13.4
                        (NO ML)
```

**Layout note (gotcha):** `ml/config.py` defines `ROOT = src/` (one level up from
the `ml/` package). Therefore *all* DB paths and artifact paths in the codebase
resolve to `<repo>/src/database/` and `<repo>/src/ml/artifacts/`. Both
`archive.sh` (singapore) and `hot-refresh.sh` (valkyrie) operate on `$SRC/...`
paths. If you rsync data out-of-band, target `src/database/` and
`src/ml/artifacts/`, **not** the parent `database/` or `ml/artifacts/`.

systemd (verified 2026-05-08):
- `zspd-archive.timer` (enabled, active) — fires `zspd-archive.service` at 16:30 UTC daily, `RandomizedDelaySec=120`, `Persistent=true`
- `zspd-archive.service` (static, oneshot, manual or timer) — runs `archive.sh` (~5 min, `TimeoutStartSec=2700`)
- `zspd-dashboard.service` (disabled-on-boot but currently active/running) — bound to 127.0.0.1:8090, manual lifecycle
- `zspd-retrain.timer` / `zspd-retrain.service` — **removed** (verified absent from `/etc/systemd/system/`)

SSH:
- Dedicated key `/root/.ssh/zspd_deploy` for valkyrie (no passphrase)
  - Fingerprint: `SHA256:808lkjRZNhED2Qv7UA2EzcfKnZ9vzBhRtavuBBhuhvA zspd-deploy@singapore`
- `/root/.ssh/config` has `Host valkyrie` → `204.168.241.66`, `User root`, `IdentityFile /root/.ssh/zspd_deploy`, `IdentitiesOnly yes`
- Public half whitelisted in `valkyrie:/root/.ssh/authorized_keys` with `from="217.15.166.15"`
- Singapore→valkyrie one-way push verified working (BatchMode echo OK)

### Valkyrie — `/opt/zspd-trader/` (verified 2026-05-08)
```
.
├── src/                       full repo, used at runtime
│   ├── database/              ← hot DBs (refreshed daily by hot-refresh.sh)
│   │     daily_labels.db (140K)  data.db (146M)  standard.db (7.4M)
│   │     token.db (476K)         weather_om.db (4.7M)
│   └── ml/artifacts/v5/       ← rsync target from off-host trainer
│         lgbm_quantile_tmax.pkl (18.5M)  lgbm_quantile_tmin.pkl (21.5M)
│         lgbm_l2_tmax.pkl (2.4M)  lgbm_l2_tmin.pkl (2.4M)
│         eval_metrics.json   feature_cols.json   cv_meta_tmax.json   cv_meta_tmin.json
│         (last refreshed 2026-05-08 03:23–03:29 UTC)
├── hot-refresh.sh             pre-market data pull (`pipeline update` + `fetch_weather --forecast-only`)
├── ml/                        empty top-level dir (artefact of older layout, ignored)
├── .env                       POLY_API_KEY, POLY_API_PASSPHRASE, POLY_API_SECRET,
│                              POLY_CHAIN_ID=137, POLY_CLOB_HOST, POLY_GAMMA_HOST,
│                              POLY_FUNDER=0x65917b…dA58, POLY_PRIVATE_KEY,
│                              POLY_SIGNATURE_TYPE=1, PROXY_AUTH_TOKEN, PROXY_URL
│                              (mode 600; same POLY_PRIVATE_KEY as /opt/clob-proxy/.env, sha256-verified)
└── .venv/                     Python 3.12.3, key versions:
                                 lightgbm 4.6.0    pandas 3.0.2    numpy 2.4.4
                                 pyarrow 24.0.0    scikit-learn 1.8.0
                                 py-clob-client-v2 1.0.0    py-order-utils 0.3.2
                                 requests 2.33.1   httpx 0.28.1   pydantic 2.13.4
```

Same layout note as singapore: paths under `src/`, not the parent.

Sibling on the same host (separate, kept):
- `/opt/clob-proxy/` — FastAPI proxy from earlier work; alt access path for Mac dev via SSH tunnel.
  - `clob_proxy_server.py` (single file) + `.env` (same shape as trader's)
  - `.venv/`: fastapi 0.136.1, uvicorn 0.46.0, httpx 0.28.1, requests 2.33.1,
    py_clob_client 0.34.6 **and** py_clob_client_v2 1.0.0 (legacy + current SDK both present),
    py_order_utils 0.3.2

systemd (verified 2026-05-08):
- `zspd-hot-refresh.timer` (enabled, active) — fires at 17:30 UTC daily, `Persistent=true`, `RandomizedDelaySec=60`
- `zspd-hot-refresh.service` (disabled, oneshot, `TimeoutStartSec=600`)
- `zspd-strategy.service` (`static` — no `[Install]` section — **inactive**) — manual `systemctl start` after wallet funded
  - Default ExecStart: `modify_str.py --backend direct --version v5 --size 0.5 --max-per-slot 1 --min-edge 0.08`
  - Has `After=zspd-hot-refresh.service` so unit ordering is correct if both started together
- `clob-proxy.service` (enabled, active/running on 127.0.0.1:8088) — kept active for fallback

Swap: `/swapfile` 2 GB (priority -2), in `/etc/fstab` so survives reboot.

## Wallet status

The production wallet is the **pre-existing Polymarket "DongYu." account**
(Magic.link Email login, `cousinxu@gmail.com`). It came pre-funded and
pre-approved for trading.

```
EOA (signer):       0x398112D299d60B31e2e2Bd736232c4caD07E6a1a
Proxy (funder):     0x65917b5e41485B8c62814344184aC70c05CbdA58
Signature type:     1 (POLY_PROXY)
USDC balance:       $2,021.23  (verify live with `curl /balance`, see below)
Allowance:          ∞ (max uint256, set by Polymarket UI)
```

`.env` is in sync across:
- local Mac: `<repo>/.env`
- valkyrie: `/opt/zspd-trader/.env`
- valkyrie: `/opt/clob-proxy/.env`

Verify on valkyrie any time:
```bash
ssh valkyrie 'curl -s -H "Authorization: Bearer $(grep PROXY_AUTH_TOKEN /opt/clob-proxy/.env | cut -d= -f2)" http://127.0.0.1:8088/balance'
```

> **Stable wallet — do not rotate routinely.** Rotating after funds are deposited
> abandons those funds. `scripts/rotate_wallet.py` is for leak-emergency only.
