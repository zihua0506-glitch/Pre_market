"""
scripts/rotate_wallet.py

Rotate the Polymarket wallet to a fresh EOA + re-derive CLOB API creds.

What it does, in order:
  1. Generate a fresh secp256k1 keypair (eth_account).
  2. Back up the local .env to .env.rotated-YYYYMMDD-HHMMSS.bak.
  3. Update local .env with the new POLY_PRIVATE_KEY.
  4. Wipe the old POLY_API_KEY/SECRET/PASSPHRASE (they were derived from the OLD
     key and won't authenticate any more).
  5. Use the new private key to derive fresh API creds via py-clob-client.
  6. Write those creds back into local .env.
  7. (Unless --skip-remote) push the updated .env to:
        valkyrie:/opt/zspd-trader/.env
        valkyrie:/opt/clob-proxy/.env
     and restart clob-proxy.service so the proxy picks up the new key.

What it does NOT do (manual follow-ups):
  - Connect to polymarket.com to deploy the Proxy Wallet — you must do this
    in a browser from a non-blocked IP (UK and SG are blocked).
  - Bridge USDC into the Proxy Wallet.
  - Set POLY_FUNDER + POLY_SIGNATURE_TYPE in valkyrie's .env (you set those
    after the proxy address is known and funded).
  - Sweep funds out of the OLD wallet — old key remains valid until you do.

Usage:
  python -m scripts.rotate_wallet               # interactive, asks before write
  python -m scripts.rotate_wallet --no-confirm  # CI-style, no prompt
  python -m scripts.rotate_wallet --skip-remote # only update local .env
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT     = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

# Where to push the updated .env (host alias, remote path)
REMOTE_TARGETS: list[tuple[str, str]] = [
    ("valkyrie", "/opt/zspd-trader/.env"),
    ("valkyrie", "/opt/clob-proxy/.env"),
]

# Services that need to restart after .env changes
REMOTE_RESTART_UNITS: list[tuple[str, str]] = [
    ("valkyrie", "clob-proxy.service"),
]


# ── env file helpers (delegated to common.env) ──────────────────────────────

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.env import load_env as _load_env, write_env as _write_env


# ── steps ────────────────────────────────────────────────────────────────────

def generate_eoa() -> tuple[str, str]:
    from eth_account import Account
    a = Account.create()
    priv = a.key.hex() if isinstance(a.key, bytes) else a.key
    if not priv.startswith("0x"):
        priv = "0x" + priv
    return priv, a.address


def derive_creds(private_key: str, host: str, chain_id: int) -> tuple[str, str, str]:
    """Use py-clob-client (v1) to derive API creds. v1 is sufficient for /auth/api-key."""
    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        sys.exit("py-clob-client not installed. Run:\n  uv pip install py-clob-client")
    client = ClobClient(host=host, key=private_key, chain_id=chain_id)
    creds = client.create_or_derive_api_creds()
    return creds.api_key, creds.api_secret, creds.api_passphrase


def push_remote(local_env_path: Path, host: str, remote_path: str) -> None:
    print(f"  scp → {host}:{remote_path}")
    subprocess.run(
        ["/usr/bin/scp", str(local_env_path), f"{host}:{remote_path}"],
        check=True,
    )
    subprocess.run(
        ["/usr/bin/ssh", host, f"chmod 600 {remote_path}"],
        check=True,
    )


def restart_remote(host: str, unit: str) -> None:
    print(f"  ssh {host} systemctl restart {unit}")
    subprocess.run(
        ["/usr/bin/ssh", host, f"systemctl restart {unit}"],
        check=True,
    )


# ── main ─────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Rotate Polymarket wallet + CLOB creds")
    p.add_argument("--no-confirm",  action="store_true",
                   help="Skip the interactive confirmation prompt")
    p.add_argument("--skip-remote", action="store_true",
                   help="Only update local .env; don't push to valkyrie")
    args = p.parse_args(argv[1:])

    env  = _load_env(ENV_FILE)
    host = env.get("POLY_CLOB_HOST", "https://clob.polymarket.com")
    cid  = int(env.get("POLY_CHAIN_ID", "137"))

    old_pk = env.get("POLY_PRIVATE_KEY")
    if old_pk:
        from eth_account import Account
        old_addr = Account.from_key(old_pk).address
        print(f"\n  Current EOA: {old_addr}")
    else:
        print("\n  (no existing POLY_PRIVATE_KEY in .env — fresh setup)")

    new_pk, new_addr = generate_eoa()
    print(f"  NEW EOA:     {new_addr}")
    print(f"  NEW priv:    {new_pk}")

    if not args.no_confirm:
        print("\nThis will:")
        print(f"  1. Back up {ENV_FILE} → {ENV_FILE}.rotated-<ts>.bak")
        print(f"  2. Replace POLY_PRIVATE_KEY in {ENV_FILE}")
        print(f"  3. Derive fresh CLOB API creds for the new key (signs into Polymarket)")
        if not args.skip_remote:
            print(f"  4. scp updated .env to: {', '.join(f'{h}:{p}' for h,p in REMOTE_TARGETS)}")
            print(f"  5. Restart units: {', '.join(f'{h}:{u}' for h,u in REMOTE_RESTART_UNITS)}")
        ans = input("\nProceed? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 1

    # 1+2: backup + write new private key
    ts      = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    backup  = ENV_FILE.with_suffix(f".rotated-{ts}.bak")
    if ENV_FILE.exists():
        shutil.copy2(ENV_FILE, backup)
        print(f"\n  backed up local .env → {backup.name}")

    env["POLY_PRIVATE_KEY"] = new_pk
    # Wipe old creds — they were derived from old key, won't auth anymore.
    for k in ("POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE"):
        env.pop(k, None)
    # Wipe old proxy-wallet config — new EOA has no proxy yet.
    for k in ("POLY_FUNDER", "POLY_SIGNATURE_TYPE"):
        env.pop(k, None)
    _write_env(ENV_FILE, env)
    print(f"  wrote new POLY_PRIVATE_KEY to {ENV_FILE}")

    # 3: derive fresh CLOB creds with the new key
    print("\n  deriving fresh CLOB API creds ...")
    api_key, api_secret, api_passphrase = derive_creds(new_pk, host, cid)
    env["POLY_API_KEY"]        = api_key
    env["POLY_API_SECRET"]     = api_secret
    env["POLY_API_PASSPHRASE"] = api_passphrase
    _write_env(ENV_FILE, env)
    print(f"  api_key:        {api_key}")
    print(f"  api_secret:     {'*' * 8}{api_secret[-4:]}")
    print(f"  api_passphrase: {'*' * 8}{api_passphrase[-4:]}")

    # 4+5: remote propagation
    if not args.skip_remote:
        print("\n  pushing .env to remote hosts ...")
        for host_alias, remote_path in REMOTE_TARGETS:
            try:
                push_remote(ENV_FILE, host_alias, remote_path)
            except subprocess.CalledProcessError as e:
                print(f"  [warn] push failed for {host_alias}:{remote_path} — {e}")
        for host_alias, unit in REMOTE_RESTART_UNITS:
            try:
                restart_remote(host_alias, unit)
            except subprocess.CalledProcessError as e:
                print(f"  [warn] restart failed for {host_alias}:{unit} — {e}")

    # next-steps banner
    print("\n" + "─" * 70)
    print("  ROTATION COMPLETE — manual follow-up required:")
    print("─" * 70)
    print(f"""
  1. From a non-blocked IP, browse https://polymarket.com and connect
     wallet  {new_addr}
     (private key: see {ENV_FILE})

  2. Polymarket auto-deploys a Proxy Wallet contract.
     Note its address from "Settings → Wallet → Deposit address".
     It will look like 0xXXXX...  and is NOT the EOA above.

  3. Bridge USDC (Polygon-native, 6 decimals,
     contract 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174)
     to the PROXY WALLET — not the EOA.
     Recommended starter: $5–10 to verify the link, then scale.

  4. On valkyrie:
       ssh valkyrie 'cat >> /opt/zspd-trader/.env <<EOF
       POLY_FUNDER=0x<your proxy wallet address>
       POLY_SIGNATURE_TYPE=1
       EOF'
       ssh valkyrie 'cat >> /opt/clob-proxy/.env <<EOF
       POLY_FUNDER=0x<your proxy wallet address>
       POLY_SIGNATURE_TYPE=1
       EOF'
       ssh valkyrie 'systemctl restart clob-proxy'

  5. Verify with:
       python -m scripts.preflight_clob --skip-order

  6. Once balance shows non-zero, start trading:
       ssh valkyrie 'systemctl start zspd-strategy'
""")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
