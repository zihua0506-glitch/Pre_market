import json
from collector.ws import LatestStore, Tick, handle_message


def test_handle_book_message_updates_latest():
    store = LatestStore()
    msg = json.dumps({
        "event_type": "book",
        "asset_id": "tok-1",
        "market": "m1",
        "bids": [{"price": "0.42", "size": "1000"}],
        "asks": [{"price": "0.45", "size": "800"}],
        "timestamp": "1715000000000",
    })
    handle_message(store, msg)
    snap = store.snapshot()
    assert "tok-1" in snap
    assert snap["tok-1"].best_bid == 0.42
    assert snap["tok-1"].best_ask == 0.45


def test_handle_price_change_updates_mid():
    store = LatestStore()
    handle_message(store, json.dumps({
        "event_type": "book",
        "asset_id": "tok-1",
        "market": "m1",
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.50", "size": "100"}],
        "timestamp": "1715000000000",
    }))
    handle_message(store, json.dumps({
        "event_type": "price_change",
        "asset_id": "tok-1",
        "market": "m1",
        "changes": [{"price": "0.41", "side": "BUY", "size": "100"}],
        "timestamp": "1715000001000",
    }))
    snap = store.snapshot()
    assert snap["tok-1"].best_bid == 0.41


def test_handle_unknown_event_is_noop():
    store = LatestStore()
    handle_message(store, json.dumps({"event_type": "??", "asset_id": "tok-1"}))
    assert store.snapshot() == {}


def test_latest_store_gc_drops_unwanted_keys():
    store = LatestStore()
    for key in ("k1", "k2", "k3"):
        store._data[key] = Tick(market_id="m")
    removed = store.gc({"k1", "k3"})
    assert removed == 1
    assert set(store._data.keys()) == {"k1", "k3"}
