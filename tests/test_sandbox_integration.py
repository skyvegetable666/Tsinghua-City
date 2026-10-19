"""Real Docker boundaries and MCP lifecycle; CITY_TEST_DOCKER=1 opts in."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from conftest import create_agent, create_world

from tsinghua_city.api import create_app
from tsinghua_city.config import Settings
from tsinghua_city.docker_runtime import LABEL, DockerRuntimeAdapter
from tsinghua_city.models import Agent, AgentRuntime
from tsinghua_city.runtime import RuntimeManager
from tsinghua_city.scheduler import tick_once

pytestmark = pytest.mark.skipif(
    os.environ.get("CITY_TEST_DOCKER") != "1", reason="set CITY_TEST_DOCKER=1 for real containers"
)


def docker(*args):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=True, timeout=30,
    )
    return result.stdout.strip()


def eventually(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("condition did not become true before timeout")


@pytest.fixture
def live_city(tmp_path, monkeypatch):
    monkeypatch.setenv("CITY_ADMIN_TOKEN", "host-admin-must-not-leak")
    monkeypatch.setenv("CITY_DATABASE_URL", "host-database-must-not-leak")
    monkeypatch.setenv("CRS_OAI_KEY", "unrelated-provider-must-not-leak")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'city.db'}", admin_token="integration-admin",
        scheduler_enabled=False, runtime_log_dir=str(tmp_path / "logs"),
        docker_api_url=f"http://host.docker.internal:{port}",
        docker_image=os.environ.get("CITY_DOCKER_IMAGE", "tsinghua-city-agent:0.1"),
    )
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    eventually(lambda: server.started)
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30) as client:
        try:
            yield client, {"Authorization": "Bearer integration-admin"}, app
        finally:
            with app.state.session_factory() as session:
                for runtime in session.query(AgentRuntime).all():
                    if runtime.adapter == "docker":
                        app.state.runtime_manager._adapter("docker").stop(runtime)
                for agent in session.query(Agent).all():
                    name = "city-work-" + agent.id
                    # Remove only this disposable test's volumes; production stop preserves them.
                    info = subprocess.run(
                        ["docker", "volume", "inspect", name], capture_output=True, check=False,
                    )
                    if info.returncode == 0:
                        docker("volume", "rm", name)
            server.should_exit = True
            thread.join(timeout=10)
            app.state.engine.dispose()


def _start(client, admin, world, agent, command, **kwargs):
    base = f"/v1/worlds/{world['id']}/agents/{agent['id']}/runtime"
    response = client.post(base + "/start", headers=admin, json={
        "adapter": "docker", "command": command, **kwargs,
    })
    assert response.status_code == 200, response.text
    return base, response.json()


def _state(name):
    return json.loads(docker("inspect", name))[0]


def _token(name):
    return next(item.split("=", 1)[1] for item in _state(name)["Config"]["Env"]
                if item.startswith("CITY_AGENT_TOKEN="))


def test_real_isolation_rotation_persistence_and_recovery(live_city):
    client, admin, app = live_city
    world = create_world(client, admin)
    alice = create_agent(client, admin, world["id"], "alice")
    bob = create_agent(client, admin, world["id"], "bob")
    probe = '''
import json, os, socket, pathlib, time, sys
p = pathlib.Path('/work/probe.json')
count = json.loads(p.read_text())['count'] + 1 if p.exists() else 1
try:
    pathlib.Path('/opt/city/forbidden').write_text('forbidden')
    readonly = False
except OSError:
    readonly = True
try:
    os.setuid(0)
    nonroot = False
except PermissionError:
    nonroot = True
status = pathlib.Path('/proc/self/status').read_text()
result = dict(count=count, uid=os.getuid(), readonly=readonly, nonroot=nonroot,
    no_new_privs='NoNewPrivs:\\t1' in status, caps_dropped='CapEff:\\t0000000000000000' in status,
    no_socket=not pathlib.Path('/var/run/docker.sock').exists(),
    no_host_home=not pathlib.Path('/Users').exists(),
    no_secrets=all(k not in os.environ for k in ('CITY_ADMIN_TOKEN','CITY_DATABASE_URL','CRS_OAI_KEY')))
p.write_text(json.dumps(result))
print(json.dumps(result), flush=True)
print('stderr-preserved', file=sys.stderr, flush=True)
server = socket.socket(); server.bind(('0.0.0.0', 9123)); server.listen()
time.sleep(300)
'''
    base, started = _start(client, admin, world, alice, ["python", "-c", probe],
                           restart_policy="on_failure", max_restarts=2)
    name = DockerRuntimeAdapter.container_name(started["id"])
    info = _state(name)
    assert info["HostConfig"]["ReadonlyRootfs"]
    assert info["HostConfig"]["PidsLimit"] == 128
    assert info["HostConfig"]["Memory"] == 1024**3
    assert info["HostConfig"]["NanoCpus"] == 10**9
    assert info["Config"]["User"] == "1000:1000"
    assert all(mount["Type"] == "volume" for mount in info["Mounts"])
    assert any(
        mount["Destination"] == "/shared" and mount["Name"] == "city-public-" + world["id"]
        for mount in info["Mounts"]
    )
    assert info["Config"]["Labels"][LABEL + ".owner"] == started["id"]
    eventually(lambda: "readonly" in docker("logs", name))
    proof = json.loads(docker("exec", name, "cat", "/work/probe.json"))
    assert proof.pop("count") == 1
    assert proof.pop("uid") == 1000
    assert all(proof.values()), proof
    token = _token(name)
    assert client.get("/v1/me", headers={"Authorization": "Bearer " + token}).status_code == 200

    _, other = _start(client, admin, world, bob, ["python", "-c", "import time; time.sleep(300)"])
    other_name = DockerRuntimeAdapter.container_name(other["id"])
    ip = next(iter(info["NetworkSettings"]["Networks"].values()))["IPAddress"]
    peer_probe = '''
import socket, pathlib, sys
assert not pathlib.Path('/work/probe.json').exists()
try:
    socket.create_connection((sys.argv[1], 9123), timeout=1)
except OSError:
    print('isolated')
else:
    raise AssertionError('peer network is reachable')
'''
    assert docker("exec", other_name, "python", "-c", peer_probe, ip) == "isolated"
    assert client.post(base + "/suspend", headers=admin).status_code == 200
    assert client.get("/v1/me", headers={"Authorization": "Bearer " + token}).status_code == 401
    assert client.post(base + "/resume", headers=admin).status_code == 200
    eventually(lambda: "readonly" in docker("logs", name))
    assert json.loads(docker("exec", name, "cat", "/work/probe.json"))["count"] == 2
    assert _token(name) != token

    manager = RuntimeManager(app.state.settings, app.state.session_factory)
    assert manager.reconcile() == 0
    docker("kill", name)
    assert manager.reconcile() == 1
    with app.state.session_factory() as session:
        runtime = session.get(AgentRuntime, started["id"])
        assert runtime.exit_code == 137
        runtime.next_restart_at = 0
        session.commit()
    assert tick_once(app.state.session_factory, manager)["restarted_runtimes"] == 1
    eventually(lambda: "readonly" in docker("logs", name))
    assert json.loads(docker("exec", name, "cat", "/work/probe.json"))["count"] == 3
    assert client.post(base + "/stop", headers=admin).status_code == 200
    assert "readonly" in Path(started["log_path"]).read_text()
    assert "stderr-preserved" in Path(started["log_path"]).read_text()
    assert docker("ps", "-aq", "--filter", "name=^/" + name + "$") == ""
    assert docker("network", "ls", "-q", "--filter", "name=^" + name + "$") == ""


def test_real_mcp_task_settlement_and_life_extension(live_city):
    client, admin, _app = live_city
    world = create_world(client, admin)
    alice = create_agent(client, admin, world["id"], "alice")
    bob = create_agent(client, admin, world["id"], "bob")
    command = ["python", "-m", "tsinghua_city.reference_harness", "--ticks", "1",
               "--journal-dir", "/work/journal"]
    # Existing deterministic policy exercises the full MCP wire path, independent of a model API.
    for agent in (alice, bob, alice):
        base, runtime = _start(client, admin, world, agent, command)
        name = DockerRuntimeAdapter.container_name(runtime["id"])
        eventually(lambda name=name: not _state(name)["State"]["Running"])
        assert _state(name)["State"]["ExitCode"] == 0, docker("logs", name)
        assert client.post(base + "/stop", headers=admin).status_code == 200
    tasks = client.get(f"/v1/worlds/{world['id']}/tasks", headers=admin).json()
    assert any(task["status"] == "COMPLETED" for task in tasks)
    state = client.get(f"/v1/worlds/{world['id']}/agents", headers=admin).json()
    assert next(agent["balance"] for agent in state if agent["id"] == bob["id"]) == 110
    # Spend through MCP too; no model or direct database mutation drives this action.
    code = '''
import asyncio, os
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from tsinghua_city.reference_harness import _mcp_config
async def main():
    async with stdio_client(_mcp_config(os.environ['CITY_MCP_CONFIG'])) as (r,w):
        async with ClientSession(r,w) as session:
            await session.initialize()
            result = await session.call_tool('extend_life', {'credits': 1})
            assert not result.isError
asyncio.run(main())
'''
    base, runtime = _start(client, admin, world, bob, ["python", "-c", code])
    name = DockerRuntimeAdapter.container_name(runtime["id"])
    eventually(lambda: not _state(name)["State"]["Running"])
    assert _state(name)["State"]["ExitCode"] == 0, docker("logs", name)
    state = client.get(f"/v1/worlds/{world['id']}/agents", headers=admin).json()
    after = next(agent for agent in state if agent["id"] == bob["id"])
    assert after["balance"] == 109
    assert after["death_at"] > bob["death_at"]


def test_failed_container_start_rolls_back_credentials_and_network(live_city, monkeypatch):
    from dataclasses import replace

    client, admin, app = live_city
    world = create_world(client, admin)
    alice = create_agent(client, admin, world["id"], "alice")
    manager = app.state.runtime_manager
    adapter = DockerRuntimeAdapter(replace(
        app.state.settings, docker_image="city-test-deliberately-missing-image:nonexistent"
    ))
    manager._adapters["docker"] = adapter
    attempted = []
    original_start = adapter.start

    def track_start(**kwargs):
        attempted.append(kwargs["runtime_id"])
        return original_start(**kwargs)

    monkeypatch.setattr(adapter, "start", track_start)
    base = f"/v1/worlds/{world['id']}/agents/{alice['id']}/runtime"
    failed = client.post(base + "/start", headers=admin, json={
        "adapter": "docker", "command": ["python"],
    })
    assert failed.status_code == 503
    assert client.get(base, headers=admin).status_code == 404
    name = DockerRuntimeAdapter.container_name(attempted[0])
    assert docker("network", "ls", "-q", "--filter", "name=^" + name + "$") == ""
    assert client.get("/v1/me", headers={
        "Authorization": "Bearer " + alice["token"]
    }).status_code == 200


def test_foreign_volume_is_not_reused_or_deleted(live_city):
    client, admin, _app = live_city
    world = create_world(client, admin)
    alice = create_agent(client, admin, world["id"], "alice")
    volume = "city-work-" + alice["id"]
    docker("volume", "create", "--label", LABEL + ".owner=another-world", volume)
    base = f"/v1/worlds/{world['id']}/agents/{alice['id']}/runtime"
    failed = client.post(base + "/start", headers=admin, json={
        "adapter": "docker", "command": ["python"],
    })
    assert failed.status_code == 409
    info = json.loads(docker("volume", "inspect", volume))[0]
    assert info["Labels"][LABEL + ".owner"] == "another-world"
    assert client.get("/v1/me", headers={
        "Authorization": "Bearer " + alice["token"]
    }).status_code == 200
