"""Polymarket gamma discovery — finds weather contracts and upserts to markets.db.

Real gamma format (verified 2026-05-08):

- Events endpoint: ``GET /events?tag_slug=daily-temperature&closed=false`` returns
  ~150-200 events. Each event has ``tags`` containing one city slug
  (``london``, ``shanghai``, ``new-york-city``, ...) and one of
  ``highest-temperature`` / ``lowest-temperature``.
- Each event has 11 markets (one per °C threshold). Market slug pattern:
  ``{lowest|highest}-temperature-in-{city}-on-{month}-{day}-{date_year}-{N}c``
  where the trailing ``{N}c`` becomes ``{N}corbelow`` for ≤, ``{N}corabove``
  for ≥, and bare ``{N}c`` for ==.

We use **event tags** for city/kind, and only the trailing fragment of the
market slug for threshold/comparison. ``classify_slug`` is kept for backward
compat with the unit tests but is no longer called from ``discover_once``.
"""
from __future__ import annotations
import asyncio
import json
import logging
import re
from typing import Any
import httpx
from collector import db
from collector.config import Config

log = logging.getLogger(__name__)

# Map tag-slug city -> our internal code. Shanghai uses ZSPD (matches the
# existing ML pipeline / strategy code). For other cities we just uppercase
# the slug because there isn't always an obvious airport code.
_CITY_TAG_TO_CODE = {
    "shanghai": "ZSPD",
    "new-york-city": "NYC",
    "london": "LON",
    "paris": "PAR",
    "seoul": "SEL",
    "miami": "MIA",
    "tokyo": "TYO",
    "hong-kong": "HKG",
    "los-angeles": "LAX",
    "chicago": "ORD",
    "san-francisco": "SFO",
    "berlin": "BER",
    "moscow": "MOW",
    "sydney": "SYD",
    "dubai": "DXB",
    "delhi": "DEL",
    "beijing": "PEK",
    "singapore": "SIN",
    "bangkok": "BKK",
    "mumbai": "BOM",
    "rome": "ROM",
    "madrid": "MAD",
    "istanbul": "IST",
    "mexico-city": "MEX",
    "buenos-aires": "BUE",
    "rio-de-janeiro": "RIO",
    "sao-paulo": "GRU",
    "toronto": "YYZ",
}

# Trailing slug fragment, e.g. "-4corbelow", "-23c", "--5corabove".
_THRESHOLD_RE = re.compile(r"-(-?\d+(?:\.\d+)?)c(orbelow|orabove)?$")

# Legacy regexes kept for the original ``classify_slug`` path.
_LEGACY_THRESHOLD_RE = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*(?:c|celsius|degree)\b")
_LEGACY_THRESHOLD_RE_F = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*(?:f|fahrenheit)\b")
_LEGACY_CITY_TOKENS = {
    "zspd": "ZSPD", "shanghai": "ZSPD",
    "kjfk": "KJFK", "jfk": "KJFK",
    "klga": "KLGA", "laguardia": "KLGA",
    "kewr": "KEWR", "newark": "KEWR",
    "kmia": "KMIA", "miami": "MIA",
    "klax": "KLAX",
    "kord": "KORD", "chicago": "ORD",
    "katx": "KATX", "austin": "AUS",
    "kdca": "KDCA", "washington": "DCA",
    "kbos": "KBOS", "boston": "BOS",
    "ksfo": "KSFO", "san-francisco": "SFO",
}


def city_code_for(tag_slugs: list[str] | set[str]) -> str | None:
    """Pick a city code from a list of event tag slugs (returns None if unknown)."""
    tags = set(tag_slugs)
    for slug, code in _CITY_TAG_TO_CODE.items():
        if slug in tags:
            return code
    # Fall back: the first tag that is *not* one of the controlled vocab
    # ('weather', 'recurring', 'hide-from-new', 'daily-temperature',
    # 'highest-temperature', 'lowest-temperature', 'rewards-automation-*').
    controlled = {
        "weather", "recurring", "hide-from-new", "daily-temperature",
        "highest-temperature", "lowest-temperature",
    }
    for t in tags:
        if t in controlled or t.startswith("rewards-automation"):
            continue
        # Treat unknown city tag as the city code (uppercased + dashes -> dashes).
        return t.upper()
    return None


