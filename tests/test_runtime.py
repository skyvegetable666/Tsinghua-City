"""Local runtime lifecycle: start, token rotation, suspend/resume, stop, recovery."""

from __future__ import annotations

import sys
import time

import psutil
from conftest import agent_headers, create_agent, create_world


def _sleep_command() -> list[str]:
    return [sys.executable, "-c", "import time; time.sleep(30)"]


def test_runtime_lifecycle_and_token_rotation(client, admin_headers, session_factory):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    admin = {"Authorization": "Bearer admin-token"}
    base = f"/v1/worlds/{world['id']}/agents/{alice['id']}/runtime"

    started = client.post(
        base + "/start",
        headers=admin,
        json={"command": _sleep_command()},
    )
    assert started.status_code == 200, started.text
    runtime = started.json()
    assert runtime["status"] == "RUNNING"
    assert runtime["pid"] is not None
    assert runtime["mcp_config_path"] is not None

    # Starting a runtime rotates the agent credential.
    assert client.get("/v1/me", headers=agent_headers(alice)).status_code == 401

    status = client.get(base, headers=admin)
    assert status.status_code == 200
    assert status.json()["status"] == "RUNNING"

    suspended = client.post(base + "/suspend", headers=admin)
    assert suspended.status_code == 200
    assert suspended.json()["status"] == "SUSPENDED"
    assert suspended.json()["pid"] is None

    resumed = client.post(base + "/resume", headers=admin)
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "RUNNING"
    assert resumed.json()["pid"] is not None

    stopped = client.post(base + "/stop", headers=admin)
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "STOPPED"
    assert stopped.json()["pid"] is None


def test_runtime_does_not_leak_server_env(client, admin_headers, tmp_path):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    admin = {"Authorization": "Bearer admin-token"}
    marker = tmp_path / "env-marker.txt"
    code = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(marker)!r}).write_text("
        "os.environ.get('CITY_ADMIN_TOKEN', 'MISSING')); time.sleep(30)"
    )
    base = f"/v1/worlds/{world['id']}/agents/{alice['id']}/runtime"
    started = client.post(
        base + "/start",
        headers=admin,
        json={"command": [sys.executable, "-c", code]},
    )
    assert started.status_code == 200, started.text
    for _ in range(40):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.read_text() == "MISSING"
    client.post(base + "/stop", headers=admin)


def test_runtime_reconcile_marks_killed_process_crashed(client, admin_headers):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    admin = {"Authorization": "Bearer admin-token"}
    base = f"/v1/worlds/{world['id']}/agents/{alice['id']}/runtime"

    started = client.post(
        base + "/start",
        headers=admin,
        json={"command": _sleep_command()},
    ).json()
    pid = started["pid"]
    psutil.Process(pid).kill()
    for _ in range(20):
        if not psutil.pid_exists(pid):
            break
        time.sleep(0.05)

    reconciled = client.post(
        f"/v1/worlds/{world['id']}/runtimes/reconcile",
        headers=admin,
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["reconciled"] >= 1

    after = client.get(base, headers=admin)
    assert after.status_code == 200
    assert after.json()["status"] == "CRASHED"
    assert after.json()["pid"] is None
