"""Light test for health_loop: verifies a line is written to the log file."""
from __future__ import annotations
import asyncio
from pathlib import Path

import pytest

from collector.config import Config
from collector.main import health_loop
from collector.ws import LatestStore


@pytest.mark.asyncio
async def test_health_loop_writes_line(tmp_path: Path):
    cfg = Config(health_interval_s=1)
    store = LatestStore()
    stop = asyncio.Event()
    log_path = tmp_path / "health.log"

    # Run health_loop for one tick then stop it.
    async def _stopper():
        # Give the loop one iteration to write
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(
        health_loop(cfg, store, stop, log_path),
        _stopper(),
    )

    assert log_path.exists(), "health.log was not created"
    content = log_path.read_text()
    assert "ws_connected=" in content
    assert "ws_stale_s=" in content
