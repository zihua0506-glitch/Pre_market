"""tests for common.orderbook dataclasses"""

from common.orderbook import BookLevel, Order, OrderBook


def test_booklevel_construction():
    bl = BookLevel(price=0.27, size=100.0)
    assert bl.price == 0.27
    assert bl.size == 100.0


def test_orderbook_default_empty():
    ob = OrderBook(bids=[], asks=[])
    assert ob.bids == []
    assert ob.asks == []


def test_orderbook_holds_levels():
    ob = OrderBook(
        bids=[BookLevel(0.30, 100), BookLevel(0.28, 200)],
        asks=[BookLevel(0.32, 50),  BookLevel(0.34, 100)],
    )
    assert len(ob.bids) == 2
    assert len(ob.asks) == 2
    assert ob.bids[0].price == 0.30


def test_order_default_note_blank():
    o = Order(token_id="abc123", side="BUY", size=5.0, limit_price=0.27)
    assert o.note == ""


def test_order_full():
    o = Order(token_id="abc", side="BUY", size=10.0, limit_price=0.4, note="testorder")
    assert o.token_id == "abc"
    assert o.side == "BUY"
    assert o.size == 10.0
    assert o.limit_price == 0.4
    assert o.note == "testorder"
