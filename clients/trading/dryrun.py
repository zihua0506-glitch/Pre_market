"""
clients/trading/dryrun.py

DryRunBackend — stubs for dry-run / unit tests. Returns synthetic order books;
"places" orders by printing them; never touches the network.
"""

from __future__ import annotations

from typing import Optional
import itertools

from common.orderbook import BookLevel, Order, OrderBook
from clients.trading.base import (
    BalanceInfo, CancelResult, OrderResult, TradingBackend,
)


class DryRunBackend(TradingBackend):
    """Synthetic trading backend. No network. No real orders."""

    def __init__(self, *,
                 yes_mid: float = 0.25,
                 ask_offset: float = 0.02,
                 bid_offset: float = 0.02,
                 ask_size: float = 200.0,
                 bid_size: float = 300.0,
                 verbose: bool = True):
        self.yes_mid    = yes_mid
        self.ask_offset = ask_offset
        self.bid_offset = bid_offset
        self.ask_size   = ask_size
        self.bid_size   = bid_size
        self.verbose    = verbose
        self._oid_seq   = itertools.count(1)
        self.placed:   list[Order]     = []
        self.cancelled: list[str]      = []

    # ── TradingBackend ──────────────────────────────────────────────────────

    def get_book(self, token_id: str) -> OrderBook:
        bids = [
            BookLevel(price=round(self.yes_mid - self.bid_offset, 4),       size=self.bid_size),
            BookLevel(price=round(self.yes_mid - self.bid_offset * 2, 4),   size=self.bid_size * 2),
        ]
        asks = [
            BookLevel(price=round(self.yes_mid + self.ask_offset, 4),       size=self.ask_size),
            BookLevel(price=round(self.yes_mid + self.ask_offset * 2, 4),   size=self.ask_size * 2),
        ]
        return OrderBook(bids=bids, asks=asks)

    def place_order(self, order: Order) -> OrderResult:
        if order.side != "BUY":
            raise ValueError(f"Only BUY supported, got {order.side!r}")
        if float(order.size) <= 0:
            return OrderResult(ok=False, raw={"error": "zero size"})

        oid = f"dryrun-{next(self._oid_seq):08d}"
        self.placed.append(order)
        if self.verbose:
            print(f"      [DRY-RUN] BUY  size={order.size:.2f}  "
                  f"limit={order.limit_price:.4f}  "
                  f"token={order.token_id[:12]}…  ({order.note})")
        return OrderResult(
            ok=True, order_id=oid, status="dryrun",
            raw={"placed": True, "order": order.__dict__},
        )

    def cancel_order(self, order_id: str) -> CancelResult:
        self.cancelled.append(order_id)
        return CancelResult(ok=True, raw={"cancelled": order_id})

    def get_balance(self) -> BalanceInfo:
        return BalanceInfo(
            usdc_balance=1000.0, allowance_usdc=1e15,
            spender_allowances={}, raw={"dryrun": True},
        )
