"""
clients/trading/

Order book + order placement library. Reusable by any strategy, dashboard,
monitor, or one-off script.

Public surface (lazy-imported — see __getattr__ below):

  TradingBackend          ABC: get_book / place_order / cancel_order / get_balance
  DirectBackend           Native py-clob-client-v2 client. Used by valkyrie's
                          live strategy (no extra hop). Requires py-clob-client-v2.
  ProxyBackend            HTTP client to a remote clob-proxy-server. Requires
                          requests.
  DryRunBackend           Stub. No network. No real orders.
  make_backend(kind, ...) Factory: pick a backend by string name.

  OrderResult / CancelResult / BalanceInfo  Common return types.

DirectBackend and ProxyBackend are imported lazily so that environments without
their respective dependencies (e.g. local Mac without py-clob-client-v2) can
still import `clients.trading.{base,dryrun}` for tests.
"""

from clients.trading.base import (
    BalanceInfo, CancelResult, OrderResult, TradingBackend,
)


def __getattr__(name: str):
    """PEP 562 lazy attribute import."""
    if name == "DirectBackend":
        from clients.trading.direct import DirectBackend
        return DirectBackend
    if name == "ProxyBackend":
        from clients.trading.proxy import ProxyBackend
        return ProxyBackend
    if name == "DryRunBackend":
        from clients.trading.dryrun import DryRunBackend
        return DryRunBackend
    if name == "make_backend":
        return _make_backend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _make_backend(kind: str, **kwargs) -> TradingBackend:
    """Factory: pick a backend by name. kind ∈ {'direct','proxy','dryrun'}."""
    if kind == "direct":
        from clients.trading.direct import DirectBackend
        return DirectBackend(**kwargs)
    if kind == "proxy":
        from clients.trading.proxy import ProxyBackend
        return ProxyBackend(**kwargs)
    if kind == "dryrun":
        from clients.trading.dryrun import DryRunBackend
        return DryRunBackend(**kwargs)
    raise ValueError(f"unknown trading backend: {kind!r}")


__all__ = [
    "BalanceInfo",
    "CancelResult",
    "DirectBackend",
    "DryRunBackend",
    "OrderResult",
    "ProxyBackend",
    "TradingBackend",
    "make_backend",
]
