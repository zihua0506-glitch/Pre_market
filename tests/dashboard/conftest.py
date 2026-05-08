"""Shared pytest fixtures for dashboard tests."""
from __future__ import annotations
import pytest


@pytest.fixture(autouse=True)
def allow_no_auth(monkeypatch):
    """Set DASHBOARD_ALLOW_NO_AUTH=1 for all dashboard tests so existing
    tests that don't configure an auth token still pass."""
    monkeypatch.setenv("DASHBOARD_ALLOW_NO_AUTH", "1")
