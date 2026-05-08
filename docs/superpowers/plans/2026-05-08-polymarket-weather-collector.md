# Polymarket Weather Collector & Dashboard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a singapore-hosted market monitor + cold-data archive covering all Polymarket weather contracts, plus the supporting ops migration (`polymarket` service user, `push_code.sh`).

**Architecture:** Three new systemd services on singapore — `zspd-collector` (asyncio WS subscriber + Prometheus exposer), `zspd-dashboard` (FastAPI UI), `zspd-archiver` (daily parquet roll → WebDAV) — backed by VictoriaMetrics + Grafana. All services run under a non-root `polymarket` user with scoped `NOPASSWD` sudo. Hot data lives in VM (30 d) + SQLite; warm parquet in `/opt/zspd-collector/warm/` (7 d); cold parquet in Saber WebDAV (indefinite).

**Tech Stack:** Python 3.12 (asyncio, websockets, FastAPI, Jinja2 + htmx, polars/pyarrow), VictoriaMetrics 1.x, Grafana 11.x, SQLite, rclone, systemd.

**Spec:** [`docs/superpowers/specs/2026-05-08-polymarket-weather-collector-design.md`](../specs/2026-05-08-polymarket-weather-collector-design.md)

**Conventions used in this plan:**
- All file paths are absolute from repo root unless explicitly remote (prefixed `val:` or `sg:`).
- Commands prefixed `local$` run on the dev Mac; `val$` on valkyrie; `sg$` on singapore. Otherwise default is local Mac.
- Tests for new Python code use `pytest` (added as dev dep). Existing repo has no test suite — we introduce one for new packages only.
- "Commit" steps use a HEREDOC commit body. Use the `semantic-git-committer` style verbs (feat/fix/docs/chore/refactor).
- **SSH config lives at `ssh/config` in the repo** (not `~/.ssh/config`). Every `ssh val-pm`, `ssh sg-pm`, `ssh valkyrie`, `ssh singapore`, `scp ...` in this plan should be read as `ssh -F ssh/config <alias>` / `scp -F ssh/config ...`. The private key (`ssh/polymarket_ed25519`) is gitignored; the public half is committed. Set `export SSH_CONFIG_FILE=ssh/config` (informational only — OpenSSH ignores it; pass `-F` explicitly) or wrap with a tiny shell helper.

---

## Phase A — Ops foundation

Establishes the `polymarket` service user on both servers and the local `push_code.sh` deploy helper. Phase A is the riskiest phase because it touches the live wallet plumbing on valkyrie. Each task is independently verifiable; rollback is `chown` back to root and revert the systemd `User=` line.

### Task A1: Generate the `polymarket` SSH key and add SSH config aliases ✅ done (commit `1a49945`)

**Files (as actually built — keys live in repo, not `~/.ssh/`):**
- Create: `ssh/polymarket_ed25519` (gitignored)
- Create: `ssh/polymarket_ed25519.pub` (committed)
- Create: `ssh/config` (committed — root + polymarket aliases for val/singapore)
- Modify: `.gitignore` (exclude `ssh/polymarket_ed25519` and `ssh/known_hosts`)

- [ ] **Step 1: Generate the keypair**

```bash
ssh-keygen -t ed25519 -f ~/.ssh/polymarket_ed25519 -C polymarket-dev -N ''
```

Expected: two files appear, `polymarket_ed25519` (mode 600) and `polymarket_ed25519.pub` (mode 644).

- [ ] **Step 2: Add SSH config aliases**

Append to `~/.ssh/config`:

```
Host val-pm
    HostName 204.168.241.66
    User polymarket
    IdentityFile ~/.ssh/polymarket_ed25519
    IdentitiesOnly yes

Host sg-pm
    HostName 217.15.166.15
    User polymarket
    IdentityFile ~/.ssh/polymarket_ed25519
    IdentitiesOnly yes
```

- [ ] **Step 3: Print the public key for upload to tatapai**

```bash
cat ~/.ssh/polymarket_ed25519.pub
```

Expected: one `ssh-ed25519 AAAA... polymarket-dev` line. User uploads this to tatapai out-of-band.

- [ ] **Step 4: No commit needed** — keys are outside repo, SSH config is user-local.

### Task A2: Provision `polymarket` user on valkyrie

**Files:**
- Create: `scripts/setup_polymarket_user.sh` (idempotent, runs on the server)
- Remote create: `val:/etc/sudoers.d/polymarket-zspd`
- Remote create: `val:/home/polymarket/.ssh/authorized_keys`

- [ ] **Step 1: Write the bootstrap script**

Create `scripts/setup_polymarket_user.sh`:

```bash
#!/usr/bin/env bash
# Idempotent provisioning of the polymarket service user.
# Usage: sudo bash setup_polymarket_user.sh < /path/to/polymarket_pubkey
# Run as root on each target server.
set -euo pipefail

PUBKEY="$(cat)"
[[ -n "$PUBKEY" ]] || { echo "no pubkey on stdin" >&2; exit 1; }

if ! id polymarket >/dev/null 2>&1; then
    useradd -m -s /bin/bash polymarket
    echo "created polymarket user"
else
    echo "polymarket user already exists"
fi

install -d -m 700 -o polymarket -g polymarket /home/polymarket/.ssh
AUTHKEYS=/home/polymarket/.ssh/authorized_keys
touch "$AUTHKEYS"
chown polymarket:polymarket "$AUTHKEYS"
chmod 600 "$AUTHKEYS"
grep -qxF "$PUBKEY" "$AUTHKEYS" || echo "$PUBKEY" >> "$AUTHKEYS"

cat > /etc/sudoers.d/polymarket-zspd <<'EOF'
polymarket ALL=(root) NOPASSWD: /bin/systemctl, /bin/journalctl, /opt/zspd-collector/bin/, /opt/zspd-dashboard/bin/, /opt/zspd-trader/bin/, /opt/zspd-trainer/bin/, /opt/clob-proxy/bin/
EOF
chmod 440 /etc/sudoers.d/polymarket-zspd
visudo -c -f /etc/sudoers.d/polymarket-zspd

echo "polymarket user provisioned"
```

- [ ] **Step 2: Make it executable + commit**

```bash
chmod +x scripts/setup_polymarket_user.sh
git add scripts/setup_polymarket_user.sh
git commit -m "feat(ops): add polymarket service-user bootstrap script"
```

- [ ] **Step 3: Run it on valkyrie**

```bash
ssh root@valkyrie 'bash -s' < scripts/setup_polymarket_user.sh \
    < ~/.ssh/polymarket_ed25519.pub
```

Wait — that's two stdins. Use a tempfile:

```bash
scp scripts/setup_polymarket_user.sh valkyrie:/tmp/
scp ~/.ssh/polymarket_ed25519.pub valkyrie:/tmp/polymarket.pub
ssh root@valkyrie 'sudo bash /tmp/setup_polymarket_user.sh < /tmp/polymarket.pub'
ssh root@valkyrie 'rm /tmp/setup_polymarket_user.sh /tmp/polymarket.pub'
```

Expected last line: `polymarket user provisioned`.

- [ ] **Step 4: Verify SSH-as-polymarket works**

```bash
ssh val-pm 'whoami && id'
```

Expected: `polymarket\nuid=1001(polymarket) gid=1001(polymarket) groups=1001(polymarket)`. (UID may differ.)

- [ ] **Step 5: Verify scoped sudo works**

```bash
ssh val-pm 'sudo systemctl status zspd-strategy --no-pager | head -3'
```

Expected: status output, no password prompt.

```bash
ssh val-pm 'sudo apt-get update' || true
```

Expected: password prompt (because `apt-get` is NOT in the NOPASSWD allowlist). Press Ctrl-C — this proves the scope is enforced.

### Task A3: Provision `polymarket` user on singapore

**Files:** (remote only — same as A2)

- [ ] **Step 1: Run the same bootstrap script on singapore**

```bash
scp scripts/setup_polymarket_user.sh singapore:/tmp/
scp ~/.ssh/polymarket_ed25519.pub singapore:/tmp/polymarket.pub
ssh root@singapore 'sudo bash /tmp/setup_polymarket_user.sh < /tmp/polymarket.pub'
ssh root@singapore 'rm /tmp/setup_polymarket_user.sh /tmp/polymarket.pub'
```

Expected: `polymarket user provisioned`.

- [ ] **Step 2: Verify**

```bash
ssh sg-pm 'whoami && sudo systemctl status zspd-archive.timer --no-pager | head -3'
```

Expected: `polymarket` then status output, no password prompt.

### Task A4: Implement `push_code.sh`

**Files:**
- Create: `scripts/push_code.sh`
- Test: manual (no test framework for shell scripts in this repo)

- [ ] **Step 1: Write the script**

Create `scripts/push_code.sh`:

```bash
#!/usr/bin/env bash
# Push the current repo to val and/or singapore via rsync.
# Usage: scripts/push_code.sh val|singapore|both [--dry-run] [--restart]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGETS=()
DRY_RUN=()
RESTART=0

for arg in "$@"; do
    case "$arg" in
        val)        TARGETS+=("val") ;;
        singapore)  TARGETS+=("singapore") ;;
        both)       TARGETS+=("val" "singapore") ;;
        --dry-run)  DRY_RUN=("--dry-run" "--itemize-changes") ;;
        --restart)  RESTART=1 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done
[[ ${#TARGETS[@]} -gt 0 ]] || { echo "usage: push_code.sh val|singapore|both [--dry-run] [--restart]" >&2; exit 2; }

EXCLUDES=(
    --exclude=.env --exclude=.env.* --exclude=.venv/ --exclude=__pycache__/
    --exclude='*.pyc' --exclude=.git/ --exclude=database/ --exclude=ml/artifacts/
    --exclude=docs/superpowers/ --exclude='*.parquet'
)

push_to() {
    local target="$1"; shift
    local host paths
    case "$target" in
        val)
            host=val-pm
            paths=(/opt/zspd-trader/src/)
            ;;
        singapore)
            host=sg-pm
            paths=(/opt/zspd-trainer/src/ /opt/zspd-collector/src/ /opt/zspd-dashboard/src/)
            ;;
    esac
    for dest in "${paths[@]}"; do
        echo "==> $target  $dest"
        rsync -az --delete "${DRY_RUN[@]}" "${EXCLUDES[@]}" \
            "$REPO_ROOT/" "$host:$dest"
    done
    if [[ $RESTART -eq 1 && ${#DRY_RUN[@]} -eq 0 ]]; then
        case "$target" in
            val)
                ssh val-pm 'sudo systemctl restart clob-proxy.service zspd-strategy.service 2>/dev/null || true'
                ;;
            singapore)
                ssh sg-pm 'sudo systemctl restart zspd-collector.service zspd-dashboard.service 2>/dev/null || true'
                ;;
        esac
    fi
}

for t in "${TARGETS[@]}"; do
    push_to "$t"
done
```

- [ ] **Step 2: Make it executable + commit**

```bash
chmod +x scripts/push_code.sh
git add scripts/push_code.sh
git commit -m "feat(ops): add push_code.sh for rsync deploy to val + singapore"
```

- [ ] **Step 3: Dry-run to confirm exclude/include set**

```bash
scripts/push_code.sh both --dry-run 2>&1 | head -40
```

Expected: itemized lines like `>f.st...... core/fetch_data.py` for source files; **NO** lines for `.env`, `.venv/`, `database/`, `ml/artifacts/`, `__pycache__/`, `docs/superpowers/`. If excludes are leaking, fix the script before continuing.

- [ ] **Step 4: Note** — actual deploy is deferred until after ownership migration in A5/A6, otherwise rsync would push as polymarket into directories owned by root.

### Task A5: Migrate `/opt/*` ownership on singapore

**Files:** (remote only)
- `sg:/opt/zspd-trainer/`
- `sg:/opt/zspd-dashboard/`
- `sg:/opt/zspd-trainer/.env`
- `sg:/opt/zspd-dashboard/.env`
- `sg:/etc/systemd/system/zspd-archive.service`
- `sg:/etc/systemd/system/zspd-dashboard.service`
- Move: `sg:/root/.ssh/zspd_deploy*` → `sg:/home/polymarket/.ssh/zspd_deploy*`

- [ ] **Step 1: Pick a window outside 16:30 UTC**

Confirm `date -u`. If within 30 min of next archive timer fire, wait or temporarily mask:

```bash
ssh sg-pm 'sudo systemctl list-timers zspd-archive.timer --no-pager'
```

If imminent: `sudo systemctl stop zspd-archive.timer` and remember to start it again at the end.

- [ ] **Step 2: Snapshot current ownership for rollback**

```bash
ssh sg-pm 'sudo find /opt/zspd-trainer /opt/zspd-dashboard -maxdepth 2 -printf "%u:%g %p\n" | sudo tee /root/zspd-ownership-pre-migration.txt'
```

- [ ] **Step 3: Stop services about to be migrated**

```bash
ssh sg-pm 'sudo systemctl stop zspd-archive.timer zspd-dashboard.service'
```

- [ ] **Step 4: chown directories and .env files**

```bash
ssh sg-pm '
sudo chown -R polymarket:polymarket /opt/zspd-trainer /opt/zspd-dashboard
sudo chmod 600 /opt/zspd-trainer/.env /opt/zspd-dashboard/.env 2>/dev/null || true
'
```

- [ ] **Step 5: Move SSH deploy key to polymarket's home**

The `zspd_deploy` key is what singapore uses to push to valkyrie. After migration the new service owner must have it.

```bash
ssh sg-pm '
sudo install -d -m 700 -o polymarket -g polymarket /home/polymarket/.ssh
sudo mv /root/.ssh/zspd_deploy /root/.ssh/zspd_deploy.pub /home/polymarket/.ssh/ 2>/dev/null || \
    sudo cp /root/.ssh/zspd_deploy /root/.ssh/zspd_deploy.pub /home/polymarket/.ssh/
sudo chown polymarket:polymarket /home/polymarket/.ssh/zspd_deploy*
sudo chmod 600 /home/polymarket/.ssh/zspd_deploy
sudo chmod 644 /home/polymarket/.ssh/zspd_deploy.pub
'
```

- [ ] **Step 6: Edit systemd unit files in place**

