"""Tests for the market grid view."""
from __future__ import annotations
from unittest.mock import patch
from fastapi.testclient import TestClient
from dashboard.main import build_app


def test_grid_renders_with_markets():
    fake = [
        {"market_id": "m1", "city": "ZSPD", "contract_type": "high_ge",
         "yes_bid": 0.42, "yes_ask": 0.45, "no_bid": 0.55, "no_ask": 0.58, "ts_ms": 0},
        {"market_id": "m2", "city": "KJFK", "contract_type": "low_le",
         "yes_bid": 0.10, "yes_ask": 0.12, "no_bid": 0.88, "no_ask": 0.90, "ts_ms": 0},
    ]
    with patch("dashboard.views.grid.fetch_snapshot", return_value=fake):
        client = TestClient(build_app())
        r = client.get("/", headers={"Authorization": "Bearer "})
        assert r.status_code == 200
        assert "ZSPD" in r.text
        assert "KJFK" in r.text


def test_grid_filter_by_city():
    fake = [
        {"market_id": "m1", "city": "ZSPD", "contract_type": "high_ge",
         "yes_bid": 0.42, "yes_ask": 0.45, "no_bid": 0.55, "no_ask": 0.58, "ts_ms": 0},
        {"market_id": "m2", "city": "KJFK", "contract_type": "low_le",
         "yes_bid": 0.10, "yes_ask": 0.12, "no_bid": 0.88, "no_ask": 0.90, "ts_ms": 0},
    ]
    with patch("dashboard.views.grid.fetch_snapshot", return_value=fake):
        client = TestClient(build_app())
        r = client.get("/?city=ZSPD", headers={"Authorization": "Bearer "})
        assert r.status_code == 200
        assert "ZSPD" in r.text
        assert "KJFK" not in r.text
