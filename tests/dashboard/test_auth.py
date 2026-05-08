"""Tests for dashboard bearer auth."""
from __future__ import annotations
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from dashboard.auth import bearer_auth
from dashboard.config import Config


def _app(token: str) -> FastAPI:
    app = FastAPI()
    cfg = Config(auth_token=token)

    @app.get("/x")
    def x(_=Depends(bearer_auth(cfg))) -> dict:
        return {"ok": True}
    return app


def test_missing_token_rejected():
    r = TestClient(_app("secret")).get("/x")
    assert r.status_code == 401


def test_correct_token_allowed():
    r = TestClient(_app("secret")).get("/x", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200


def test_health_route_unauthed_should_be_skipped_by_caller():
    # Auth dependency only applied where caller adds it; /health is opt-out.
    # This test documents the contract.
    pass
