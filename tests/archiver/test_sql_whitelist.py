"""Test that the SQL identifier whitelist rejects unknown table/ts_col pairs."""
from __future__ import annotations
from pathlib import Path

import pytest

from archiver.cleanup import delete_old_sqlite_rows


def test_validated_rejects_unknown_table(tmp_path: Path):
    db_path = tmp_path / "fake.db"
    db_path.touch()
    with pytest.raises(ValueError, match="refusing unknown table/ts_col"):
        delete_old_sqlite_rows(db_path, "users", "ts", 30)


def test_validated_rejects_wrong_ts_col(tmp_path: Path):
    db_path = tmp_path / "fake.db"
    db_path.touch()
    with pytest.raises(ValueError, match="refusing unknown table/ts_col"):
        delete_old_sqlite_rows(db_path, "l2_snapshot", "created_at", 30)


def test_validated_allows_l2_snapshot(tmp_path: Path):
    # Non-existent DB path → returns 0 without error
    result = delete_old_sqlite_rows(tmp_path / "nonexistent.db", "l2_snapshot", "ts", 30)
    assert result == 0


def test_validated_allows_trades(tmp_path: Path):
    result = delete_old_sqlite_rows(tmp_path / "nonexistent.db", "trades", "ts", 30)
    assert result == 0
