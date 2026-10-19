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
