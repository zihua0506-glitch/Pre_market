# STATUS

Single-page snapshot of where the system is **right now**. Read this first; the
other docs are reference manuals.

Last updated: 2026-05-08 (collector + dashboard + archiver deployed on singapore; polymarket service-user migration complete on both servers; production wallet still wired and balance verified post-migration).

## TL;DR

```
✅  Two-machine prod topology: singapore (collector + archive + dashboard) + valkyrie (trade)
✅  Daily systemd timers running on both (archive 16:30 UTC, hot-refresh 17:30 UTC, archiver 03:00 UTC)
✅  Production wallet is the pre-existing Polymarket "DongYu." account
✅  $2,021.23 USDC funded (live: 2021.233418, verified post user-migration), all 3 CLOB-spender allowances at max uint256
✅  CLOB API auth verified end-to-end (local Mac → valkyrie → Polymarket)
✅  All systemd units on both servers now run as `polymarket` (NOPASSWD systemctl/journalctl only)
✅  zspd-collector.service on singapore: WS subscribed to ~2700 tokens across ~1360 daily-temperature markets (3s tick → VictoriaMetrics, 30 d retention)
✅  victoriametrics.service + grafana-server.service on singapore (127.0.0.1:8428 / 127.0.0.1:3000)
✅  zspd-dashboard.service rewritten — 4 routes (grid / market / strategy / replay) on 127.0.0.1:8090, bearer auth
✅  zspd-archiver.timer + restart-timer deployed; round-trip parquet → WebDAV → restore.sh verified (md5 match)
✅  Cold archive: driver.saber-ai.org/dav/Local/Polymarket/ via rclone (saber: remote)
✅  Codebase: pytest tests/   →   97 + 39 new = 136 passed
🟡  zspd-strategy.service on valkyrie is `static`, NOT STARTED — operator-gated
🟡  Sizing tier not chosen yet (default unit file is Starter: size 0.5, max-per-slot 1, min-edge 0.08)
```

When you start the strategy, real money trades will be placed. See "Going live"
at the bottom.

---

## Wallet (production)

```
EOA (signer)        0x398112D299d60B31e2e2Bd736232c4caD07E6a1a
Proxy (funder)      0x65917b5e41485B8c62814344184aC70c05CbdA58
Polymarket account  username "DongYu." / cousinxu@gmail.com
                    (created via Magic.link Email login)
SDK signature_type  1 (POLY_PROXY)
USDC balance        $2,021.23 (live: 2021.233418, verified via clob-proxy /balance)
Allowances          ∞ (max uint256) for 3 CLOB spenders:
                      0xE111180000d2663C0091e4f400237545B87B996B
                      0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296
                      0xe2222d279d744050d28e00520010520000310F59
Private key         in .env (POLY_PRIVATE_KEY=0x8fc2…040d, sha256 fb900af1…7a2b)
                    Same key in /opt/zspd-trader/.env and /opt/clob-proxy/.env on valkyrie (verified equal)
```

Three machines hold this `.env` (all with `chmod 600`):
- local Mac: `/Users/chivier/.../Pre_market/.env`
- valkyrie: `/opt/zspd-trader/.env`
- valkyrie: `/opt/clob-proxy/.env`

**Stable wallet** — do not rotate routinely. `scripts/rotate_wallet.py` is
emergency-only (private key leak); rotating after the wallet is funded
abandons the funds.

---

## Servers

### `valkyrie` — polymarket@204.168.241.66 (🇫🇮 FI Hetzner)
| Aspect | Status |
|---|---|
| Polymarket geo | NOT blocked (POST /order works) |
| Resources | 2 cores, 3.7 GB RAM + 2 GB swap (`/swapfile`, in fstab), 38 GB disk (5.2G used = 15%) |
| Service user | `polymarket` (uid 1000), all `/opt/zspd-*` + `/opt/clob-proxy` chowned; sudo NOPASSWD scoped to systemctl/journalctl only |
| Trading wallet `.env` | ✅ in sync with Mac (POLY_PRIVATE_KEY + creds + FUNDER + SIG_TYPE=1, FUNDER=0x65917b…dA58, CHAIN=137); now polymarket:polymarket 600 |
| `clob-proxy.service` | ✅ enabled, active (running) **as polymarket** on 127.0.0.1:8088 (balance verified $2021.23 post-migration) |
| `zspd-hot-refresh.timer` | ✅ enabled, active. Next fire: 2026-05-08 17:30 UTC (daily). Runs as polymarket. |
| `zspd-strategy.service` | 🟡 **static (no [Install]), NOT started** — User=polymarket already configured. `ssh -F ssh/config val-pm 'sudo systemctl start zspd-strategy'` to go live. |
| Models @ `src/ml/artifacts/v5/` | freshly trained 2026-05-08 03:23–03:29 UTC (8 .pkl + eval_metrics + cv_meta + feature_cols, ~45 MB) |
| SSH access from local Mac | `Host val-pm` in `ssh/config` (project-local), key `ssh/polymarket_ed25519` |