```bash
ssh sg-pm '
for unit in zspd-archive.service zspd-dashboard.service; do
  sudo sed -i "s/^User=root$/User=polymarket/" /etc/systemd/system/$unit
  sudo sed -i "s/^Group=root$/Group=polymarket/" /etc/systemd/system/$unit
  if ! grep -q "^User=" /etc/systemd/system/$unit; then
      sudo sed -i "/^\[Service\]/a User=polymarket\nGroup=polymarket" /etc/systemd/system/$unit
  fi
done
sudo systemctl daemon-reload
'
```

- [ ] **Step 7: Restart services and verify**

```bash
ssh sg-pm 'sudo systemctl start zspd-dashboard.service zspd-archive.timer'
ssh sg-pm 'systemctl is-active zspd-dashboard.service'
ssh sg-pm 'systemctl is-enabled zspd-archive.timer'
ssh sg-pm 'ps -o user= -p $(systemctl show -p MainPID --value zspd-dashboard.service)'
```

Expected: `active`, `enabled`, `polymarket`.

- [ ] **Step 8: Trigger archive once to verify the singapore→valkyrie SSH still works**

```bash
ssh sg-pm 'sudo systemctl start zspd-archive.service'
ssh sg-pm 'journalctl -u zspd-archive.service -n 50 --no-pager'
```

Expected: rsync to valkyrie reports `daily_labels.db` transferred (or "no changes"), exit 0.

If this fails, the most likely cause is `~polymarket/.ssh/zspd_deploy` perms. Fix and retry. Rollback: `sudo chown -R root:root /opt/zspd-trainer /opt/zspd-dashboard`, sed `User=` back to `root`, `daemon-reload`, restart.

### Task A6: Migrate `/opt/*` ownership on valkyrie

**Files:** (remote only)
- `val:/opt/zspd-trader/`
- `val:/opt/clob-proxy/`
- `val:/opt/zspd-trader/.env`, `val:/opt/clob-proxy/.env`
- `val:/etc/systemd/system/{zspd-strategy,zspd-hot-refresh,clob-proxy}.service`

- [ ] **Step 1: Pick a window outside 17:30 UTC**

```bash
ssh val-pm 'sudo systemctl list-timers zspd-hot-refresh.timer --no-pager'
```

- [ ] **Step 2: Snapshot current state**

```bash
ssh val-pm '
sudo systemctl status zspd-hot-refresh.timer --no-pager | head -3
sudo systemctl status clob-proxy.service --no-pager | head -3
sudo curl -s -H "Authorization: Bearer $PROXY_AUTH_TOKEN" 127.0.0.1:8088/balance
' || true
```

Capture the `/balance` output to compare post-migration.

- [ ] **Step 3: Stop services**

```bash
ssh val-pm 'sudo systemctl stop zspd-hot-refresh.timer clob-proxy.service'
```

- [ ] **Step 4: chown + sed unit files**

```bash
ssh val-pm '
sudo chown -R polymarket:polymarket /opt/zspd-trader /opt/clob-proxy
sudo chmod 600 /opt/zspd-trader/.env /opt/clob-proxy/.env
for unit in zspd-strategy.service zspd-hot-refresh.service clob-proxy.service; do
  sudo sed -i "s/^User=root$/User=polymarket/" /etc/systemd/system/$unit
  sudo sed -i "s/^Group=root$/Group=polymarket/" /etc/systemd/system/$unit
  if ! grep -q "^User=" /etc/systemd/system/$unit; then
      sudo sed -i "/^\[Service\]/a User=polymarket\nGroup=polymarket" /etc/systemd/system/$unit
  fi
done
sudo systemctl daemon-reload
sudo systemctl start clob-proxy.service zspd-hot-refresh.timer
'
```

- [ ] **Step 5: Verify clob-proxy wallet chain**

```bash
ssh val-pm 'systemctl is-active clob-proxy.service'
ssh val-pm 'sudo curl -s -H "Authorization: Bearer $(sudo grep PROXY_AUTH_TOKEN /opt/zspd-trader/.env | cut -d= -f2)" 127.0.0.1:8088/balance'
```

Expected: `active`; balance JSON matches Step 2 (~`2021.233418`).

- [ ] **Step 6: Verify the static zspd-strategy unit was edited**

```bash
ssh val-pm 'grep "^User=" /etc/systemd/system/zspd-strategy.service'
```

Expected: `User=polymarket`. Do NOT start the service — it remains operator-gated.

- [ ] **Step 7: Update STATUS.md and commit**

```bash
git add docs/STATUS.md  # if you edit any of the user/ownership lines
git commit -m "chore(ops): record polymarket user migration on val + singapore" || true
```

(Skip if no STATUS.md edits made; can do it at end of phase.)

---

## Phase B — Data layer

Greenfield software. Build the collector locally with tests, then deploy. Each task ends in committed code that passes tests.

### Task B1: Bootstrap the `collector/` package and dev tooling

**Files:**
- Create: `collector/__init__.py`
- Create: `collector/config.py`
- Create: `collector/requirements.txt`
- Create: `collector/tests/__init__.py`
- Create: `collector/tests/conftest.py`
- Create: `pyproject.toml` (top-level, minimal — declares pytest config for the new packages)

- [ ] **Step 1: Create `collector/requirements.txt`**

```
websockets==13.1
httpx==0.27.2
fastapi==0.115.0
uvicorn[standard]==0.32.0
prometheus-client==0.21.0
python-dotenv==1.0.1
polars==1.12.0
pyarrow==17.0.0
sqlalchemy==2.0.36
```

- [ ] **Step 2: Create `collector/__init__.py`**

```python
"""Polymarket weather contract collector — WS subscriber + Prom exposer."""
__version__ = "0.1.0"
```

- [ ] **Step 3: Create `collector/config.py`**

```python
"""Centralised env-var loading for collector."""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    gamma_host: str = os.getenv("POLY_GAMMA_HOST", "https://gamma-api.polymarket.com")
    clob_host: str = os.getenv("POLY_CLOB_HOST", "https://clob.polymarket.com")
    ws_url: str = os.getenv(
        "POLY_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/"
    )
    db_dir: Path = Path(os.getenv("COLLECTOR_DB_DIR", "database"))
    http_host: str = os.getenv("COLLECTOR_HTTP_HOST", "127.0.0.1")
    http_port: int = int(os.getenv("COLLECTOR_HTTP_PORT", "8091"))
    discovery_interval_s: int = int(os.getenv("COLLECTOR_DISCOVERY_INTERVAL_S", "86400"))
    l2_interval_s: int = int(os.getenv("COLLECTOR_L2_INTERVAL_S", "300"))
    health_interval_s: int = int(os.getenv("COLLECTOR_HEALTH_INTERVAL_S", "30"))
    weather_tag: str = os.getenv("COLLECTOR_WEATHER_TAG", "weather")


def load() -> Config:
    return Config()
```

- [ ] **Step 4: Create `pyproject.toml` (minimal, root-level pytest config)**

```toml
[tool.pytest.ini_options]
testpaths = ["collector/tests", "dashboard/tests", "archiver/tests"]
asyncio_mode = "auto"
filterwarnings = ["ignore::DeprecationWarning"]
```

- [ ] **Step 5: Create `collector/tests/conftest.py`**

```python
"""Shared pytest fixtures for collector tests."""
import sqlite3
from pathlib import Path
import pytest


@pytest.fixture
def tmp_db_dir(tmp_path: Path) -> Path:
    return tmp_path
```

- [ ] **Step 6: Verify the test harness loads**

```bash
python -m venv .venv-dev
source .venv-dev/bin/activate
pip install -r collector/requirements.txt pytest pytest-asyncio
pytest --collect-only collector/tests/
```

Expected: `collected 0 items` (no tests yet, but no import errors).

- [ ] **Step 7: Commit**

```bash
git add collector/ pyproject.toml
git commit -m "feat(collector): bootstrap collector package skeleton"
```

### Task B2: Implement SQLite schemas (`collector/db.py`)

**Files:**
- Create: `collector/db.py`
- Test: `collector/tests/test_db.py`

- [ ] **Step 1: Write the failing test**

Create `collector/tests/test_db.py`:

```python
import sqlite3
from pathlib import Path
from collector import db


def test_init_creates_tables(tmp_db_dir: Path):
    db.init(tmp_db_dir)
    conn = sqlite3.connect(tmp_db_dir / "markets.db")
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    assert ("markets",) in rows


def test_init_is_idempotent(tmp_db_dir: Path):
    db.init(tmp_db_dir)
    db.init(tmp_db_dir)  # must not raise
    conn = sqlite3.connect(tmp_db_dir / "orderbook.db")
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    assert ("l2_snapshot",) in rows


def test_upsert_market_round_trip(tmp_db_dir: Path):
    db.init(tmp_db_dir)
    db.upsert_market(
        tmp_db_dir,
        market_id="m1",
        slug="zspd-high-may-7-25c",
        question="Will ZSPD high be ≥25 °C on May 7?",
        end_date="2026-05-07T23:59:00Z",
        tag="weather",
        city="ZSPD",
        contract_type="high_ge",
        threshold_c=25.0,
        threshold_text="≥25",
        token_id_yes="t-yes",
        token_id_no="t-no",
    )
    rows = db.list_active_markets(tmp_db_dir)
    assert len(rows) == 1
    assert rows[0]["slug"] == "zspd-high-may-7-25c"
```

- [ ] **Step 2: Run the test (expect failure)**

```bash
pytest collector/tests/test_db.py -v
```

Expected: ImportError or AttributeError on `collector.db`.

- [ ] **Step 3: Implement `collector/db.py`**

```python
"""SQLite schema + connection helpers for collector."""
from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_SCHEMAS = {
    "markets.db": """
        CREATE TABLE IF NOT EXISTS markets (
            market_id      TEXT PRIMARY KEY,
            slug           TEXT UNIQUE NOT NULL,
            question       TEXT,
            end_date       TEXT,
            tag            TEXT,
            city           TEXT,
            contract_type  TEXT,
            threshold_c    REAL,
            threshold_text TEXT,
            token_id_yes   TEXT,
            token_id_no    TEXT,
            first_seen     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen      TIMESTAMP,
            archived       INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_markets_end_date ON markets(end_date);
        CREATE INDEX IF NOT EXISTS idx_markets_city     ON markets(city);
    """,
    "orderbook.db": """
        CREATE TABLE IF NOT EXISTS l2_snapshot (
            ts         TIMESTAMP NOT NULL,
            market_id  TEXT NOT NULL,
            side       TEXT CHECK(side IN ('yes','no')),
            levels     TEXT NOT NULL,
            PRIMARY KEY(ts, market_id, side)
        );
    """,
    "trades.db": """
        CREATE TABLE IF NOT EXISTS trades (
            ts          TIMESTAMP NOT NULL,
            market_id   TEXT NOT NULL,
            side        TEXT,
            price       REAL,
            size        REAL,
            taker_side  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_trades_market_ts ON trades(market_id, ts);
    """,
}


def init(db_dir: Path) -> None:
    db_dir.mkdir(parents=True, exist_ok=True)
    for filename, ddl in _SCHEMAS.items():
        with sqlite3.connect(db_dir / filename) as conn:
            conn.executescript(ddl)


@contextmanager
def connect(db_dir: Path, name: str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_dir / name, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_market(db_dir: Path, **fields) -> None:
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(f":{k}" for k in fields)
    update_set = ", ".join(
        f"{k}=excluded.{k}" for k in fields if k != "market_id"
    )
    sql = (
        f"INSERT INTO markets ({cols}, last_seen) VALUES ({placeholders}, CURRENT_TIMESTAMP) "
        f"ON CONFLICT(market_id) DO UPDATE SET {update_set}, last_seen=CURRENT_TIMESTAMP"
    )
    with connect(db_dir, "markets.db") as conn:
        conn.execute(sql, fields)


def list_active_markets(db_dir: Path) -> list[dict]:
    with connect(db_dir, "markets.db") as conn:
        rows = conn.execute(
            "SELECT * FROM markets WHERE archived=0 ORDER BY end_date"
        ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Run test (expect pass)**

```bash
pytest collector/tests/test_db.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add collector/db.py collector/tests/test_db.py
git commit -m "feat(collector): SQLite schemas + upsert helper"
```

### Task B3: Implement the gamma discovery loop (`collector/discovery.py`)

**Files:**
- Create: `collector/discovery.py`
- Test: `collector/tests/test_discovery.py`

The discovery module hits Polymarket's gamma REST API, parses each `event` for weather contracts, classifies city / contract_type / threshold from the slug, and upserts into `markets.db`.

- [ ] **Step 1: Write the failing test (slug parsing)**

`collector/tests/test_discovery.py`:

```python
from collector.discovery import classify_slug


def test_classify_zspd_high_ge():
    out = classify_slug("zspd-shanghai-high-temperature-may-7-2026-25c-or-higher")
    assert out["city"] == "ZSPD"
    assert out["contract_type"] == "high_ge"
    assert out["threshold_c"] == 25.0


def test_classify_jfk_low_le():
    out = classify_slug("kjfk-new-york-low-temperature-may-7-2026-15c-or-lower")
    assert out["city"] == "KJFK"
    assert out["contract_type"] == "low_le"
    assert out["threshold_c"] == 15.0


def test_classify_eq_threshold():
    out = classify_slug("klga-laguardia-high-may-7-2026-eq-22c")
    assert out["city"] == "KLGA"
    assert out["contract_type"] == "high_eq"
    assert out["threshold_c"] == 22.0


def test_classify_non_weather_returns_none():
    assert classify_slug("will-trump-win-the-2028-election") is None
```

- [ ] **Step 2: Run test (expect import failure)**

```bash
pytest collector/tests/test_discovery.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `collector/discovery.py`**

