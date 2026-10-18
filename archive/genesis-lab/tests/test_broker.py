"""Broker: genesis barrier, routing, delivery accounting, shutdown."""

from __future__ import annotations

import asyncio
import time

import aiohttp
import pytest
from aiohttp.test_utils import TestServer
from conftest import EventStream

from supervisor.broker import Broker
from supervisor.prompts import PromptBundle
from supervisor.settings import AgentSpec, RunContext, Settings, agent_token
from supervisor.store import RunStore


def make_settings(tmp_path, *, end_grace: float = 0.05) -> Settings:
    return Settings(
        root=tmp_path,
        broker_host="127.0.0.1",
        broker_port=0,
        broker_token="",
        agents={
            "a": AgentSpec(agent_id="a", name="agent-a", peer_id="b", peer_name="agent-b"),
            "b": AgentSpec(agent_id="b", name="agent-b", peer_id="a", peer_name="agent-a"),
        },
        max_message_chars=200,
        hook_batch_max_chars=1000,
        run_max_messages=0,
        run_max_seconds=0,
        end_grace_seconds=end_grace,
        agent_image="test",
        mock_llm_port=0,
        world_path=tmp_path / "world.md",
        mission_path=tmp_path / "mission.md",
        genesis_path=tmp_path / "genesis.md",
    )


@pytest.fixture
async def world(tmp_path):
    settings = make_settings(tmp_path)
    run = RunContext(
        run_id="run-test",
        run_dir=tmp_path / "run-test",
        token="test-token",
        started_at=time.time(),
    )
    store = RunStore(run.run_dir, run.run_id)
    prompts = PromptBundle(
        world="WORLD RULES",
        mission="THE MISSION",
        genesis_template=(
            "You are $agent_name, peer $peer_name. Mission: $mission. "
            "Run $run_id at $genesis_time."
        ),
    )
    broker = Broker(settings, run, prompts, store)
    server = TestServer(broker.make_app())
    await server.start_server()
    session = aiohttp.ClientSession()
    headers = {"Authorization": "Bearer test-token"}
    base = str(server.make_url("/")).rstrip("/")
    try:
        yield {
            "broker": broker,
            "server": server,
            "session": session,
            "headers": headers,
            "base": base,
            "tmp_path": tmp_path,
        }
    finally:
        await session.close()
        await server.close()
        store.close()


async def register(env, agent_id: str, name: str) -> dict:
    async with env["session"].post(
        env["base"] + f"/v1/agents/{agent_id}/register",
        headers=env["headers"],
        json={"run_id": "run-test", "agent_name": name, "model": "mock"},
    ) as response:
        assert response.status == 200
        return await response.json()


async def open_stream(env, agent_id: str, since: int = 0) -> EventStream:
    stream = EventStream(
        env["session"],
        env["base"] + f"/v1/agents/{agent_id}/stream?since={since}",
        env["headers"],
    )
    await stream.start()
    return stream


async def post_json(env, path: str, body: dict):
    return await env["session"].post(env["base"] + path, headers=env["headers"], json=body)


async def test_genesis_barrier_routing_and_delivery(world):
    env = world
    broker = env["broker"]

    reg_a = await register(env, "a", "agent-a")
    assert reg_a["peer"]["name"] == "agent-b"
    assert reg_a["world_prompt"] == "WORLD RULES"

    stream_a = await open_stream(env, "a")
    with pytest.raises(asyncio.TimeoutError):
        await stream_a.next(timeout=0.3)
    assert broker.phase == "waiting"

    await register(env, "b", "agent-b")
    stream_b = await open_stream(env, "b")

    genesis_a = await stream_a.next()
    genesis_b = await stream_b.next()
    assert genesis_a["event"] == "genesis"
    assert genesis_b["event"] == "genesis"
    assert "You are agent-a" in genesis_a["data"]["prompt"]
    assert "THE MISSION" in genesis_a["data"]["prompt"]
    assert broker.phase == "running"
    assert broker.genesis_at is not None

    response = await post_json(
        env, "/v1/agents/a/messages", {"recipient": "agent-b", "text": "hello b"}
    )
    assert response.status == 200
    payload = await response.json()
    message_id = payload["message_id"]
    assert message_id.startswith("m")
    assert payload["peer_state"] == "starting"

    event = await stream_b.next()
    assert event["event"] == "message"
    assert event["data"]["text"] == "hello b"
    assert event["data"]["sender"] == "agent-a"

    ack = await post_json(
        env,
        "/v1/agents/b/delivered",
        {
            "message_ids": [message_id],
            "channel": "hook",
            "turn": 3,
            "received_at": {message_id: time.time()},
        },
    )
    assert (await ack.json())["accepted"] == 1
    status = await (await env["session"].get(env["base"] + "/v1/status", headers=env["headers"])).json()
    assert status["messages"]["delivered"] == 1

    # duplicate ack is idempotent
    ack2 = await post_json(
        env, "/v1/agents/b/delivered", {"message_ids": [message_id], "channel": "turn", "turn": 4}
    )
    assert (await ack2.json())["accepted"] == 0

    await stream_a.close()
    await stream_b.close()