### `singapore` — polymarket@217.15.166.15 (🇸🇬 SG Contabo)
| Aspect | Status |
|---|---|
| Polymarket geo | BLOCKED for POST /order; GET endpoints OK |
| Resources | 6 cores, 11 GB RAM, 193 GB disk (7.6G used = 4%); no swap |
| Role | data archive + read-only dashboard + **all-cities collector**. **NO training.** |
| Service user | `polymarket` (uid 1000), all `/opt/zspd-*` chowned; sudo NOPASSWD scoped to systemctl/journalctl only |
| `zspd-archive.timer` | ✅ enabled, active (as polymarket). Next fire 16:30 UTC daily. archive.sh patched to use `/home/polymarket/.ssh/zspd_deploy`. |
| `zspd-collector.service` | ✅ enabled, active (NEW). asyncio orchestrator: gamma discovery (24 h) + WS subscriber (3 s ticks) + L2 poller (5 min) + Prom `/metrics` on 127.0.0.1:8091. ~1360 markets across 30+ cities. |
| `zspd-dashboard.service` | ✅ enabled, active (REWRITTEN). FastAPI on 127.0.0.1:8090 with 4 routes (grid / market / strategy / replay). Bearer auth disabled — sits behind NPM Basic Auth (see below). |
| `zspd-dashboard-tunnel.service` | ✅ enabled, active. autossh `singapore→artoria:172.20.0.1:8190` (key `~polymarket/.ssh/artoria_tunnel`, restricted to port-forwarding only). |
| Public URL | **https://dashboard.tatapai.site** (NPM proxy host id=42 on artoria, Let's Encrypt cert id=7 expires 2026-08-06, HTTP→HTTPS redirect). Basic Auth user `chivier`. Password in NPM access list id=1 + recorded in `/opt/zspd-dashboard/.env` comments. |
| `zspd-archiver.timer` | ✅ enabled, active (NEW). 03:00 UTC daily oneshot: VM + SQLite → parquet → rclone → WebDAV → cleanup (hot 30 d, warm 7 d). |
| `zspd-archiver-restart.timer` | ✅ enabled, active. Retries archiver every 6 h until `/run/zspd-archiver/success` exists. |
| `victoriametrics.service` | ✅ enabled, active (NEW). v1.142.0 single-binary, 127.0.0.1:8428, retention 30 d, scrapes collector at 3 s. |
| `grafana-server.service` | ✅ enabled, active (NEW). v13.0.1, 127.0.0.1:3000, datasource = VictoriaMetrics, dashboard `ZSPD/ZSPD Overview` provisioned. |
| Cold archive | rclone `saber:` → `https://driver.saber-ai.org/dav/Local/Polymarket/` (round-trip md5 verified). End-to-end smoke today: 423k price rows + 1360 markets snapshot uploaded. |
| Old `zspd-retrain.timer/.service` | ❌ removed (verified absent from `/etc/systemd/system/`) |
| Snapshots @ `/opt/zspd-trainer/snapshots/` | 1 day so far (`2026-05-08/`); will roll out to 14 |
| Legacy artifacts @ `src/ml/artifacts/` | v0/v2/v3/v4/v5/ + `_history/20260508-v5/` (frozen, not used by valkyrie) |
| `retrain.sh.disabled` | present at `/opt/zspd-trainer/retrain.sh.disabled` (kept for reference, not invoked) |
| SSH deploy key | `/root/.ssh/zspd_deploy` ed25519, fp `SHA256:808lkjRZNhED2Qv7UA2EzcfKnZ9vzBhRtavuBBhuhvA zspd-deploy@singapore` |
| singapore→valkyrie SSH | ✅ verified (`echo ok-from-singapore` succeeded under BatchMode) |
| Disk usage `/opt/zspd-trainer` | 1016 MB; `/opt/zspd-dashboard` 44 MB |

### Local Mac — 🇬🇧 UK (Jisc)
| Aspect | Status |
|---|---|
| Polymarket geo | BLOCKED for POST /order |
| Role | dev only — backtest, dry-run, edits, ops via SSH |
| `.env` | ✅ in sync with valkyrie |

---

## Daily lifecycle (CST = UTC+8)

```
00:30 CST  16:30 UTC   singapore  archive.timer fires       (~5 min)
                         → pipeline update + labels + features
                         → rsync daily_labels.db → valkyrie
                         → 14-day snapshot rolled

01:30 CST  17:30 UTC   valkyrie   hot-refresh.timer fires   (~3 min)
                         → today's WU + GFS + Polymarket meta refreshed

(any time, off-host)   local/GPU  optional retrain
                         → scripts/pull_training_data.sh
                         → python -m ml.pipeline train --version v5
                         → scripts/push_artifacts.sh

08:00 → 23:59 CST      valkyrie   strategy main loop (when started)
                         → wakes HH:01 / HH:31 each half-hour slot
                         → tier-1 certainty signals + tier-2 model edge
                         → modify_str.py via direct CLOB API
```

