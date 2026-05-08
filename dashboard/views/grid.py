"""View A — market grid with filtering, sorting, color-coded prices."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from dashboard.clients.collector import fetch_snapshot
from dashboard.clients.sqlite import all_markets
from dashboard.config import load

router = APIRouter()
_templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")

# Order matters for the chip filter UI.
_CONTRACT_TYPES = ["high_eq", "high_ge", "high_le", "low_eq", "low_ge", "low_le"]
_SORT_KEYS = {
    "spread":     lambda m: -((m.get("yes_ask") or 0) - (m.get("yes_bid") or 0)),
    "yes_mid":    lambda m: -((m.get("yes_bid") or 0) + (m.get("yes_ask") or 0)) / 2,
    "city":       lambda m: (m.get("city") or "", m.get("threshold_c") or 0),
    "end_date":   lambda m: m.get("end_date") or "9999",
    "active":     lambda m: -(m.get("ts_ms") or 0),  # most recently updated first
    "threshold":  lambda m: m.get("threshold_c") if m.get("threshold_c") is not None else 999,
}


def _enrich(snap: list[dict], meta: dict[str, dict]) -> list[dict]:
    """Merge collector snapshot with markets-table metadata."""
    out: list[dict] = []
    for s in snap:
        mid = s["market_id"]
        m = dict(meta.get(mid, {}))
        m.update(s)  # snapshot fields win
        # Normalise: ensure threshold/end_date present from meta even if absent from snapshot
        m.setdefault("threshold_c", meta.get(mid, {}).get("threshold_c"))
        m.setdefault("end_date", meta.get(mid, {}).get("end_date"))
        m.setdefault("question", meta.get(mid, {}).get("question"))
        out.append(m)
    return out


def _has_prices(m: dict) -> bool:
    return bool(
        (m.get("yes_bid") or 0) > 0 or (m.get("yes_ask") or 0) > 0
        or (m.get("no_bid") or 0) > 0 or (m.get("no_ask") or 0) > 0
    )


def _within_days(end_date: str | None, days: int) -> bool:
    if not end_date or days <= 0:
        return True
    try:
        end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return True
    delta = (end - datetime.now(timezone.utc)).total_seconds() / 86400.0
    return -1 <= delta <= days


@router.get("/", response_class=HTMLResponse)
async def grid(
    request: Request,
    city: str | None = None,
    contract_type: str | None = None,
    q: str | None = None,
    has_prices: int = 1,
    end_within_days: int = 7,
    sort: str = "active",
    page: int = 1,
    per_page: int = 100,
):
    cfg = load()
    snap = await fetch_snapshot(cfg)
    meta = all_markets(cfg.db_dir)
    rows = _enrich(snap, meta)

    # City counts for the filter sidebar — computed BEFORE city filter is applied.
    city_counts: dict[str, dict[str, int]] = {}
    for m in rows:
        c = m.get("city") or "—"
        cc = city_counts.setdefault(c, {"total": 0, "active": 0})
        cc["total"] += 1
        if _has_prices(m):
            cc["active"] += 1
    cities_sorted = sorted(city_counts.items(), key=lambda kv: -kv[1]["active"])

    # Apply filters.
    if city:
        rows = [m for m in rows if (m.get("city") or "") == city]
    if contract_type:
        rows = [m for m in rows if (m.get("contract_type") or "") == contract_type]
    if q:
        ql = q.lower()
        rows = [m for m in rows if ql in (m.get("question") or "").lower()
                or ql in (m.get("slug") or "").lower()
                or ql in (m.get("market_id") or "")]
    if has_prices:
        rows = [m for m in rows if _has_prices(m)]
    if end_within_days > 0:
        rows = [m for m in rows if _within_days(m.get("end_date"), end_within_days)]

    # Sort.
    sort_fn = _SORT_KEYS.get(sort, _SORT_KEYS["active"])
    rows.sort(key=sort_fn)

    total_filtered = len(rows)
    pages = max(1, (total_filtered + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    paged = rows[start:start + per_page]

    return _templates.TemplateResponse(request, "grid.html", {
        "markets": paged,
        "city": city,
        "contract_type": contract_type,
        "q": q,
        "has_prices": bool(has_prices),
        "end_within_days": end_within_days,
        "sort": sort,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "total_filtered": total_filtered,
        "total_all": len(snap),
        "cities": cities_sorted,
        "contract_types": _CONTRACT_TYPES,
    })
