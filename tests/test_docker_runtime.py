from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

from tsinghua_city.docker_runtime import DockerRuntimeAdapter


def _fake_docker(tmp_path):
    path = tmp_path / "fake-docker"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "state = os.environ.get('FAKE_DOCKER_STATE', '')\n"
        "with open(state, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "args = sys.argv[1:]\n"
        "if args[:1] == ['inspect']:\n"
        "    if any('State.Running' in a for a in args):\n"
        "        print('true')\n"
        "    elif any('State.Pid' in a for a in args):\n"
        "        print('12345')\n"
        "    else:\n"
        "        print('0')\n"
        "elif args[:1] == ['run']:\n"
        "    print('container-id')\n"
        "elif args[:1] == ['logs']:\n"
        "    pass\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_docker_adapter_builds_container_without_host_secrets(tmp_path, monkeypatch):
    state = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    adapter = DockerRuntimeAdapter(
        api_url="http://127.0.0.1:8200",
        log_dir=str(tmp_path / "logs"),
        image="tsinghua-city-agent:test",
        docker_bin=str(_fake_docker(tmp_path)),
    )
    started = adapter.start(
        runtime_id="rt_test",
        command=["/opt/venv/bin/python", "-m", "tsinghua_city.claude_agent"],
        agent_id="ag_1",
        world_id="w_1",
        token="agent-token",
    )
    assert started.pid == 12345
    config = json.loads(Path(started.mcp_config_path).read_text(encoding="utf-8"))
    server = config["mcpServers"]["tsinghua-city"]
    assert server["command"] == "/opt/venv/bin/python"
    assert server["env"]["CITY_API_URL"] == "http://host.docker.internal:8200"
    assert server["env"]["CITY_AGENT_TOKEN"] == "agent-token"

    calls = [json.loads(line) for line in state.read_text().splitlines()]
    run = next(call for call in calls if call[:1] == ["run"])
    assert "host.docker.internal:host-gateway" in run
    assert "--security-opt" in run
    assert any("/workspace" in part for part in run)
    assert "--env" not in run
    assert "admin-token" not in json.dumps(run)

    runtime = SimpleNamespace(id="rt_test", mcp_config_path=started.mcp_config_path)
    assert adapter.is_running(runtime) is True
    assert adapter.stop(runtime) == 0
    assert not os.path.exists(started.mcp_config_path)
