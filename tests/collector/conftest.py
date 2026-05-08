"""Shared pytest fixtures for collector tests."""
from pathlib import Path
import pytest


@pytest.fixture
def tmp_db_dir(tmp_path: Path) -> Path:
    return tmp_path
