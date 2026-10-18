"""N-agent settings, compose generation, genesis, and routing."""

from __future__ import annotations

import json
import time
from pathlib import Path

import aiohttp
import pytest
import yaml
from aiohttp.test_utils import TestServer
from conftest import EventStream

from runtime.bridge import BridgeConfig
from supervisor.broker import Broker
from supervisor.compose import render_compose
from supervisor.prompts import PromptBundle, render_genesis
from supervisor.settings import AgentSpec, RunContext, Settings, load_settings
from supervisor.store import RunStore

NAMES = ("alice", "bob", "carol")
IDS = ("a", "b", "c")


def make_settings(tmp_path: Path) -> Settings:
    agents: dict[str, AgentSpec] = {}
    for _, (agent_id, name) in enumerate(zip(IDS, NAMES, strict=True)):
        others = [item for item in NAMES if item != name]
        other_ids = [IDS[NAMES.index(item)] for item in others]
        agents[agent_id] = AgentSpec(
            agent_id=agent_id,
            name=name,
            peer_id=other_ids[0],
            peer_name=others[0],
        )
    return Settings(
        root=tmp_path,
        broker_host="127.0.0.1",
        broker_port=0,
        broker_token="",
        agents=agents,
        max_message_chars=200,
        hook_batch_max_chars=1000,
        run_max_messages=0,
        run_max_seconds=0,
        end_grace_seconds=0.05,
        agent_image="test-image",
        mock_llm_port=0,
        world_path=tmp_path / "world.md",
        mission_path=tmp_path / "mission.md",
        genesis_path=tmp_path / "genesis.md",
    )


