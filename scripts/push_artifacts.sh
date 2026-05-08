#!/usr/bin/env bash
# scripts/push_artifacts.sh
#
# Push freshly trained ml/artifacts/<version>/ to valkyrie's hot path
# AND mirror to singapore's archive (so it's reproducible later).
#
# Env vars:
#   ZSPD_VERSION         model version to push   (default: v5)
#   ZSPD_TRADER_HOST     trader ssh alias        (default: valkyrie)
#   ZSPD_TRADER_PATH     trader artifacts root   (default: /opt/zspd-trader/src/ml/artifacts)
#   ZSPD_ARCHIVE_HOST    archive ssh alias       (default: singapore)
#   ZSPD_ARCHIVE_PATH    archive artifacts root  (default: /opt/zspd-trainer/src/ml/artifacts)
#   ZSPD_SKIP_ARCHIVE    set 1 to only push to trader, not archive
#
# Usage:
#   scripts/push_artifacts.sh                      # push v5 to both
#   ZSPD_VERSION=v3 scripts/push_artifacts.sh      # push v3
#   ZSPD_SKIP_ARCHIVE=1 scripts/push_artifacts.sh  # trader only

set -euo pipefail

VERSION=${ZSPD_VERSION:-v5}
TRADER_HOST=${ZSPD_TRADER_HOST:-valkyrie}
TRADER_PATH=${ZSPD_TRADER_PATH:-/opt/zspd-trader/src/ml/artifacts}
ARCHIVE_HOST=${ZSPD_ARCHIVE_HOST:-singapore}
ARCHIVE_PATH=${ZSPD_ARCHIVE_PATH:-/opt/zspd-trainer/src/ml/artifacts}

LOCAL_DIR="./src/ml/artifacts/${VERSION}"
[ -d "$LOCAL_DIR" ] || LOCAL_DIR="./ml/artifacts/${VERSION}"
if [ ! -d "$LOCAL_DIR" ]; then
    echo "  ERROR: cannot find ${VERSION} artifacts under ./src/ml/artifacts/ or ./ml/artifacts/" >&2
    exit 1
fi

echo "── pushing artifacts ──"
echo "  source: ${LOCAL_DIR}"
echo "  → trader  : ${TRADER_HOST}:${TRADER_PATH}/${VERSION}/"
if [ -z "${ZSPD_SKIP_ARCHIVE:-}" ]; then
    echo "  → archive : ${ARCHIVE_HOST}:${ARCHIVE_PATH}/${VERSION}/"
fi
echo ""

# Trader push (HOT — strategy will pick it up on next slot)
rsync -avz --delete --timeout=60 \
    "${LOCAL_DIR}/" \
    "${TRADER_HOST}:${TRADER_PATH}/${VERSION}/"

# Archive push (COLD — for reproducibility / backup)
if [ -z "${ZSPD_SKIP_ARCHIVE:-}" ]; then
    rsync -avz --delete --timeout=60 \
        "${LOCAL_DIR}/" \
        "${ARCHIVE_HOST}:${ARCHIVE_PATH}/${VERSION}/"
fi

# Show what trader has now
echo ""
echo "── trader confirmation ──"
ssh "${TRADER_HOST}" "ls -la ${TRADER_PATH}/${VERSION}/ | tail -10"

echo ""
echo "── done ──"
echo "  trader now serves ${VERSION} from the new artifacts."
echo "  to verify inference still works:"
echo "    ssh ${TRADER_HOST} 'cd /opt/zspd-trader/src && /opt/zspd-trader/.venv/bin/python -m ml.pipeline predict --version ${VERSION}'"
