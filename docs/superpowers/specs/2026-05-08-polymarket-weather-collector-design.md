# Polymarket Weather Collector & Dashboard — Design

**Status**: draft, pending implementation
**Date**: 2026-05-08
**Owner**: chivier
**Scope**: a singapore-hosted "看盘 + 数据存档" tool covering all Polymarket weather contracts, plus the supporting ops migration (polymarket user, code-push script).

## 1. Goal

Build a long-running market monitor and archive on `singapore` that:

- Discovers and tracks **every** Polymarket weather contract (not just ZSPD).
- Records prices at **3-second** granularity, plus L2 orderbook every 5 min and trades in real time.
- Provides an operator-facing **read-only dashboard** with four views: market grid, single-market drill-down, ZSPD strategy ops panel, and historical replay.
- Tiers data into **hot** (singapore, 30 d), **warm** (singapore parquet, 7 d), and **cold** (Saber WebDAV, indefinite).
- Migrates singapore + valkyrie to a non-root `polymarket` service user with scoped sudo.
- Adds a `push_code.sh` deploy helper for future code rollouts.

Trading itself is unchanged; this design touches the data + observability + ops layer only.

## 2. Architecture

```
┌────────────── singapore (217.15.166.15) ────────────────┐
│                                                          │
│  Polymarket WS  ──► zspd-collector.service              │
│  gamma  REST    ──►   discovery / WS subscriber          │
│  /book  REST    ──►   L2 5-min poller, trades stream     │
│                       in-mem latest[token_id]            │
│                       /metrics /snapshot /health (8091)  │
│                              │                           │
│                              ▼ (scrape 3s)               │
│                  victoria-metrics.service (8428)         │
│                              │                           │
│                              ▼                           │
│                  grafana-server.service (3000)           │
│                              │ iframe + REST             │
│                              ▼                           │
│                  zspd-dashboard.service (8090)           │
│                     /          grid (A)                  │
│                     /m/{id}    drill-down (B)            │
│                     /strategy  ZSPD ops (C)              │
│                     /replay    historical (D)            │
│                                                          │
│  zspd-archiver.timer  03:00 UTC daily                    │
│      VM + SQLite → parquet → rclone → WebDAV             │
│      cleanup VM>30d / SQLite>30d / warm parquet>7d       │
└──────────────────────────────────────────────────────────┘
                              │ rclone (WebDAV)
                              ▼
                https://driver.saber-ai.org/Local/Polymarket/
                    YYYY/MM/DD/{prices,orderbook,trades,markets}.parquet
```

**Bind addresses** — all `127.0.0.1` only; access via SSH tunnel.

| Port | Service | Purpose |
|---|---|---|
| 8090 | zspd-dashboard | FastAPI UI |
| 8091 | zspd-collector | `/metrics` (VM scrape), `/snapshot` (dashboard), `/health` |
| 8428 | victoria-metrics | TSDB + PromQL |
| 3000 | grafana-server | time-series charts |

**Deploy paths** (singapore):

- `/opt/zspd-collector/` — new (code, .venv, warm/)
- `/opt/zspd-dashboard/` — existing, rewritten from skeleton
- `/var/lib/victoria-metrics/` — TSDB data
- `/var/lib/grafana/` — Grafana state
- `/opt/zspd-collector/warm/YYYY-MM-DD/*.parquet` — 7-day warm tier

## 3. Components

### 3.1 zspd-collector.service (singapore, new)

Single Python asyncio process. Source: `collector/` package in main repo.

| Coroutine | Cadence | Job |
|---|---|---|
| `discovery_loop` | 24 h | `GET gamma /events?tag=weather` + slug keyword scan; upsert `markets` table; push token_id set to `ws_loop` |
| `ws_loop` | continuous | Maintain `wss://ws-subscriptions-clob.polymarket.com/ws/`; subscribe to all token_ids; on `book`/`price_change`/`last_trade_price` update `latest[token_id]`; on trades append to `trades.db` |
| `tick_emitter` | passive | `/metrics` HTTP returns current `latest[]` snapshot; VM scrapes every 3 s |
| `l2_loop` | 5 min | `GET clob /book?token_id=...` per active market → `orderbook.db` |
| `health_loop` | 30 s | Heartbeat to `/var/log/zspd-collector/health.log` and `pm_ws_*` metrics |

**Resilience** — WS reconnect with exponential backoff (1→2→4…cap 60 s); SIGTERM flush + clean exit; internal HTTP bound to `127.0.0.1:8091` only.

### 3.2 zspd-dashboard.service (singapore, extend existing)

