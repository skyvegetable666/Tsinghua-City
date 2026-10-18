"""SDK shape and preparation tests (no Docker required)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from genesis import AgentSpec, World, WorldError, WorldSpec


def test_create_with_agents() -> None:
    world = World.create(agents=["alice", "bob"], mission="test mission")
    assert [agent.name for agent in world.agents] == ["alice", "bob"]
    assert world.started is False
    assert world.spec.mission == "test mission"


def test_add_agent_before_start() -> None:
    world = World.create()
    world.add_agent("alice")
    world.register_agent(AgentSpec("bob"))
    assert [agent.name for agent in world.agents] == ["alice", "bob"]


def test_add_three_agents() -> None:
    world = World.create(agents=["a", "b"])
    world.add_agent("c")
    assert [agent.name for agent in world.agents] == ["a", "b", "c"]
    world._validate_start()


def test_duplicate_and_reserved_names() -> None:
    world = World.create(agents=["agent-a", "agent-b"])
    with pytest.raises(WorldError, match="duplicate"):
        world.add_agent("agent-a")
    with pytest.raises(ValueError, match="reserved"):
        AgentSpec("supervisor")
    with pytest.raises(ValueError, match="must match"):
        AgentSpec("../bad")


def test_start_requires_at_least_two() -> None:
    world = World.create(agents=["only-one"])
    with pytest.raises(WorldError, match="at least 2"):
        world._validate_start()


def test_prompt_and_env_preparation(tmp_path: Path) -> None:
    genesis = (
        "$agent_name $peers $agent_count $mission $run_id $genesis_time"
    )
    world = World.create(
        agents=["alice", "bob"],
        mission="  find the signal  ",
        world_rules="  stay curious\n",
        genesis_template=genesis,
        model="deepseek-flash",
        mock=True,
        no_build=True,
        max_seconds=12,
        max_messages=7,
        end_grace_seconds=3,
        image="example/image:latest",
    )
    prompts = world._resolve_prompts(tmp_path)
    assert prompts[0] == "stay curious\n"
    assert prompts[1] == "find the signal"
    assert prompts[2] == genesis

    spec_dir = tmp_path / "spec"
    world._write_prompts(prompts, spec_dir)
    assert (spec_dir / "world.md").read_text() == "stay curious\n"
    assert (spec_dir / "mission.md").read_text() == "find the signal"
    assert (spec_dir / "genesis.md").read_text() == genesis

    env = world._build_env(tmp_path, spec_dir, 43123)
    assert env["AGENT_A_NAME"] == "alice"
    assert env["AGENT_B_NAME"] == "bob"
    assert env["AGENTS"] == '["alice", "bob"]'
    assert env["BROKER_PORT"] == "43123"
    assert env["BROKER_HOST"] == "0.0.0.0"
    assert env["WORLD_FILE"] == str(spec_dir / "world.md")
    assert env["MISSION_FILE"] == str(spec_dir / "mission.md")
    assert env["GENESIS_FILE"] == str(spec_dir / "genesis.md")
    assert env["ANTHROPIC_MODEL"] == "deepseek-flash"
    assert env["AGENT_IMAGE"] == "example/image:latest"
    assert env["RUN_MAX_SECONDS"] == "12"
    assert env["RUN_MAX_MESSAGES"] == "7"
    assert env["END_GRACE_SECONDS"] == "3"
    assert env["PYTHONUNBUFFERED"] == "1"


def test_three_agent_env(tmp_path: Path) -> None:
    world = World.create(agents=["alice", "bob", "carol"])
    world._validate_start()
    env = world._build_env(tmp_path, tmp_path, 43124)
    assert json.loads(env["AGENTS"]) == ["alice", "bob", "carol"]


def test_bad_genesis_placeholders(tmp_path: Path) -> None:
    world = World.create(
        agents=["a", "b"],
        world_rules="rules",
        mission="mission",
        genesis_template="$agent_name only",
    )
    with pytest.raises(WorldError, match="missing placeholders"):
        world._resolve_prompts(tmp_path)

    world = World.create(
        agents=["a", "b"],
        world_rules="rules",
        mission="mission",
        genesis_template="$oops",
    )
    with pytest.raises(WorldError, match="unknown placeholders"):
        world._resolve_prompts(tmp_path)


def test_command_flags() -> None:
    world = World.create(
        agents=["a", "b"],
        mock=True,
        no_build=True,
        max_seconds=9,
        max_messages=4,
    )
    command = world._build_command()
    assert command[0].endswith("python") or "python" in command[0]
    assert command[1:4] == ["-m", "supervisor", "up"]
    assert "--mock" in command
    assert "--no-build" in command
    assert command[command.index("--max-seconds") + 1] == "9"
    assert command[command.index("--max-messages") + 1] == "4"


def test_events_reader(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text(
        json.dumps({"event": "run_started", "n": 1})
        + "\n"
        + "not-json\n"
        + json.dumps({"event": "run_ended", "n": 2})
        + "\n"
    )
    world = World.create(agents=["a", "b"])
    world._run_dir = run_dir
    assert list(world.events()) == [
        {"event": "run_started", "n": 1},
        {"event": "run_ended", "n": 2},
    ]


def test_spec_is_json_serializable(tmp_path: Path) -> None:
    world = World.create(
        agents=["a", "b"],
        project_root=tmp_path,
        log_path=tmp_path / "log.txt",
    )
    payload = world.spec.to_dict()
    json.dumps(payload)
    assert payload["project_root"] == str(tmp_path)
    assert payload["log_path"] == str(tmp_path / "log.txt")


def test_spec_validation() -> None:
    with pytest.raises(ValueError, match="broker_port"):
        WorldSpec(broker_port=70000)
    with pytest.raises(ValueError, match="startup_timeout"):
        WorldSpec(startup_timeout=0)
    with pytest.raises(TypeError, match="sequence"):
        World.create(agents="not-a-list")  # type: ignore[arg-type]