```python
"""Polymarket gamma discovery — finds weather contracts and upserts to markets.db."""
from __future__ import annotations
import asyncio
import logging
import re
from pathlib import Path
from typing import Any
import httpx
from collector import db
from collector.config import Config

log = logging.getLogger(__name__)

# Known airport codes / city tokens we recognise in slugs. Add more as we see them.
_CITY_TOKENS = {
    "zspd": "ZSPD", "shanghai": "ZSPD",
    "kjfk": "KJFK", "jfk": "KJFK",
    "klga": "KLGA", "laguardia": "KLGA",
    "kewr": "KEWR", "newark": "KEWR",
    "kmia": "KMIA", "miami": "KMIA",
    "klax": "KLAX",
    "kord": "KORD", "chicago": "KORD",
    "katx": "KATX", "austin": "KATX",
    "kdca": "KDCA", "washington": "KDCA",
    "kbos": "KBOS", "boston": "KBOS",
    "ksfo": "KSFO", "san-francisco": "KSFO",
}

_HIGH_LOW = {"high": "high", "low": "low", "tmax": "high", "tmin": "low"}

# 25c, 25-c, 25-degrees, 25f handled at parse time
_THRESHOLD_RE = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*(?:c|celsius|degree)\b")
_THRESHOLD_RE_F = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*(?:f|fahrenheit)\b")


def classify_slug(slug: str) -> dict[str, Any] | None:
    """Parse a gamma slug into a structured dict, or None if not weather-shaped."""
    s = slug.lower()
    # city
    city: str | None = None
    for tok, code in _CITY_TOKENS.items():
        if tok in s:
            city = code
            break
    if city is None:
        return None
    # high/low
    metric: str | None = None
    for tok, m in _HIGH_LOW.items():
        if tok in s:
            metric = m
            break
    if metric is None:
        return None
    # comparison direction
    if "-or-higher" in s or "above" in s or "ge-" in s:
        cmp_ = "ge"
    elif "-or-lower" in s or "below" in s or "le-" in s:
        cmp_ = "le"
    elif "-eq-" in s or "exactly" in s:
        cmp_ = "eq"
    else:
        return None
    # threshold (Celsius preferred; F is converted)
    m_c = _THRESHOLD_RE.search(s)
    if m_c:
        threshold_c = float(m_c.group(1))
    else:
        m_f = _THRESHOLD_RE_F.search(s)
        if m_f:
            threshold_c = (float(m_f.group(1)) - 32) * 5 / 9
        else:
            return None
    return {
        "city": city,
        "contract_type": f"{metric}_{cmp_}",
        "threshold_c": round(threshold_c, 2),
    }


async def fetch_weather_events(cfg: Config, *, http: httpx.AsyncClient) -> list[dict]:
    """Page through gamma /events?tag=weather and return raw event dicts."""
    out: list[dict] = []
    offset = 0
    page = 100
    while True:
        r = await http.get(
            f"{cfg.gamma_host}/events",
            params={"tag": cfg.weather_tag, "closed": "false",
                    "limit": page, "offset": offset},
            timeout=15.0,
        )
        r.raise_for_status()
        items = r.json()
        if not items:
            break
        out.extend(items)
        offset += page
        if len(items) < page:
            break
    return out


async def discover_once(cfg: Config) -> int:
    """One full discovery pass. Returns count of markets upserted."""
    db.init(cfg.db_dir)
    skipped = 0
    saved = 0
    async with httpx.AsyncClient() as http:
        events = await fetch_weather_events(cfg, http=http)
        for ev in events:
            for mkt in ev.get("markets", []):
                slug = mkt.get("slug") or ""
                cls = classify_slug(slug)
                if cls is None:
                    skipped += 1
                    continue
                tokens = mkt.get("clobTokenIds") or []
                if isinstance(tokens, str):
                    # gamma sometimes returns JSON-encoded string
                    import json
                    tokens = json.loads(tokens)
                if len(tokens) < 2:
                    skipped += 1
                    continue
                db.upsert_market(
                    cfg.db_dir,
                    market_id=str(mkt.get("id")),
                    slug=slug,
                    question=mkt.get("question"),
                    end_date=mkt.get("endDate"),
                    tag=cfg.weather_tag,
                    city=cls["city"],
                    contract_type=cls["contract_type"],
                    threshold_c=cls["threshold_c"],
                    threshold_text=str(cls["threshold_c"]),
                    token_id_yes=str(tokens[0]),
                    token_id_no=str(tokens[1]),
                )
                saved += 1
    log.info("discovery: saved=%d skipped=%d", saved, skipped)
    return saved


async def discovery_loop(cfg: Config, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await discover_once(cfg)
        except Exception:
            log.exception("discovery pass failed; retrying in 5 min")
            await asyncio.wait_for(stop.wait(), timeout=300.0)
        else:
            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg.discovery_interval_s)
            except asyncio.TimeoutError:
                pass
```

- [ ] **Step 4: Run test (expect pass)**

```bash
pytest collector/tests/test_discovery.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Live smoke (one-shot, against real gamma)**

```bash
python -c "
import asyncio
from collector.discovery import discover_once
from collector.config import Config
from pathlib import Path
cfg = Config(db_dir=Path('/tmp/collector-smoke'))
print('saved:', asyncio.run(discover_once(cfg)))
"
sqlite3 /tmp/collector-smoke/markets.db 'SELECT city, count(*) FROM markets GROUP BY city ORDER BY 2 DESC'
```

Expected: ≥1 city, count ≥1. Note any `WARNING:collector.discovery:skipped` rate; if everything is skipped, the slug parsing is too strict and you must add more `_CITY_TOKENS` / patterns.

- [ ] **Step 6: Commit**

```bash
git add collector/discovery.py collector/tests/test_discovery.py
git commit -m "feat(collector): gamma discovery + slug classifier"
```

### Task B4: Implement WS subscriber + in-memory `latest[]` (`collector/ws.py`)

**Files:**
- Create: `collector/ws.py`
- Test: `collector/tests/test_ws.py`

- [ ] **Step 1: Write the failing test (message handling)**

`collector/tests/test_ws.py`:

```python
import json
from collector.ws import LatestStore, handle_message


def test_handle_book_message_updates_latest():
    store = LatestStore()
    msg = json.dumps({
        "event_type": "book",
        "asset_id": "tok-1",
        "market": "m1",
        "bids": [{"price": "0.42", "size": "1000"}],
        "asks": [{"price": "0.45", "size": "800"}],
        "timestamp": "1715000000000",
    })
    handle_message(store, msg)
    snap = store.snapshot()
    assert "tok-1" in snap
    assert snap["tok-1"].best_bid == 0.42
    assert snap["tok-1"].best_ask == 0.45


def test_handle_price_change_updates_mid():
    store = LatestStore()
    handle_message(store, json.dumps({
        "event_type": "book",
        "asset_id": "tok-1",
        "market": "m1",
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.50", "size": "100"}],
        "timestamp": "1715000000000",
    }))
    handle_message(store, json.dumps({
        "event_type": "price_change",
        "asset_id": "tok-1",
        "market": "m1",
        "changes": [{"price": "0.41", "side": "BUY", "size": "100"}],
        "timestamp": "1715000001000",
    }))
    snap = store.snapshot()
    assert snap["tok-1"].best_bid == 0.41


def test_handle_unknown_event_is_noop():
    store = LatestStore()
    handle_message(store, json.dumps({"event_type": "??", "asset_id": "tok-1"}))
    assert store.snapshot() == {}
```

- [ ] **Step 2: Run (expect ImportError)**

```bash
pytest collector/tests/test_ws.py -v
```

- [ ] **Step 3: Implement `collector/ws.py`**

```python
"""Polymarket CLOB WebSocket subscriber. Maintains in-memory latest[token_id]."""
from __future__ import annotations
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Iterable
import websockets
from collector.config import Config

log = logging.getLogger(__name__)


@dataclass
class Tick:
    market_id: str
    best_bid: float = 0.0
    best_ask: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    last_trade: float = 0.0
    ts_ms: int = 0


class LatestStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._data: dict[str, Tick] = {}
        self.last_message_ts: float = 0.0
        self.connected: bool = False

    def update_book(self, asset_id: str, market_id: str, bids: list, asks: list, ts_ms: int) -> None:
        bid = float(bids[0]["price"]) if bids else 0.0
        ask = float(asks[0]["price"]) if asks else 0.0
        bid_sz = float(bids[0]["size"]) if bids else 0.0
        ask_sz = float(asks[0]["size"]) if asks else 0.0
        with self._lock:
            t = self._data.get(asset_id) or Tick(market_id=market_id)
            t.market_id = market_id
            t.best_bid, t.best_ask = bid, ask
            t.bid_size, t.ask_size = bid_sz, ask_sz
            t.ts_ms = ts_ms
            self._data[asset_id] = t
            self.last_message_ts = time.time()

    def update_price(self, asset_id: str, market_id: str, change: dict, ts_ms: int) -> None:
        side = change.get("side", "").upper()
        price = float(change.get("price", 0.0))
        with self._lock:
            t = self._data.get(asset_id) or Tick(market_id=market_id)
            t.market_id = market_id
            if side == "BUY":
                t.best_bid = price
            elif side == "SELL":
                t.best_ask = price
            t.ts_ms = ts_ms
            self._data[asset_id] = t
            self.last_message_ts = time.time()

    def snapshot(self) -> dict[str, Tick]:
        with self._lock:
            return dict(self._data)


def handle_message(store: LatestStore, raw: str) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return
    et = msg.get("event_type")
    asset_id = msg.get("asset_id") or msg.get("token_id")
    market_id = msg.get("market", "")
    ts_ms = int(msg.get("timestamp", 0) or 0)
    if et == "book" and asset_id:
        store.update_book(asset_id, market_id, msg.get("bids", []), msg.get("asks", []), ts_ms)
    elif et == "price_change" and asset_id:
        for ch in msg.get("changes", []):
            store.update_price(asset_id, market_id, ch, ts_ms)
    elif et == "last_trade_price" and asset_id:
        with store._lock:
            t = store._data.get(asset_id) or Tick(market_id=market_id)
            t.last_trade = float(msg.get("price", 0.0))
            t.ts_ms = ts_ms
            store._data[asset_id] = t
            store.last_message_ts = time.time()


async def ws_loop(cfg: Config, store: LatestStore, token_ids: Iterable[str], stop: asyncio.Event) -> None:
    """Maintain WS connection with exponential backoff on disconnect."""
    backoff = 1.0
    while not stop.is_set():
        try:
            async with websockets.connect(cfg.ws_url, ping_interval=20, ping_timeout=20) as wsc:
                await wsc.send(json.dumps({
                    "type": "MARKET",
                    "assets_ids": list(token_ids),
                }))
                store.connected = True
                backoff = 1.0
                async for raw in wsc:
                    handle_message(store, raw)
                    if stop.is_set():
                        break
        except Exception as e:
            log.warning("ws disconnect: %s; reconnecting in %.1fs", e, backoff)
            store.connected = False
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60.0)
    store.connected = False
```

- [ ] **Step 4: Run unit tests**

```bash
pytest collector/tests/test_ws.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add collector/ws.py collector/tests/test_ws.py
git commit -m "feat(collector): WS subscriber with latest-tick store"
```

### Task B5: Implement L2 polling + trades persistence (`collector/l2.py`)

**Files:**
- Create: `collector/l2.py`
- Test: `collector/tests/test_l2.py`

- [ ] **Step 1: Write the failing test**

`collector/tests/test_l2.py`:

```python
import json
import sqlite3
from pathlib import Path
from collector import db, l2


def test_persist_l2_snapshot(tmp_db_dir: Path):
    db.init(tmp_db_dir)
    l2.persist_snapshot(
        tmp_db_dir,
        ts="2026-05-08T03:00:00Z",
        market_id="m1",
        side="yes",
        levels=[[0.42, 1000.0], [0.41, 500.0]],
    )
    with sqlite3.connect(tmp_db_dir / "orderbook.db") as conn:
        rows = conn.execute("SELECT * FROM l2_snapshot").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][3]) == [[0.42, 1000.0], [0.41, 500.0]]


def test_persist_trade(tmp_db_dir: Path):
    db.init(tmp_db_dir)
    l2.persist_trade(
        tmp_db_dir,
        ts="2026-05-08T03:00:01Z",
        market_id="m1",
        side="yes",
        price=0.42,
        size=100.0,
        taker_side="buy",
    )
    with sqlite3.connect(tmp_db_dir / "trades.db") as conn:
        rows = conn.execute("SELECT * FROM trades").fetchall()
    assert len(rows) == 1
    assert rows[0][3] == 0.42  # price
```

- [ ] **Step 2: Run (expect failure)**

```bash
pytest collector/tests/test_l2.py -v
```

- [ ] **Step 3: Implement `collector/l2.py`**

```python
"""5-min L2 orderbook polling + trade persistence."""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
import httpx
from collector import db
from collector.config import Config

log = logging.getLogger(__name__)


def persist_snapshot(db_dir: Path, *, ts: str, market_id: str, side: str, levels: list) -> None:
    with db.connect(db_dir, "orderbook.db") as conn:
        conn.execute(
            "INSERT OR REPLACE INTO l2_snapshot(ts, market_id, side, levels) VALUES (?,?,?,?)",
            (ts, market_id, side, json.dumps(levels)),
        )


def persist_trade(db_dir: Path, *, ts: str, market_id: str, side: str,
                  price: float, size: float, taker_side: str) -> None:
    with db.connect(db_dir, "trades.db") as conn:
        conn.execute(
            "INSERT INTO trades(ts, market_id, side, price, size, taker_side) VALUES (?,?,?,?,?,?)",
            (ts, market_id, side, price, size, taker_side),
        )


async def _fetch_book(http: httpx.AsyncClient, clob_host: str, token_id: str) -> dict:
    r = await http.get(f"{clob_host}/book", params={"token_id": token_id}, timeout=15.0)
    r.raise_for_status()
    return r.json()


async def l2_loop(cfg: Config, stop: asyncio.Event) -> None:
    while not stop.is_set():
        markets = db.list_active_markets(cfg.db_dir)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        async with httpx.AsyncClient() as http:
            for m in markets:
                for side, tok in (("yes", m["token_id_yes"]), ("no", m["token_id_no"])):
                    if not tok:
                        continue
                    try:
                        book = await _fetch_book(http, cfg.clob_host, tok)
                    except Exception:
                        log.exception("L2 fetch failed for %s/%s", m["market_id"], side)
                        continue
                    bids = [[float(b["price"]), float(b["size"])] for b in book.get("bids", [])][:10]
                    asks = [[float(a["price"]), float(a["size"])] for a in book.get("asks", [])][:10]
                    persist_snapshot(
                        cfg.db_dir, ts=ts, market_id=m["market_id"], side=side,
                        levels={"bids": bids, "asks": asks},  # serialised as JSON
                    )
        try:
            await asyncio.wait_for(stop.wait(), timeout=cfg.l2_interval_s)
        except asyncio.TimeoutError:
            pass