FastAPI + Jinja2 + htmx. Source: `dashboard/` package in main repo. Bearer-token auth (`DASHBOARD_AUTH_TOKEN` from `.env`).

| Route | View | Data source |
|---|---|---|
| `GET /` | A — market grid; sortable by `\|edge\|`; filter `?city=`, `?contract_type=`; htmx 10-s refresh | `collector:8091/snapshot` + on-server ZSPD model inference (v5 production, ZSPD markets only; non-ZSPD rows show `edge=—`) |
| `GET /m/{market_id}` | B — drill-down: Grafana iframe (today's tick), latest L2 top, last 50 trades, model 7-quantile + edge | VM REST + `orderbook.db` + `trades.db` |
| `GET /strategy` | C — ZSPD ops: valkyrie service status, positions, today's PnL, expected-vs-actual orders | SSH `valkyrie` (singapore→valkyrie is allowed direction) running `scripts/inspect_strategy.py` (authored as part of this spec); cached 30 s |
| `GET /replay/{date}` | D — replay; overlays obs temp, model preds, YES price, backtest virtual orders | warm parquet (or pull from cold WebDAV on demand) + `intraday_features.parquet` |
| `GET /api/*` | JSON | internal |
| `GET /health` | unauth | liveness |

### 3.3 zspd-archiver.timer + .service (singapore, new)

Daily 03:00 UTC. Oneshot:

```
1. VM:  /api/v1/export/native?match=...&start=Y00:00&end=Y23:59 → parquet
2. SQL: SELECT ... yesterday → parquet (orderbook, trades, markets)
3. rclone copy warm/YYYY-MM-DD/  webdav:Local/Polymarket/YYYY/MM/DD/  --checksum
4. verify cold size + checksum
5. cleanup: VM blocks >30 d, SQLite rows >30 d, warm parquet >7 d
6. exit 0 on success; on failure → systemd marks failed → archiver-restart.timer 6 h
```

A separate `restore.sh date_from date_to [target_dir]` pulls a date range from cold WebDAV back to a working dir for ad-hoc analysis.

### 3.4 victoria-metrics + grafana (singapore, new)

- VM: official `.deb`, single binary `/usr/local/bin/victoria-metrics`, `-retentionPeriod=30d`, scrape config `127.0.0.1:8091/metrics` interval `3s`.
- Grafana: official `.deb`, dashboards committed under `dashboards/grafana/` in repo (provisioned via files), datasource = VM.

### 3.5 push_code.sh (local Mac, new)

`scripts/push_code.sh val|singapore|both [--dry-run] [--restart]`:

- rsync to `/opt/zspd-trader/src/` (val) and `/opt/zspd-{trainer,collector,dashboard}/src/` (singapore).
- Excludes: `.env`, `.venv/`, `__pycache__/`, `database/`, `ml/artifacts/`, `.git/`, `*.pyc`, `docs/superpowers/`.
- Includes: `core/`, `ml/`, `strategies/`, `collector/`, `dashboard/`, `scripts/`, `requirements.txt`, `pyproject.toml` if present.
- SSH alias `val-pm` / `sg-pm` (polymarket user, key `~/.ssh/polymarket_ed25519`).
- `--restart` triggers `systemctl restart` of impacted units after push.

## 4. Data schema

### SQLite (singapore)

```sql
-- markets.db (new; do not reuse token.db, which stays ZSPD-only)
CREATE TABLE markets (
    market_id      TEXT PRIMARY KEY,        -- gamma `id`
    slug           TEXT UNIQUE NOT NULL,
    question       TEXT,
    end_date       TEXT,                    -- ISO8601 UTC
    tag            TEXT,                    -- 'weather'
    city           TEXT,                    -- ZSPD/KJFK/KLGA/KMIA/...
    contract_type  TEXT,                    -- 'high_eq'|'high_ge'|'high_le'|'low_*'|'rain_yes'|...
    threshold_c    REAL,
    threshold_text TEXT,
    token_id_yes   TEXT,
    token_id_no    TEXT,
    first_seen     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen      TIMESTAMP,
    archived       INTEGER DEFAULT 0
);
CREATE INDEX idx_markets_end_date ON markets(end_date);
CREATE INDEX idx_markets_city     ON markets(city);

-- orderbook.db
CREATE TABLE l2_snapshot (
    ts         TIMESTAMP NOT NULL,
    market_id  TEXT NOT NULL,
    side       TEXT CHECK(side IN ('yes','no')),
    levels     TEXT NOT NULL,                -- JSON [[price,size], ...] top10
    PRIMARY KEY(ts, market_id, side)
);

-- trades.db
CREATE TABLE trades (
    ts          TIMESTAMP NOT NULL,
    market_id   TEXT NOT NULL,
    side        TEXT,
    price       REAL,
    size        REAL,
    taker_side  TEXT
);
CREATE INDEX idx_trades_market_ts ON trades(market_id, ts);
```

### VictoriaMetrics

```
pm_yes_mid{market_id, city, contract_type}     gauge
pm_yes_bid / pm_yes_ask                        gauge
pm_no_mid  / pm_no_bid / pm_no_ask             gauge
pm_spread                                      gauge  = yes_ask - yes_bid
pm_volume_24h                                  gauge  refreshed via gamma /markets every 30 min
pm_ws_connected                                gauge  0|1
pm_ws_stale_seconds                            gauge
pm_skipped_markets                             counter
```

Cardinality budget: ~150 markets × 8 metrics ≈ 1200 series.

### Cold parquet (per day, in WebDAV `YYYY/MM/DD/`)

- `prices.parquet` — long format: `ts, metric, market_id, city, contract_type, value`
- `orderbook.parquet` — `ts, market_id, side, levels (JSON string)`
- `trades.parquet` — `ts, market_id, side, price, size, taker_side`
- `markets.parquet` — full row snapshot of `markets` table at end of day

## 5. Failure modes

| Failure | Behavior | Auto-recover? |
|---|---|---|
| WS disconnect | exponential backoff reconnect; `latest[]` holds last value; VM writes flat curve | yes |
| WS disconnect > 5 min | ERROR log + `pm_ws_stale_seconds` exposed; Grafana red banner; no paging | yes |
| collector crash | systemd `Restart=on-failure RestartSec=10`; lose ≤10 s WS state | yes |
| VM down | collector keeps serving `/metrics`; gap visible as missing samples; resumes on VM restart | yes |
| WebDAV upload fail | archiver `exit ≠ 0` → failed state; warm parquet retained; `archiver-restart.timer` retries 6 h later | yes |
| disk < 10 % free | Grafana alert; archiver refuses to delete VM blocks unless cold checksum verified | partial — operator |
| Polymarket rate-limit | per-endpoint backoff; discovery has 24 h slack | yes |
| Malformed gamma metadata | log warning, skip; counter `pm_skipped_markets` | yes |
| /strategy can't reach valkyrie | banner "valkyrie unreachable"; cached state ≤5 min then greys out | yes |

## 6. Polymarket service-user migration (C2)

Decisions taken:
- **A2** — sudo: `NOPASSWD` only for `systemctl`/`journalctl` and scripts under `/opt/zspd-*`; password required for everything else.
- **B2** — separate SSH key `~/.ssh/polymarket_ed25519` (registered to tatapai by user; treated as a temporary dev key).
- **C2** — migrate **all** services (`zspd-strategy`, `zspd-archive`, `zspd-hot-refresh`, `clob-proxy`, plus the new collector/dashboard/archiver) to `User=polymarket`.
- **D1** — `/opt/zspd-*` chowned `polymarket:polymarket`; root retains write via being root.

Files to migrate ownership of:
- `/opt/zspd-trader/` and `.env` (valkyrie)
- `/opt/clob-proxy/` and `.env` (valkyrie)
- `/opt/zspd-trainer/` (singapore)
- `/opt/zspd-dashboard/` (singapore; existing skeleton)
- `/opt/zspd-collector/` (singapore; new)
- The dedicated singapore→valkyrie deploy key currently at `/root/.ssh/zspd_deploy` → moved to `~polymarket/.ssh/zspd_deploy`; `~polymarket/.ssh` mode 700.

`/etc/sudoers.d/polymarket-zspd` content (visudo-validated):

```
polymarket ALL=(root) NOPASSWD: /bin/systemctl, /bin/journalctl, /opt/zspd-collector/bin/*, /opt/zspd-dashboard/bin/*, /opt/zspd-trader/bin/*, /opt/zspd-trainer/bin/*
```

## 7. Deployment runbook (staged, each step verifiable + rollback-able)

1. **Local**: generate keypair `ssh-keygen -t ed25519 -f ~/.ssh/polymarket_ed25519 -C polymarket-dev`; add `Host val-pm` / `Host sg-pm` to `~/.ssh/config`.
2. **Each server (as root)**: `useradd -m -s /bin/bash polymarket`; install pubkey to `~polymarket/.ssh/authorized_keys`; `visudo -f /etc/sudoers.d/polymarket-zspd`; verify `ssh val-pm 'whoami'` and `ssh sg-pm 'whoami'` both print `polymarket`.
3. **Migrate ownership** (one server at a time; pick a window outside the unit's timer):
   - singapore (avoid 16:30 UTC archive window): stop `zspd-archive.timer` and `zspd-dashboard.service`; `chown -R polymarket:polymarket /opt/zspd-{trainer,dashboard}` and their `.env`; move `/root/.ssh/zspd_deploy*` to `~polymarket/.ssh/`; edit unit files `User=polymarket Group=polymarket`; `daemon-reload`; start; trigger `zspd-archive.service` once and verify the rsync to valkyrie still works.
   - valkyrie (avoid 17:30 UTC hot-refresh): stop `zspd-hot-refresh.timer`, `clob-proxy.service`; chown `/opt/{clob-proxy,zspd-trader}` + `.env`; edit units; daemon-reload; start; verify with `curl -H "Authorization: Bearer $PROXY_AUTH_TOKEN" 127.0.0.1:8088/balance` and `journalctl -u clob-proxy -n 50` clean.
   - `zspd-strategy.service` is `static`/inactive — just edit `User=polymarket`, `daemon-reload`; do NOT start (operator-gated).
4. **Local**: write `scripts/push_code.sh`; `--dry-run` first to confirm exclude/include set; then push to both.
5. **Singapore**: install `victoria-metrics-prod` and `grafana` via official `.deb`; provision VM scrape config (`127.0.0.1:8091`, 3 s); provision Grafana datasource + committed dashboards from `dashboards/grafana/`; `systemctl enable --now victoria-metrics grafana-server`.
6. **Singapore**: `python -m venv /opt/zspd-collector/.venv`; install `collector/requirements.txt`; install collector + dashboard + archiver units; `systemctl enable --now zspd-collector zspd-dashboard zspd-archiver.timer`.
7. **Smoke test** (all 7 items in §8) within 10 min of bringing services up.
8. **Soak**: observe for 24 h. Check WebDAV got first-day parquet, disk usage stable, no Grafana red banners. Sign off.

Rollback hooks at each step: §3 keeps a root-owned snapshot of `/opt/zspd-{trader,trainer,dashboard,clob-proxy}` before chown; §5–6 are new services so `systemctl stop` + `disable` is full rollback; any §7 failure stops the runbook before §8 sign-off.

## 8. Verification checklist

| # | Test | Expected |
|---|---|---|
| 1 | Discovery sanity | `sqlite3 markets.db 'SELECT city, count(*) FROM markets GROUP BY city'` shows ≥5 cities within 24 h |
| 2 | WS plumbing | `health.log` shows `ws_connected=true` heartbeats every 30 s within 30 s of start |
| 3 | VM ingestion | `curl 127.0.0.1:8428/api/v1/query?query=pm_yes_mid` returns active-market points within 60 s of fresh start |
| 4 | Dashboard E2E | SSH-tunnel + open `/` → grid populated; click market → Grafana iframe loads ≥1 h of tick data |
| 5 | Archiver E2E | manual `systemctl start zspd-archiver` → 4 parquet files in warm dir → `rclone ls webdav:Local/Polymarket/...` shows them remote → cleanup ran |
| 6 | Restore path | `restore.sh 2026-04-01 2026-04-07` pulls 7 days into temp dir; byte-identical to warm copy |
| 7 | push_code | `scripts/push_code.sh both --dry-run` lists expected diffs; without `--dry-run` deploys; `ssh val-pm cat .../modify_str.py | head -1` matches local |

## 9. Out of scope (for this spec)

- Adding new ML models for non-ZSPD cities. Discovery + archive happens regardless; modeling per city is a follow-on.
- Live trading on non-ZSPD markets.
- Public-facing dashboard. All UIs stay on `127.0.0.1` + SSH tunnel.
- Paging / on-call alerting integration. Grafana banners only for v1.
- Migrating off SQLite for orderbook/trades. Re-evaluate at 1 yr or 100 GB, whichever first.

## 10. Open questions / known unknowns

- **Polymarket WS rate limits** for subscribing to ~150 token IDs at once. Plan: subscribe in batches of 50 if a single subscription fails.
- **gamma's exact tag scheme for weather** — relies on `tag=weather`, but slug-keyword fallback covers both. Confirm during discovery dry-run.
- **clob-proxy systemd unit `User=polymarket` migration risk** — `.env` ownership change is the only material change; service binds 127.0.0.1:8088 either way. Verify with `curl /balance` post-migration.
- **tatapai key registration** — out of scope; user handles upload of public half.
