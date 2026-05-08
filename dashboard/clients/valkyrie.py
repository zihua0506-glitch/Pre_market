"""SSH into valkyrie and run inspect_strategy.py. Cached 30s (5s on error)."""
from __future__ import annotations
import asyncio
import json
import logging
import time
from dashboard.config import Config

log = logging.getLogger(__name__)

_cache: dict = {"ts": 0.0, "data": None}

_SUCCESS_TTL_S = 30
_ERROR_TTL_S = 5
_STDOUT_LIMIT = 256 * 1024  # 256 KiB
_STDERR_LIMIT = 8 * 1024    # 8 KiB


async def fetch_strategy_state(cfg: Config, ttl_s: int = _SUCCESS_TTL_S) -> dict:
    now = time.time()
    if _cache["data"] is not None:
        cached_ttl = _ERROR_TTL_S if "error" in _cache["data"] else ttl_s
        if now - _cache["ts"] < cached_ttl:
            return _cache["data"]

    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        cfg.valkyrie_host,
        "/opt/zspd-trader/.venv/bin/python /opt/zspd-trader/src/scripts/inspect_strategy.py",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )

    def _cache_error(err: dict) -> dict:
        _cache["ts"] = now
        _cache["data"] = err
        return err

    try:
        # Cap stdout to avoid unbounded memory use
        stdout = await asyncio.wait_for(proc.stdout.read(_STDOUT_LIMIT), timeout=10.0)
        stderr = await proc.stderr.read(_STDERR_LIMIT)
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        return _cache_error({"error": "valkyrie timeout"})

    if len(stdout) == _STDOUT_LIMIT:
        log.warning("valkyrie stdout truncated at %d bytes; treating as parse failure", _STDOUT_LIMIT)
        return _cache_error({"error": "valkyrie stdout truncated"})

    if proc.returncode != 0:
        return _cache_error({
            "error": f"valkyrie ssh exit {proc.returncode}",
            "stderr": stderr.decode(errors="replace")[:500],
        })

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return _cache_error({"error": "bad json from valkyrie", "raw": stdout.decode()[:500]})

    _cache["ts"] = now
    _cache["data"] = data
    return data