def test_load_settings_n(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    for name in ("world.md", "mission.md", "genesis.md"):
        (tmp_path / "config" / name).write_text("$agent_name", encoding="utf-8")
    monkeypatch.setenv("AGENTS", json.dumps([{"name": name} for name in NAMES]))
    settings = load_settings(root=tmp_path)
    assert settings.agent_count == 3
    assert settings.agent_ids == IDS
    assert settings.agent_by_name("carol").agent_id == "c"  # type: ignore[union-attr]
    assert [peer.name for peer in settings.peers_of("a")] == ["bob", "carol"]


def test_load_settings_names_env(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    for name in ("world.md", "mission.md", "genesis.md"):
        (tmp_path / "config" / name).write_text("x", encoding="utf-8")
    monkeypatch.delenv("AGENTS", raising=False)
    monkeypatch.setenv("AGENT_NAMES", "alice,bob,carol")
    settings = load_settings(root=tmp_path)
    assert [spec.name for spec in settings.agents.values()] == list(NAMES)


def test_names_cannot_collide_with_ids(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    for name in ("world.md", "mission.md", "genesis.md"):
        (tmp_path / "config" / name).write_text("x", encoding="utf-8")
    monkeypatch.setenv("AGENTS", json.dumps(["a", "bob"]))
    with pytest.raises(ValueError, match="collide"):
        load_settings(root=tmp_path)


def test_compose_n(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    text = render_compose(settings)
    data = yaml.safe_load(text)
    assert set(data["services"]) == {"agent-a", "agent-b", "agent-c"}
    assert set(data["networks"]) == {"net-a", "net-b", "net-c"}
    env_a = data["services"]["agent-a"]["environment"]
    assert json.loads(env_a["PEER_IDS"]) == ["b", "c"]
    assert json.loads(env_a["PEER_NAMES"]) == ["bob", "carol"]
    assert env_a["AGENT_TOKEN"] == "${AGENT_TOKEN_A}"
    assert data["services"]["agent-c"]["environment"]["AGENT_TOKEN"] == "${AGENT_TOKEN_C}"
    assert data["services"]["agent-b"]["networks"] == ["net-b"]


def test_bridge_env_n(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENT_ID", "a")
    monkeypatch.setenv("AGENT_NAME", "alice")
    monkeypatch.setenv("RUN_ID", "run-n")
    monkeypatch.setenv("BROKER_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("AGENT_TOKEN", "token")
    monkeypatch.setenv("AGENT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("PEER_IDS", json.dumps(["b", "c"]))
    monkeypatch.setenv("PEER_NAMES", json.dumps(["bob", "carol"]))
    config = BridgeConfig.from_env()
    assert config.agent_id == "a"
    assert config.peer_ids == ("b", "c")
    assert config.peer_names == ("bob", "carol")


def test_render_genesis_n() -> None:
    bundle = PromptBundle(
        world="WORLD",
        mission="MISSION",
        genesis_template="You are $agent_name of $agent_count; peers $peers.",
    )
    alice = AgentSpec("a", "alice")
    bob = AgentSpec("b", "bob")
    carol = AgentSpec("c", "carol")
    rendered = render_genesis(
        bundle,
        agent=alice,
        peers=[bob, carol],
        agent_count=3,
        run_id="run",
        genesis_time="now",
    )
    assert rendered == "You are alice of 3; peers bob, carol."


async def register(env: dict, agent_id: str, name: str) -> dict:
    async with env["session"].post(
        env["base"] + f"/v1/agents/{agent_id}/register",
        headers=env["headers"],
        json={"run_id": "run-n", "agent_name": name, "model": "mock"},
    ) as response:
        assert response.status == 200
        return await response.json()


async def post_json(env: dict, path: str, body: dict):
    return await env["session"].post(env["base"] + path, headers=env["headers"], json=body)


async def test_broker_n_genesis_and_routing(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    run = RunContext(
        run_id="run-n",
        run_dir=tmp_path / "run-n",
        token="n-token",
        started_at=time.time(),
    )
    store = RunStore(run.run_dir, run.run_id)
    prompts = PromptBundle(
        world="WORLD RULES",
        mission="THE MISSION",
        genesis_template=(
            "You are $agent_name, one of $agent_count. Peers: $peers. Mission: $mission."
        ),
    )
    broker = Broker(settings, run, prompts, store)
    server = TestServer(broker.make_app())
    await server.start_server()
    session = aiohttp.ClientSession()
    headers = {"Authorization": "Bearer n-token"}
    base = str(server.make_url("/")).rstrip("/")
    env = {"session": session, "base": base, "headers": headers}
    streams = {}
    try:
        for agent_id, name in zip(IDS, NAMES, strict=True):
            registered = await register(env, agent_id, name)
            assert registered["self"]["name"] == name
            assert {peer["name"] for peer in registered["peers"]} == (
                set(NAMES) - {name}
            )
            streams[agent_id] = EventStream(
                session,
                base + f"/v1/agents/{agent_id}/stream?since=0",
                headers,
            )
            await streams[agent_id].start()

        for agent_id in IDS:
            genesis = await streams[agent_id].next()
            assert genesis["event"] == "genesis"
        assert broker.phase == "running"
        assert len(broker.agents) == 3

        sent = await post_json(
            env,
            "/v1/agents/a/messages",
            {"recipient": "carol", "text": "hello carol"},
        )
        assert sent.status == 200
        payload = await sent.json()
        assert payload["recipient"] == "carol"
        event = await streams["c"].next()
        assert event["event"] == "message"
        assert event["data"]["sender"] == "alice"
        assert event["data"]["text"] == "hello carol"

        reply = await post_json(
            env,
            "/v1/agents/c/messages",
            {"recipient": "a", "text": "hello alice"},
        )
        assert reply.status == 200
        event = await streams["a"].next()
        assert event["event"] == "message"
        assert event["data"]["sender"] == "carol"

        self_send = await post_json(
            env, "/v1/agents/a/messages", {"recipient": "alice", "text": "self"}
        )
        assert self_send.status == 400
        unknown = await post_json(
            env, "/v1/agents/a/messages", {"recipient": "nobody", "text": "x"}
        )
        assert unknown.status == 400

        system = await post_json(
            env, "/v1/system/messages", {"to": "all", "text": "operator notice"}
        )
        assert system.status == 200
        assert len((await system.json())["message_ids"]) == 3
    finally:
        for stream in streams.values():
            await stream.close()
        await session.close()
        await server.close()
        store.close()


def test_agent_ids_beyond_twenty_six(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    for name in ("world.md", "mission.md", "genesis.md"):
        (tmp_path / "config" / name).write_text("x", encoding="utf-8")
    names = [f"agent-{index}" for index in range(28)]
    monkeypatch.setenv("AGENTS", json.dumps(names))
    settings = load_settings(root=tmp_path)
    assert len(settings.agents) == 28
    assert settings.agent_ids[-2:] == ("a1", "b1")
