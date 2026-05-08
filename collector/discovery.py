"""Polymarket gamma discovery — finds weather contracts and upserts to markets.db."""
from __future__ import annotations
import asyncio
import logging
import re
from pathlib import Path
from typing import Any
import httpx
from collector import db
from collector.config import Config

log = logging.getLogger(__name__)

# Known airport codes / city tokens we recognise in slugs. Add more as we see them.
_CITY_TOKENS = {
    "zspd": "ZSPD", "shanghai": "ZSPD",
    "kjfk": "KJFK", "jfk": "KJFK",
    "klga": "KLGA", "laguardia": "KLGA",
    "kewr": "KEWR", "newark": "KEWR",
    "kmia": "KMIA", "miami": "KMIA",
    "klax": "KLAX",
    "kord": "KORD", "chicago": "KORD",
    "katx": "KATX", "austin": "KATX",
    "kdca": "KDCA", "washington": "KDCA",
    "kbos": "KBOS", "boston": "KBOS",
    "ksfo": "KSFO", "san-francisco": "KSFO",
}

_HIGH_LOW = {"high": "high", "low": "low", "tmax": "high", "tmin": "low"}

# 25c, 25-c, 25-degrees, 25f handled at parse time
_THRESHOLD_RE = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*(?:c|celsius|degree)\b")
_THRESHOLD_RE_F = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*(?:f|fahrenheit)\b")


def classify_slug(slug: str) -> dict[str, Any] | None:
    """Parse a gamma slug into a structured dict, or None if not weather-shaped."""
    s = slug.lower()
    # city
    city: str | None = None
    for tok, code in _CITY_TOKENS.items():
        if tok in s:
            city = code
            break
    if city is None:
        return None
    # high/low
    metric: str | None = None
    for tok, m in _HIGH_LOW.items():
        if tok in s:
            metric = m
            break
    if metric is None:
        return None
    # comparison direction
    if "-or-higher" in s or "above" in s or "ge-" in s:
        cmp_ = "ge"
    elif "-or-lower" in s or "below" in s or "le-" in s:
        cmp_ = "le"
    elif "-eq-" in s or "exactly" in s:
        cmp_ = "eq"
    else:
        return None
    # threshold (Celsius preferred; F is converted)
    m_c = _THRESHOLD_RE.search(s)
    if m_c:
        threshold_c = float(m_c.group(1))
    else:
        m_f = _THRESHOLD_RE_F.search(s)
        if m_f:
            threshold_c = (float(m_f.group(1)) - 32) * 5 / 9
        else:
            return None
    return {
        "city": city,
        "contract_type": f"{metric}_{cmp_}",
        "threshold_c": round(threshold_c, 2),
    }


async def fetch_weather_events(cfg: Config, *, http: httpx.AsyncClient) -> list[dict]:
    """Page through gamma /events?tag=weather and return raw event dicts."""
    out: list[dict] = []
    offset = 0
    page = 100
    while True:
        r = await http.get(
            f"{cfg.gamma_host}/events",
            params={"tag": cfg.weather_tag, "closed": "false",
                    "limit": page, "offset": offset},
            timeout=15.0,
        )
        r.raise_for_status()
        items = r.json()
        if not items:
            break
        out.extend(items)
        offset += page
        if len(items) < page:
            break
    return out


async def discover_once(cfg: Config) -> int:
    """One full discovery pass. Returns count of markets upserted."""
    db.init(cfg.db_dir)
    skipped = 0
    saved = 0
    async with httpx.AsyncClient() as http:
        events = await fetch_weather_events(cfg, http=http)
        for ev in events:
            for mkt in ev.get("markets", []):
                slug = mkt.get("slug") or ""
                cls = classify_slug(slug)
                if cls is None:
                    skipped += 1
                    continue
                tokens = mkt.get("clobTokenIds") or []
                if isinstance(tokens, str):
                    # gamma sometimes returns JSON-encoded string
                    import json
                    tokens = json.loads(tokens)
                if len(tokens) < 2:
                    skipped += 1
                    continue
                db.upsert_market(
                    cfg.db_dir,
                    market_id=str(mkt.get("id")),
                    slug=slug,
                    question=mkt.get("question"),
                    end_date=mkt.get("endDate"),
                    tag=cfg.weather_tag,
                    city=cls["city"],
                    contract_type=cls["contract_type"],
                    threshold_c=cls["threshold_c"],
                    threshold_text=str(cls["threshold_c"]),
                    token_id_yes=str(tokens[0]),
                    token_id_no=str(tokens[1]),
                )
                saved += 1
    log.info("discovery: saved=%d skipped=%d", saved, skipped)
    return saved


async def discovery_loop(cfg: Config, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await discover_once(cfg)
        except Exception:
            log.exception("discovery pass failed; retrying in 5 min")
            await asyncio.wait_for(stop.wait(), timeout=300.0)
        else:
            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg.discovery_interval_s)
            except asyncio.TimeoutError:
                pass
