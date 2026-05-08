"""Collector orchestrator. Wires discovery, ws, l2, and HTTP together."""
from __future__ import annotations
import asyncio
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from collector import db, discovery, l2
from collector.config import load
from collector.http import build_app
from collector.ws import LatestStore, ws_loop
import uvicorn

log = logging.getLogger(__name__)


async def health_loop(cfg, store: LatestStore, stop: asyncio.Event, log_path: Path) -> None:
    """Write a 30s heartbeat to *log_path* with WS state and time-since-last-message."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    while not stop.is_set():
        connected = store.connected
        stale = time.time() - store.last_message_ts if store.last_message_ts else None
        line = (
            f"{datetime.now(timezone.utc).isoformat()} "
            f"ws_connected={connected} "
            f"ws_stale_s={stale}\n"
        )
        try:
            with open(log_path, "a") as f:
                f.write(line)
        except OSError:
            pass  # log dir may not be writable; don't crash collector
        try:
            await asyncio.wait_for(stop.wait(), timeout=cfg.health_interval_s)
        except asyncio.TimeoutError:
            pass


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

    health_log_path = Path(
        os.getenv("COLLECTOR_HEALTH_LOG", "/var/log/zspd-collector/health.log")
    )

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
        asyncio.create_task(
            health_loop(cfg, store, stop, health_log_path), name="health"
        ),
    ]
    await stop.wait()
    server.should_exit = True
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
