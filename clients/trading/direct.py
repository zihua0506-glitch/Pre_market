"""
clients/trading/direct.py

Native Polymarket CLOB backend using py-clob-client-v2 directly. Used by
valkyrie's live strategy. Requires:

  - A non-geo-blocked outbound IP (Hetzner FI works; UK/US/SG do not).
  - py-clob-client-v2 installed (NOT the legacy py-clob-client 0.34.6, which
    fails with `order_version_mismatch` after Polymarket's CLOB v2 migration).
  - .env with POLY_PRIVATE_KEY + POLY_API_{KEY,SECRET,PASSPHRASE} +
    POLY_FUNDER + POLY_SIGNATURE_TYPE.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

# py-clob-client-v2 is mandatory after Polymarket's late-Apr-2026 CLOB v2 migration.
from py_clob_client_v2 import ClobClient
from py_clob_client_v2.clob_types import (
    ApiCreds, AssetType, BalanceAllowanceParams, OrderArgs, OrderPayload,
)
from py_clob_client_v2.order_builder.constants import BUY, SELL

from common.env       import load_env
from common.orderbook import BookLevel, Order, OrderBook
from clients.trading.base import (
    BalanceInfo, CancelResult, OrderResult, TradingBackend,
)

ROOT     = Path(__file__).resolve().parent.parent.parent
ENV_FILE = ROOT / ".env"


class DirectBackend(TradingBackend):
    """Backend that talks straight to clob.polymarket.com via py-clob-client-v2."""

    DEFAULT_TICK = 0.01
    PRICE_MIN    = 0.01
    PRICE_MAX    = 0.99

    def __init__(self, env: Optional[dict[str, str]] = None,
                 env_file: Path | str = ENV_FILE):
        env = env or load_env(env_file)

        for k in ("POLY_PRIVATE_KEY", "POLY_API_KEY",
                  "POLY_API_SECRET",  "POLY_API_PASSPHRASE"):
            if not env.get(k):
                raise RuntimeError(
                    f"{k} missing from .env — run `python -m scripts.setup_clob_creds`"
                )

        host     = env.get("POLY_CLOB_HOST", "https://clob.polymarket.com")
        chain_id = int(env.get("POLY_CHAIN_ID", "137"))
        creds    = ApiCreds(
            api_key        = env["POLY_API_KEY"],
            api_secret     = env["POLY_API_SECRET"],
            api_passphrase = env["POLY_API_PASSPHRASE"],
        )
        kwargs: dict[str, Any] = dict(host=host, key=env["POLY_PRIVATE_KEY"],
                                      chain_id=chain_id, creds=creds)
        # Proxy-wallet flow (required by Polymarket post-CLOB-v2): set these in
        # .env after creating a Polymarket proxy wallet. Without them, EOAs cannot
        # be makers and orders are rejected.
        if env.get("POLY_FUNDER"):
            kwargs["funder"] = env["POLY_FUNDER"]
        if env.get("POLY_SIGNATURE_TYPE"):
            kwargs["signature_type"] = int(env["POLY_SIGNATURE_TYPE"])
        self.client = ClobClient(**kwargs)
        self.host = host

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _level_pair(lvl) -> tuple[float, float]:
        """v2 SDK returns dicts; v1 returned dataclasses. Accept both."""
        if isinstance(lvl, dict):
            return float(lvl["price"]), float(lvl["size"])
        return float(lvl.price), float(lvl.size)

    @classmethod
    def _snap_price(cls, price: float, tick: float = DEFAULT_TICK) -> float:
        """Clamp to [PRICE_MIN, PRICE_MAX] and round to the nearest tick."""
        p = max(cls.PRICE_MIN, min(cls.PRICE_MAX, float(price)))
        return round(round(p / tick) * tick, 4)

    # ── TradingBackend ──────────────────────────────────────────────────────

    def get_book(self, token_id: str) -> OrderBook:
        ob = self.client.get_order_book(token_id)
        raw_bids = ob.get("bids") if isinstance(ob, dict) else (ob.bids or [])
        raw_asks = ob.get("asks") if isinstance(ob, dict) else (ob.asks or [])

        # Polymarket: bids ASC (low→high), asks DESC (high→low). We expose the
        # opposite (bids DESC, asks ASC) so that bids[0] is the best bid.
        bids = [BookLevel(*self._level_pair(b)) for b in (raw_bids or [])]
        asks = [BookLevel(*self._level_pair(a)) for a in (raw_asks or [])]
        bids.sort(key=lambda x: -x.price)
        asks.sort(key=lambda x:  x.price)
        return OrderBook(bids=bids, asks=asks)

    def place_order(self, order: Order) -> OrderResult:
        if order.side != "BUY":
            raise ValueError(f"Only BUY supported in this strategy, got {order.side!r}")
        if float(order.size) <= 0:
            return OrderResult(ok=False, raw={"error": "zero size"})

        limit = self._snap_price(order.limit_price)
        args  = OrderArgs(
            token_id = order.token_id,
            price    = limit,
            size     = float(order.size),
            side     = BUY,
        )
        try:
            resp = self.client.create_and_post_order(args)
        except Exception as e:
            return OrderResult(ok=False, raw={"error": str(e)})

        if not isinstance(resp, dict):
            return OrderResult(ok=False, raw=str(resp))

        ok = bool(resp.get("success"))
        return OrderResult(
            ok       = ok,
            order_id = resp.get("orderID") or resp.get("orderId") or resp.get("id"),
            status   = resp.get("status"),
            raw      = resp,
        )

    def cancel_order(self, order_id: str) -> CancelResult:
        try:
            resp = self.client.cancel_order(OrderPayload(orderID=order_id))
        except Exception as e:
            return CancelResult(ok=False, raw={"error": str(e)})

        # v2 SDK returns {"canceled": [...], "not_canceled": {...}}.
        cancelled = resp.get("canceled", []) if isinstance(resp, dict) else []
        return CancelResult(ok=order_id in cancelled, raw=resp)

    def get_balance(self) -> BalanceInfo:
        bal = self.client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        if not isinstance(bal, dict):
            return BalanceInfo(usdc_balance=0.0, allowance_usdc=0.0,
                               spender_allowances={}, raw=bal)

        usdc_balance = int(bal.get("balance", "0")) / 1e6

        spender_map: dict[str, int] = {}
        if "allowances" in bal and isinstance(bal["allowances"], dict):
            for k, v in bal["allowances"].items():
                try:
                    spender_map[k] = int(v)
                except (TypeError, ValueError):
                    pass

        # Single-allowance legacy path
        if "allowance" in bal:
            try:
                allow = int(bal["allowance"]) / 1e6
            except (TypeError, ValueError):
                allow = 0.0
        elif spender_map:
            m = min(spender_map.values())
            # Anything ≥ 2^200 is effectively "infinite" — surface as a large
            # finite sentinel so downstream callers don't OverflowError.
            allow = 1e15 if m >= (1 << 200) else m / 1e6
        else:
            allow = 0.0

        return BalanceInfo(
            usdc_balance       = usdc_balance,
            allowance_usdc     = allow,
            spender_allowances = spender_map,
            raw                = bal,
        )
