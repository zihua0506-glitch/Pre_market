"""Collector orchestrator. Wires discovery, ws, l2, and HTTP together."""
from __future__ import annotations
import asyncio
import logging
import signal
from collector import db, discovery, l2
from collector.config import load
from collector.http import build_app
from collector.ws import LatestStore, ws_loop
import uvicorn

log = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load()
    db.init(cfg.db_dir)

    store = LatestStore()
    stop = asyncio.Event()
    tokens_changed = asyncio.Event()

    def _on_signal(*_):
        log.info("signal received, shutting down")
        stop.set()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    def lookup_markets() -> list[dict]:
        return db.list_active_markets(cfg.db_dir)

    def lookup_tokens() -> list[str]:
        ids: list[str] = []
        for m in lookup_markets():
            ids += [m["token_id_yes"], m["token_id_no"]]
        return ids

    def _prune(keep: set[str]) -> None:
        removed = store.gc(keep)
        if removed:
            log.info("store.gc: removed %d stale token entries", removed)

    # Bootstrap discovery so we have markets for WS subscribe.
    # tokens_changed will be set here if any markets are saved; ws_loop will
    # see it on its first iteration, clear it, and subscribe.
    await discovery.discover_once(cfg, tokens_changed=tokens_changed, prune=_prune)

    log.info("subscribing to %d tokens", len(lookup_tokens()))

    app = build_app(store=store, markets_lookup=lookup_markets)
    server = uvicorn.Server(uvicorn.Config(
        app, host=cfg.http_host, port=cfg.http_port, log_level="warning"
    ))

    tasks = [
        asyncio.create_task(server.serve(), name="http"),
        asyncio.create_task(
            ws_loop(cfg, store, lookup_tokens, stop, tokens_changed), name="ws"
        ),
        asyncio.create_task(l2.l2_loop(cfg, stop), name="l2"),
        asyncio.create_task(
            discovery.discovery_loop(
                cfg, stop, tokens_changed=tokens_changed, prune=_prune
            ),
            name="discovery",
        ),
    ]
    await stop.wait()
    server.should_exit = True
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
