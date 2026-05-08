"""
strategies/proxy_clob_adapter.py

Backwards-compat shim. The real implementation moved to
`clients.trading.proxy.ProxyBackend`.

`ProxyClobBackend` here is a thin subclass that prints + returns None on
place_order(), preserving the older modify_str-friendly contract. New code
should prefer:
    from clients.trading import ProxyBackend
"""

from __future__ import annotations

from clients.trading.proxy import ProxyBackend
from common.orderbook      import Order


class ProxyClobBackend(ProxyBackend):
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
            print(f"      [LIVE-FAIL] proxy POST /order  {label}  err={res.raw}")


__all__ = ["ProxyClobBackend", "ProxyBackend"]
