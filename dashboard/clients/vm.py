"""PromQL queries against VictoriaMetrics."""
from __future__ import annotations
import httpx
from dashboard.config import Config


async def query_range(cfg: Config, expr: str, start_unix: int, end_unix: int, step_s: int) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as http:
        r = await http.get(f"{cfg.vm_url}/api/v1/query_range",
                           params={"query": expr, "start": start_unix,
                                   "end": end_unix, "step": f"{step_s}s"})
        r.raise_for_status()
        return r.json()