---

## Going live (when you decide)

```bash
# 1. (optional) Tune sizing in the systemd unit if you don't want the
#    default $0.5/signal starter sizing — see "Sizing options" below
ssh valkyrie 'systemctl edit zspd-strategy'

# 2. Start
ssh valkyrie 'systemctl start zspd-strategy'

# 3. Watch
ssh valkyrie 'journalctl -u zspd-strategy -f'

# 4. Stop immediately (panic button)
ssh valkyrie 'systemctl stop zspd-strategy'
```

### Sizing options (given $2,021 in funder)

The systemd unit currently runs:

```
modify_str.py --backend direct --version v5 \
              --size 0.5 --max-per-slot 1 --min-edge 0.08
```

| Tier | Sizing | Daily exposure (max) | Comment |
|---|---|---|---|
| Starter (default) | size 0.5, max-per-slot 1, min-edge 0.08 | ~$16 | Conservative — for first week to verify behaviour |
| Normal | size 1.0, max-per-slot 2, min-edge 0.05 | ~$64 | Doubles per-trade, looser edge filter |
| Aggressive | size 2.0, max-per-slot 3, min-edge 0.05 | ~$192 | ~10% of capital cycling daily |

(Backtest in CLAUDE.md was at base $1; PnL scales linearly with size, but so
does drawdown. v5 worst-day backtest drawdown was ~14% of cycled exposure.)

Override sizing without editing source:
```bash
ssh valkyrie 'systemctl edit zspd-strategy'
# add a [Service] block with ExecStart= override, then:
ssh valkyrie 'systemctl daemon-reload && systemctl restart zspd-strategy'
```

---

## Known stale / deprecated items (kept for archaeology)

- `scripts/deploy_proxy.py` — written to deploy a Polymarket Proxy on a fresh EOA. Not used in current production because we adopted the pre-existing Magic.link account. Keep as backup if we ever rotate.
- `scripts/rotate_wallet.py` — emergency-only.
- `singapore:/opt/zspd-trainer/retrain.sh.disabled` — old retrain entrypoint, not invoked. Kept for reference.
- `singapore:/opt/zspd-trainer/.venv` still has scikit-learn / scipy. They were used by the old retrain pipeline. Unused now; safe to leave; prune later if disk pressure.
- `singapore:/opt/zspd-trainer/ml/artifacts/` — last legacy snapshot from singapore-side training. Never refreshed since the role change. Production artifacts on valkyrie come from off-host training.
- `ArdenCalloway` Polymarket account (EOA `0x9f2F9b…6b48`) — abandoned. Empty, no on-chain history. Can be ignored.

---

## Open decisions / next actions

1. **Pick a sizing tier** before flipping the strategy on (default is Starter — fine for week 1).
2. **Decide when to start** — tonight, or wait until 01:30 CST after the next archive + hot-refresh cycle finishes.
3. **First retrain on a non-singapore host** — once you have a GPU box (or just run on Mac), exercise `pull_training_data.sh` + `push_artifacts.sh` end-to-end so the off-host retrain path is proven.
4. **Dashboard UI** — singapore's `zspd-dashboard` is a skeleton. Build out `/api/positions`, `/api/pnl`, `/api/strategy/recent-signals` as you find what you actually want to monitor.

---

## Code layout (post-refactor)

```
common/                 env loader + dataclasses
strategy_math/          pure math (shared by backtest + live; was hidden in backtest.py)
clients/data/           data acquisition + persistence (was core/)
clients/trading/        order execution (TradingBackend ABC + Direct/Proxy/DryRun + FastAPI server)
ops/                    wallet / preflight / proxy-deploy CLIs
strategies/             thin orchestrators (modify_str, plain_as)
ml/                     training + backtest engine
scripts/                shell helpers + 5-line shims to ops/
tests/                  pytest, 97 offline tests
```

See [`../CLAUDE.md`](../CLAUDE.md) "Package layout" for the file-by-file map.

## Pointers to the rest of the docs

- [`README.md`](README.md) — index of all docs by audience
- [`deployment.md`](deployment.md) — full topology reference, systemd units, runbooks
- [`operations.md`](operations.md) — daily ops cheat sheet, troubleshooting table, training-elsewhere flow
- [`overview.md`](overview.md), [`weather_strategies.md`](weather_strategies.md) — research / strategy taxonomy
- [`../CLAUDE.md`](../CLAUDE.md) — code-agent guidance (architecture, conventions, version metrics)
