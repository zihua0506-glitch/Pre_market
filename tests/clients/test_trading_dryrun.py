"""Tests for clients.trading.dryrun.DryRunBackend.

This is the only backend we can test fully without mocking — no network, no
SDK. Acts as a sanity check for the TradingBackend contract.
"""

import pytest

from clients.trading.dryrun import DryRunBackend
from common.orderbook import Order


@pytest.fixture
def backend():
    return DryRunBackend(verbose=False)


class TestGetBook:
    def test_returns_two_levels_each_side(self, backend):
        ob = backend.get_book("any-token-id")
        assert len(ob.bids) == 2
        assert len(ob.asks) == 2

    def test_bids_descending(self, backend):
        ob = backend.get_book("any")
        assert ob.bids[0].price > ob.bids[1].price

    def test_asks_ascending(self, backend):
        ob = backend.get_book("any")
        assert ob.asks[0].price < ob.asks[1].price

    def test_no_crossed_book(self, backend):
        ob = backend.get_book("any")
        # best ask should be > best bid (no crossed book)
        assert ob.asks[0].price > ob.bids[0].price

    def test_yes_mid_param_takes_effect(self):
        b1 = DryRunBackend(yes_mid=0.50, verbose=False)
        b2 = DryRunBackend(yes_mid=0.10, verbose=False)
        assert b1.get_book("x").bids[0].price > b2.get_book("x").bids[0].price


class TestPlaceOrder:
    def test_returns_ok_on_buy(self, backend):
        res = backend.place_order(Order(token_id="x", side="BUY", size=5, limit_price=0.2))
        assert res.ok is True
        assert res.order_id.startswith("dryrun-")
        assert res.status == "dryrun"

    def test_rejects_zero_size(self, backend):
        res = backend.place_order(Order(token_id="x", side="BUY", size=0, limit_price=0.2))
        assert res.ok is False

    def test_rejects_negative_size(self, backend):
        res = backend.place_order(Order(token_id="x", side="BUY", size=-1, limit_price=0.2))
        assert res.ok is False

    def test_raises_on_non_buy(self, backend):
        with pytest.raises(ValueError):
            backend.place_order(Order(token_id="x", side="SELL", size=5, limit_price=0.2))

    def test_records_orders(self, backend):
        backend.place_order(Order(token_id="A", side="BUY", size=1, limit_price=0.1))
        backend.place_order(Order(token_id="B", side="BUY", size=2, limit_price=0.2))
        assert len(backend.placed) == 2
        assert backend.placed[0].token_id == "A"
        assert backend.placed[1].token_id == "B"

    def test_unique_order_ids(self, backend):
        ids = set()
        for _ in range(5):
            r = backend.place_order(Order(token_id="x", side="BUY", size=1, limit_price=0.2))
            ids.add(r.order_id)
        assert len(ids) == 5


class TestCancelOrder:
    def test_records_cancellation(self, backend):
        r = backend.cancel_order("dryrun-001")
        assert r.ok
        assert "dryrun-001" in backend.cancelled

    def test_idempotent(self, backend):
        backend.cancel_order("dryrun-001")
        r = backend.cancel_order("dryrun-001")
        assert r.ok
        # cancelled list grows on each call (no dedup); both attempts present
        assert backend.cancelled.count("dryrun-001") == 2


class TestGetBalance:
    def test_returns_canned_values(self, backend):
        b = backend.get_balance()
        assert b.usdc_balance == 1000.0
        assert b.allowance_usdc == 1e15
        assert b.raw == {"dryrun": True}
