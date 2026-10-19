"""Process configuration.

The world service is intentionally a modular monolith for the first milestone.
Only the database URL and admin token are required; everything else is a world
parameter stored in Postgres/SQLite, not process configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime

DEFAULT_ADMIN_TOKEN = "dev-admin-token"


@dataclass(frozen=True)
class Settings:
    database_url: str
    admin_token: str
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"
    scheduler_enabled: bool = True
    scheduler_interval_seconds: float = 5.0
    api_url: str = "http://127.0.0.1:8000"
    runtime_log_dir: str = "runs/runtime"
    agent_image: str = "tsinghua-city-agent:latest"
    docker_bin: str = "docker"
    docker_python: str = "/opt/venv/bin/python"
    workspace_volume_prefix: str = "tsinghua-city-workspace"

    @property
    def uses_default_admin_token(self) -> bool:
        return self.admin_token == DEFAULT_ADMIN_TOKEN


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("CITY_DATABASE_URL", "sqlite:///./runs/city.db"),
        admin_token=os.environ.get("CITY_ADMIN_TOKEN", DEFAULT_ADMIN_TOKEN),
        host=os.environ.get("CITY_HOST", "127.0.0.1"),
        port=int(os.environ.get("CITY_PORT", "8000")),
        log_level=os.environ.get("CITY_LOG_LEVEL", "info"),
        scheduler_enabled=os.environ.get("CITY_SCHEDULER_ENABLED", "1").lower()
        not in {"0", "false", "no", "off"},
        scheduler_interval_seconds=float(
            os.environ.get("CITY_SCHEDULER_INTERVAL_SECONDS", "5")
        ),
        api_url=os.environ.get("CITY_API_URL", "http://127.0.0.1:8000").rstrip("/"),
        runtime_log_dir=os.environ.get("CITY_RUNTIME_LOG_DIR", "runs/runtime"),
        agent_image=os.environ.get("CITY_AGENT_IMAGE", "tsinghua-city-agent:latest"),
        docker_bin=os.environ.get("CITY_DOCKER_BIN", "docker"),
        docker_python=os.environ.get("CITY_DOCKER_PYTHON", "/opt/venv/bin/python"),
        workspace_volume_prefix=os.environ.get(
            "CITY_WORKSPACE_VOLUME_PREFIX", "tsinghua-city-workspace"
        ),
    )


def utcnow() -> float:
    return datetime.now(tz=UTC).timestamp()


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")
