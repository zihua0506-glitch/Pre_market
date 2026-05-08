"""Archiver config from env."""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    db_dir: Path = Path(os.getenv("ARCHIVER_DB_DIR", "/opt/zspd-collector/database"))
    warm_dir: Path = Path(os.getenv("ARCHIVER_WARM_DIR", "/opt/zspd-collector/warm"))
    vm_url: str = os.getenv("ARCHIVER_VM_URL", "http://127.0.0.1:8428")
    rclone_remote: str = os.getenv("ARCHIVER_RCLONE_REMOTE", "saber:Local/Polymarket")
    hot_retention_days: int = int(os.getenv("ARCHIVER_HOT_RETENTION_DAYS", "30"))
    warm_retention_days: int = int(os.getenv("ARCHIVER_WARM_RETENTION_DAYS", "7"))


def load() -> Config:
    return Config()
