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

# Scoped NOPASSWD sudo: only systemctl + journalctl.
# Anything else (apt, useradd, mount, ...) requires a password.
cat > /etc/sudoers.d/polymarket-zspd <<'EOF'
polymarket ALL=(root) NOPASSWD: /bin/systemctl, /usr/bin/systemctl, /bin/journalctl, /usr/bin/journalctl
EOF
chmod 440 /etc/sudoers.d/polymarket-zspd
visudo -c -f /etc/sudoers.d/polymarket-zspd

echo "polymarket user provisioned"