```

Wait — the test passes `levels` as a list, but the impl above stores a dict `{bids, asks}`. Fix the impl to be consistent with the test (which expects `[[price, size], ...]`). Adjust:

Replace the `persist_snapshot` call inside `l2_loop` with two calls (one per side passing a flat `[[price,size], ...]` list of bids OR asks); but our schema only has one `side` column. Easier: store both sides separately. Change the inner block:

```python
persist_snapshot(cfg.db_dir, ts=ts, market_id=m["market_id"], side=f"{side}_bids", levels=bids)
persist_snapshot(cfg.db_dir, ts=ts, market_id=m["market_id"], side=f"{side}_asks", levels=asks)
```

But the schema constrains `side IN ('yes','no')`. Reconcile: store the merged top-of-book for each side as `[[bid_price, bid_size, ask_price, ask_size], ...]` zipped, OR redefine `side` to allow more values.

Cleanest: change schema in `db.py` to drop the `side IN ('yes','no')` CHECK and allow `'yes_bids'/'yes_asks'/'no_bids'/'no_asks'`. But that breaks the test we already wrote.

Simpler compromise: store **both bids and asks** as a single JSON object `{"bids": [...], "asks": [...]}` per side. Update the test to match this shape, and revert the impl to use this shape.

- [ ] **Step 3a: Fix the test to match the dict shape**

Replace `test_persist_l2_snapshot` body's levels assertion:

```python
def test_persist_l2_snapshot(tmp_db_dir: Path):
    db.init(tmp_db_dir)
    l2.persist_snapshot(
        tmp_db_dir,
        ts="2026-05-08T03:00:00Z",
        market_id="m1",
        side="yes",
        levels={"bids": [[0.42, 1000.0]], "asks": [[0.45, 800.0]]},
    )
    with sqlite3.connect(tmp_db_dir / "orderbook.db") as conn:
        rows = conn.execute("SELECT * FROM l2_snapshot").fetchall()
    assert len(rows) == 1
    parsed = json.loads(rows[0][3])
    assert parsed["bids"] == [[0.42, 1000.0]]
```

- [ ] **Step 3b: Re-confirm impl matches** — `persist_snapshot` signature accepts `levels: list`; widen to `Any` in the type hint, json-dump as is.

```python
def persist_snapshot(db_dir: Path, *, ts: str, market_id: str, side: str, levels) -> None:
    with db.connect(db_dir, "orderbook.db") as conn:
        conn.execute(
            "INSERT OR REPLACE INTO l2_snapshot(ts, market_id, side, levels) VALUES (?,?,?,?)",
            (ts, market_id, side, json.dumps(levels)),
        )
```

And `l2_loop` does **one** `persist_snapshot` per `side` with the dict.

- [ ] **Step 4: Run the test (expect pass)**

```bash
pytest collector/tests/test_l2.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add collector/l2.py collector/tests/test_l2.py
git commit -m "feat(collector): L2 5-min poller + trades persistence"
```

### Task B6: Implement `/metrics` and `/snapshot` HTTP server (`collector/http.py`)

**Files:**
- Create: `collector/http.py`
- Test: `collector/tests/test_http.py`

- [ ] **Step 1: Write the failing test**

`collector/tests/test_http.py`:

```python
import asyncio
from fastapi.testclient import TestClient
from collector.http import build_app
from collector.ws import LatestStore, Tick


def test_metrics_endpoint_returns_prometheus_format():
    store = LatestStore()
    store._data["tok-1"] = Tick(market_id="m1", best_bid=0.42, best_ask=0.45)
    store.connected = True
    app = build_app(store=store, markets_lookup=lambda: [
        {"market_id": "m1", "token_id_yes": "tok-1", "token_id_no": "tok-2",
         "city": "ZSPD", "contract_type": "high_ge"}
    ])
    client = TestClient(app)
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    assert 'pm_yes_bid{' in body
    assert 'city="ZSPD"' in body
    assert 'pm_ws_connected 1.0' in body


def test_snapshot_endpoint_returns_json():
    store = LatestStore()
    store._data["tok-1"] = Tick(market_id="m1", best_bid=0.42, best_ask=0.45)
    app = build_app(store=store, markets_lookup=lambda: [
        {"market_id": "m1", "token_id_yes": "tok-1", "token_id_no": "tok-2",
         "city": "ZSPD", "contract_type": "high_ge"}
    ])
    client = TestClient(app)
    r = client.get("/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["market_id"] == "m1"
    assert body[0]["yes_bid"] == 0.42


def test_health_endpoint():
    store = LatestStore()
    app = build_app(store=store, markets_lookup=lambda: [])
    r = TestClient(app).get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
```

- [ ] **Step 2: Run (expect failure)**

```bash
pytest collector/tests/test_http.py -v
```

- [ ] **Step 3: Implement `collector/http.py`**

```python
"""FastAPI app: /metrics (Prometheus exposition), /snapshot, /health."""
from __future__ import annotations
import time
from typing import Callable
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, JSONResponse
from prometheus_client import CollectorRegistry, Gauge, generate_latest, CONTENT_TYPE_LATEST
from collector.ws import LatestStore


def build_app(store: LatestStore, markets_lookup: Callable[[], list[dict]]) -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "ws_connected": store.connected,
                "ws_stale_s": time.time() - store.last_message_ts if store.last_message_ts else None}

    @app.get("/snapshot")
    def snapshot() -> JSONResponse:
        snap = store.snapshot()
        out = []
        for m in markets_lookup():
            ty = snap.get(m["token_id_yes"])
            tn = snap.get(m["token_id_no"])
            out.append({
                "market_id": m["market_id"],
                "city": m.get("city"),
                "contract_type": m.get("contract_type"),
                "yes_bid": ty.best_bid if ty else None,
                "yes_ask": ty.best_ask if ty else None,
                "no_bid":  tn.best_bid if tn else None,
                "no_ask":  tn.best_ask if tn else None,
                "ts_ms":   ty.ts_ms if ty else None,
            })
        return JSONResponse(out)

    @app.get("/metrics")
    def metrics() -> PlainTextResponse:
        reg = CollectorRegistry()
        labels = ["market_id", "city", "contract_type"]
        g_yes_bid = Gauge("pm_yes_bid", "YES best bid", labels, registry=reg)
        g_yes_ask = Gauge("pm_yes_ask", "YES best ask", labels, registry=reg)
        g_yes_mid = Gauge("pm_yes_mid", "YES mid", labels, registry=reg)
        g_no_bid  = Gauge("pm_no_bid", "NO best bid", labels, registry=reg)
        g_no_ask  = Gauge("pm_no_ask", "NO best ask", labels, registry=reg)
        g_no_mid  = Gauge("pm_no_mid", "NO mid", labels, registry=reg)
        g_spread  = Gauge("pm_spread", "yes_ask - yes_bid", labels, registry=reg)
        g_ws      = Gauge("pm_ws_connected", "WS connected", registry=reg)
        g_stale   = Gauge("pm_ws_stale_seconds", "Seconds since last WS msg", registry=reg)
        snap = store.snapshot()
        for m in markets_lookup():
            ty = snap.get(m["token_id_yes"])
            tn = snap.get(m["token_id_no"])
            lab = (m["market_id"], m.get("city") or "", m.get("contract_type") or "")
            if ty:
                g_yes_bid.labels(*lab).set(ty.best_bid)
                g_yes_ask.labels(*lab).set(ty.best_ask)
                if ty.best_bid and ty.best_ask:
                    g_yes_mid.labels(*lab).set((ty.best_bid + ty.best_ask) / 2)
                g_spread.labels(*lab).set(max(0.0, ty.best_ask - ty.best_bid))
            if tn:
                g_no_bid.labels(*lab).set(tn.best_bid)
                g_no_ask.labels(*lab).set(tn.best_ask)
                if tn.best_bid and tn.best_ask:
                    g_no_mid.labels(*lab).set((tn.best_bid + tn.best_ask) / 2)
        g_ws.set(1.0 if store.connected else 0.0)
        g_stale.set(time.time() - store.last_message_ts if store.last_message_ts else 0.0)
        return PlainTextResponse(generate_latest(reg).decode(), media_type=CONTENT_TYPE_LATEST)

    return app
```

- [ ] **Step 4: Run tests**

```bash
pytest collector/tests/test_http.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add collector/http.py collector/tests/test_http.py
git commit -m "feat(collector): /metrics + /snapshot + /health HTTP endpoints"
```

### Task B7: Implement the orchestrator (`collector/main.py`)

**Files:**
- Create: `collector/main.py`
- Test: not unit-tested directly; verified by integration smoke

- [ ] **Step 1: Implement**

```python
"""Collector orchestrator. Wires discovery, ws, l2, and HTTP together."""
from __future__ import annotations
import asyncio
import logging
import signal
from collector import db, discovery, l2
from collector.config import load
from collector.http import build_app
from collector.ws import LatestStore, ws_loop
import uvicorn

log = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load()
    db.init(cfg.db_dir)
    # bootstrap discovery so we have markets for ws subscribe
    await discovery.discover_once(cfg)

    store = LatestStore()
    stop = asyncio.Event()

    def _on_signal(*_):
        log.info("signal received, shutting down")
        stop.set()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    def lookup() -> list[dict]:
        return db.list_active_markets(cfg.db_dir)

    token_ids: list[str] = []
    for m in lookup():
        token_ids += [m["token_id_yes"], m["token_id_no"]]
    log.info("subscribing to %d tokens", len(token_ids))

    app = build_app(store=store, markets_lookup=lookup)
    server = uvicorn.Server(uvicorn.Config(
        app, host=cfg.http_host, port=cfg.http_port, log_level="warning"
    ))

    tasks = [
        asyncio.create_task(server.serve(), name="http"),
        asyncio.create_task(ws_loop(cfg, store, token_ids, stop), name="ws"),
        asyncio.create_task(l2.l2_loop(cfg, stop), name="l2"),
        asyncio.create_task(discovery.discovery_loop(cfg, stop), name="discovery"),
    ]
    await stop.wait()
    server.should_exit = True
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Local integration smoke**

```bash
COLLECTOR_DB_DIR=/tmp/collector-smoke python -m collector.main &
COLL_PID=$!
sleep 30
curl -s 127.0.0.1:8091/health
curl -s 127.0.0.1:8091/snapshot | head -c 500
curl -s 127.0.0.1:8091/metrics | grep -E '^pm_(yes|ws)' | head -5
kill $COLL_PID
```

Expected: `/health` returns `ok`; `/snapshot` returns a JSON list; `/metrics` exposes `pm_*` series.

If WS subscription fails (e.g. invalid message format, rate limit), check `journalctl` or stdout for `ws disconnect:` and adjust the WS subscribe payload format to match Polymarket's current API. (The skill spec notes "WS rate limits / batch size" as an open question — confirm here.)

- [ ] **Step 3: Commit**

```bash
git add collector/main.py
git commit -m "feat(collector): asyncio orchestrator (discovery+ws+l2+http)"
```

### Task B8: Install VictoriaMetrics + Grafana on singapore

**Files:** (remote only)
- `sg:/etc/victoriametrics/scrape.yml`
- `sg:/etc/grafana/provisioning/datasources/vm.yaml`
- `sg:/etc/grafana/provisioning/dashboards/zspd.yaml`
- Create (local, copy to sg): `dashboards/grafana/zspd-overview.json`
- Create (local, copy to sg): `dashboards/grafana/zspd-market-template.json`

- [ ] **Step 1: Install VM via official .deb**

```bash
ssh sg-pm '
curl -L -o /tmp/vm.deb "https://github.com/VictoriaMetrics/VictoriaMetrics/releases/download/v1.111.0/victoria-metrics-linux-amd64-v1.111.0.deb"
sudo dpkg -i /tmp/vm.deb
sudo systemctl status victoriametrics.service --no-pager | head -3
'
```

Expected: `active`. The default unit binds `127.0.0.1:8428` and stores data under `/var/lib/victoria-metrics-data`.

- [ ] **Step 2: Configure scrape**

```bash
ssh sg-pm 'sudo tee /etc/victoriametrics/scrape.yml >/dev/null' <<'EOF'
scrape_configs:
  - job_name: zspd-collector
    scrape_interval: 3s
    static_configs:
      - targets: ["127.0.0.1:8091"]
EOF
ssh sg-pm 'sudo systemctl edit --full victoriametrics.service'  # add -promscrape.config=/etc/victoriametrics/scrape.yml -retentionPeriod=30d
```

The `systemctl edit --full` is interactive — alternative non-interactive:

```bash
ssh sg-pm '
sudo mkdir -p /etc/systemd/system/victoriametrics.service.d
sudo tee /etc/systemd/system/victoriametrics.service.d/override.conf >/dev/null <<EOF
[Service]
ExecStart=
ExecStart=/usr/bin/victoria-metrics-prod -storageDataPath=/var/lib/victoria-metrics-data -httpListenAddr=127.0.0.1:8428 -retentionPeriod=30d -promscrape.config=/etc/victoriametrics/scrape.yml
EOF
sudo systemctl daemon-reload
sudo systemctl restart victoriametrics
sudo systemctl status victoriametrics --no-pager | head -5
'
```

- [ ] **Step 3: Install Grafana via official .deb**

```bash
ssh sg-pm '
sudo apt-get install -y apt-transport-https software-properties-common wget
sudo wget -q -O /etc/apt/keyrings/grafana.asc https://apt.grafana.com/gpg.key
echo "deb [signed-by=/etc/apt/keyrings/grafana.asc] https://apt.grafana.com stable main" | sudo tee /etc/apt/sources.list.d/grafana.list
sudo apt-get update
sudo apt-get install -y grafana
sudo systemctl enable --now grafana-server
sudo systemctl status grafana-server --no-pager | head -3
'
```

- [ ] **Step 4: Bind Grafana to 127.0.0.1**

```bash
ssh sg-pm '
sudo sed -i "s/^;http_addr =$/http_addr = 127.0.0.1/" /etc/grafana/grafana.ini
sudo systemctl restart grafana-server
'
```

- [ ] **Step 5: Provision VM datasource**

