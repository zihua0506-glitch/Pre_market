"""Test that archiver cleanup runs even when there is no data for the day."""
from __future__ import annotations
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
async def test_cleanup_runs_on_empty_day(tmp_path: Path, monkeypatch):
    cfg = _make_cfg(tmp_path)

    with (
        patch("archiver.main.load", return_value=cfg),
        patch("archiver.main.export_day", new_callable=AsyncMock, return_value=0) as mock_export,
        patch("archiver.main._dump_sqlite_to_parquet", return_value=0) as mock_dump,
        patch("archiver.main._dump_markets_snapshot", return_value=0),
        patch("archiver.main._rclone_sync") as mock_rclone,
        patch("archiver.cleanup.old_warm_dirs", return_value=[]) as mock_old_dirs,
        patch("archiver.cleanup.delete_old_sqlite_rows", return_value=0) as mock_delete,
    ):
        from archiver.main import run
        rc = await run()

    # No data -> rclone should NOT have been called
    mock_rclone.assert_not_called()
    # But cleanup helpers MUST have been called regardless
    mock_old_dirs.assert_called_once()
    assert mock_delete.call_count == 2
    assert rc == 0
