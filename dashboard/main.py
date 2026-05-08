"""Dashboard FastAPI entrypoint."""
from __future__ import annotations
from pathlib import Path
from fastapi import FastAPI, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dashboard.auth import bearer_auth
from dashboard.config import load
from dashboard.views import grid as grid_view
from dashboard.views import market as market_view


def build_app() -> FastAPI:
    cfg = load()
    app = FastAPI(title="ZSPD Polymarket Dashboard")
    base = Path(__file__).parent
    app.mount("/static", StaticFiles(directory=base / "static"), name="static")
    templates = Jinja2Templates(directory=base / "templates")
    auth = bearer_auth(cfg)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    app.include_router(grid_view.router, dependencies=[Depends(auth)])
    app.include_router(market_view.router, dependencies=[Depends(auth)])
    return app


app = build_app()


if __name__ == "__main__":
    import uvicorn
    cfg = load()
    uvicorn.run(app, host=cfg.listen_host, port=cfg.listen_port)