async def test_validation_and_isolation(world):
    env = world
    await register(env, "a", "agent-a")
    await register(env, "b", "agent-b")
    stream_a = await open_stream(env, "a")
    stream_b = await open_stream(env, "b")
    await stream_a.next()
    await stream_b.next()

    bad_recipient = await post_json(
        env, "/v1/agents/a/messages", {"recipient": "agent-a", "text": "hi"}
    )
    assert bad_recipient.status == 400

    too_long = await post_json(
        env, "/v1/agents/a/messages", {"recipient": "agent-b", "text": "x" * 201}
    )
    assert too_long.status == 400

    empty = await post_json(
        env, "/v1/agents/a/messages", {"recipient": "agent-b", "text": "  "}
    )
    assert empty.status == 400

    unauth = await env["session"].get(env["base"] + "/v1/status")
    assert unauth.status == 401

    system = await post_json(
        env, "/v1/system/messages", {"to": "all", "text": "a notice from the operator"}
    )
    assert system.status == 200
    ids = (await system.json())["message_ids"]
    assert len(ids) == 2
    event_a = await stream_a.next()
    event_b = await stream_b.next()
    assert "a notice from the operator" in event_a["data"]["text"]
    assert "a notice from the operator" in event_b["data"]["text"]

    await stream_a.close()
    await stream_b.close()


async def test_end_sequence_and_report(world):
    env = world
    broker = env["broker"]
    await register(env, "a", "agent-a")
    await register(env, "b", "agent-b")
    stream_a = await open_stream(env, "a")
    stream_b = await open_stream(env, "b")
    await stream_a.next()
    await stream_b.next()

    assert await broker.request_end("test over") is True
    assert broker.phase == "ending"
    notice_a = await stream_a.next()
    notice_b = await stream_b.next()
    assert notice_a["event"] == "message"
    assert notice_b["event"] == "message"
    assert "ending the experiment" in notice_a["data"]["text"]
    assert "ending the experiment" in notice_b["data"]["text"]

    shutdown_a = await stream_a.next(timeout=3)
    shutdown_b = await stream_b.next(timeout=3)
    assert shutdown_a["event"] == "shutdown"
    assert shutdown_b["event"] == "shutdown"
    assert shutdown_b["data"]["reason"] == "test over"
    assert shutdown_a["data"]["reason"] == "test over"
    assert broker.shutdown_sent_at is not None

    # Final messages are still accepted during the ending grace period.
    final = await post_json(
        env, "/v1/agents/a/messages", {"recipient": "agent-b", "text": "goodbye"}
    )
    assert final.status == 200

    await broker.finalize("test over")
    assert broker.phase == "ended"
    assert broker.ended_event.is_set()
    assert (broker.run.run_dir / "transcript.md").exists()
    assert (broker.run.run_dir / "metrics.json").exists()

    await stream_a.close()
    await stream_b.close()


async def test_per_agent_token_cannot_impersonate(world):
    env = world
    token_a = agent_token("test-token", "a")
    agent_headers = {"Authorization": "Bearer " + token_a}

    # An agent token works on its own registration path...
    async with env["session"].post(
        env["base"] + "/v1/agents/a/register",
        headers=agent_headers,
        json={"run_id": "run-test", "agent_name": "agent-a", "model": "mock"},
    ) as response:
        assert response.status == 200

    # ...but not on the peer's path.
    async with env["session"].post(
        env["base"] + "/v1/agents/b/register",
        headers=agent_headers,
        json={"run_id": "run-test", "agent_name": "agent-b", "model": "mock"},
    ) as response:
        assert response.status == 403

    # Host-only control endpoints reject agent tokens.
    async with env["session"].post(
        env["base"] + "/v1/end", headers=agent_headers, json={"reason": "nope"}
    ) as response:
        assert response.status == 403

    # The host token still works everywhere.
    async with env["session"].get(env["base"] + "/v1/status", headers=env["headers"]) as response:
        assert response.status == 200


async def test_send_and_trace_retries_are_idempotent(world):
    env = world
    await register(env, "a", "agent-a")
    await register(env, "b", "agent-b")
    stream_a = await open_stream(env, "a")
    stream_b = await open_stream(env, "b")
    await stream_a.next()
    await stream_b.next()

    body = {"recipient": "agent-b", "text": "only once", "client_message_id": "cid-1"}
    first = await post_json(env, "/v1/agents/a/messages", body)
    second = await post_json(env, "/v1/agents/a/messages", body)
    assert first.status == 200 and second.status == 200
    first_json = await first.json()
    second_json = await second.json()
    assert first_json["message_id"] == second_json["message_id"]
    assert second_json.get("duplicate") is True
    chat_from_a = [
        m
        for m in env["broker"].messages.values()
        if m.sender_id == "a" and m.kind == "chat"
    ]
    assert len(chat_from_a) == 1

    conflicting = await post_json(
        env,
        "/v1/agents/a/messages",
        {"recipient": "agent-b", "text": "different", "client_message_id": "cid-1"},
    )
    assert conflicting.status == 409

    trace = {
        "batch_id": "batch-1",
        "events": [
            {
                "type": "turn_result",
                "turn": 1,
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "cost_usd": 0.001,
            }
        ],
    }
    first_trace = await post_json(env, "/v1/agents/a/trace", trace)
    second_trace = await post_json(env, "/v1/agents/a/trace", trace)
    assert first_trace.status == 200 and second_trace.status == 200
    assert (await second_trace.json()).get("duplicate") is True
    assert env["broker"].agents["a"].usage["input_tokens"] == 10
    assert env["broker"].agents["a"].cost_usd == pytest.approx(0.001)

    await stream_a.close()
    await stream_b.close()


async def test_presence_offline_notice(world):
    env = world
    await register(env, "a", "agent-a")
    await register(env, "b", "agent-b")
    stream_a = await open_stream(env, "a")
    stream_b = await open_stream(env, "b")
    await stream_a.next()
    await stream_b.next()

    response = await post_json(
        env, "/v1/agents/b/state", {"state": "offline", "turn": 2, "detail": "test crash"}
    )
    assert response.status == 200
    event = await stream_a.next()
    assert event["event"] == "message"
    assert "agent-b is now offline" in event["data"]["text"]
    assert env["broker"].agents["b"].state == "offline"

    await stream_a.close()
    await stream_b.close()
