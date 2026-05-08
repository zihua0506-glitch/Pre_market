"""Tests for clients.trading.proxy.ProxyBackend.

Uses requests-mock to stub the FastAPI HTTP server. Verifies that we honour
the on-the-wire contract documented in clients/trading/server.py.
"""

import pytest
import requests_mock

from clients.trading.proxy import ProxyBackend
from common.orderbook import Order


@pytest.fixture
def env():
    return {
        "PROXY_URL":         "http://test-host:9999",
        "PROXY_AUTH_TOKEN":  "test-token",
    }


@pytest.fixture
def backend(env):
    """A ProxyBackend wired to a mock /health endpoint."""
    with requests_mock.Mocker(real_http=False) as m:
        m.get("http://test-host:9999/health", json={"ok": True, "service": "clob-proxy"})
        yield ProxyBackend(env=env)


class TestConstruction:
    def test_requires_token(self):
        with pytest.raises(RuntimeError, match="PROXY_AUTH_TOKEN"):
            ProxyBackend(env={"PROXY_URL": "http://x"})

    def test_health_failure_aborts(self):
        with requests_mock.Mocker() as m:
            m.get("http://x:1/health", status_code=500)
            with pytest.raises(RuntimeError, match="unreachable"):
                ProxyBackend(env={"PROXY_URL": "http://x:1", "PROXY_AUTH_TOKEN": "t"})

    def test_health_not_ok_aborts(self):
        with requests_mock.Mocker() as m:
            m.get("http://x:1/health", json={"ok": False})
            with pytest.raises(RuntimeError, match="not ok"):
                ProxyBackend(env={"PROXY_URL": "http://x:1", "PROXY_AUTH_TOKEN": "t"})

    def test_uses_default_url(self):
        with requests_mock.Mocker() as m:
            m.get("http://127.0.0.1:8088/health", json={"ok": True})
            b = ProxyBackend(env={"PROXY_AUTH_TOKEN": "t"})
            assert b.url == "http://127.0.0.1:8088"

    def test_strips_trailing_slash(self):
        with requests_mock.Mocker() as m:
            m.get("http://x:1/health", json={"ok": True})
            b = ProxyBackend(env={"PROXY_URL": "http://x:1/", "PROXY_AUTH_TOKEN": "t"})
            assert b.url == "http://x:1"


class TestGetBook:
    def test_parses_response(self, env):
        with requests_mock.Mocker() as m:
            m.get("http://test-host:9999/health", json={"ok": True})
            m.get("http://test-host:9999/book", json={
                "bids": [{"price": 0.30, "size": 100}, {"price": 0.28, "size": 50}],
                "asks": [{"price": 0.32, "size": 80}, {"price": 0.34, "size": 200}],
            })
            b = ProxyBackend(env=env)
            ob = b.get_book("token-id-here")
            assert len(ob.bids) == 2
            assert len(ob.asks) == 2
            assert ob.bids[0].price == 0.30
            assert ob.asks[0].price == 0.32

    def test_passes_auth_header(self, env):
        with requests_mock.Mocker() as m:
            m.get("http://test-host:9999/health", json={"ok": True})
            m.get("http://test-host:9999/book", json={"bids": [], "asks": []})
            b = ProxyBackend(env=env)
            b.get_book("anything")
            req = m.last_request
            assert req.headers["Authorization"] == "Bearer test-token"
            assert req.qs == {"token_id": ["anything"]}


class TestPlaceOrder:
    def test_success(self, env):
        with requests_mock.Mocker() as m:
            m.get("http://test-host:9999/health", json={"ok": True})
            m.post("http://test-host:9999/order", json={
                "ok": True, "order_id": "abc123", "status": "live",
                "raw": {"success": True}
            })
            b = ProxyBackend(env=env)
            res = b.place_order(Order(token_id="t", side="BUY", size=5, limit_price=0.2))
            assert res.ok is True
            assert res.order_id == "abc123"
            assert res.status == "live"

    def test_request_body_shape(self, env):
        with requests_mock.Mocker() as m:
            m.get("http://test-host:9999/health", json={"ok": True})
            m.post("http://test-host:9999/order", json={"ok": True})
            b = ProxyBackend(env=env)
            b.place_order(Order(token_id="t1", side="BUY", size=10, limit_price=0.27, note="hello"))
            body = m.last_request.json()
            assert body == {
                "token_id": "t1", "price": 0.27, "size": 10.0,
                "side": "BUY", "note": "hello",
            }

    def test_rejects_non_buy(self, env):
        with requests_mock.Mocker() as m:
            m.get("http://test-host:9999/health", json={"ok": True})
            b = ProxyBackend(env=env)
            with pytest.raises(ValueError):
                b.place_order(Order(token_id="t", side="SELL", size=5, limit_price=0.2))

    def test_rejects_zero_size(self, env):
        with requests_mock.Mocker() as m:
            m.get("http://test-host:9999/health", json={"ok": True})
            b = ProxyBackend(env=env)
            res = b.place_order(Order(token_id="t", side="BUY", size=0, limit_price=0.2))
            assert res.ok is False

    def test_handles_http_error(self, env):
        with requests_mock.Mocker() as m:
            m.get("http://test-host:9999/health", json={"ok": True})
            m.post("http://test-host:9999/order", status_code=500)
            b = ProxyBackend(env=env)
            res = b.place_order(Order(token_id="t", side="BUY", size=5, limit_price=0.2))
            assert res.ok is False


class TestCancelOrder:
    def test_success(self, env):
        with requests_mock.Mocker() as m:
            m.get("http://test-host:9999/health", json={"ok": True})
            m.post("http://test-host:9999/cancel",
                   json={"ok": True, "raw": {"canceled": ["abc"]}})
            b = ProxyBackend(env=env)
            res = b.cancel_order("abc")
            assert res.ok is True
            assert m.last_request.json() == {"order_id": "abc"}

    def test_handles_failure(self, env):
        with requests_mock.Mocker() as m:
            m.get("http://test-host:9999/health", json={"ok": True})
            m.post("http://test-host:9999/cancel", json={"ok": False, "raw": {"error": "x"}})
            b = ProxyBackend(env=env)
            res = b.cancel_order("nope")
            assert res.ok is False


class TestGetBalance:
    def test_parses_response(self, env):
        with requests_mock.Mocker() as m:
            m.get("http://test-host:9999/health", json={"ok": True})
            m.get("http://test-host:9999/balance", json={
                "balance_usdc": 2021.23,
                "allowance_usdc": 1e15,
                "raw": {"balance": "2021230000"},
            })
            b = ProxyBackend(env=env)
            bal = b.get_balance()
            assert bal.usdc_balance == 2021.23
            assert bal.allowance_usdc == 1e15
