"""Tests for valkyrie SSH client caching and stdout cap."""
from __future__ import annotations
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dashboard.config import Config


def _make_proc(returncode: int, stdout: bytes, stderr: bytes = b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = MagicMock()
    proc.stdout.read = AsyncMock(return_value=stdout)
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=stderr)
    proc.wait = AsyncMock()
    proc.kill = MagicMock()
    return proc


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset the module-level _cache between tests."""
    import dashboard.clients.valkyrie as v
    v._cache = {"ts": 0.0, "data": None}
    yield
    v._cache = {"ts": 0.0, "data": None}


@pytest.mark.asyncio
async def test_success_is_cached():
    cfg = Config(auth_token="x", allow_no_auth=True)
    payload = {"state": "ok"}
    proc = _make_proc(0, json.dumps(payload).encode())

    with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
        from dashboard.clients.valkyrie import fetch_strategy_state
        r1 = await fetch_strategy_state(cfg)
        r2 = await fetch_strategy_state(cfg)

    assert r1 == payload
    assert r2 == payload
    # Second call should have been served from cache — subprocess called only once
    assert mock_exec.call_count == 1


@pytest.mark.asyncio
async def test_failure_is_cached_for_5s():
    cfg = Config(auth_token="x", allow_no_auth=True)
    proc = _make_proc(1, b"", b"connection refused")

    with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
         patch("dashboard.clients.valkyrie.time.time", side_effect=[0.0, 3.0, 6.0]):
        from dashboard.clients.valkyrie import fetch_strategy_state
        r1 = await fetch_strategy_state(cfg)   # time=0 → miss → subprocess
        r2 = await fetch_strategy_state(cfg)   # time=3 → hit (within 5s TTL)
        r3 = await fetch_strategy_state(cfg)   # time=6 → miss (past 5s TTL)

    assert "error" in r1
    assert "error" in r2
    assert "error" in r3
    # Called on first fetch and after 5s TTL expires
    assert mock_exec.call_count == 2


@pytest.mark.asyncio
async def test_success_cached_for_30s():
    cfg = Config(auth_token="x", allow_no_auth=True)
    payload = {"state": "ok"}
    proc = _make_proc(0, json.dumps(payload).encode())

    with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
         patch("dashboard.clients.valkyrie.time.time", side_effect=[0.0, 20.0, 35.0]):
        from dashboard.clients.valkyrie import fetch_strategy_state
        await fetch_strategy_state(cfg)   # time=0 → miss
        await fetch_strategy_state(cfg)   # time=20 → hit
        await fetch_strategy_state(cfg)   # time=35 → miss (past 30s TTL)

    assert mock_exec.call_count == 2
