"""Tests for the single-market drill-down view."""
from __future__ import annotations
from unittest.mock import patch
from fastapi.testclient import TestClient
from dashboard.main import build_app


def test_market_view_renders():
    with patch("dashboard.views.market.market_meta", return_value={
        "market_id": "m1", "city": "ZSPD", "contract_type": "high_ge",
        "threshold_c": 25.0, "end_date": "2026-05-08T23:59Z",
        "token_id_yes": "tok-1", "token_id_no": "tok-2", "slug": "x", "question": "?",
    }), patch("dashboard.views.market.latest_l2", return_value={
        "yes": {"bids": [[0.42, 100]], "asks": [[0.45, 80]]},
    }), patch("dashboard.views.market.recent_trades", return_value=[]):
        client = TestClient(build_app())
        r = client.get("/m/m1", headers={"Authorization": "Bearer "})
        assert r.status_code == 200
        assert "ZSPD" in r.text
        assert "0.42" in r.text


def test_market_view_404():
    with patch("dashboard.views.market.market_meta", return_value=None):
        client = TestClient(build_app())
        r = client.get("/m/nope", headers={"Authorization": "Bearer "})
        assert r.status_code == 404
