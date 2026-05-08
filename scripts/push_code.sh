#!/usr/bin/env bash
# Push the current repo to val and/or singapore via rsync.
# Usage: scripts/push_code.sh val|singapore|both [--dry-run] [--restart]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SSH_CFG="$REPO_ROOT/ssh/config"
SSH=(ssh -F "$SSH_CFG")
RSYNC_SSH="ssh -F $SSH_CFG"

TARGETS=()
DRY_RUN=()
RESTART=0

for arg in "$@"; do
    case "$arg" in
        val|valkyrie)         TARGETS+=("val") ;;
        sg|singapore)         TARGETS+=("singapore") ;;
        both)                 TARGETS+=("val" "singapore") ;;
        --dry-run|-n)         DRY_RUN=("--dry-run" "--itemize-changes") ;;
        --restart)            RESTART=1 ;;
        -h|--help)
            cat <<EOF
Usage: $0 val|singapore|both [--dry-run] [--restart]

Pushes the current repo (excluding .env, .venv, database, ml/artifacts, etc.)
to the deploy paths on val and/or singapore. Uses ssh/config (project-local).

Targets:
  val        => /opt/zspd-trader/src/
  singapore  => /opt/zspd-trainer/src/
                /opt/zspd-collector/src/  (if dir exists)
                /opt/zspd-dashboard/src/  (if dir exists)
EOF
            exit 0
            ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done
[[ ${#TARGETS[@]} -gt 0 ]] || {
    echo "usage: push_code.sh val|singapore|both [--dry-run] [--restart]" >&2
    exit 2
}

EXCLUDES=(
    --exclude=.env --exclude='.env.*'
    --exclude=.venv/ --exclude=__pycache__/ --exclude='*.pyc'
    --exclude=.git/ --exclude=.gitignore
    --exclude=database/ --exclude=ml/artifacts/
    --exclude=docs/superpowers/ --exclude='*.parquet'
    --exclude=ssh/ --exclude=.pytest_cache/
    --exclude=.venv-dev/ --exclude=node_modules/
)

remote_path_exists() {
    local host="$1" path="$2"
    "${SSH[@]}" "$host" "test -d '$path'" 2>/dev/null
}

push_to() {
    local target="$1"
    local host paths=()
    case "$target" in
        val)
            host=val-pm
            paths=(/opt/zspd-trader/src/)
            ;;
        singapore)
            host=sg-pm
            for p in /opt/zspd-trainer/src/ /opt/zspd-collector/src/ /opt/zspd-dashboard/src/; do
                if remote_path_exists "$host" "$(dirname "$p")"; then
                    paths+=("$p")
                else
                    echo "  skip $p (parent dir missing on $host)" >&2
                fi
            done
            ;;
    esac
    for dest in "${paths[@]}"; do
        echo "==> $target  rsync -> $dest"
        rsync -az --delete -e "$RSYNC_SSH" \
            "${DRY_RUN[@]}" "${EXCLUDES[@]}" \
            "$REPO_ROOT/" "$host:$dest"
    done
    if [[ $RESTART -eq 1 && ${#DRY_RUN[@]} -eq 0 ]]; then
        case "$target" in
            val)
                "${SSH[@]}" val-pm 'sudo systemctl restart clob-proxy.service zspd-strategy.service 2>/dev/null || true'
                ;;
            singapore)
                "${SSH[@]}" sg-pm 'sudo systemctl restart zspd-collector.service zspd-dashboard.service 2>/dev/null || true'
                ;;
        esac
    fi
}

for t in "${TARGETS[@]}"; do
    push_to "$t"
done
