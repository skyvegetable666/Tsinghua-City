"""Local runtime lifecycle: start, token rotation, suspend/resume, stop, recovery."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import psutil
from conftest import agent_headers, create_agent, create_world


def _sleep_command() -> list[str]:
    return [sys.executable, "-c", "import time; time.sleep(30)"]


def test_completed_local_child_does_not_touch_a_reused_pid(tmp_path, monkeypatch):
    from tsinghua_city.models import AgentRuntime
    from tsinghua_city.runtime import LocalRuntimeAdapter

    adapter = LocalRuntimeAdapter(api_url="http://localhost", log_dir=str(tmp_path))
    runtime = AgentRuntime(pid=123, pid_create_time=1)
    adapter._processes[123] = Mock(poll=Mock(return_value=0))

    def replacement(pid):
        raise AssertionError("a completed child must not inspect or signal a replacement process")

    monkeypatch.setattr(psutil, "Process", replacement)
    assert adapter.stop(runtime) == 0


def test_successful_local_exit_is_not_restarted(client, admin_headers, session_factory, tmp_path):
    from tsinghua_city.scheduler import tick_once

    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    marker = tmp_path / "finished"
    base = f"/v1/worlds/{world['id']}/agents/{alice['id']}/runtime"
    response = client.post(base + "/start", headers=admin_headers, json={
        "command": [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('done')"],
        "restart_policy": "on_failure",
    })
    assert response.status_code == 200
    for _ in range(100):
        runtime = client.get(base, headers=admin_headers).json()
        if runtime["status"] != "RUNNING":
            break
        time.sleep(0.02)
    assert runtime["status"] == "STOPPED"
    assert runtime["exit_code"] == 0
    assert runtime["next_restart_at"] is None
    assert marker.read_text() == "done"
    assert tick_once(session_factory, client.app.state.runtime_manager)["restarted_runtimes"] == 0


def test_death_stops_running_local_process(client, admin_headers, session_factory):
    from tsinghua_city.models import Agent
    from tsinghua_city.scheduler import tick_once

    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    base = f"/v1/worlds/{world['id']}/agents/{alice['id']}/runtime"
    response = client.post(base + "/start", headers=admin_headers, json={
        "command": _sleep_command(), "restart_policy": "on_failure",
    }).json()
    with session_factory() as session:
        session.get(Agent, alice["id"]).death_at = 0
        session.commit()
    result = tick_once(session_factory, client.app.state.runtime_manager)
    assert result["stopped_dead_runtimes"] == 1
    assert not psutil.pid_exists(response["pid"])
    assert client.get(base, headers=admin_headers).json()["status"] == "STOPPED"
    assert client.post(base + "/resume", headers=admin_headers).status_code == 409


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




def test_unread_message_does_not_resume_suspended_runtime(client, admin_headers, session_factory):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    bob = create_agent(client, admin_headers, world["id"], "bob")
    admin = {"Authorization": "Bearer admin-token"}
    base = f"/v1/worlds/{world['id']}/agents/{alice['id']}/runtime"

    assert client.post(
        base + "/start",
        headers=admin,
        json={"command": _sleep_command()},
    ).status_code == 200
    assert client.post(base + "/suspend", headers=admin).status_code == 200

    sent = client.post(
        "/v1/me/messages",
        headers=agent_headers(bob),
        json={"to": "alice", "body": "new work is available"},
    )
    assert sent.status_code == 201, sent.text

    from tsinghua_city.scheduler import tick_once

    manager = client.app.state.runtime_manager
    result = tick_once(session_factory, manager)
    assert "woken_runtimes" not in result

    after = client.get(base, headers=admin)
    assert after.status_code == 200
    assert after.json()["status"] == "SUSPENDED"
    assert client.post(base + "/resume", headers=admin).status_code == 200
    client.post(base + "/stop", headers=admin)


def test_dead_agent_cannot_resume_or_restart(client, admin_headers, session_factory):
    from tsinghua_city.models import Agent
    from tsinghua_city.scheduler import tick_once

    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    base = f"/v1/worlds/{world['id']}/agents/{alice['id']}/runtime"
    assert client.post(base + "/start", headers=admin_headers, json={
        "command": _sleep_command(),
    }).status_code == 200
    assert client.post(base + "/suspend", headers=admin_headers).status_code == 200
    with session_factory() as session:
        session.get(Agent, alice["id"]).death_at = 0
        session.commit()
    tick_once(session_factory, client.app.state.runtime_manager)
    assert client.post(base + "/resume", headers=admin_headers).status_code == 409
    assert client.post(base + "/start", headers=admin_headers, json={
        "command": _sleep_command(),
    }).status_code == 409
    with session_factory() as session:
        assert session.get(Agent, alice["id"]).alive is False


def test_runtime_auto_restart_on_failure(client, admin_headers, session_factory):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    admin = {"Authorization": "Bearer admin-token"}
    base = f"/v1/worlds/{world['id']}/agents/{alice['id']}/runtime"

    started = client.post(
        base + "/start",
        headers=admin,
        json={
            "command": _sleep_command(),
            "restart_policy": "on_failure",
            "max_restarts": 2,
        },
    ).json()
    assert started["restart_policy"] == "on_failure"
    first_config = json.loads(Path(started["mcp_config_path"]).read_text())
    first_token = first_config["mcpServers"]["tsinghua-city"]["env"]["CITY_AGENT_TOKEN"]
    psutil.Process(started["pid"]).kill()
    for _ in range(20):
        if not psutil.pid_exists(started["pid"]):
            break
        time.sleep(0.05)

    from tsinghua_city.scheduler import tick_once

    manager = client.app.state.runtime_manager
    first_tick = tick_once(session_factory, manager)
    assert first_tick["crashed_runtimes"] >= 1

    crashed = client.get(base, headers=admin)
    assert crashed.status_code == 200
    assert crashed.json()["status"] == "CRASHED"
    assert crashed.json()["next_restart_at"] is not None

    with session_factory() as session:
        from tsinghua_city.models import AgentRuntime

        runtime = session.get(AgentRuntime, started["id"])
        assert runtime is not None
        runtime.next_restart_at = 0
        session.commit()

    result = tick_once(session_factory, manager)
    assert result["restarted_runtimes"] >= 1

    after = client.get(base, headers=admin)
    assert after.status_code == 200
    assert after.json()["status"] == "RUNNING"
    assert after.json()["restart_count"] == 1

    second_config = json.loads(Path(after.json()["mcp_config_path"]).read_text())
    second_token = second_config["mcpServers"]["tsinghua-city"]["env"]["CITY_AGENT_TOKEN"]
    assert second_token != first_token
    assert client.get(
        "/v1/me", headers={"Authorization": f"Bearer {first_token}"}
    ).status_code == 401
    assert client.get(
        "/v1/me", headers={"Authorization": f"Bearer {second_token}"}
    ).status_code == 200
    client.post(base + "/stop", headers=admin)


def test_runtime_never_restarts_by_default(client, admin_headers):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    admin = {"Authorization": "Bearer admin-token"}
    base = f"/v1/worlds/{world['id']}/agents/{alice['id']}/runtime"

    started = client.post(
        base + "/start",
        headers=admin,
        json={"command": _sleep_command()},
    ).json()
    psutil.Process(started["pid"]).kill()
    for _ in range(20):
        if not psutil.pid_exists(started["pid"]):
            break
        time.sleep(0.05)

    crashed = client.get(base, headers=admin)
    assert crashed.status_code == 200
    assert crashed.json()["status"] == "CRASHED"
    assert crashed.json()["next_restart_at"] is None
    assert crashed.json()["restart_count"] == 0




def test_terminate_crashed_runtime_cancels_auto_restart(client, admin_headers, session_factory):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    admin = {"Authorization": "Bearer admin-token"}
    base = f"/v1/worlds/{world['id']}/agents/{alice['id']}/runtime"

    started = client.post(
        base + "/start",
        headers=admin,
        json={
            "command": _sleep_command(),
            "restart_policy": "on_failure",
            "max_restarts": 3,
        },
    ).json()
    psutil.Process(started["pid"]).kill()
    for _ in range(20):
        if not psutil.pid_exists(started["pid"]):
            break
        time.sleep(0.05)

    crashed = client.get(base, headers=admin)
    assert crashed.json()["status"] == "CRASHED"
    terminated = client.post(base + "/terminate", headers=admin)
    assert terminated.status_code == 200
    assert terminated.json()["status"] == "STOPPED"
    assert terminated.json()["next_restart_at"] is None

    from tsinghua_city.scheduler import tick_once

    result = tick_once(session_factory, client.app.state.runtime_manager)
    assert result["restarted_runtimes"] == 0


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
