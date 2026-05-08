#!/usr/bin/env bash
# scripts/pull_training_data.sh
#
# Pull the canonical historical DBs from singapore (the archive host) into
# this repo's src/database/, so you can train v5 locally or on a GPU server.
#
# After training:
#   scripts/push_artifacts.sh        # push fresh ml/artifacts/v5/ to valkyrie
#
# Env vars (with defaults):
#   ZSPD_ARCHIVE_HOST   ssh alias of the archive host       (default: singapore)
#   ZSPD_ARCHIVE_PATH   remote path to its src/database/    (default: /opt/zspd-trainer/src/database)
#   ZSPD_LOCAL_PATH     local target                        (default: ./src/database)
#
# Usage:
#   scripts/pull_training_data.sh                     # default destination
#   scripts/pull_training_data.sh /path/to/database   # custom destination

set -euo pipefail

REMOTE_HOST=${ZSPD_ARCHIVE_HOST:-singapore}
REMOTE_PATH=${ZSPD_ARCHIVE_PATH:-/opt/zspd-trainer/src/database}
LOCAL_PATH=${1:-${ZSPD_LOCAL_PATH:-./src/database}}

mkdir -p "$LOCAL_PATH"

echo "── pulling training data ──"
echo "  remote: ${REMOTE_HOST}:${REMOTE_PATH}"
echo "  local : ${LOCAL_PATH}"
echo ""

# The five DBs the training pipeline needs. data.db (Polymarket prices) is
# only needed for backtest, so we pull it too — disk is cheap on a trainer.
FILES=(
  standard.db
  weather_om.db
  daily_labels.db
  iem_asos_zspd.db
  intraday_features.parquet
  data.db
)

for f in "${FILES[@]}"; do
  rsync -avzP --timeout=60 \
    "${REMOTE_HOST}:${REMOTE_PATH}/${f}" \
    "${LOCAL_PATH}/${f}"
done

echo ""
echo "── done ──"
echo "  next: python -m ml.pipeline train --version v5"
echo "        python -m ml.pipeline evaluate --version v5"
echo "        scripts/push_artifacts.sh"
