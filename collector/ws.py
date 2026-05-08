"""Polymarket CLOB WebSocket subscriber. Maintains in-memory latest[token_id]."""
from __future__ import annotations
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Callable, Iterable
import websockets
from collector.config import Config

log = logging.getLogger(__name__)


@dataclass
class Tick:
    market_id: str
    best_bid: float = 0.0
    best_ask: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    last_trade: float = 0.0
    ts_ms: int = 0


class LatestStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._data: dict[str, Tick] = {}
        self.last_message_ts: float = 0.0
        self.connected: bool = False

    def update_book(self, asset_id: str, market_id: str, bids: list, asks: list, ts_ms: int) -> None:
        bid = float(bids[0]["price"]) if bids else 0.0
        ask = float(asks[0]["price"]) if asks else 0.0
        bid_sz = float(bids[0]["size"]) if bids else 0.0
        ask_sz = float(asks[0]["size"]) if asks else 0.0
        with self._lock:
            t = self._data.get(asset_id) or Tick(market_id=market_id)
            t.market_id = market_id
            t.best_bid, t.best_ask = bid, ask
            t.bid_size, t.ask_size = bid_sz, ask_sz
            t.ts_ms = ts_ms
            self._data[asset_id] = t
            self.last_message_ts = time.time()

    def update_price(self, asset_id: str, market_id: str, change: dict, ts_ms: int) -> None:
        side = change.get("side", "").upper()
        price = float(change.get("price", 0.0))
        with self._lock:
            t = self._data.get(asset_id) or Tick(market_id=market_id)
            t.market_id = market_id
            if side == "BUY":
                t.best_bid = price
            elif side == "SELL":
                t.best_ask = price
            t.ts_ms = ts_ms
            self._data[asset_id] = t
            self.last_message_ts = time.time()

    def gc(self, keep: set[str]) -> int:
        """Remove entries whose key is not in *keep*. Returns count removed."""
        with self._lock:
            stale = [k for k in self._data if k not in keep]
            for k in stale:
                del self._data[k]
            return len(stale)

    def snapshot(self) -> dict[str, Tick]:
        with self._lock:
            return dict(self._data)


def handle_message(store: LatestStore, raw: str) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return
    et = msg.get("event_type")
    asset_id = msg.get("asset_id") or msg.get("token_id")
    market_id = msg.get("market", "")
    ts_ms = int(msg.get("timestamp", 0) or 0)
    if et == "book" and asset_id:
        store.update_book(asset_id, market_id, msg.get("bids", []), msg.get("asks", []), ts_ms)
    elif et == "price_change" and asset_id:
        for ch in msg.get("changes", []):
            store.update_price(asset_id, market_id, ch, ts_ms)
    elif et == "last_trade_price" and asset_id:
        with store._lock:
            t = store._data.get(asset_id) or Tick(market_id=market_id)
            t.last_trade = float(msg.get("price", 0.0))
            t.ts_ms = ts_ms
            store._data[asset_id] = t
            store.last_message_ts = time.time()


async def ws_loop(
    cfg: Config,
    store: LatestStore,
    lookup: Callable[[], list[str]],
    stop: asyncio.Event,
    tokens_changed: asyncio.Event,
) -> None:
    """Maintain WS connection with exponential backoff on disconnect.

    Re-subscribes automatically when *tokens_changed* is set (e.g. discovery
    found new markets).  The caller should pass a *lookup* callable that
    returns the current flat list of token IDs.
    """
    backoff = 1.0
    while not stop.is_set():
        token_ids = lookup()
        try:
            async with websockets.connect(cfg.ws_url, ping_interval=20, ping_timeout=20) as wsc:
                await wsc.send(json.dumps({
                    "type": "MARKET",
                    "assets_ids": token_ids,
                }))
                store.connected = True
                backoff = 1.0
                # Spawn a watcher task; when tokens_changed fires it closes the
                # WS connection so we break out of the recv loop and reconnect.
                change_task = asyncio.create_task(tokens_changed.wait())
                async for raw in wsc:
                    handle_message(store, raw)
                    if stop.is_set():
                        break
                    if tokens_changed.is_set():
                        await wsc.close()
                        break
                change_task.cancel()
                try:
                    await change_task
                except (asyncio.CancelledError, Exception):
                    pass
        except Exception as e:
            log.warning("ws disconnect: %s; reconnecting in %.1fs", e, backoff)
            store.connected = False
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60.0)
        finally:
            store.connected = False

        if tokens_changed.is_set():
            tokens_changed.clear()
            log.info("ws: tokens changed, re-subscribing")
            # loop continues immediately (backoff not applied)
