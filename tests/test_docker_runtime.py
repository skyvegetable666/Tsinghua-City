"""Adapter dispatch regressions; Docker integration is opt-in in a separate module."""

from __future__ import annotations

from conftest import create_agent, create_world

from tsinghua_city.runtime import StartResult


class FakeDocker:
    name = "docker"

    def __init__(self):
        self.running = False
        self.starts = 0
        self.stops = 0

    def start(self, **kwargs):
        self.running = True
        self.starts += 1
        return StartResult(None, None, "docker.log", None)

    def is_running(self, runtime):
        return self.running

    def stop(self, runtime):
        self.running = False
        self.stops += 1
        return 0


def test_runtime_dispatch_uses_persisted_adapter(client, admin_headers, session_factory):
    from tsinghua_city.models import AgentRuntime
    from tsinghua_city.runtime import RuntimeManager
    from tsinghua_city.scheduler import tick_once

    manager = client.app.state.runtime_manager
    adapter = FakeDocker()
    manager._adapters["docker"] = adapter
    world = create_world(client, admin_headers)
    agent = create_agent(client, admin_headers, world["id"], "alice")
    base = f"/v1/worlds/{world['id']}/agents/{agent['id']}/runtime"
    started = client.post(base + "/start", headers=admin_headers, json={
        "adapter": "docker", "command": ["python"], "restart_policy": "on_failure",
    })
    assert started.status_code == 200
    assert started.json()["adapter"] == "docker"
    assert started.json()["pid"] is None
    assert client.post(base + "/suspend", headers=admin_headers).status_code == 200
    assert client.post(base + "/resume", headers=admin_headers).status_code == 200
    assert adapter.starts == 2
    # A new manager must reconcile the stored Docker identity, never a host PID.
    recovered = RuntimeManager(manager.settings, session_factory)
    recovered._adapters["docker"] = adapter
    assert recovered.reconcile() == 0
    adapter.running = False
    assert recovered.reconcile() == 1
    with session_factory() as session:
        runtime = session.get(AgentRuntime, started.json()["id"])
        runtime.next_restart_at = 0
        session.commit()
    assert tick_once(session_factory, recovered)["restarted_runtimes"] == 1
    assert adapter.starts == 3
    changed = client.post(base + "/start", headers=admin_headers, json={"command": ["python"]})
    assert changed.status_code == 409
    assert client.post(base + "/stop", headers=admin_headers).status_code == 200


def test_reconcile_survives_unavailable_docker(client, admin_headers, session_factory):
    from tsinghua_city.models import AgentRuntime
    from tsinghua_city.service import DomainError

    manager = client.app.state.runtime_manager
    adapter = FakeDocker()
    manager._adapters["docker"] = adapter
    world = create_world(client, admin_headers)
    agent = create_agent(client, admin_headers, world["id"], "alice")
    base = f"/v1/worlds/{world['id']}/agents/{agent['id']}/runtime"
    started = client.post(base + "/start", headers=admin_headers, json={
        "adapter": "docker", "command": ["python"],
    })
    assert started.status_code == 200

    def unavailable(runtime):
        raise DomainError("Docker unavailable", 503)

    adapter.is_running = unavailable
    assert manager.reconcile() == 0
    with session_factory() as session:
        runtime = session.get(AgentRuntime, started.json()["id"])
        assert runtime is not None
        assert runtime.status == "RUNNING"


def test_unavailable_docker_does_not_mark_runtime_crashed(client, admin_headers):
    from tsinghua_city.service import DomainError

    manager = client.app.state.runtime_manager
    adapter = FakeDocker()
    manager._adapters["docker"] = adapter
    world = create_world(client, admin_headers)
    agent = create_agent(client, admin_headers, world["id"], "alice")
    base = f"/v1/worlds/{world['id']}/agents/{agent['id']}/runtime"
    assert client.post(base + "/start", headers=admin_headers, json={
        "adapter": "docker", "command": ["python"],
    }).status_code == 200

    def unavailable(runtime):
        raise DomainError("Docker unavailable", 503)

    adapter.is_running = unavailable
    assert client.get(base, headers=admin_headers).status_code == 503
    assert client.get(f"/v1/worlds/{world['id']}/runtimes", headers=admin_headers).json()[0][
        "status"
    ] == "RUNNING"
