#!/usr/bin/env python3
"""Opt-in live Docker + Claude Code smoke test.

Requires a working Docker CLI, the built agent image, and
ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY. Never prints credentials.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from tsinghua_city.api import create_app
from tsinghua_city.config import Settings

if not (os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
    print("set ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY", file=sys.stderr)
    raise SystemExit(2)
image = os.environ.get("CITY_DOCKER_IMAGE", "tsinghua-city-agent:0.1")

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

settings = Settings(
    database_url="sqlite:///./runs/live-smoke.db",
    admin_token="live-smoke-admin",
    scheduler_enabled=False,
    runtime_log_dir="runs/live-smoke-logs",
    docker_api_url=f"http://host.docker.internal:{port}",
    docker_image=image,
)
Path("runs").mkdir(exist_ok=True)
Path("runs/live-smoke.db").unlink(missing_ok=True)
app = create_app(settings)
server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="error"))
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
while not server.started:
    time.sleep(0.05)

with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30) as client:
    admin = {"Authorization": "Bearer live-smoke-admin"}
    world = client.post("/v1/worlds", headers=admin, json={"name": "live-smoke"}).json()
    agent = client.post(
        f"/v1/worlds/{world['id']}/agents", headers=admin, json={"name": "claude"}
    ).json()
    prompt = (
        "Call check_state once to inspect your world, then reply with exactly "
        "the token LIVE_CLAUDE_OK and stop. Do not do anything else."
    )
    response = client.post(
        f"/v1/worlds/{world['id']}/agents/{agent['id']}/runtime/start",
        headers=admin,
        json={
            "adapter": "docker",
            "command": [
                "python", "-m", "tsinghua_city.claude_agent",
                "--max-turns", "3", "--prompt", prompt,
            ],
            "restart_policy": "never",
        },
    )
    print("start", response.status_code)
    if response.status_code != 200:
        print(response.text[:2000])
        raise SystemExit(1)
    runtime = response.json()
    base = f"/v1/worlds/{world['id']}/agents/{agent['id']}/runtime"
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        status = client.get(base, headers=admin).json()
        print("status", status.get("status"), status.get("pid"))
        if status.get("status") not in {"STARTING", "RUNNING", "SUSPENDED"}:
            break
        time.sleep(1)
    logs = Path(runtime["log_path"]).read_text(encoding="utf-8", errors="replace")
    print("log chars", len(logs))
    print("marker", "LIVE_CLAUDE_OK" in logs)
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    print("tail", logs[-1200:].replace(token, "<redacted>") if token else logs[-1200:])
    print("stop", client.post(base + "/stop", headers=admin).status_code)
    if "LIVE_CLAUDE_OK" not in logs or "tool_result" not in logs:
        raise SystemExit(1)
server.should_exit = True
thread.join(timeout=10)
