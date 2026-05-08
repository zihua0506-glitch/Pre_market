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
    # bootstrap discovery so we have markets for ws subscribe
    await discovery.discover_once(cfg)

    store = LatestStore()
    stop = asyncio.Event()

    def _on_signal(*_):
        log.info("signal received, shutting down")
        stop.set()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    def lookup() -> list[dict]:
        return db.list_active_markets(cfg.db_dir)

    token_ids: list[str] = []
    for m in lookup():
        token_ids += [m["token_id_yes"], m["token_id_no"]]
    log.info("subscribing to %d tokens", len(token_ids))

    app = build_app(store=store, markets_lookup=lookup)
    server = uvicorn.Server(uvicorn.Config(
        app, host=cfg.http_host, port=cfg.http_port, log_level="warning"
    ))

    tasks = [
        asyncio.create_task(server.serve(), name="http"),
        asyncio.create_task(ws_loop(cfg, store, token_ids, stop), name="ws"),
        asyncio.create_task(l2.l2_loop(cfg, stop), name="l2"),
        asyncio.create_task(discovery.discovery_loop(cfg, stop), name="discovery"),
    ]
    await stop.wait()
    server.should_exit = True
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
