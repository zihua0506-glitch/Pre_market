"""
scripts/preflight_clob.py

Live-trading preflight check. Run this ONCE before letting modify_str go live.

What it does (in order):
  1. Print outbound IP + Polymarket ban-status for the wallet.
  2. Print USDC balance + allowance (collateral) on the wallet.
  3. Place a tiny BUY YES limit at price 0.01 on a chosen token_id.
     Limit is so far below best_ask that it will NOT fill.
  4. Show the order via get_order().
  5. Cancel the order via cancel(order_id).

Defaults: today's "highest eq 22°C" YES on ZSPD, $1 size, limit 0.01.
Override with --token-id / --size / --limit.

Usage:
  python -m scripts.preflight_clob                  # full check
  python -m scripts.preflight_clob --skip-order     # only balance + IP
  python -m scripts.preflight_clob --token-id ...   # custom token
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CST = timezone(timedelta(hours=8))


def _ip_info() -> dict:
    """Best-effort outbound IP + country lookup."""
    info: dict = {}
    try:
        r = requests.get("https://ipinfo.io/json", timeout=8)
        if r.ok:
            info = r.json()
    except requests.RequestException as e:
        info["error"] = str(e)
    return info


def _ban_status(host: str, address: str) -> dict | str:
    """GET /ban-status/{address} on the CLOB host. Public endpoint."""
    try:
        r = requests.get(f"{host}/ban-status/cert-required/{address}", timeout=8)
        if r.ok:
            try:
                return r.json()
            except json.JSONDecodeError:
                return r.text[:200]
        return f"HTTP {r.status_code}: {r.text[:200]}"
    except requests.RequestException as e:
        return f"err: {e}"


def _today_default_token(target_date: date) -> str | None:
    """Pick a sensible default: highest eq 22°C YES for today (or any tmax YES)."""
    db = ROOT / "database" / "data.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(db)
    row = conn.execute(
        """SELECT token_id FROM token_meta
           WHERE day_date=? AND side='YES' AND kind='highest' AND bound='eq'
           ORDER BY ABS(threshold_c - 22) LIMIT 1""",
        (target_date.isoformat(),),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Polymarket CLOB preflight check")
    p.add_argument("--token-id", default=None,
                   help="Token to test against (default: today's tmax eq 22°C YES)")
    p.add_argument("--size",     type=float, default=1.0,
                   help="Order size in shares (default 1.0)")
    p.add_argument("--limit",    type=float, default=0.01,
                   help="Limit price; 0.01 = guaranteed no-fill (default)")
    p.add_argument("--skip-order", action="store_true",
                   help="Only run IP + balance checks, no test order")
    args = p.parse_args(argv[1:])

    from strategies.clob_adapter import LiveClobBackend
    from common.env import load_env
    from py_clob_client.clob_types import (
        ApiCreds, AssetType, BalanceAllowanceParams, OrderArgs,
    )
    from py_clob_client.order_builder.constants import BUY

    env  = load_env(ROOT / ".env")
    host = env.get("POLY_CLOB_HOST", "https://clob.polymarket.com")
    pk   = env.get("POLY_PRIVATE_KEY", "")

    # Wallet address (recover from private key)
    from eth_account import Account
    addr = Account.from_key(pk).address
    print(f"\n── 1. Identity ────────────────────────────────────────────")
    print(f"  wallet_address  = {addr}")
    print(f"  clob_host       = {host}")

    print(f"\n── 2. Outbound IP / geo ──────────────────────────────────")
    ip = _ip_info()
    print(f"  ip      = {ip.get('ip', '?')}")
    print(f"  country = {ip.get('country', '?')}  region = {ip.get('region', '?')}  org = {ip.get('org', '?')}")

    print(f"\n── 3. Polymarket ban-status ─────────────────────────────")
    bs = _ban_status(host, addr)
    print(f"  {bs}")

    print(f"\n── 4. Wallet balance / allowance ─────────────────────────")
    backend = LiveClobBackend(env)
    client  = backend.client
    try:
        bal = client.get_balance_allowance(BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
        ))
        # USDC has 6 decimals on Polygon
        bal_usdc = int(bal.get("balance", "0")) / 1e6
        allow    = int(bal.get("allowance", "0")) / 1e6
        print(f"  USDC balance   = ${bal_usdc:.4f}")
        print(f"  USDC allowance = ${allow:.4f}")
        if allow < 1.0:
            print(f"  ⚠️  allowance 太低；下单会被拒。先调 update_balance_allowance() 或在 Polymarket UI 触发一次操作。")
        if bal_usdc < args.size * args.limit + 0.5:
            print(f"  ⚠️  余额不足以下 ${args.size * args.limit:.4f} 单 + gas。")
    except Exception as e:
        print(f"  err: {e}")

    if args.skip_order:
        return 0

    # ── 5. Test order ────────────────────────────────────────────────────
    target_date = datetime.now(CST).date()
    token_id    = args.token_id or _today_default_token(target_date)
    if not token_id:
        print(f"\n  ❌ no token_id provided and no tmax YES contract found for {target_date}")
        return 1

    print(f"\n── 5. Place test order ───────────────────────────────────")
    print(f"  token_id = {token_id[:20]}…")
    print(f"  size     = {args.size}  limit = {args.limit}  side = BUY YES")

    book = backend.get_book(token_id)
    best_ask = book.asks[0].price if book.asks else None
    print(f"  best_ask = {best_ask}  best_bid = {book.bids[0].price if book.bids else None}")
    if best_ask is not None and args.limit >= best_ask:
        print(f"  ⚠️  limit {args.limit} >= best_ask {best_ask} — order may fill instantly! 中止。")
        return 1

    args_obj = OrderArgs(token_id=token_id, price=args.limit, size=args.size, side=BUY)
    try:
        resp = client.create_and_post_order(args_obj)
    except Exception as e:
        print(f"  ❌ create_and_post_order err: {e}")
        return 2

    print(f"  resp = {json.dumps(resp, indent=2)[:600]}")
    order_id = (resp.get("orderID") or resp.get("orderId") or
                resp.get("id") if isinstance(resp, dict) else None)
    if not order_id:
        print(f"  ❌ no order_id in response; cannot continue.")
        return 3

    print(f"\n── 6. Look up order ──────────────────────────────────────")
    time.sleep(1.0)
    try:
        info = client.get_order(order_id)
        print(f"  {json.dumps(info, indent=2)[:600] if isinstance(info, dict) else info}")
    except Exception as e:
        print(f"  err: {e}")

    print(f"\n── 7. Cancel order ───────────────────────────────────────")
    try:
        cancel_resp = client.cancel(order_id)
        print(f"  {cancel_resp}")
    except Exception as e:
        print(f"  ❌ cancel err: {e}")
        return 4

    print(f"\n✅ preflight OK — 链路（auth + book + order + cancel）打通。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
