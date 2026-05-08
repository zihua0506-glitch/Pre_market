"""
clients/trading/server.py

Tiny FastAPI service that runs on a non-geo-blocked host (valkyrie / FI),
holds the wallet private key + CLOB API creds, and exposes a small JSON API
for a remote strategy / dashboard / preflight tool to drive Polymarket
trading. The corresponding HTTP client is `clients.trading.proxy.ProxyBackend`.

Auth: shared bearer token in the Authorization header.
Bind: 127.0.0.1 only — exposure happens via SSH local-forward from the client.

Env vars (from .env at the project root or systemd EnvironmentFile=):
  POLY_PRIVATE_KEY, POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE
  POLY_FUNDER, POLY_SIGNATURE_TYPE  (required after CLOB v2 migration)
  POLY_CLOB_HOST          default https://clob.polymarket.com
  POLY_CHAIN_ID           default 137
  PROXY_AUTH_TOKEN        REQUIRED — must match client side
  PROXY_BIND_HOST         default 127.0.0.1
  PROXY_BIND_PORT         default 8088

Run:
  uvicorn clients.trading.server:app --host 127.0.0.1 --port 8088
  # or via systemd  (see deployed unit on valkyrie at /etc/systemd/system/clob-proxy.service)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from common.env       import load_env
from common.orderbook import BookLevel, Order, OrderBook
from clients.trading.direct import DirectBackend

# ── env loading ────────────────────────────────────────────────────────────────

ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"
_env     = load_env(ENV_FILE)

PROXY_AUTH_TOKEN = _env.get("PROXY_AUTH_TOKEN")
if not PROXY_AUTH_TOKEN:
    raise RuntimeError("PROXY_AUTH_TOKEN missing — set it in .env before starting the server")

# ── single shared DirectBackend (one ClobClient instance) ─────────────────────

_backend = DirectBackend(env=_env)
_clob    = _backend.client

# ── app + auth ─────────────────────────────────────────────────────────────────

app = FastAPI(title="clob-proxy", version="2")


def _check_auth(authorization: Optional[str] = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != PROXY_AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="invalid bearer token")


# ── schemas ────────────────────────────────────────────────────────────────────

class BookLevelOut(BaseModel):
    price: float
    size:  float


class BookOut(BaseModel):
    bids: list[BookLevelOut]   # DESC
    asks: list[BookLevelOut]   # ASC


class OrderIn(BaseModel):
    token_id: str
    price:    float
    size:     float
    side:     str = "BUY"
    note:     str = ""


class OrderOut(BaseModel):
    ok:       bool
    order_id: Optional[str] = None
    status:   Optional[str] = None
    raw:      dict | str | None = None


class CancelIn(BaseModel):
    order_id: str


class BalanceOut(BaseModel):
    balance_usdc:   float
    allowance_usdc: float
    raw:            dict


# ── routes ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "clob-proxy"}


@app.get("/book", response_model=BookOut, dependencies=[Depends(_check_auth)])
def get_book(token_id: str) -> BookOut:
    ob = _backend.get_book(token_id)
    return BookOut(
        bids=[BookLevelOut(price=l.price, size=l.size) for l in ob.bids],
        asks=[BookLevelOut(price=l.price, size=l.size) for l in ob.asks],
    )


@app.post("/order", response_model=OrderOut, dependencies=[Depends(_check_auth)])
def place_order(o: OrderIn) -> OrderOut:
    if o.side not in ("BUY", "SELL"):
        raise HTTPException(400, f"bad side {o.side}")
    order = Order(token_id=o.token_id, side=o.side, size=o.size,
                  limit_price=o.price, note=o.note)
    res = _backend.place_order(order)
    return OrderOut(ok=res.ok, order_id=res.order_id, status=res.status,
                    raw=res.raw if isinstance(res.raw, (dict, str)) else None)


@app.post("/cancel", dependencies=[Depends(_check_auth)])
def cancel_order(c: CancelIn) -> dict:
    res = _backend.cancel_order(c.order_id)
    return {"ok": res.ok, "raw": res.raw}


@app.get("/balance", response_model=BalanceOut, dependencies=[Depends(_check_auth)])
def balance() -> BalanceOut:
    info = _backend.get_balance()
    return BalanceOut(
        balance_usdc   = info.usdc_balance,
        allowance_usdc = info.allowance_usdc,
        raw            = info.raw if isinstance(info.raw, dict) else {},
    )


@app.get("/order/{order_id}", dependencies=[Depends(_check_auth)])
def get_order(order_id: str) -> dict:
    try:
        info = _clob.get_order(order_id)
        return {"ok": True, "raw": info if isinstance(info, dict) else str(info)}
    except Exception as e:
        return {"ok": False, "raw": {"error": str(e)}}


# ── CLI launcher (so `python -m clients.trading.server` works) ─────────────────

if __name__ == "__main__":
    import uvicorn
    host = _env.get("PROXY_BIND_HOST", "127.0.0.1")
    port = int(_env.get("PROXY_BIND_PORT", "8088"))
    uvicorn.run(app, host=host, port=port, log_level="info")
