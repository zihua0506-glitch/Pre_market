"""Read-only client for collector's /snapshot endpoint."""
from __future__ import annotations
import httpx
from dashboard.config import Config


async def fetch_snapshot(cfg: Config) -> list[dict]:
    async with httpx.AsyncClient(timeout=5.0) as http:
        r = await http.get(f"{cfg.collector_url}/snapshot")
        r.raise_for_status()
        return r.json()
