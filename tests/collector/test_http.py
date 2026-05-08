import asyncio
from fastapi.testclient import TestClient
from collector.http import build_app
from collector.ws import LatestStore, Tick


def test_metrics_endpoint_returns_prometheus_format():
    store = LatestStore()
    store._data["tok-1"] = Tick(market_id="m1", best_bid=0.42, best_ask=0.45)
    store.connected = True
    app = build_app(store=store, markets_lookup=lambda: [
        {"market_id": "m1", "token_id_yes": "tok-1", "token_id_no": "tok-2",
         "city": "ZSPD", "contract_type": "high_ge"}
    ])
    client = TestClient(app)
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    assert 'pm_yes_bid{' in body
    assert 'city="ZSPD"' in body
    assert 'pm_ws_connected 1.0' in body


def test_snapshot_endpoint_returns_json():
    store = LatestStore()
    store._data["tok-1"] = Tick(market_id="m1", best_bid=0.42, best_ask=0.45)
    app = build_app(store=store, markets_lookup=lambda: [
        {"market_id": "m1", "token_id_yes": "tok-1", "token_id_no": "tok-2",
         "city": "ZSPD", "contract_type": "high_ge"}
    ])
    client = TestClient(app)
    r = client.get("/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["market_id"] == "m1"
    assert body[0]["yes_bid"] == 0.42


def test_health_endpoint():
    store = LatestStore()
    app = build_app(store=store, markets_lookup=lambda: [])
    r = TestClient(app).get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