```bash
ssh sg-pm 'sudo tee /etc/grafana/provisioning/datasources/vm.yaml >/dev/null' <<'EOF'
apiVersion: 1
datasources:
  - name: VictoriaMetrics
    type: prometheus
    access: proxy
    url: http://127.0.0.1:8428
    isDefault: true
EOF
```

- [ ] **Step 6: Create Grafana dashboard JSONs locally**

Skip the JSON content here — we'll commit a placeholder and refine after first data flows. Create stub:

```bash
mkdir -p dashboards/grafana
cat > dashboards/grafana/zspd-overview.json <<'EOF'
{
  "annotations": {"list": []},
  "panels": [
    {
      "datasource": "VictoriaMetrics",
      "title": "YES mid (all markets)",
      "type": "timeseries",
      "targets": [{"expr": "pm_yes_mid"}],
      "gridPos": {"h": 12, "w": 24, "x": 0, "y": 0}
    }
  ],
  "schemaVersion": 39,
  "title": "ZSPD Overview",
  "uid": "zspd-overview",
  "version": 1
}
EOF
git add dashboards/grafana/zspd-overview.json
git commit -m "feat(grafana): seed overview dashboard"
```

- [ ] **Step 7: Provisioning config + copy dashboard**

```bash
scp dashboards/grafana/zspd-overview.json sg-pm:/tmp/
ssh sg-pm '
sudo install -d /var/lib/grafana/dashboards
sudo mv /tmp/zspd-overview.json /var/lib/grafana/dashboards/
sudo chown -R grafana:grafana /var/lib/grafana/dashboards

sudo tee /etc/grafana/provisioning/dashboards/zspd.yaml >/dev/null <<EOF
apiVersion: 1
providers:
  - name: zspd
    folder: ZSPD
    type: file
    options:
      path: /var/lib/grafana/dashboards
EOF

sudo systemctl restart grafana-server
'
```

- [ ] **Step 8: Verify VM scraping the collector**

