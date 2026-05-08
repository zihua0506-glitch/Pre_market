"""SSH into valkyrie and run inspect_strategy.py. Cached 30s."""
from __future__ import annotations
import asyncio
import json
import time
from dashboard.config import Config

_cache: dict = {"ts": 0.0, "data": None}


async def fetch_strategy_state(cfg: Config, ttl_s: int = 30) -> dict:
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < ttl_s:
        return _cache["data"]
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        cfg.valkyrie_host,
        "/opt/zspd-trader/.venv/bin/python /opt/zspd-trader/src/scripts/inspect_strategy.py",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except asyncio.TimeoutError:
        proc.kill()
        return {"error": "valkyrie timeout"}
    if proc.returncode != 0:
        return {"error": f"valkyrie ssh exit {proc.returncode}",
                "stderr": stderr.decode(errors="replace")[:500]}
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {"error": "bad json from valkyrie", "raw": stdout.decode()[:500]}
    _cache["ts"] = now
    _cache["data"] = data
    return data
