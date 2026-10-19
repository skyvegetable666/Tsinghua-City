"""Versioned experiment manifests over the existing World API and Docker adapter."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .schemas import WorldConfig


class RunLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    max_budget_usd: float = Field(default=1.0, gt=0)
    max_turns: int = Field(default=30, gt=0)
    max_rounds: int = Field(default=10, gt=0)
    run_timeout: float = Field(default=600, gt=0)
    turn_timeout: float = Field(default=120, gt=0)
    listen_seconds: float = Field(default=60, ge=0)
    poll_interval: float = Field(default=1, gt=0)


class ExperimentAgent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    model: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    initial_credits: int | None = Field(default=None, ge=0)
    initial_life_seconds: int | None = Field(default=None, gt=0)
    limits: RunLimits = Field(default_factory=RunLimits)


class ExperimentManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=96)
    world: WorldConfig = Field(default_factory=WorldConfig)
    agents: list[ExperimentAgent] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_agents(self):
        if len({agent.name for agent in self.agents}) != len(self.agents):
            raise ValueError("agent names must be unique")
        return self


def _archive(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def launch_experiment(
    manifest: ExperimentManifest, client, *, headers: dict, archive_dir: Path,
) -> dict:
    experiment_id = "exp_" + uuid.uuid4().hex[:16]
    root = archive_dir / experiment_id
    root.mkdir(parents=True, mode=0o700)
    _archive(root / "manifest.json", manifest.model_dump(mode="json"))
    report = {
        "experiment_id": experiment_id, "status": "starting", "world_id": None,
        "agents": [], "max_budget_usd": sum(a.limits.max_budget_usd for a in manifest.agents),
    }

    def post(path, body=None):
        response = client.post(path, headers=headers, json=body)
        response.raise_for_status()
        return response.json()

    try:
        world = post("/v1/worlds", {
            "name": manifest.name + "-" + experiment_id,
            "config": manifest.world.model_dump(),
        })
        report["world_id"] = world["id"]
        for spec in manifest.agents:
            agent = post(f"/v1/worlds/{world['id']}/agents", {
                "name": spec.name, "initial_credits": spec.initial_credits,
                "initial_life_seconds": spec.initial_life_seconds,
            })
            report["agents"].append({"id": agent["id"], "name": spec.name})
        # All peers must exist before the first model invocation can discover them.
        for spec, agent in zip(manifest.agents, report["agents"], strict=True):
            command = [
                "python", "-m", "tsinghua_city.claude_agent",
                "--agent-name", spec.name, "--model", spec.model, "--prompt", spec.prompt,
                "--experiment-id", experiment_id, "--run-id", experiment_id + "-" + spec.name,
            ]
            for key, value in spec.limits.model_dump().items():
                command.extend(["--" + key.replace("_", "-"), str(value)])
            runtime = post(f"/v1/worlds/{world['id']}/agents/{agent['id']}/runtime/start", {
                "adapter": "docker", "command": command, "restart_policy": "never",
            })
            agent["runtime_id"] = runtime["id"]
            _archive(root / "run.json", report)
        report["status"] = "started"
    except Exception:
        report["status"] = "failed"
        # Retain the failed world's history, but stop only this launch's new runtimes.
        for agent in report["agents"]:
            try:
                base = f"/v1/worlds/{report['world_id']}/agents/{agent['id']}/runtime"
                current = client.get(base, headers=headers)
                if current.status_code == 404:
                    continue
                current.raise_for_status()
                if current.json()["status"] != "STOPPED":
                    post(f"/v1/worlds/{report['world_id']}/agents/{agent['id']}/runtime/stop")
            except Exception:
                agent["cleanup_pending"] = True
        raise
    finally:
        _archive(root / "run.json", report)
    return {**report, "archive": str(root)}


def stop_experiment(report: dict, client, *, headers: dict) -> dict:
    for agent in report["agents"]:
        base = f"/v1/worlds/{report['world_id']}/agents/{agent['id']}/runtime"
        current = client.get(base, headers=headers)
        if current.status_code == 404:
            continue
        current.raise_for_status()
        if current.json()["status"] != "STOPPED":
            response = client.post(base + "/stop", headers=headers)
            response.raise_for_status()
    return {**report, "status": "stopped"}
