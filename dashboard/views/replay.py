"""View D — historical replay."""
from __future__ import annotations
from pathlib import Path
import polars as pl
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from dashboard.clients.parquet import load_day
from dashboard.config import load

router = APIRouter()
_templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


@router.get("/replay/{date_iso}", response_class=HTMLResponse)
async def replay(request: Request, date_iso: str):
    cfg = load()
    df = load_day(cfg.warm_dir, date_iso, "prices")
    if df is None:
        return _templates.TemplateResponse(request,
            "replay.html", {"date": date_iso, "rows": None})
    summary = (
        df.group_by("market_id").agg(pl.len().alias("n_ticks"))
        .sort("n_ticks", descending=True).head(50).to_dicts()
    )
    return _templates.TemplateResponse(request,
        "replay.html", {"date": date_iso, "rows": summary})


@router.get("/replay", response_class=HTMLResponse)
async def replay_index(request: Request):
    cfg = load()
    days = sorted(p.name for p in cfg.warm_dir.iterdir() if p.is_dir()) if cfg.warm_dir.exists() else []
    return _templates.TemplateResponse(request,
        "replay.html", {"date": None, "days": days, "rows": None})
