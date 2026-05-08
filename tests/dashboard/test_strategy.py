"""Tests for the strategy ops view."""
from __future__ import annotations
from unittest.mock import patch
from fastapi.testclient import TestClient
from dashboard.main import build_app


def test_strategy_view_ok():
    fake = {"strategy": {"ActiveState": "inactive", "SubState": "dead"},
            "positions_today": []}
    with patch("dashboard.views.strategy.fetch_strategy_state", return_value=fake):
        r = TestClient(build_app()).get("/strategy", headers={"Authorization": "Bearer "})
        assert r.status_code == 200
        assert "inactive" in r.text


def test_strategy_view_handles_error():
    with patch("dashboard.views.strategy.fetch_strategy_state",
               return_value={"error": "valkyrie unreachable"}):
        r = TestClient(build_app()).get("/strategy", headers={"Authorization": "Bearer "})
        assert r.status_code == 200
        assert "valkyrie unreachable" in r.text
