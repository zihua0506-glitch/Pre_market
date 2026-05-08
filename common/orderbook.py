"""
common/orderbook.py

Shared data types for order book / orders. Used by:
  - strategies (modify_str.py, plain_as.py)
  - trading clients (clients.trading.{direct,proxy,dryrun,server})

Conventions:
  - prices are floats in [0, 1] (Polymarket probability prices)
  - sizes are floats counting shares (no implicit scaling)
  - OrderBook bids are sorted DESC (highest bid first), asks ASC (cheapest ask
    first). Polymarket's raw API returns the opposite; clients are responsible
    for reversing on construction.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BookLevel:
    price: float   # token price ∈ (0, 1), Polymarket probability units
    size:  float   # contracts available at this price


@dataclass
class OrderBook:
    bids: list[BookLevel]   # sorted DESC (highest bid first)
    asks: list[BookLevel]   # sorted ASC  (cheapest ask first)


@dataclass
class Order:
    token_id:    str
    side:        str    # 'BUY' (this strategy never SELLs)
    size:        float
    limit_price: float
    note:        str = ""
