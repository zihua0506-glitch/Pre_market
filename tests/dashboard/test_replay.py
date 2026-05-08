"""Tests for the historical replay view."""
from __future__ import annotations
from unittest.mock import patch
import polars as pl
from fastapi.testclient import TestClient
from dashboard.main import build_app


def test_replay_no_data():
    with patch("dashboard.views.replay.load_day", return_value=None):
        r = TestClient(build_app()).get("/replay/2025-01-01",
            headers={"Authorization": "Bearer "})
        assert r.status_code == 200
        assert "no data" in r.text


def test_replay_renders_summary():
    df = pl.DataFrame({"ts": ["2025-01-01T00:00"], "metric": ["pm_yes_mid"],
                       "market_id": ["m1"], "value": [0.42]})
    with patch("dashboard.views.replay.load_day", return_value=df):
        r = TestClient(build_app()).get("/replay/2025-01-01",
            headers={"Authorization": "Bearer "})
        assert r.status_code == 200
        assert "m1" in r.text
