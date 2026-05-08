"""View C — ZSPD strategy ops."""
from __future__ import annotations
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from dashboard.clients.valkyrie import fetch_strategy_state
from dashboard.config import load

router = APIRouter()
_templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


@router.get("/strategy", response_class=HTMLResponse)
async def strategy(request: Request):
    cfg = load()
    state = await fetch_strategy_state(cfg)
    return _templates.TemplateResponse(request, "strategy.html", {"s": state})
