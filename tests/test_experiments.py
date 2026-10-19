from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError
from test_docker_runtime import FakeDocker

from tsinghua_city.experiments import ExperimentManifest, launch_experiment, stop_experiment


def manifest():
    return ExperimentManifest.model_validate({
        "version": 1, "name": "cooperation",
        "agents": [{"name": name, "model": "test-model", "prompt": "Act autonomously."}
                   for name in ("alice", "bob")],
    })


def test_experiment_batch_start_archive_and_stop(client, admin_headers, tmp_path):
    manager = client.app.state.runtime_manager
    adapter = FakeDocker()
    manager._adapters["docker"] = adapter
    launched = launch_experiment(manifest(), client, headers=admin_headers, archive_dir=tmp_path)
    assert launched["status"] == "started"
    assert launched["max_budget_usd"] == 2
    assert adapter.starts == 2
    archive = tmp_path / launched["experiment_id"]
    assert json.loads((archive / "manifest.json").read_text())["version"] == 1
    assert "token" not in (archive / "run.json").read_text()
    for agent in launched["agents"]:
        runtime = client.get(
            f"/v1/worlds/{launched['world_id']}/agents/{agent['id']}/runtime",
            headers=admin_headers,
        ).json()
        assert runtime["adapter"] == "docker"
        assert runtime["restart_policy"] == "never"
        assert "--max-budget-usd" in runtime["command"]
        assert launched["experiment_id"] in runtime["command"]
    assert stop_experiment(launched, client, headers=admin_headers)["status"] == "stopped"
    assert adapter.stops == 2


def test_experiment_failed_launch_stops_started_peers(client, admin_headers, tmp_path):
    from tsinghua_city.service import DomainError

    class FailsOnBob(FakeDocker):
        def start(self, **kwargs):
            if self.starts == 1:
                raise DomainError("test launch failure", 503)
            return super().start(**kwargs)

    adapter = FailsOnBob()
    client.app.state.runtime_manager._adapters["docker"] = adapter
    with pytest.raises(httpx.HTTPStatusError):
        launch_experiment(manifest(), client, headers=admin_headers, archive_dir=tmp_path)
    report = json.loads(next(tmp_path.glob("*/run.json")).read_text())
    assert report["status"] == "failed"
    assert adapter.starts == 1
    assert adapter.stops == 1
    assert "token" not in json.dumps(report)


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf")])
def test_experiment_rejects_invalid_spending_limits(budget):
    data = manifest().model_dump()
    data["agents"][0]["limits"]["max_budget_usd"] = budget
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(data)


def test_experiment_rejects_duplicate_agents_and_secret_fields():
    data = manifest().model_dump()
    data["agents"][1]["name"] = "alice"
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(data)
    data = manifest().model_dump()
    data["api_key"] = "must-be-environment-only"
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(data)


def test_second_stop_failure_still_stops_third_and_can_retry(client, admin_headers, tmp_path):
    from tsinghua_city.service import DomainError

    class FailsSecondStop(FakeDocker):
        def __init__(self):
            super().__init__()
            self.attempts = []
            self.fail = True

        def stop(self, runtime):
            self.attempts.append(runtime.agent_id)
            if len(self.attempts) == 2 and self.fail:
                raise DomainError("private-provider-credential", 503)
            return super().stop(runtime)

    adapter = FailsSecondStop()
    client.app.state.runtime_manager._adapters["docker"] = adapter
    data = manifest().model_dump()
    data["agents"].append({**data["agents"][0], "name": "carol"})
    launched = launch_experiment(ExperimentManifest.model_validate(data), client,
                                 headers=admin_headers, archive_dir=tmp_path)
    partial = stop_experiment(launched, client, headers=admin_headers)
    assert partial["status"] == "cleanup_pending"
    assert adapter.attempts == [agent["id"] for agent in launched["agents"]]
    assert [agent["cleanup_pending"] for agent in partial["agents"]] == [False, True, False]
    assert "private-provider-credential" not in json.dumps(partial)
    adapter.fail = False
    retried = stop_experiment(partial, client, headers=admin_headers)
    assert retried["status"] == "stopped"
    assert len(adapter.attempts) == 4
    assert all(not agent["cleanup_pending"] for agent in retried["agents"])
    assert all("stop_error" not in agent for agent in retried["agents"])


def test_stop_cli_persists_partial_report_and_returns_failure(monkeypatch, tmp_path, capsys):
    from tsinghua_city.cli import main

    path = tmp_path / "run.json"
    path.write_text(json.dumps({"world_id": "world", "agents": [{"id": x} for x in "abc"]}))
    attempts = []

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"status": "RUNNING"})
        agent_id = request.url.path.split("/")[5]
        attempts.append(agent_id)
        return httpx.Response(503 if agent_id == "b" else 200, json={})

    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(
        **kw, transport=httpx.MockTransport(respond),
    ))
    assert main(["stop-experiment", str(path)]) == 1
    result = json.loads(path.read_text())
    assert attempts == list("abc")
    assert result["status"] == "cleanup_pending"
    assert [a["cleanup_pending"] for a in result["agents"]] == [False, True, False]
    assert json.loads(capsys.readouterr().out)["status"] == "cleanup_pending"