def classify_event_market(event: dict, market: dict) -> dict[str, Any] | None:
    """Use event.tags to determine city/kind; market.slug only for threshold."""
    tag_slugs = [t.get("slug", "") for t in (event.get("tags") or [])]
    if "daily-temperature" not in tag_slugs:
        return None
    city = city_code_for(tag_slugs)
    if not city:
        return None
    if "highest-temperature" in tag_slugs:
        kind = "high"
    elif "lowest-temperature" in tag_slugs:
        kind = "low"
    else:
        return None
    slug = (market.get("slug") or "").lower()
    m = _THRESHOLD_RE.search(slug)
    if not m:
        return None
    threshold_c = float(m.group(1))
    suffix = m.group(2) or ""
    if suffix == "orbelow":
        cmp_ = "le"
    elif suffix == "orabove":
        cmp_ = "ge"
    else:
        cmp_ = "eq"
    return {
        "city": city,
        "contract_type": f"{kind}_{cmp_}",
        "threshold_c": round(threshold_c, 2),
    }


def classify_slug(slug: str) -> dict[str, Any] | None:
    """Legacy heuristic that infers everything from the slug alone.

    Kept for the unit-test fixture (`tests/collector/test_discovery.py`),
    which uses synthetic airport-prefixed slugs that wouldn't exist in
    real gamma data. ``discover_once`` now uses ``classify_event_market``.
    """
    s = slug.lower()
    city: str | None = None
    for tok, code in _LEGACY_CITY_TOKENS.items():
        if tok in s:
            city = code
            break
    if city is None:
        return None
    if "high" in s or "tmax" in s:
        kind = "high"
    elif "low" in s or "tmin" in s:
        kind = "low"
    else:
        return None
    if "-or-higher" in s or "above" in s or "ge-" in s:
        cmp_ = "ge"
    elif "-or-lower" in s or "below" in s or "le-" in s:
        cmp_ = "le"
    elif "-eq-" in s or "exactly" in s:
        cmp_ = "eq"
    else:
        return None
    m_c = _LEGACY_THRESHOLD_RE.search(s)
    if m_c:
        threshold_c = float(m_c.group(1))
    else:
        m_f = _LEGACY_THRESHOLD_RE_F.search(s)
        if m_f:
            threshold_c = (float(m_f.group(1)) - 32) * 5 / 9
        else:
            return None
    return {
        "city": city,
        "contract_type": f"{kind}_{cmp_}",
        "threshold_c": round(threshold_c, 2),
    }


async def fetch_daily_temperature_events(cfg: Config, *, http: httpx.AsyncClient) -> list[dict]:
    """Page through gamma /events?tag_slug=daily-temperature&closed=false.

    Bounded — gamma has ~200 daily temperature events at any time. We cap at
    20 pages (= 2000 events) as a safety net.
    """
    out: list[dict] = []
    offset = 0
    page = 100
    for _ in range(20):
        r = await http.get(
            f"{cfg.gamma_host}/events",
            params={"tag_slug": "daily-temperature", "closed": "false",
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
        events = await fetch_daily_temperature_events(cfg, http=http)
        for ev in events:
            for mkt in ev.get("markets", []):
                cls = classify_event_market(ev, mkt)
                if cls is None:
                    skipped += 1
                    continue
                tokens = mkt.get("clobTokenIds") or []
                if isinstance(tokens, str):
                    tokens = json.loads(tokens)
                if len(tokens) < 2:
                    skipped += 1
                    continue
                db.upsert_market(
                    cfg.db_dir,
                    market_id=str(mkt.get("id")),
                    slug=mkt.get("slug") or "",
                    question=mkt.get("question"),
                    end_date=mkt.get("endDate"),
                    tag="daily-temperature",
                    city=cls["city"],
                    contract_type=cls["contract_type"],
                    threshold_c=cls["threshold_c"],
                    threshold_text=str(cls["threshold_c"]),
                    token_id_yes=str(tokens[0]),
                    token_id_no=str(tokens[1]),
                )
                saved += 1
    log.info("discovery: events=%d saved=%d skipped=%d", len(events), saved, skipped)
    return saved


async def discovery_loop(cfg: Config, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await discover_once(cfg)
        except Exception:
            log.exception("discovery pass failed; retrying in 5 min")
            try:
                await asyncio.wait_for(stop.wait(), timeout=300.0)
            except asyncio.TimeoutError:
                pass
        else:
            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg.discovery_interval_s)
            except asyncio.TimeoutError:
                pass


# Backward-compat alias for code that still references the old name.
fetch_weather_events = fetch_daily_temperature_events
