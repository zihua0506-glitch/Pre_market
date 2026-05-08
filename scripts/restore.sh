#!/usr/bin/env bash
# Restore a date range of parquet from cold WebDAV to a local working dir.
# Usage: restore.sh YYYY-MM-DD YYYY-MM-DD [target_dir]
set -eo pipefail

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