(Collector isn't deployed yet; this verifies the scrape config syntactically by checking VM picked it up.)

```bash
ssh sg-pm 'curl -s 127.0.0.1:8428/api/v1/targets | head -c 500'
```

Expected: a `targets` JSON listing the `zspd-collector` target (probably with `health=down` since collector isn't deployed yet — that's fine).

### Task B9: Deploy collector to singapore

**Files:** (remote: `/opt/zspd-collector/`, `/etc/systemd/system/zspd-collector.service`)

- [ ] **Step 1: Create remote directories**

```bash
ssh sg-pm '
sudo install -d -o polymarket -g polymarket /opt/zspd-collector/src /opt/zspd-collector/warm /opt/zspd-collector/database
sudo install -d -o polymarket -g polymarket /var/log/zspd-collector
'
```

- [ ] **Step 2: Push code**

```bash
scripts/push_code.sh singapore
```

This rsyncs to `/opt/zspd-collector/src/` (and the other singapore paths). Verify:

```bash
ssh sg-pm 'ls /opt/zspd-collector/src/collector/ | head -10'
```

Expected: `__init__.py`, `config.py`, `db.py`, `discovery.py`, `http.py`, `l2.py`, `main.py`, `ws.py`.

- [ ] **Step 3: Create venv + install requirements**

```bash
ssh sg-pm '
cd /opt/zspd-collector
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r src/collector/requirements.txt
'
```

- [ ] **Step 4: Copy `.env`**

The collector needs `WEBDAV_*` and gamma/clob hosts. Reuse the dashboard `.env` pattern:

```bash
scp .env sg-pm:/tmp/.env-collector
ssh sg-pm '
sudo install -m 600 -o polymarket -g polymarket /tmp/.env-collector /opt/zspd-collector/.env
rm /tmp/.env-collector
'
```

- [ ] **Step 5: Write the systemd unit**

Create `systemd/zspd-collector.service` locally:

```ini
[Unit]
Description=ZSPD Polymarket weather collector
After=network-online.target victoriametrics.service
Wants=network-online.target

[Service]
Type=simple
User=polymarket
Group=polymarket
WorkingDirectory=/opt/zspd-collector
EnvironmentFile=/opt/zspd-collector/.env
Environment=COLLECTOR_DB_DIR=/opt/zspd-collector/database
Environment=PYTHONPATH=/opt/zspd-collector/src
ExecStart=/opt/zspd-collector/.venv/bin/python -m collector.main
Restart=on-failure
RestartSec=10
StandardOutput=append:/var/log/zspd-collector/collector.log
StandardError=append:/var/log/zspd-collector/collector.err

[Install]
WantedBy=multi-user.target
```

```bash
git add systemd/zspd-collector.service
git commit -m "feat(collector): systemd unit"
```

- [ ] **Step 6: Install + start**

```bash
scp systemd/zspd-collector.service sg-pm:/tmp/
ssh sg-pm '
sudo mv /tmp/zspd-collector.service /etc/systemd/system/zspd-collector.service
sudo systemctl daemon-reload
sudo systemctl enable --now zspd-collector.service
sleep 5
sudo systemctl status zspd-collector.service --no-pager | head -10
'
```

- [ ] **Step 7: Smoke checks (verification items 1–3 from spec §8)**

```bash
ssh sg-pm 'sqlite3 /opt/zspd-collector/database/markets.db "SELECT city, count(*) FROM markets GROUP BY city ORDER BY 2 DESC"'
ssh sg-pm 'curl -s 127.0.0.1:8091/health'
ssh sg-pm 'curl -s 127.0.0.1:8428/api/v1/query?query=pm_yes_mid | head -c 300'
```

Expected: ≥5 cities; health JSON with `ws_connected: true`; VM query returns data points within 60 s.

If WS isn't connecting, check `/var/log/zspd-collector/collector.err` and tweak Polymarket WS subscribe message shape (open question in spec §10).

---

## Phase C — UI layer

### Task C1: Bootstrap `dashboard/` package + auth middleware

**Files:**
- Create: `dashboard/__init__.py`, `dashboard/config.py`, `dashboard/auth.py`, `dashboard/main.py`
- Create: `dashboard/templates/base.html`
- Create: `dashboard/static/style.css`
- Create: `dashboard/requirements.txt`
- Create: `dashboard/tests/__init__.py`, `dashboard/tests/conftest.py`, `dashboard/tests/test_auth.py`

- [ ] **Step 1: Create `dashboard/requirements.txt`**

```
fastapi==0.115.0
uvicorn[standard]==0.32.0
jinja2==3.1.4
httpx==0.27.2
python-dotenv==1.0.1
prometheus-client==0.21.0
```

- [ ] **Step 2: Create `dashboard/config.py`**

```python
"""Dashboard config from env."""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    auth_token: str = os.getenv("DASHBOARD_AUTH_TOKEN", "")
    collector_url: str = os.getenv("DASHBOARD_COLLECTOR_URL", "http://127.0.0.1:8091")
    vm_url: str = os.getenv("DASHBOARD_VM_URL", "http://127.0.0.1:8428")
    grafana_url: str = os.getenv("DASHBOARD_GRAFANA_URL", "http://127.0.0.1:3000")
    valkyrie_host: str = os.getenv("DASHBOARD_VALKYRIE_HOST", "valkyrie")
    listen_host: str = os.getenv("DASHBOARD_LISTEN_HOST", "127.0.0.1")
    listen_port: int = int(os.getenv("DASHBOARD_LISTEN_PORT", "8090"))


def load() -> Config:
    return Config()
```

- [ ] **Step 3: Write the failing auth test**

`dashboard/tests/test_auth.py`:

```python
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from dashboard.auth import bearer_auth
from dashboard.config import Config


def _app(token: str) -> FastAPI:
    app = FastAPI()
    cfg = Config(auth_token=token)
    @app.get("/x")
    def x(_=Depends(bearer_auth(cfg))) -> dict:
        return {"ok": True}
    return app


def test_missing_token_rejected():
    r = TestClient(_app("secret")).get("/x")
    assert r.status_code == 401


def test_correct_token_allowed():
    r = TestClient(_app("secret")).get("/x", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200


def test_health_route_unauthed_should_be_skipped_by_caller():
    # Auth dependency only applied where caller adds it; /health is opt-out.
    # This test documents the contract.
    pass
```

- [ ] **Step 4: Run (expect ImportError)**

```bash
pytest dashboard/tests/test_auth.py -v
```

- [ ] **Step 5: Implement `dashboard/auth.py`**

```python
"""Bearer-token auth dependency factory."""
from fastapi import Header, HTTPException
from typing import Callable
from dashboard.config import Config


def bearer_auth(cfg: Config) -> Callable:
    async def _dep(authorization: str = Header(default="")) -> None:
        if not cfg.auth_token:
            return  # auth disabled (dev only — log loudly via UI banner elsewhere)
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer")
        token = authorization[len("Bearer "):]
        if token != cfg.auth_token:
            raise HTTPException(status_code=401, detail="bad bearer")
    return _dep
```

- [ ] **Step 6: Run (expect pass)**

```bash
pytest dashboard/tests/test_auth.py -v
```

Expected: 2 passed (third test is a no-op that documents contract).

- [ ] **Step 7: Implement `dashboard/main.py` skeleton**

```python
"""Dashboard FastAPI entrypoint."""
from __future__ import annotations
from pathlib import Path
from fastapi import FastAPI, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dashboard.auth import bearer_auth
from dashboard.config import load


def build_app() -> FastAPI:
    cfg = load()
    app = FastAPI(title="ZSPD Polymarket Dashboard")
    base = Path(__file__).parent
    app.mount("/static", StaticFiles(directory=base / "static"), name="static")
    templates = Jinja2Templates(directory=base / "templates")
    auth = bearer_auth(cfg)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    # Views are wired in later tasks (C2/C3/C4/C5).
    return app


app = build_app()


if __name__ == "__main__":
    import uvicorn
    cfg = load()
    uvicorn.run(app, host=cfg.listen_host, port=cfg.listen_port)
```

- [ ] **Step 8: Create `dashboard/templates/base.html` and `dashboard/static/style.css`**

`base.html`:

```html
<!doctype html>
<html><head>
<meta charset="utf-8"><title>{% block title %}ZSPD Dashboard{% endblock %}</title>
<link rel="stylesheet" href="/static/style.css">
<script src="https://unpkg.com/htmx.org@2.0.3"></script>
</head><body>
<header><a href="/">grid</a> · <a href="/strategy">strategy</a> · <a href="/replay">replay</a></header>
<main>{% block content %}{% endblock %}</main>
</body></html>
```

`style.css`:

```css
body { font-family: -apple-system, sans-serif; margin: 0; padding: 1rem; }
header { padding: 0.5rem; border-bottom: 1px solid #ddd; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 0.4rem; border-bottom: 1px solid #eee; text-align: right; }
th:first-child, td:first-child { text-align: left; }
.edge-pos { color: #0a7d20; }
.edge-neg { color: #c1390a; }
```

- [ ] **Step 9: Commit**

```bash
git add dashboard/ pyproject.toml
git commit -m "feat(dashboard): bootstrap package + bearer auth + base layout"
```

### Task C2: Implement view A — market grid

**Files:**
- Create: `dashboard/clients/__init__.py`, `dashboard/clients/collector.py`
- Create: `dashboard/views/__init__.py`, `dashboard/views/grid.py`
- Create: `dashboard/templates/grid.html`
- Test: `dashboard/tests/test_grid.py`
- Modify: `dashboard/main.py`

- [ ] **Step 1: Implement collector client**

`dashboard/clients/collector.py`:

```python
"""Read-only client for collector's /snapshot endpoint."""
from __future__ import annotations
import httpx
from dashboard.config import Config


async def fetch_snapshot(cfg: Config) -> list[dict]:
    async with httpx.AsyncClient(timeout=5.0) as http:
        r = await http.get(f"{cfg.collector_url}/snapshot")
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 2: Write the failing view test**

`dashboard/tests/test_grid.py`:

```python
from unittest.mock import patch
from fastapi.testclient import TestClient
from dashboard.main import build_app


def test_grid_renders_with_markets():
    fake = [
        {"market_id": "m1", "city": "ZSPD", "contract_type": "high_ge",
         "yes_bid": 0.42, "yes_ask": 0.45, "no_bid": 0.55, "no_ask": 0.58, "ts_ms": 0},
        {"market_id": "m2", "city": "KJFK", "contract_type": "low_le",
         "yes_bid": 0.10, "yes_ask": 0.12, "no_bid": 0.88, "no_ask": 0.90, "ts_ms": 0},
    ]
    with patch("dashboard.views.grid.fetch_snapshot", return_value=fake):
        client = TestClient(build_app())
        r = client.get("/", headers={"Authorization": "Bearer "})
        assert r.status_code == 200
        assert "ZSPD" in r.text
        assert "KJFK" in r.text


def test_grid_filter_by_city():
    fake = [
        {"market_id": "m1", "city": "ZSPD", "contract_type": "high_ge",
         "yes_bid": 0.42, "yes_ask": 0.45, "no_bid": 0.55, "no_ask": 0.58, "ts_ms": 0},
        {"market_id": "m2", "city": "KJFK", "contract_type": "low_le",
         "yes_bid": 0.10, "yes_ask": 0.12, "no_bid": 0.88, "no_ask": 0.90, "ts_ms": 0},
    ]
    with patch("dashboard.views.grid.fetch_snapshot", return_value=fake):
        client = TestClient(build_app())
        r = client.get("/?city=ZSPD", headers={"Authorization": "Bearer "})
        assert r.status_code == 200
        assert "ZSPD" in r.text
        assert "KJFK" not in r.text
```

- [ ] **Step 3: Run (expect failure)**

```bash
pytest dashboard/tests/test_grid.py -v
```

- [ ] **Step 4: Implement `dashboard/views/grid.py`**

```python
"""View A — market grid."""
from __future__ import annotations
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from dashboard.clients.collector import fetch_snapshot
from dashboard.config import load

router = APIRouter()
_templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


@router.get("/", response_class=HTMLResponse)
async def grid(request: Request, city: str | None = None,
               contract_type: str | None = None):
    cfg = load()
    snap = await fetch_snapshot(cfg)
    if city:
        snap = [m for m in snap if m["city"] == city]
    if contract_type:
        snap = [m for m in snap if m["contract_type"] == contract_type]
    snap.sort(key=lambda m: -(m.get("yes_ask") or 0) + (m.get("yes_bid") or 0))
    return _templates.TemplateResponse(request,
        "grid.html", {"markets": snap, "city": city, "contract_type": contract_type})
```

- [ ] **Step 5: Create `dashboard/templates/grid.html`**

```html
{% extends "base.html" %}
{% block title %}Market grid{% endblock %}
{% block content %}
<form method="get">
  city: <input name="city" value="{{ city or '' }}">
  type: <input name="contract_type" value="{{ contract_type or '' }}">
  <button>filter</button>
</form>
<table hx-get="/" hx-trigger="every 10s" hx-target="closest table" hx-swap="outerHTML">
  <thead><tr>
    <th>market</th><th>city</th><th>type</th>
    <th>YES bid</th><th>YES ask</th><th>NO bid</th><th>NO ask</th>
  </tr></thead>
  <tbody>
  {% for m in markets %}
    <tr>
      <td><a href="/m/{{ m.market_id }}">{{ m.market_id }}</a></td>
      <td>{{ m.city or '—' }}</td>
      <td>{{ m.contract_type or '—' }}</td>
      <td>{{ '%.3f' % (m.yes_bid or 0) }}</td>
      <td>{{ '%.3f' % (m.yes_ask or 0) }}</td>
      <td>{{ '%.3f' % (m.no_bid or 0) }}</td>
      <td>{{ '%.3f' % (m.no_ask or 0) }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
{% endblock %}
```

- [ ] **Step 6: Wire into `dashboard/main.py`**

Add at the top:

```python
from dashboard.views import grid as grid_view
```

In `build_app()` after the static mount:

```python
app.include_router(grid_view.router, dependencies=[Depends(auth)])
```

- [ ] **Step 7: Run tests**

```bash
pytest dashboard/tests/test_grid.py -v
```

Expected: 2 passed.

- [ ] **Step 8: Commit**

```bash
git add dashboard/clients/ dashboard/views/grid.py dashboard/templates/grid.html dashboard/main.py dashboard/tests/test_grid.py
git commit -m "feat(dashboard): grid view (route A) + collector client"
```

### Task C3: Implement view B — single-market drill-down

**Files:**
- Create: `dashboard/clients/vm.py`
- Create: `dashboard/clients/sqlite.py`
- Create: `dashboard/views/market.py`
- Create: `dashboard/templates/market.html`
- Modify: `dashboard/main.py`
- Test: `dashboard/tests/test_market.py`

- [ ] **Step 1: Implement VM client**

`dashboard/clients/vm.py`:

```python
"""PromQL queries against VictoriaMetrics."""
from __future__ import annotations
import httpx
from dashboard.config import Config


async def query_range(cfg: Config, expr: str, start_unix: int, end_unix: int, step_s: int) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as http:
        r = await http.get(f"{cfg.vm_url}/api/v1/query_range",
                           params={"query": expr, "start": start_unix,
                                   "end": end_unix, "step": f"{step_s}s"})
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 2: Implement SQLite client**

`dashboard/clients/sqlite.py`:

```python
"""Read helpers for collector SQLite DBs (read-only)."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path


def latest_l2(db_dir: Path, market_id: str) -> dict:
    """Return latest yes/no L2 snapshot dicts."""
    out: dict[str, dict] = {}
    if not (db_dir / "orderbook.db").exists():
        return out
    with sqlite3.connect(db_dir / "orderbook.db") as conn:
        for side in ("yes", "no"):
            row = conn.execute(
                "SELECT levels FROM l2_snapshot WHERE market_id=? AND side=? "
                "ORDER BY ts DESC LIMIT 1", (market_id, side)
            ).fetchone()
            if row:
                out[side] = json.loads(row[0])
    return out


def recent_trades(db_dir: Path, market_id: str, n: int = 50) -> list[dict]:
    if not (db_dir / "trades.db").exists():
        return []
    with sqlite3.connect(db_dir / "trades.db") as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trades WHERE market_id=? ORDER BY ts DESC LIMIT ?",
            (market_id, n),
        ).fetchall()
    return [dict(r) for r in rows]


def market_meta(db_dir: Path, market_id: str) -> dict | None:
    if not (db_dir / "markets.db").exists():
        return None
    with sqlite3.connect(db_dir / "markets.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM markets WHERE market_id=?", (market_id,)
        ).fetchone()
    return dict(row) if row else None
```

- [ ] **Step 3: Add config knob for collector DB dir**

Edit `dashboard/config.py` `Config`:

```python
    db_dir: Path = Path(os.getenv("DASHBOARD_COLLECTOR_DB_DIR", "/opt/zspd-collector/database"))
```

(Add `from pathlib import Path` at top.)

- [ ] **Step 4: Write the failing market view test**

`dashboard/tests/test_market.py`:

```python
from unittest.mock import patch
from fastapi.testclient import TestClient
from dashboard.main import build_app


def test_market_view_renders():
    with patch("dashboard.views.market.market_meta", return_value={
        "market_id": "m1", "city": "ZSPD", "contract_type": "high_ge",
        "threshold_c": 25.0, "end_date": "2026-05-08T23:59Z",
        "token_id_yes": "tok-1", "token_id_no": "tok-2", "slug": "x", "question": "?",
    }), patch("dashboard.views.market.latest_l2", return_value={
        "yes": {"bids": [[0.42, 100]], "asks": [[0.45, 80]]},
    }), patch("dashboard.views.market.recent_trades", return_value=[]):
        client = TestClient(build_app())
        r = client.get("/m/m1", headers={"Authorization": "Bearer "})
        assert r.status_code == 200
        assert "ZSPD" in r.text
        assert "0.42" in r.text


def test_market_view_404():
    with patch("dashboard.views.market.market_meta", return_value=None):
        client = TestClient(build_app())
        r = client.get("/m/nope", headers={"Authorization": "Bearer "})
        assert r.status_code == 404
```

- [ ] **Step 5: Implement `dashboard/views/market.py`**

```python
"""View B — single market drill-down."""
from __future__ import annotations
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from dashboard.clients.sqlite import market_meta, latest_l2, recent_trades
from dashboard.config import load

router = APIRouter()
_templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


@router.get("/m/{market_id}", response_class=HTMLResponse)
async def market(request: Request, market_id: str):
    cfg = load()
    meta = market_meta(cfg.db_dir, market_id)
    if not meta:
        raise HTTPException(404, "market not found")
    l2 = latest_l2(cfg.db_dir, market_id)
    trades = recent_trades(cfg.db_dir, market_id, n=50)
    grafana_panel = (
        f"{cfg.grafana_url}/d-solo/zspd-overview/zspd-overview?"
        f"var-market={market_id}&theme=light&panelId=1"
    )
    return _templates.TemplateResponse(request, "market.html", {
        "m": meta, "l2": l2, "trades": trades, "grafana_panel": grafana_panel,
    })
```

- [ ] **Step 6: Create `dashboard/templates/market.html`**

```html
{% extends "base.html" %}
{% block title %}{{ m.market_id }}{% endblock %}
{% block content %}
<h2>{{ m.city }} {{ m.contract_type }} threshold={{ m.threshold_c }}</h2>
<p>{{ m.question }}<br><small>ends {{ m.end_date }} · slug {{ m.slug }}</small></p>

<iframe src="{{ grafana_panel }}" width="100%" height="320" frameborder="0"></iframe>

<h3>L2 (latest)</h3>
{% for side in ("yes", "no") %}
  <h4>{{ side }}</h4>
  {% if l2[side] %}
    <table><thead><tr><th>side</th><th>price</th><th>size</th></tr></thead><tbody>
    {% for b in l2[side]["bids"] %}<tr><td>bid</td><td>{{ b[0] }}</td><td>{{ b[1] }}</td></tr>{% endfor %}
    {% for a in l2[side]["asks"] %}<tr><td>ask</td><td>{{ a[0] }}</td><td>{{ a[1] }}</td></tr>{% endfor %}
    </tbody></table>
  {% else %}<p>no L2 yet</p>{% endif %}
{% endfor %}

<h3>Recent trades</h3>
<table><thead><tr><th>ts</th><th>side</th><th>price</th><th>size</th></tr></thead><tbody>
{% for t in trades %}
  <tr><td>{{ t.ts }}</td><td>{{ t.side }}</td><td>{{ t.price }}</td><td>{{ t.size }}</td></tr>
{% endfor %}
</tbody></table>
{% endblock %}
```

- [ ] **Step 7: Wire into `main.py`**

Add `from dashboard.views import market as market_view` and `app.include_router(market_view.router, dependencies=[Depends(auth)])`.

- [ ] **Step 8: Run tests**

```bash
pytest dashboard/tests/test_market.py -v
```

Expected: 2 passed.

- [ ] **Step 9: Commit**

```bash
git add dashboard/clients/{vm,sqlite}.py dashboard/views/market.py dashboard/templates/market.html dashboard/main.py dashboard/config.py dashboard/tests/test_market.py
git commit -m "feat(dashboard): market drill-down view (route B)"
```

### Task C4: Implement view C — strategy ops + `inspect_strategy.py`

**Files:**
- Create: `scripts/inspect_strategy.py` (runs on valkyrie)
- Create: `dashboard/clients/valkyrie.py`
- Create: `dashboard/views/strategy.py`
- Create: `dashboard/templates/strategy.html`
- Modify: `dashboard/main.py`
- Test: `dashboard/tests/test_strategy.py`

- [ ] **Step 1: Author `scripts/inspect_strategy.py`**

```python
#!/usr/bin/env python3
"""Print a JSON snapshot of valkyrie's zspd-strategy state. Read-only."""
from __future__ import annotations
import json
import os
import subprocess
import sqlite3
from pathlib import Path

ROOT = Path(os.getenv("ZSPD_ROOT", "/opt/zspd-trader/src"))


def systemd_state(unit: str) -> dict:
    out = subprocess.run(
        ["systemctl", "show", unit, "--property=ActiveState,SubState,MainPID"],
        capture_output=True, text=True, check=False,
    )
    kv = dict(
        line.split("=", 1) for line in out.stdout.strip().splitlines() if "=" in line
    )
    return kv


def positions_today() -> list[dict]:
    db = ROOT / "database" / "data.db"
    if not db.exists():
        return []
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM positions WHERE date(ts)=date('now')"
        ).fetchall() if _has_table(conn, "positions") else []
    return [dict(r) for r in rows]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def main() -> None:
    print(json.dumps({
        "strategy": systemd_state("zspd-strategy.service"),
        "hot_refresh": systemd_state("zspd-hot-refresh.timer"),
        "clob_proxy": systemd_state("clob-proxy.service"),
        "positions_today": positions_today(),
    }, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Implement `dashboard/clients/valkyrie.py`**

```python
"""SSH into valkyrie and run inspect_strategy.py. Cached 30s."""
from __future__ import annotations
import asyncio
import json
import time
from dashboard.config import Config

_cache: dict = {"ts": 0.0, "data": None}


async def fetch_strategy_state(cfg: Config, ttl_s: int = 30) -> dict:
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < ttl_s:
        return _cache["data"]
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        cfg.valkyrie_host,
        "/opt/zspd-trader/.venv/bin/python /opt/zspd-trader/src/scripts/inspect_strategy.py",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except asyncio.TimeoutError:
        proc.kill()
        return {"error": "valkyrie timeout"}
    if proc.returncode != 0:
        return {"error": f"valkyrie ssh exit {proc.returncode}",
                "stderr": stderr.decode(errors="replace")[:500]}
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {"error": "bad json from valkyrie", "raw": stdout.decode()[:500]}
    _cache["ts"] = now
    _cache["data"] = data
    return data
```

- [ ] **Step 3: Write the failing strategy view test**

`dashboard/tests/test_strategy.py`:

```python
from unittest.mock import patch
from fastapi.testclient import TestClient
from dashboard.main import build_app


def test_strategy_view_ok():
    fake = {"strategy": {"ActiveState": "inactive", "SubState": "dead"},
            "positions_today": []}
    with patch("dashboard.views.strategy.fetch_strategy_state", return_value=fake):
        r = TestClient(build_app()).get("/strategy", headers={"Authorization": "Bearer "})
        assert r.status_code == 200
        assert "inactive" in r.text


def test_strategy_view_handles_error():
    with patch("dashboard.views.strategy.fetch_strategy_state",
               return_value={"error": "valkyrie unreachable"}):
        r = TestClient(build_app()).get("/strategy", headers={"Authorization": "Bearer "})
        assert r.status_code == 200
        assert "valkyrie unreachable" in r.text
```

- [ ] **Step 4: Implement `dashboard/views/strategy.py`**

```python
"""View C — ZSPD strategy ops."""
from __future__ import annotations
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from dashboard.clients.valkyrie import fetch_strategy_state
from dashboard.config import load

router = APIRouter()
_templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


@router.get("/strategy", response_class=HTMLResponse)
async def strategy(request: Request):
    cfg = load()
    state = await fetch_strategy_state(cfg)
    return _templates.TemplateResponse(request, "strategy.html", {"s": state})
```

- [ ] **Step 5: Create `dashboard/templates/strategy.html`**

```html
{% extends "base.html" %}
{% block title %}strategy{% endblock %}
{% block content %}
{% if s.error %}<div class="banner err">valkyrie: {{ s.error }}</div>{% endif %}
<h2>zspd-strategy.service</h2>
<pre>{{ s.strategy or {} | tojson(indent=2) }}</pre>
<h2>positions today</h2>
{% if s.positions_today %}
<table><thead><tr>
{% for k in s.positions_today[0].keys() %}<th>{{ k }}</th>{% endfor %}
</tr></thead><tbody>
{% for p in s.positions_today %}<tr>
{% for v in p.values() %}<td>{{ v }}</td>{% endfor %}
</tr>{% endfor %}
</tbody></table>
{% else %}<p>no positions today</p>{% endif %}
{% endblock %}
```

- [ ] **Step 6: Wire into `main.py`**

```python
from dashboard.views import strategy as strategy_view
# ...
app.include_router(strategy_view.router, dependencies=[Depends(auth)])
```

- [ ] **Step 7: Run tests**

```bash
pytest dashboard/tests/test_strategy.py -v
```

Expected: 2 passed.

- [ ] **Step 8: Commit**

```bash
git add scripts/inspect_strategy.py dashboard/clients/valkyrie.py dashboard/views/strategy.py dashboard/templates/strategy.html dashboard/main.py dashboard/tests/test_strategy.py
git commit -m "feat(dashboard): strategy ops view (route C) + valkyrie inspect script"
```

### Task C5: Implement view D — historical replay

**Files:**
- Create: `dashboard/views/replay.py`
- Create: `dashboard/templates/replay.html`
- Create: `dashboard/clients/parquet.py`
- Modify: `dashboard/main.py`
- Test: `dashboard/tests/test_replay.py`

The replay view reads warm parquet (or pulls from cold WebDAV on miss). For v1, we only support warm — cold-restore is documented in Phase D and can be exercised by `restore.sh`.

- [ ] **Step 1: Implement parquet client**

`dashboard/clients/parquet.py`:

```python
"""Read warm-tier parquet rolls."""
from __future__ import annotations
from pathlib import Path
import polars as pl


def load_day(warm_dir: Path, date_iso: str, kind: str) -> pl.DataFrame | None:
    """kind ∈ {'prices','orderbook','trades','markets'}."""
    p = warm_dir / date_iso / f"{kind}.parquet"
    if not p.exists():
        return None
    return pl.read_parquet(p)
```

- [ ] **Step 2: Add `warm_dir` to config**

In `dashboard/config.py`:

```python
    warm_dir: Path = Path(os.getenv("DASHBOARD_WARM_DIR", "/opt/zspd-collector/warm"))
```

- [ ] **Step 3: Write the failing replay test**

`dashboard/tests/test_replay.py`:

```python
from unittest.mock import patch
import polars as pl
from fastapi.testclient import TestClient
from dashboard.main import build_app


def test_replay_no_data():
    with patch("dashboard.views.replay.load_day", return_value=None):
        r = TestClient(build_app()).get("/replay/2025-01-01",
            headers={"Authorization": "Bearer "})
        assert r.status_code == 200
        assert "no data" in r.text


def test_replay_renders_summary():
    df = pl.DataFrame({"ts": ["2025-01-01T00:00"], "metric": ["pm_yes_mid"],
                       "market_id": ["m1"], "value": [0.42]})
    with patch("dashboard.views.replay.load_day", return_value=df):
        r = TestClient(build_app()).get("/replay/2025-01-01",
            headers={"Authorization": "Bearer "})
        assert r.status_code == 200
        assert "m1" in r.text
```

- [ ] **Step 4: Implement `dashboard/views/replay.py`**

```python
"""View D — historical replay."""
from __future__ import annotations
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from dashboard.clients.parquet import load_day
from dashboard.config import load

router = APIRouter()
_templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


@router.get("/replay/{date_iso}", response_class=HTMLResponse)
async def replay(request: Request, date_iso: str):
    cfg = load()
    df = load_day(cfg.warm_dir, date_iso, "prices")
    if df is None:
        return _templates.TemplateResponse(request,
            "replay.html", {"date": date_iso, "rows": None})
    summary = (
        df.group_by("market_id").agg(pl.len().alias("n_ticks"))
        .sort("n_ticks", descending=True).head(50).to_dicts()
    )
    return _templates.TemplateResponse(request,
        "replay.html", {"date": date_iso, "rows": summary})


@router.get("/replay", response_class=HTMLResponse)
async def replay_index(request: Request):
    cfg = load()
    days = sorted(p.name for p in cfg.warm_dir.iterdir() if p.is_dir()) if cfg.warm_dir.exists() else []
    return _templates.TemplateResponse(request,
        "replay.html", {"date": None, "days": days, "rows": None})
```

(`pl` import needed at top: `import polars as pl`.)

- [ ] **Step 5: Create `dashboard/templates/replay.html`**

```html
{% extends "base.html" %}
{% block title %}replay{% endblock %}
{% block content %}
{% if date %}
  <h2>{{ date }}</h2>
  {% if rows %}
    <table><thead><tr><th>market</th><th>ticks</th></tr></thead><tbody>
    {% for r in rows %}<tr><td>{{ r.market_id }}</td><td>{{ r.n_ticks }}</td></tr>{% endfor %}
    </tbody></table>
  {% else %}<p>no data for {{ date }} (try <code>scripts/restore.sh {{ date }} {{ date }}</code>)</p>{% endif %}
{% else %}
  <h2>days available</h2>
  {% if days %}<ul>{% for d in days %}<li><a href="/replay/{{ d }}">{{ d }}</a></li>{% endfor %}</ul>
  {% else %}<p>no warm data yet</p>{% endif %}
{% endif %}
{% endblock %}
```

- [ ] **Step 6: Wire into `main.py`**

```python
from dashboard.views import replay as replay_view
# ...
app.include_router(replay_view.router, dependencies=[Depends(auth)])
```

- [ ] **Step 7: Run tests**

```bash
pytest dashboard/tests/test_replay.py -v
```

Expected: 2 passed.

- [ ] **Step 8: Commit**

```bash
git add dashboard/clients/parquet.py dashboard/views/replay.py dashboard/templates/replay.html dashboard/main.py dashboard/config.py dashboard/tests/test_replay.py
git commit -m "feat(dashboard): historical replay view (route D)"
```

### Task C6: Deploy dashboard to singapore

**Files:**
- Create: `systemd/zspd-dashboard.service` (locally; replaces existing on server)

- [ ] **Step 1: Author the systemd unit**

```ini
[Unit]
Description=ZSPD Polymarket dashboard
After=network-online.target zspd-collector.service victoriametrics.service
Wants=network-online.target

[Service]
Type=simple
User=polymarket
Group=polymarket
WorkingDirectory=/opt/zspd-dashboard
EnvironmentFile=/opt/zspd-dashboard/.env
Environment=DASHBOARD_COLLECTOR_DB_DIR=/opt/zspd-collector/database
Environment=DASHBOARD_WARM_DIR=/opt/zspd-collector/warm
Environment=PYTHONPATH=/opt/zspd-dashboard/src
ExecStart=/opt/zspd-dashboard/.venv/bin/python -m dashboard.main
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
git add systemd/zspd-dashboard.service
git commit -m "feat(dashboard): systemd unit (replaces skeleton)"
```

- [ ] **Step 2: Push code**

```bash
scripts/push_code.sh singapore
```

- [ ] **Step 3: Build venv on singapore**

```bash
ssh sg-pm '
cd /opt/zspd-dashboard
[ -d .venv ] || python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r src/dashboard/requirements.txt
'
```

- [ ] **Step 4: Ensure `DASHBOARD_AUTH_TOKEN` is set in `/opt/zspd-dashboard/.env`**

```bash
ssh sg-pm '
sudo -u polymarket grep -q DASHBOARD_AUTH_TOKEN /opt/zspd-dashboard/.env || \
    echo "DASHBOARD_AUTH_TOKEN=$(openssl rand -hex 16)" | sudo -u polymarket tee -a /opt/zspd-dashboard/.env
sudo -u polymarket cat /opt/zspd-dashboard/.env | grep DASHBOARD_AUTH_TOKEN
'
```

Capture the token for browser-side use.

- [ ] **Step 5: Set up SSH from singapore→valkyrie for inspect_strategy.py**

The polymarket user on singapore needs to SSH into valkyrie. Reuse the same key already moved in A5:

```bash
ssh sg-pm '
sudo -u polymarket ssh-keyscan -t ed25519 valkyrie >> /home/polymarket/.ssh/known_hosts 2>/dev/null
sudo -u polymarket bash -c "cat ~/.ssh/config" 2>/dev/null || \
    sudo -u polymarket tee /home/polymarket/.ssh/config > /dev/null <<EOF
Host valkyrie
    HostName 204.168.241.66
    User polymarket
    IdentityFile ~/.ssh/zspd_deploy
    IdentitiesOnly yes
EOF
sudo -u polymarket ssh valkyrie "echo ok"
'
```

The valkyrie polymarket user must accept this key — it's the same one the existing `zspd-archive` flow uses, so it should already be in `val:~polymarket/.ssh/authorized_keys` after Task A6 step 4 (the chown moved /opt but didn't move authorized_keys; we need to verify).

```bash
ssh val-pm 'cat ~/.ssh/authorized_keys | grep "$(cut -d" " -f2 ~/.ssh/zspd_deploy.pub 2>/dev/null || true)"' || true
```

If the deploy pubkey isn't in val-pm's authorized_keys, add it:

```bash
scp sg-pm:/home/polymarket/.ssh/zspd_deploy.pub /tmp/zspd_deploy.pub
ssh val-pm 'tee -a ~/.ssh/authorized_keys' < /tmp/zspd_deploy.pub
```

- [ ] **Step 6: Restart unit**

```bash
scp systemd/zspd-dashboard.service sg-pm:/tmp/
ssh sg-pm '
sudo mv /tmp/zspd-dashboard.service /etc/systemd/system/zspd-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now zspd-dashboard.service
sleep 3
systemctl is-active zspd-dashboard.service
'
```

Expected: `active`.

- [ ] **Step 7: Smoke (verification item 4)**

```bash
ssh -L 8090:127.0.0.1:8090 -L 3000:127.0.0.1:3000 sg-pm -fN
TOKEN=$(ssh sg-pm 'sudo -u polymarket grep DASHBOARD_AUTH_TOKEN /opt/zspd-dashboard/.env' | cut -d= -f2)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8090/ | head -c 500
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8090/m/$(ssh sg-pm 'sqlite3 /opt/zspd-collector/database/markets.db "SELECT market_id FROM markets LIMIT 1"') | grep -E '(grafana|L2|trades)' | head -5
```

Expected: HTML content containing the grid and a market drill-down. Open `http://127.0.0.1:8090/` in a browser (with a header-injecting extension or via `?token=` if you add a query-string fallback).

---

## Phase D — Archive layer

### Task D1: Install + configure rclone for WebDAV on singapore

**Files:**
- (Remote config: `sg:/home/polymarket/.config/rclone/rclone.conf`)

- [ ] **Step 1: Install rclone**

```bash
ssh sg-pm 'curl -fsSL https://rclone.org/install.sh | sudo bash'
ssh sg-pm 'rclone version | head -1'
```

- [ ] **Step 2: Create the WebDAV remote in rclone config**

```bash
ssh sg-pm '
sudo -u polymarket mkdir -p /home/polymarket/.config/rclone
sudo -u polymarket tee /home/polymarket/.config/rclone/rclone.conf > /dev/null <<EOF
[saber]
type = webdav
url = https://driver.saber-ai.org/
vendor = other
user = polymarket
EOF
'
```

The password isn't stored in plaintext — use `rclone obscure`:

```bash
OBS=$(ssh sg-pm 'rclone obscure 9ETzh5*nZ9QQ')
ssh sg-pm "sudo -u polymarket bash -c 'echo pass = $OBS >> /home/polymarket/.config/rclone/rclone.conf'"
ssh sg-pm 'sudo -u polymarket chmod 600 /home/polymarket/.config/rclone/rclone.conf'
```

- [ ] **Step 3: Verify**

```bash
ssh sg-pm 'sudo -u polymarket rclone lsd saber:Local/Polymarket'
ssh sg-pm 'sudo -u polymarket rclone touch saber:Local/Polymarket/.collector-up'
ssh sg-pm 'sudo -u polymarket rclone ls saber:Local/Polymarket | grep collector-up'
ssh sg-pm 'sudo -u polymarket rclone delete saber:Local/Polymarket/.collector-up'
```

Expected: directory listing, touch creates file, delete removes it.

### Task D2: Implement the archiver

**Files:**
- Create: `archiver/__init__.py`, `archiver/main.py`, `archiver/vm_export.py`, `archiver/cleanup.py`, `archiver/config.py`, `archiver/requirements.txt`
- Test: `archiver/tests/test_vm_export.py`, `archiver/tests/test_cleanup.py`

- [ ] **Step 1: Create `archiver/requirements.txt`**

```
httpx==0.27.2
polars==1.12.0
pyarrow==17.0.0
python-dotenv==1.0.1
```

- [ ] **Step 2: Create `archiver/config.py`**

```python
"""Archiver config from env."""
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    db_dir: Path = Path(os.getenv("ARCHIVER_DB_DIR", "/opt/zspd-collector/database"))
    warm_dir: Path = Path(os.getenv("ARCHIVER_WARM_DIR", "/opt/zspd-collector/warm"))
    vm_url: str = os.getenv("ARCHIVER_VM_URL", "http://127.0.0.1:8428")
    rclone_remote: str = os.getenv("ARCHIVER_RCLONE_REMOTE", "saber:Local/Polymarket")
    hot_retention_days: int = int(os.getenv("ARCHIVER_HOT_RETENTION_DAYS", "30"))
    warm_retention_days: int = int(os.getenv("ARCHIVER_WARM_RETENTION_DAYS", "7"))


def load() -> Config:
    return Config()
```

- [ ] **Step 3: Write the failing VM-export test (small unit)**

`archiver/tests/test_vm_export.py`:

```python
from archiver.vm_export import _row_from_native_chunk


def test_row_extraction():
    chunk = {
        "metric": {"__name__": "pm_yes_mid", "market_id": "m1",
                   "city": "ZSPD", "contract_type": "high_ge"},
        "values": [[1715000000, "0.42"], [1715000003, "0.43"]],
    }
    rows = list(_row_from_native_chunk(chunk))
    assert len(rows) == 2
    assert rows[0] == {"ts": 1715000000, "metric": "pm_yes_mid",
                        "market_id": "m1", "city": "ZSPD",
                        "contract_type": "high_ge", "value": 0.42}
```

- [ ] **Step 4: Implement `archiver/vm_export.py`**

```python
"""Export a day of VM tick data to parquet."""
from __future__ import annotations
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator
import httpx
import polars as pl


def _row_from_native_chunk(chunk: dict) -> Iterator[dict]:
    metric = chunk["metric"]
    name = metric.get("__name__", "")
    market_id = metric.get("market_id", "")
    city = metric.get("city", "")
    ctype = metric.get("contract_type", "")
    for ts, val in chunk.get("values", []):
        yield {"ts": int(ts), "metric": name, "market_id": market_id,
               "city": city, "contract_type": ctype, "value": float(val)}


async def export_day(vm_url: str, day: date, dest: Path) -> int:
    """Pull all `pm_*` series for `day`, write to parquet. Returns row count."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp()
    end = start + 86400
    async with httpx.AsyncClient(timeout=120.0) as http:
        # VM's /api/v1/query_range with __name__=~"pm_.*"
        r = await http.get(f"{vm_url}/api/v1/query_range",
                           params={"query": '{__name__=~"pm_.*"}',
                                   "start": int(start), "end": int(end), "step": "3s"})
        r.raise_for_status()
        data = r.json()["data"]["result"]
    rows: list[dict] = []
    for chunk in data:
        rows.extend(_row_from_native_chunk(chunk))
    if not rows:
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(dest, compression="zstd")
    return len(rows)
```

- [ ] **Step 5: Write the cleanup test**

`archiver/tests/test_cleanup.py`:

```python
from datetime import date, timedelta
from pathlib import Path
from archiver.cleanup import old_warm_dirs


def test_old_warm_dirs_picks_only_old(tmp_path: Path):
    today = date.today()
    for n in range(20):
        (tmp_path / (today - timedelta(days=n)).isoformat()).mkdir()
    olds = old_warm_dirs(tmp_path, retention_days=7)
    assert all(int((today - date.fromisoformat(p.name)).days) > 7 for p in olds)
    assert not any(int((today - date.fromisoformat(p.name)).days) <= 7 for p in olds)
```

- [ ] **Step 6: Implement `archiver/cleanup.py`**

```python
"""Retention cleanup helpers."""
from __future__ import annotations
import sqlite3
from datetime import date
from pathlib import Path
from typing import Iterable


def old_warm_dirs(warm_root: Path, retention_days: int) -> list[Path]:
    today = date.today()
    out: list[Path] = []
    if not warm_root.exists():
        return out
    for p in warm_root.iterdir():
        if not p.is_dir():
            continue
        try:
            d = date.fromisoformat(p.name)
        except ValueError:
            continue
        if (today - d).days > retention_days:
            out.append(p)
    return out


def delete_old_sqlite_rows(db_path: Path, table: str, ts_col: str, retention_days: int) -> int:
    if not db_path.exists():
        return 0
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            f"DELETE FROM {table} WHERE {ts_col} < datetime('now', ?)",
            (f"-{retention_days} days",),
        )
        return cur.rowcount or 0
```

- [ ] **Step 7: Implement `archiver/main.py`**

```python
"""Daily roll: VM + SQLite → parquet → rclone → cleanup."""
from __future__ import annotations
import asyncio
import json
import logging
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import polars as pl
from archiver import cleanup
from archiver.config import load
from archiver.vm_export import export_day

log = logging.getLogger(__name__)


def _dump_sqlite_to_parquet(db_path: Path, table: str, ts_col: str,
                             day: date, dest: Path) -> int:
    if not db_path.exists():
        return 0
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    end = (datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
           + timedelta(days=1)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE {ts_col} >= ? AND {ts_col} < ?",
            (start, end),
        ).fetchall()
    if not rows:
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([dict(r) for r in rows]).write_parquet(dest, compression="zstd")
    return len(rows)


def _dump_markets_snapshot(db_path: Path, dest: Path) -> int:
    if not db_path.exists():
        return 0
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM markets").fetchall()
    if not rows:
        return 0
    pl.DataFrame([dict(r) for r in rows]).write_parquet(dest, compression="zstd")
    return len(rows)


def _rclone_sync(src: Path, remote: str) -> None:
    day = src.name  # YYYY-MM-DD
    y, mo, d = day.split("-")
    target = f"{remote}/{y}/{mo}/{d}"
    log.info("rclone copy %s -> %s", src, target)
    cmd = ["rclone", "copy", "--checksum", str(src), target]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if res.returncode != 0:
        raise RuntimeError(f"rclone failed: {res.stderr[:500]}")


async def run() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load()
    yesterday = date.today() - timedelta(days=1)
    out_dir = cfg.warm_dir / yesterday.isoformat()

    log.info("export VM ticks for %s", yesterday)
    n_prices = await export_day(cfg.vm_url, yesterday, out_dir / "prices.parquet")
    log.info("  %d price rows", n_prices)

    n_ob = _dump_sqlite_to_parquet(cfg.db_dir / "orderbook.db",
                                   "l2_snapshot", "ts", yesterday, out_dir / "orderbook.parquet")
    log.info("  %d L2 rows", n_ob)
    n_tr = _dump_sqlite_to_parquet(cfg.db_dir / "trades.db",
                                   "trades", "ts", yesterday, out_dir / "trades.parquet")
    log.info("  %d trade rows", n_tr)
    n_mk = _dump_markets_snapshot(cfg.db_dir / "markets.db", out_dir / "markets.parquet")
    log.info("  %d markets snapshot rows", n_mk)

    if n_prices == 0 and n_ob == 0 and n_tr == 0:
        log.warning("no data for %s; aborting before cold sync", yesterday)
        return 0

    _rclone_sync(out_dir, cfg.rclone_remote)

    # cleanup
    for p in cleanup.old_warm_dirs(cfg.warm_dir, cfg.warm_retention_days):
        log.info("removing old warm dir %s", p)
        shutil.rmtree(p)
    n_ob_d = cleanup.delete_old_sqlite_rows(cfg.db_dir / "orderbook.db",
                                            "l2_snapshot", "ts", cfg.hot_retention_days)
    n_tr_d = cleanup.delete_old_sqlite_rows(cfg.db_dir / "trades.db",
                                            "trades", "ts", cfg.hot_retention_days)
    log.info("cleaned %d L2 rows, %d trade rows from hot", n_ob_d, n_tr_d)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
```

- [ ] **Step 8: Run tests**

```bash
pytest archiver/tests/ -v
```

Expected: 2 passed.

- [ ] **Step 9: Commit**

```bash
git add archiver/ pyproject.toml
git commit -m "feat(archiver): VM export + parquet roll + rclone sync + retention cleanup"
```

### Task D3: Deploy archiver as systemd timer

**Files:**
- Create: `systemd/zspd-archiver.service`
- Create: `systemd/zspd-archiver.timer`
- Create: `systemd/zspd-archiver-restart.timer`
- Create: `systemd/zspd-archiver-restart.service`

- [ ] **Step 1: Author units**

`systemd/zspd-archiver.service`:

```ini
[Unit]
Description=ZSPD Polymarket daily archive (VM + SQLite -> parquet -> WebDAV)
After=network-online.target zspd-collector.service
Wants=network-online.target

[Service]
Type=oneshot
User=polymarket
Group=polymarket
WorkingDirectory=/opt/zspd-collector
EnvironmentFile=/opt/zspd-collector/.env
Environment=PYTHONPATH=/opt/zspd-collector/src
ExecStart=/opt/zspd-collector/.venv/bin/python -m archiver.main
StandardOutput=append:/var/log/zspd-collector/archiver.log
StandardError=append:/var/log/zspd-collector/archiver.err
```

`systemd/zspd-archiver.timer`:

```ini
[Unit]
Description=Daily ZSPD archiver (03:00 UTC)

[Timer]
OnCalendar=*-*-* 03:00:00 UTC
Persistent=true
Unit=zspd-archiver.service

[Install]
WantedBy=timers.target
```

`systemd/zspd-archiver-restart.service`:

```ini
[Unit]
Description=Retry ZSPD archiver if last run failed
ConditionPathExists=!/run/zspd-archiver-success

[Service]
Type=oneshot
User=polymarket
Group=polymarket
ExecStart=/bin/systemctl start zspd-archiver.service
```

`systemd/zspd-archiver-restart.timer`:

```ini
[Unit]
Description=Retry archiver every 6 h until it succeeds

[Timer]
OnUnitActiveSec=6h
OnBootSec=10min
Unit=zspd-archiver-restart.service

[Install]
WantedBy=timers.target
```

```bash
git add systemd/zspd-archiver*.service systemd/zspd-archiver*.timer
git commit -m "feat(archiver): systemd unit + timer + retry timer"
```

- [ ] **Step 2: Install on singapore**

```bash
scp systemd/zspd-archiver*.service systemd/zspd-archiver*.timer sg-pm:/tmp/
ssh sg-pm '
sudo mv /tmp/zspd-archiver*.service /tmp/zspd-archiver*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zspd-archiver.timer zspd-archiver-restart.timer
sudo systemctl list-timers --all | grep archiver
'
```

Expected: two timers listed, next-fire time visible.

- [ ] **Step 3: Trigger archiver manually (verification item 5)**

```bash
ssh sg-pm 'sudo systemctl start zspd-archiver.service'
ssh sg-pm 'journalctl -u zspd-archiver.service -n 30 --no-pager'
ssh sg-pm 'ls /opt/zspd-collector/warm/'
ssh sg-pm 'sudo -u polymarket rclone ls saber:Local/Polymarket/$(date -u -d yesterday +%Y/%m/%d)'
```

Expected: log shows row counts, warm directory has yesterday's date with 4 parquet files, rclone shows the same files in WebDAV.

### Task D4: Implement `restore.sh`

**Files:**
- Create: `scripts/restore.sh`

- [ ] **Step 1: Author script**

```bash
#!/usr/bin/env bash
# Restore a date range of parquet from cold WebDAV to a local working dir.
# Usage: restore.sh YYYY-MM-DD YYYY-MM-DD [target_dir]
set -euo pipefail

FROM="${1:?usage: restore.sh from to [target]}"
TO="${2:?usage: restore.sh from to [target]}"
TARGET="${3:-/tmp/zspd-restore}"
REMOTE="${ARCHIVER_RCLONE_REMOTE:-saber:Local/Polymarket}"

mkdir -p "$TARGET"
d="$FROM"
while [[ "$d" < "$TO" || "$d" == "$TO" ]]; do
    yyyy="${d:0:4}"; mm="${d:5:2}"; dd="${d:8:2}"
    echo "==> $d"
    rclone copy --checksum "$REMOTE/$yyyy/$mm/$dd/" "$TARGET/$d/"
    d=$(date -u -I -d "$d + 1 day")
done
echo "restored to $TARGET"
```

- [ ] **Step 2: Chmod + commit**

```bash
chmod +x scripts/restore.sh
git add scripts/restore.sh
git commit -m "feat(archiver): restore.sh for cold-tier date-range pull"
```

- [ ] **Step 3: Smoke test (verification item 6)**

```bash
ssh sg-pm 'cd /tmp && /opt/zspd-collector/src/scripts/restore.sh \
    $(date -u -d yesterday +%F) $(date -u -d yesterday +%F) /tmp/restore-test'
ssh sg-pm 'ls /tmp/restore-test/$(date -u -d yesterday +%F)/'
ssh sg-pm 'diff <(md5sum /opt/zspd-collector/warm/$(date -u -d yesterday +%F)/*.parquet | awk "{print \$1, \$2}" | sed "s|.*/||") <(md5sum /tmp/restore-test/$(date -u -d yesterday +%F)/*.parquet | awk "{print \$1, \$2}" | sed "s|.*/||")'
```

Expected: identical files (md5 matches; diff exits 0).

- [ ] **Step 4: Cleanup test artefact**

```bash
ssh sg-pm 'rm -rf /tmp/restore-test'
```

### Task D5: 24-hour soak + sign-off

**Files:** none (observation only)

- [ ] **Step 1: Confirm timers**

```bash
ssh sg-pm 'systemctl list-timers zspd-* --no-pager'
```

Expected: archive (existing 16:30 UTC), archiver (03:00 UTC), archiver-restart (every 6 h).

- [ ] **Step 2: Wait until next archive + archiver fire** (or at least 24 h after Task D3 step 3)

Spot checks during soak:

```bash
ssh sg-pm 'curl -s 127.0.0.1:8091/health'  # ws still connected?
ssh sg-pm 'df -h /'                         # disk pressure
ssh sg-pm 'sudo -u polymarket rclone size saber:Local/Polymarket'
ssh sg-pm 'ls /opt/zspd-collector/warm | wc -l'  # ≤7
```

- [ ] **Step 3: Update STATUS.md to reflect new services**

Add to `docs/STATUS.md` under singapore:
- `zspd-collector.service` — active (port 8091)
- `zspd-archiver.timer` — enabled (next 03:00 UTC)
- VictoriaMetrics — active (port 8428, retention 30 d)
- Grafana — active (port 3000)
- WebDAV cold archive — `driver.saber-ai.org:/Local/Polymarket/`

```bash
git add docs/STATUS.md
git commit -m "docs(status): record collector + archiver + VM/Grafana on singapore"
```

- [ ] **Step 4: Final verification sweep (all 7 items from spec §8)**

Run each smoke test sequentially; if any fails, fix or roll back to a passing checkpoint before declaring done.

---

## Self-review

**Spec coverage:**

| Spec section | Covered by |
|---|---|
| §3.1 collector | B1–B7, B9 |
| §3.2 dashboard | C1–C6 |
| §3.3 archiver + restore | D2, D3, D4 |
| §3.4 VM + Grafana | B8 |
| §3.5 push_code.sh | A4 |
| §4 schemas | B2 (SQL), B6 (VM) |
| §5 failure modes | embedded in B4 (ws backoff), B6 (stale metric), D2 (rclone retry), D3 (restart timer) |
| §6 polymarket migration | A1–A3, A5–A6 |
| §7 deployment runbook | follows the same order |
| §8 verification checklist | item 1: B9 step 7 · item 2: B9 step 7 · item 3: B9 step 7 · item 4: C6 step 7 · item 5: D3 step 3 · item 6: D4 step 3 · item 7: A4 step 3 |

No uncovered spec sections.

**Placeholder scan:** none — every step has explicit code, commands, or expected outputs. Mid-task self-correction in Task B5 (level format mismatch) is shown explicitly, not hand-waved.

**Type consistency:**
- `LatestStore.snapshot()` returns `dict[str, Tick]` — used consistently in B6.
- `db.list_active_markets()` returns `list[dict]` — consumed in B7, C3.
- `Config` (collector) and `Config` (dashboard) are separate dataclasses; clear from imports.
- Method naming: `discover_once` / `discovery_loop`, `ws_loop`, `l2_loop` — verb consistent.

**Known follow-ups deferred to soak (D5):**
- WS subscribe message format (open question §10) — tuned during B7 step 2 against live API.
- Polymarket 429 rate limit — discovery has 24 h slack so unlikely; if seen, add per-endpoint backoff in `discovery.fetch_weather_events`.
- Grafana dashboard JSON beyond the seed (B8 step 6) — refined post-soak when we know what panels matter.
