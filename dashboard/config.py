"""Dashboard config from env."""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    auth_token: str = os.getenv("DASHBOARD_AUTH_TOKEN", "")
    collector_url: str = os.getenv("DASHBOARD_COLLECTOR_URL", "http://127.0.0.1:8091")
    vm_url: str = os.getenv("DASHBOARD_VM_URL", "http://127.0.0.1:8428")
    grafana_url: str = os.getenv("DASHBOARD_GRAFANA_URL", "http://127.0.0.1:3000")
    valkyrie_host: str = os.getenv("DASHBOARD_VALKYRIE_HOST", "valkyrie")
    listen_host: str = os.getenv("DASHBOARD_LISTEN_HOST", "127.0.0.1")
    listen_port: int = int(os.getenv("DASHBOARD_LISTEN_PORT", "8090"))
    db_dir: Path = Path(os.getenv("DASHBOARD_COLLECTOR_DB_DIR", "/opt/zspd-collector/database"))
    warm_dir: Path = Path(os.getenv("DASHBOARD_WARM_DIR", "/opt/zspd-collector/warm"))


def load() -> Config:
    return Config()
