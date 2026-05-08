"""Export a day of VM tick data to parquet."""
from __future__ import annotations
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator
import httpx
import polars as pl


def _row_from_native_chunk(chunk: dict) -> Iterator[dict]:
    metric = chunk["metric"]
    name = metric.get("__name__", "")
    market_id = metric.get("market_id", "")
    city = metric.get("city", "")
    ctype = metric.get("contract_type", "")
    for ts, val in chunk.get("values", []):
        yield {"ts": int(ts), "metric": name, "market_id": market_id,
               "city": city, "contract_type": ctype, "value": float(val)}


async def export_day(vm_url: str, day: date, dest: Path) -> int:
    """Pull all `pm_*` series for `day`, write to parquet. Returns row count."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp()
    end = start + 86400
    async with httpx.AsyncClient(timeout=120.0) as http:
        # VM's /api/v1/query_range with __name__=~"pm_.*"
        r = await http.get(f"{vm_url}/api/v1/query_range",
                           params={"query": '{__name__=~"pm_.*"}',
                                   "start": int(start), "end": int(end), "step": "3s"})
        r.raise_for_status()
        data = r.json()["data"]["result"]
    rows: list[dict] = []
    for chunk in data:
        rows.extend(_row_from_native_chunk(chunk))
    if not rows:
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(dest, compression="zstd")
    return len(rows)
