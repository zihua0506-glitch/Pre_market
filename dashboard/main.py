"""Dashboard FastAPI entrypoint."""
from __future__ import annotations
import time
from pathlib import Path
import httpx
from fastapi import FastAPI, Depends
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from dashboard.auth import bearer_auth
from dashboard.config import load
from dashboard.views import grid as grid_view
from dashboard.views import market as market_view
from dashboard.views import strategy as strategy_view
from dashboard.views import replay as replay_view


def build_app() -> FastAPI:
    cfg = load()
    app = FastAPI(title="ZSPD Polymarket Dashboard")
    base = Path(__file__).parent
    static_dir = base / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
    auth = bearer_auth(cfg)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/health-badge", response_class=HTMLResponse)
    async def health_badge():
        """Tiny HTML fragment shown in the top nav. Polled every 15s."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(f"{cfg.collector_url}/health")
                r.raise_for_status()
                d = r.json()
            connected = d.get("ws_connected", False)
            stale_s = d.get("ws_stale_s") or 0
            color = "emerald" if connected and stale_s < 30 else ("amber" if connected else "rose")
            label = "live" if connected else "down"
            return (
                f'<span class="inline-flex items-center gap-1.5">'
                f'<span class="w-1.5 h-1.5 rounded-full bg-{color}-500"></span>'
                f'<span class="text-{color}-700 dark:text-{color}-400">{label}</span>'
                f'<span class="text-zinc-400 dark:text-zinc-500">· {stale_s:.0f}s</span>'
                f'</span>'
            )
        except Exception:
            return (
                '<span class="inline-flex items-center gap-1.5">'
                '<span class="w-1.5 h-1.5 rounded-full bg-zinc-400"></span>'
                '<span class="text-zinc-500">collector unreachable</span>'
                '</span>'
            )

    app.include_router(grid_view.router, dependencies=[Depends(auth)])
    app.include_router(market_view.router, dependencies=[Depends(auth)])
    app.include_router(strategy_view.router, dependencies=[Depends(auth)])
    app.include_router(replay_view.router, dependencies=[Depends(auth)])
    return app


app = build_app()


if __name__ == "__main__":
    import uvicorn
    cfg = load()
    uvicorn.run(app, host=cfg.listen_host, port=cfg.listen_port)
