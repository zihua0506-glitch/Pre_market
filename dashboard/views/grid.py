"""View A — market grid."""
from __future__ import annotations
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from dashboard.clients.collector import fetch_snapshot
from dashboard.config import load

router = APIRouter()
_templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


@router.get("/", response_class=HTMLResponse)
async def grid(request: Request, city: str | None = None,
               contract_type: str | None = None):
    cfg = load()
    snap = await fetch_snapshot(cfg)
    if city:
        snap = [m for m in snap if m["city"] == city]
    if contract_type:
        snap = [m for m in snap if m["contract_type"] == contract_type]
    snap.sort(key=lambda m: -(m.get("yes_ask") or 0) + (m.get("yes_bid") or 0))
    return _templates.TemplateResponse(request,
        "grid.html", {"markets": snap, "city": city, "contract_type": contract_type})
