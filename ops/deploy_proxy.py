"""
scripts/deploy_proxy.py

Deploy a Polymarket Proxy Wallet for the current EOA by calling
ProxyWalletFactory.proxy([]) with an empty calls array.

Why this script exists:
  Polymarket auto-deploys the user's proxy wallet on the first deposit through
  their UI. When the deposit page is geo-blocked (e.g. datacenter IP), no
  proxy gets deployed and orders can't be placed (`maker address not allowed`).
  This script triggers the same factory call directly on-chain, bypassing
  Polymarket's UI entirely.

Cost:
  ~150-300k gas × current Polygon gas price ≈ $0.01-0.05 in POL.

Prerequisites:
  - .env has POLY_PRIVATE_KEY for the EOA you signed into Polymarket with
  - That EOA has at least 0.05 POL on Polygon mainnet for gas

After running:
  - .env is updated with POLY_FUNDER (proxy address) + POLY_SIGNATURE_TYPE=1
  - You print the proxy address — fund it with USDC, then push .env to valkyrie

Idempotency:
  - If POLY_FUNDER is already set and the on-chain code at that address is the
    Polymarket Proxy bytecode, exit success without sending another tx.

Usage:
  python -m scripts.deploy_proxy                # interactive
  python -m scripts.deploy_proxy --no-confirm   # CI-style
  python -m scripts.deploy_proxy --rpc <url>    # alt RPC
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from eth_account import Account
from eth_utils import keccak, to_checksum_address
from web3 import Web3

ROOT     = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

FACTORY_ADDR     = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"  # Polymarket ProxyWalletFactory
IMPL_ADDR_LOWER  = "44e999d5c2f66ef0861317f9a4805ac2e90aeb4f"     # ProxyWallet implementation
DEFAULT_RPC      = "https://polygon-bor-rpc.publicnode.com"
BACKUP_RPCS      = [
    "https://polygon.llamarpc.com",
    "https://polygon-rpc.com",
    "https://1rpc.io/matic",
]
BLOCKSCOUT_API   = "https://polygon.blockscout.com/api/v2"

# selector for proxy((uint256,address,uint256,bytes)[])
PROXY_SELECTOR = keccak(b"proxy((uint256,address,uint256,bytes)[])")[:4].hex()

# pre-built calldata for proxy([]) — empty array
EMPTY_CALLDATA = (
    "0x" + PROXY_SELECTOR
    + "0000000000000000000000000000000000000000000000000000000000000020"
    + "0000000000000000000000000000000000000000000000000000000000000000"
)


# ── env helpers (delegated to common.env) ───────────────────────────────────

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.env import load_env as _load_env, write_env as _write_env


# ── Polygon helpers ──────────────────────────────────────────────────────────

def _connect(primary: str) -> Web3:
    """Try primary RPC, fall back to public alternates."""
    candidates = [primary] + [r for r in BACKUP_RPCS if r != primary]
    for url in candidates:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 12}))
            if w3.is_connected():
                cid = w3.eth.chain_id
                if cid != 137:
                    print(f"  [warn] {url} returned chain_id={cid}, skipping")
                    continue
                print(f"  using RPC: {url}")
                return w3
        except Exception as e:
            print(f"  [warn] {url} unreachable: {e}")
    raise RuntimeError("no Polygon RPC reachable")


def _internal_txs(tx_hash: str) -> list[dict]:
    url = f"{BLOCKSCOUT_API}/transactions/{tx_hash}/internal-transactions"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            return r.json().get("items", [])
    except requests.RequestException:
        pass
    return []


def _wait_blockscout_internal_txs(tx_hash: str, max_wait_sec: int = 90) -> list[dict]:
    deadline = time.time() + max_wait_sec
    while time.time() < deadline:
        items = _internal_txs(tx_hash)
        if items:
            return items
        time.sleep(3)
    return []


def _is_polymarket_proxy(w3: Web3, addr: str) -> bool:
    """An address is a Polymarket proxy iff its code references the impl."""
    try:
        code = w3.eth.get_code(to_checksum_address(addr)).hex()
        return IMPL_ADDR_LOWER in code.lower()
    except Exception:
        return False


# ── main ─────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Deploy Polymarket Proxy Wallet on-chain")
    p.add_argument("--rpc", default=DEFAULT_RPC,
                   help=f"Polygon RPC (default {DEFAULT_RPC})")
    p.add_argument("--no-confirm", action="store_true",
                   help="Skip the y/N prompt")
    p.add_argument("--gas-limit", type=int, default=400_000,
                   help="Gas limit for the deploy tx (default 400000)")
    args = p.parse_args(argv[1:])

    env = _load_env(ENV_FILE)
    pk  = env.get("POLY_PRIVATE_KEY")
    if not pk:
        print(f"ERROR: POLY_PRIVATE_KEY missing in {ENV_FILE}")
        return 1

    acct = Account.from_key(pk)
    eoa  = acct.address

    # ── connect ──
    w3 = _connect(args.rpc)

    # ── idempotency: if POLY_FUNDER already valid, no-op ──
    existing_funder = env.get("POLY_FUNDER")
    if existing_funder:
        print(f"\n  POLY_FUNDER already set in .env: {existing_funder}")
        if _is_polymarket_proxy(w3, existing_funder):
            print(f"  ✅ on-chain code matches Polymarket proxy impl. Nothing to do.")
            return 0
        else:
            print(f"  ⚠️ on-chain code at {existing_funder} doesn't look like a Polymarket proxy.")
            if not args.no_confirm:
                ans = input("  Continue and deploy a new one? [y/N] ").strip().lower()
                if ans not in ("y", "yes"):
                    return 1

    # ── balance check ──
    bal_wei = w3.eth.get_balance(eoa)
    pol     = bal_wei / 1e18
    print(f"\n  EOA:          {eoa}")
    print(f"  POL balance:  {pol:.6f}")
    print(f"  Factory:      {FACTORY_ADDR}")

    if pol < 0.05:
        print(f"\n  ❌ POL balance < 0.05; need ~$0.05 worth of POL on this EOA for gas.")
        print(f"     Send POL on Polygon to: {eoa}")
        return 1

    # ── confirm ──
    if not args.no_confirm:
        ans = input(f"\n  Send factory.proxy([]) tx to deploy proxy? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("  Aborted.")
            return 1

    # ── build, sign, send ──
    nonce     = w3.eth.get_transaction_count(eoa)
    gas_price = w3.eth.gas_price
    tx = {
        "from":     eoa,
        "to":       FACTORY_ADDR,
        "data":     EMPTY_CALLDATA,
        "value":    0,
        "gas":      args.gas_limit,
        "gasPrice": gas_price,
        "nonce":    nonce,
        "chainId":  137,
    }
    print(f"\n  signing & sending tx (gas_price={gas_price/1e9:.1f} gwei, gas={args.gas_limit}) ...")
    signed   = acct.sign_transaction(tx)
    raw      = signed.raw_transaction
    tx_hash  = w3.eth.send_raw_transaction(raw).hex()
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash

    print(f"  tx hash:      {tx_hash}")
    print(f"  view:         https://polygonscan.com/tx/{tx_hash}")

    # ── wait for receipt ──
    print(f"\n  waiting for confirmation ...")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt.status != 1:
        print(f"  ❌ tx reverted")
        return 2
    print(f"  ✅ confirmed in block {receipt.blockNumber}")
    print(f"  gas used:     {receipt.gasUsed}  (~${receipt.gasUsed * gas_price / 1e18 * 0.7:.4f} at $0.70/POL)")

    # ── find deployed proxy via Blockscout internal txs ──
    print(f"\n  fetching internal txs from Blockscout (indexer can lag a few sec) ...")
    items = _wait_blockscout_internal_txs(tx_hash)
    proxy_addr: Optional[str] = None
    if items:
        for it in items:
            if it.get("type", "").lower() in ("create", "create2"):
                created = it.get("created_contract")
                addr    = (created or {}).get("hash") if isinstance(created, dict) else created
                if addr:
                    proxy_addr = to_checksum_address(addr)
                    break
        if not proxy_addr:
            print(f"  no CREATE entry in internal txs (proxy may already exist).")
            print(f"  internal txs[:3]:")
            for it in items[:3]:
                print(f"    {json.dumps(it)[:200]}")

    # If no CREATE found (idempotent rerun) try to discover via factory call simulation:
    # we know the salt is keccak256(EOA bytes); even without the exact init code match
    # we can scan recent factory-deployed contracts and find one whose owner storage
    # hints at our EOA. As a last resort, ask the user to inspect manually.
    if not proxy_addr:
        print(f"\n  ⚠️ Could not discover proxy address automatically.")
        print(f"     Check internal txs at: https://polygonscan.com/tx/{tx_hash}#internal")
        return 3

    # ── verify ──
    if not _is_polymarket_proxy(w3, proxy_addr):
        print(f"  ⚠️ deployed code at {proxy_addr} doesn't match Polymarket impl. Aborting write.")
        return 4
    print(f"  ✅ Proxy bytecode references Polymarket impl 0x44e999d5...")

    # ── update .env ──
    _write_env(ENV_FILE, {
        "POLY_FUNDER":         proxy_addr,
        "POLY_SIGNATURE_TYPE": "1",
    })
    print(f"\n  wrote POLY_FUNDER={proxy_addr} + POLY_SIGNATURE_TYPE=1 → {ENV_FILE}")

    # ── final summary ──
    print(f"\n" + "─" * 70)
    print(f"  PROXY WALLET DEPLOYED")
    print(f"─" * 70)
    print(f"  EOA (signer):      {eoa}")
    print(f"  Proxy (funder):    {proxy_addr}")
    print(f"  Send USDC to:      {proxy_addr}")
    print(f"  USDC contract:     0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
    print(f"  Network:           Polygon Mainnet")
    print(f"")
    print(f"  Next:")
    print(f"  1. Onramp USDC (e.g. $10) → {proxy_addr} on Polygon mainnet")
    print(f"  2. Push .env:")
    print(f"       scp .env valkyrie:/opt/zspd-trader/.env")
    print(f"       scp .env valkyrie:/opt/clob-proxy/.env")
    print(f"       ssh valkyrie 'systemctl restart clob-proxy'")
    print(f"  3. Verify: python -m scripts.preflight_clob --skip-order")
    print(f"  4. Start:  ssh valkyrie 'systemctl start zspd-strategy'")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
