"""FastAPI app: /metrics (Prometheus exposition), /snapshot, /health."""
from __future__ import annotations
import time
from typing import Callable
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, JSONResponse
from prometheus_client import CollectorRegistry, Gauge, generate_latest, CONTENT_TYPE_LATEST
from collector.ws import LatestStore


def build_app(store: LatestStore, markets_lookup: Callable[[], list[dict]]) -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "ws_connected": store.connected,
                "ws_stale_s": time.time() - store.last_message_ts if store.last_message_ts else None}

    @app.get("/snapshot")
    def snapshot() -> JSONResponse:
        snap = store.snapshot()
        out = []
        for m in markets_lookup():
            ty = snap.get(m["token_id_yes"])
            tn = snap.get(m["token_id_no"])
            out.append({
                "market_id": m["market_id"],
                "city": m.get("city"),
                "contract_type": m.get("contract_type"),
                "yes_bid": ty.best_bid if ty else None,
                "yes_ask": ty.best_ask if ty else None,
                "no_bid":  tn.best_bid if tn else None,
                "no_ask":  tn.best_ask if tn else None,
                "ts_ms":   ty.ts_ms if ty else None,
            })
        return JSONResponse(out)

    @app.get("/metrics")
    def metrics() -> PlainTextResponse:
        # markets_lookup() returns only active markets; LatestStore.gc() removes
        # stale token entries after each discovery pass, so Prometheus cardinality
        # is bounded to the current active set.
        reg = CollectorRegistry()
        labels = ["market_id", "city", "contract_type"]
        g_yes_bid = Gauge("pm_yes_bid", "YES best bid", labels, registry=reg)
        g_yes_ask = Gauge("pm_yes_ask", "YES best ask", labels, registry=reg)
        g_yes_mid = Gauge("pm_yes_mid", "YES mid", labels, registry=reg)
        g_no_bid  = Gauge("pm_no_bid", "NO best bid", labels, registry=reg)
        g_no_ask  = Gauge("pm_no_ask", "NO best ask", labels, registry=reg)
        g_no_mid  = Gauge("pm_no_mid", "NO mid", labels, registry=reg)
        g_spread  = Gauge("pm_spread", "yes_ask - yes_bid", labels, registry=reg)
        g_ws      = Gauge("pm_ws_connected", "WS connected", registry=reg)
        g_stale   = Gauge("pm_ws_stale_seconds", "Seconds since last WS msg", registry=reg)
        snap = store.snapshot()
        for m in markets_lookup():
            ty = snap.get(m["token_id_yes"])
            tn = snap.get(m["token_id_no"])
            lab = (m["market_id"], m.get("city") or "", m.get("contract_type") or "")
            if ty:
                g_yes_bid.labels(*lab).set(ty.best_bid)
                g_yes_ask.labels(*lab).set(ty.best_ask)
                if ty.best_bid and ty.best_ask:
                    g_yes_mid.labels(*lab).set((ty.best_bid + ty.best_ask) / 2)
                g_spread.labels(*lab).set(max(0.0, ty.best_ask - ty.best_bid))
            if tn:
                g_no_bid.labels(*lab).set(tn.best_bid)
                g_no_ask.labels(*lab).set(tn.best_ask)
                if tn.best_bid and tn.best_ask:
                    g_no_mid.labels(*lab).set((tn.best_bid + tn.best_ask) / 2)
        g_ws.set(1.0 if store.connected else 0.0)
        g_stale.set(time.time() - store.last_message_ts if store.last_message_ts else 0.0)
        return PlainTextResponse(generate_latest(reg).decode(), media_type=CONTENT_TYPE_LATEST)

    return app
