"""
tests/conftest.py

Shared pytest fixtures.

Conventions:
  - Tests must NOT hit live external APIs by default. Mock everything.
  - Tests that DO hit a network are marked `@pytest.mark.network`; we skip
    them unless `pytest -m network` is passed explicitly.
  - All file/db paths are tmp_path (per-test isolated tempdir).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


# Make sure the repo root is importable as `common`, `clients`, etc.
import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_collection_modifyitems(config, items):
    """Auto-skip @pytest.mark.network tests unless `-m network` is passed."""
    if config.getoption("-m") and "network" in config.getoption("-m"):
        return
    skip_net = pytest.mark.skip(reason="network-required test (use `-m network` to enable)")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip_net)


# ── shared fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    """A scratch .env at tmp_path/.env, prepopulated with sentinel values."""
    p = tmp_path / ".env"
    p.write_text(
        "POLY_PRIVATE_KEY=0x" + "11" * 32 + "\n"
        "POLY_API_KEY=test-api-key\n"
        "POLY_API_SECRET=dGVzdC1zZWNyZXQ=\n"   # base64 'test-secret'
        "POLY_API_PASSPHRASE=test-passphrase\n"
        "POLY_FUNDER=0xfeedfaceaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "POLY_SIGNATURE_TYPE=1\n"
        "POLY_CLOB_HOST=https://clob.polymarket.com\n"
        "POLY_CHAIN_ID=137\n"
        "PROXY_URL=http://127.0.0.1:8088\n"
        "PROXY_AUTH_TOKEN=test-bearer-token\n"
    )
    return p


@pytest.fixture
def empty_sqlite(tmp_path: Path) -> Path:
    """An empty sqlite file. Tests can run their own CREATE/DDL."""
    p = tmp_path / "test.db"
    sqlite3.connect(p).close()
    return p
