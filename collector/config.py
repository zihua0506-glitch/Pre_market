"""Centralised env-var loading for collector."""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    gamma_host: str = os.getenv("POLY_GAMMA_HOST", "https://gamma-api.polymarket.com")
    clob_host: str = os.getenv("POLY_CLOB_HOST", "https://clob.polymarket.com")
    ws_url: str = os.getenv(
        "POLY_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/"
    )
    db_dir: Path = Path(os.getenv("COLLECTOR_DB_DIR", "database"))
    http_host: str = os.getenv("COLLECTOR_HTTP_HOST", "127.0.0.1")
    http_port: int = int(os.getenv("COLLECTOR_HTTP_PORT", "8091"))
    discovery_interval_s: int = int(os.getenv("COLLECTOR_DISCOVERY_INTERVAL_S", "86400"))
    l2_interval_s: int = int(os.getenv("COLLECTOR_L2_INTERVAL_S", "300"))
    health_interval_s: int = int(os.getenv("COLLECTOR_HEALTH_INTERVAL_S", "30"))
    weather_tag: str = os.getenv("COLLECTOR_WEATHER_TAG", "weather")


def load() -> Config:
    return Config()
