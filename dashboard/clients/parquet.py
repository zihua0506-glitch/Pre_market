"""Read warm-tier parquet rolls."""
from __future__ import annotations
from pathlib import Path
import polars as pl


def load_day(warm_dir: Path, date_iso: str, kind: str) -> pl.DataFrame | None:
    """kind in {'prices','orderbook','trades','markets'}."""
    p = warm_dir / date_iso / f"{kind}.parquet"
    if not p.exists():
        return None
    return pl.read_parquet(p)
