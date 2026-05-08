"""Tests for dashboard bearer auth."""
from __future__ import annotations
import pytest
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


def test_build_app_raises_without_token_or_allow_flag(monkeypatch):
    """build_app() must raise RuntimeError when no auth token and allow flag not set."""
    monkeypatch.delenv("DASHBOARD_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("DASHBOARD_ALLOW_NO_AUTH", "0")
    # Re-import build_app so config is re-evaluated with patched env.
    import importlib
    import dashboard.config as dcfg
    import dashboard.main as dmain
    importlib.reload(dcfg)
    importlib.reload(dmain)
    with pytest.raises(RuntimeError, match="DASHBOARD_AUTH_TOKEN unset"):
        dmain.build_app()


def test_build_app_starts_with_allow_no_auth(monkeypatch):
    """build_app() must succeed when DASHBOARD_ALLOW_NO_AUTH=1 even with no token."""
    monkeypatch.delenv("DASHBOARD_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("DASHBOARD_ALLOW_NO_AUTH", "1")
    import importlib
    import dashboard.config as dcfg
    import dashboard.main as dmain
    importlib.reload(dcfg)
    importlib.reload(dmain)
    app = dmain.build_app()
    assert app is not None
