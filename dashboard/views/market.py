"""View B — single market drill-down."""
from __future__ import annotations
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from dashboard.clients.sqlite import market_meta, latest_l2, recent_trades
from dashboard.config import load

router = APIRouter()
_templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


@router.get("/m/{market_id}", response_class=HTMLResponse)
async def market(request: Request, market_id: str):
    cfg = load()
    meta = market_meta(cfg.db_dir, market_id)
    if not meta:
        raise HTTPException(404, "market not found")
    l2 = latest_l2(cfg.db_dir, market_id)
    trades = recent_trades(cfg.db_dir, market_id, n=50)
    grafana_panel = (
        f"{cfg.grafana_url}/d-solo/zspd-overview/zspd-overview?"
        f"var-market={market_id}&theme=light&panelId=1"
    )
    return _templates.TemplateResponse(request, "market.html", {
        "m": meta, "l2": l2, "trades": trades, "grafana_panel": grafana_panel,
    })
