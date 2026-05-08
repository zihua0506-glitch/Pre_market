"""
scripts/setup_clob_creds.py

一次性脚本：用 .env 里的 POLY_PRIVATE_KEY 调 py-clob-client 的
create_or_derive_api_creds()，把 POLY_API_KEY / POLY_API_SECRET /
POLY_API_PASSPHRASE 写回 .env（覆盖已有同名条目）。

Usage:
  python -m scripts.setup_clob_creds          # 写到 .env
  python -m scripts.setup_clob_creds --print  # 仅打印，不写文件
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT    = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

sys.path.insert(0, str(ROOT))
from common.env import load_env as _load_env, write_env as _write_env


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--print", action="store_true",
                   help="只打印新凭据，不修改 .env")
    args = p.parse_args(argv[1:])

    env = _load_env(ENV_FILE)
    pk = env.get("POLY_PRIVATE_KEY") or os.environ.get("POLY_PRIVATE_KEY")
    if not pk:
        print(f"ERROR: POLY_PRIVATE_KEY not set in {ENV_FILE}", file=sys.stderr)
        return 1

    host     = env.get("POLY_CLOB_HOST", "https://clob.polymarket.com")
    chain_id = int(env.get("POLY_CHAIN_ID", "137"))

    from py_clob_client.client import ClobClient
    client = ClobClient(host=host, key=pk, chain_id=chain_id)

    print(f"[setup] host={host} chain_id={chain_id}")
    print("[setup] 派生 / 创建 API 凭据 ...")
    creds = client.create_or_derive_api_creds()

    api_key, api_secret, api_passphrase = (
        creds.api_key, creds.api_secret, creds.api_passphrase,
    )
    print(f"[setup] api_key        = {api_key}")
    print(f"[setup] api_secret     = {'*'*6}{api_secret[-4:]}")
    print(f"[setup] api_passphrase = {'*'*6}{api_passphrase[-4:]}")

    if args.print:
        return 0

    updates = {
        "POLY_API_KEY":        api_key,
        "POLY_API_SECRET":     api_secret,
        "POLY_API_PASSPHRASE": api_passphrase,
    }
    _write_env(ENV_FILE, updates)
    print(f"[setup] 写入 {ENV_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
