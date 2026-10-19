"""Process configuration.

The world service is intentionally a modular monolith for the first milestone.
Only the database URL and admin token are required; everything else is a world
parameter stored in Postgres/SQLite, not process configuration.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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
    trace_dir: str = "runs/transcripts"
    manifest_dir: str = "runs/manifests"
    docker_image: str = "tsinghua-city-agent:0.1"
    docker_api_url: str = "http://host.docker.internal:8000"
    docker_memory: str = "1g"
    docker_cpus: float = 1.0
    docker_pids_limit: int = 128
    public_workspace_volume: str = "city-public"
    runtime_host_id: str = socket.gethostname()
    viewer_token: str = ""
    operator_token: str = ""
    archive_retention_days: int = 30
    docker_network_internal: bool = False
    docker_egress_proxy: str = ""
    docker_volume_driver: str = "local"
    docker_volume_options: str = "{}"
    docker_storage_quota_required: bool = False
    model_gateway_url: str = ""
    model_gateway_agent_url: str = ""
    model_gateway_admin_key: str = ""
    require_gateway_budget: bool = False

    @property
    def uses_default_admin_token(self) -> bool:
        return self.admin_token == DEFAULT_ADMIN_TOKEN


def load_settings() -> Settings:
    def secret(name, default=""):
        file = os.environ.get(name + "_FILE")
        return Path(file).read_text(encoding="utf-8").strip() if file else os.environ.get(name, default)

    return Settings(
        database_url=secret("CITY_DATABASE_URL", "sqlite:///./runs/city.db"),
        admin_token=secret("CITY_ADMIN_TOKEN", DEFAULT_ADMIN_TOKEN),
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
        trace_dir=os.environ.get("CITY_TRACE_DIR", "runs/transcripts"),
        manifest_dir=os.environ.get("CITY_MANIFEST_DIR", "runs/manifests"),
        docker_image=os.environ.get("CITY_DOCKER_IMAGE", "tsinghua-city-agent:0.1"),
        docker_api_url=os.environ.get(
            "CITY_DOCKER_API_URL", "http://host.docker.internal:8000"
        ).rstrip("/"),
        docker_memory=os.environ.get("CITY_DOCKER_MEMORY", "1g"),
        docker_cpus=float(os.environ.get("CITY_DOCKER_CPUS", "1")),
        docker_pids_limit=int(os.environ.get("CITY_DOCKER_PIDS_LIMIT", "128")),
        public_workspace_volume=os.environ.get(
            "CITY_PUBLIC_WORKSPACE_VOLUME", "city-public"
        ),
        runtime_host_id=os.environ.get("CITY_RUNTIME_HOST_ID", socket.gethostname()),
        viewer_token=secret("CITY_VIEWER_TOKEN"),
        operator_token=secret("CITY_OPERATOR_TOKEN"),
        archive_retention_days=int(os.environ.get("CITY_ARCHIVE_RETENTION_DAYS", "30")),
        docker_network_internal=os.environ.get("CITY_DOCKER_NETWORK_INTERNAL", "0") == "1",
        docker_egress_proxy=os.environ.get("CITY_DOCKER_EGRESS_PROXY", ""),
        docker_volume_driver=os.environ.get("CITY_DOCKER_VOLUME_DRIVER", "local"),
        docker_volume_options=os.environ.get("CITY_DOCKER_VOLUME_OPTIONS", "{}"),
        docker_storage_quota_required=os.environ.get("CITY_DOCKER_STORAGE_QUOTA_REQUIRED", "0") == "1",
        model_gateway_url=os.environ.get("CITY_MODEL_GATEWAY_URL", ""),
        model_gateway_agent_url=os.environ.get("CITY_MODEL_GATEWAY_AGENT_URL", ""),
        model_gateway_admin_key=secret("CITY_MODEL_GATEWAY_ADMIN_KEY"),
        require_gateway_budget=os.environ.get("CITY_REQUIRE_GATEWAY_BUDGET", "0") == "1",
    )


def utcnow() -> float:
    return datetime.now(tz=UTC).timestamp()


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")
