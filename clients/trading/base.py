"""
clients/trading/base.py

TradingBackend ABC + common return types.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from common.orderbook import Order, OrderBook


@dataclass
class OrderResult:
    """Returned by TradingBackend.place_order().

    `ok` True iff the order was accepted by the venue (resting in book or
    filled). False indicates either a network error, a Polymarket-side reject,
    or a malformed request. `raw` carries the venue's full response for
    inspection / logging.
    """
    ok:       bool
    order_id: Optional[str] = None
    status:   Optional[str] = None    # 'live' | 'matched' | 'cancelled' | ...
    raw:      Any            = None


@dataclass
class CancelResult:
    """Returned by TradingBackend.cancel_order()."""
    ok:  bool
    raw: Any = None


@dataclass
class BalanceInfo:
    """Returned by TradingBackend.get_balance()."""
    usdc_balance:   float                          # USDC, in dollars (6-decimal -> float)
    allowance_usdc: float                          # min allowance across spenders, in dollars
    spender_allowances: dict[str, int] = field(default_factory=dict)  # full per-spender raw values
    raw:            Any = None


class TradingBackend(ABC):
    """Abstract trading API.

    Implementations must NOT mutate Order objects. They MAY clamp the limit
    price to the venue's tick grid; they MUST surface the venue's reject
    reason via OrderResult.raw when ok=False.
    """

    @abstractmethod
    def get_book(self, token_id: str) -> OrderBook:
        """Return the order book for a CLOB token, with bids DESC / asks ASC."""

    @abstractmethod
    def place_order(self, order: Order) -> OrderResult:
        """Submit a BUY limit order. Returns OrderResult."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> CancelResult:
        """Cancel a resting order by its ID."""

    @abstractmethod
    def get_balance(self) -> BalanceInfo:
        """Return USDC balance + allowance for the wallet associated with this backend."""
