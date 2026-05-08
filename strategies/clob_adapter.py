"""
strategies/clob_adapter.py

Backwards-compat shim. The real implementation moved to
`clients.trading.direct.DirectBackend` so it can be reused by dashboards,
preflight tools, and alternative strategies.

`LiveClobBackend` here is a thin subclass that adapts the new
TradingBackend interface (which returns OrderResult / CancelResult) to the
older "print-and-return-None" contract that modify_str._buy_yes / _eat_no rely
on. Strategy code can keep importing from this path unchanged.

New code should prefer:
    from clients.trading import DirectBackend
"""

from __future__ import annotations

from clients.trading.direct import DirectBackend
from common.orderbook       import Order


class LiveClobBackend(DirectBackend):
    """Strategy-side adapter: prints fills, returns None on success."""

    def place_order(self, order: Order) -> None:   # type: ignore[override]
        res = super().place_order(order)
        label = order.note or ""
        if res.ok:
            print(f"      [LIVE] BUY  size={order.size:.2f}  "
                  f"limit={order.limit_price:.4f}  "
                  f"token={order.token_id[:12]}…  ok=True  "
                  f"id={(res.order_id or '?')[:10]}  status={res.status}  ({label})")
        else:
            print(f"      [LIVE-FAIL] {label}  err={res.raw}")


__all__ = ["LiveClobBackend", "DirectBackend"]
