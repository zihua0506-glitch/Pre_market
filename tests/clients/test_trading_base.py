"""Tests for clients.trading.base ABCs and result types."""

import pytest

from clients.trading.base import (
    BalanceInfo, CancelResult, OrderResult, TradingBackend,
)


class TestOrderResult:
    def test_default_fields(self):
        r = OrderResult(ok=True)
        assert r.ok is True
        assert r.order_id is None
        assert r.status is None
        assert r.raw is None

    def test_full(self):
        r = OrderResult(ok=True, order_id="abc", status="live", raw={"x": 1})
        assert r.order_id == "abc"
        assert r.status == "live"
        assert r.raw == {"x": 1}


class TestCancelResult:
    def test_ok_default_raw(self):
        r = CancelResult(ok=False)
        assert r.ok is False
        assert r.raw is None

    def test_with_raw(self):
        r = CancelResult(ok=True, raw={"cancelled": "id1"})
        assert r.raw == {"cancelled": "id1"}


class TestBalanceInfo:
    def test_simple(self):
        b = BalanceInfo(usdc_balance=100.0, allowance_usdc=200.0)
        assert b.usdc_balance == 100.0
        assert b.allowance_usdc == 200.0
        assert b.spender_allowances == {}
        assert b.raw is None

    def test_with_spenders(self):
        b = BalanceInfo(
            usdc_balance=100.0, allowance_usdc=200.0,
            spender_allowances={"0xabc": 200_000_000},
            raw={"balance": "100000000"},
        )
        assert b.spender_allowances == {"0xabc": 200_000_000}
        assert b.raw["balance"] == "100000000"


class TestTradingBackendABC:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            TradingBackend()    # type: ignore[abstract]
