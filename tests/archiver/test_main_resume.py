"""Test that archiver skips VM/SQLite re-export when warm parquet already built."""
from __future__ import annotations
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest


def _make_cfg(tmp_path: Path):
    from archiver.config import Config
    return Config(
        db_dir=tmp_path / "db",
        warm_dir=tmp_path / "warm",
        vm_url="http://localhost:9999",
        rclone_remote="fake:remote",
        hot_retention_days=30,
        warm_retention_days=7,
    )


@pytest.mark.asyncio
async def test_export_day_called_only_once_on_second_run(tmp_path: Path):
    cfg = _make_cfg(tmp_path)

    with (
        patch("archiver.main.load", return_value=cfg),
        patch("archiver.main.export_day", new_callable=AsyncMock, return_value=5) as mock_export,
        patch("archiver.main._dump_sqlite_to_parquet", return_value=3),
        patch("archiver.main._dump_markets_snapshot", return_value=2),
        patch("archiver.main._rclone_sync"),
        patch("archiver.cleanup.old_warm_dirs", return_value=[]),
        patch("archiver.cleanup.delete_old_sqlite_rows", return_value=0),
    ):
        from archiver.main import run

        # First run: warm parquet not present → export_day should run
        rc1 = await run()
        assert rc1 == 0
        assert mock_export.call_count == 1

        # Second run: .warm_complete marker now present → export_day must NOT run again
        rc2 = await run()
        assert rc2 == 0
        assert mock_export.call_count == 1  # still 1 — not called a second time
