"""Bearer-token auth dependency factory."""
from __future__ import annotations
from typing import Callable
from fastapi import Header, HTTPException
from dashboard.config import Config


def bearer_auth(cfg: Config) -> Callable:
    async def _dep(authorization: str = Header(default="")) -> None:
        if not cfg.auth_token:
            return  # auth disabled (dev only — log loudly via UI banner elsewhere)
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer")
        token = authorization[len("Bearer "):]
        if token != cfg.auth_token:
            raise HTTPException(status_code=401, detail="bad bearer")
    return _dep
