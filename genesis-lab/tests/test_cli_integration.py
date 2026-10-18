"""End-to-end integration against the real Claude Code CLI and a mock LLM.

Skipped by default. Run with:

    GENESIS_RUN_CLI_TESTS=1 .venv/bin/python -m pytest -q tests/test_cli_integration.py -s

This starts the real host broker, the real container-side runtime as a separate
process, the real Claude Code CLI, and a scripted Anthropic-compatible mock. It
then checks the two delivery paths:

  * a message sent while the agent is mid-turn is injected through the
    PostToolBatch hook and appears in the next model request;
  * a message sent while the agent is idle starts a new turn through stdin.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import time
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestServer
from conftest import EventStream

from supervisor.broker import Broker
from supervisor.mock_llm import MockLLM
from supervisor.prompts import PromptBundle
from supervisor.settings import AgentSpec, RunContext, Settings, agent_token
from supervisor.store import RunStore

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "integration-token"
RUN_ID = "run-integration"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_settings(tmp_path: Path) -> Settings:
    return Settings(
        root=tmp_path,
        broker_host="127.0.0.1",
        broker_port=0,
        broker_token="",
        agents={
            "a": AgentSpec(agent_id="a", name="agent-a", peer_id="b", peer_name="agent-b"),
            "b": AgentSpec(agent_id="b", name="agent-b", peer_id="a", peer_name="agent-a"),
        },
        max_message_chars=4000,
        hook_batch_max_chars=8000,
        run_max_messages=0,
        run_max_seconds=0,
        end_grace_seconds=0.8,
        agent_image="test",
        mock_llm_port=0,
        world_path=tmp_path / "world.md",
        mission_path=tmp_path / "mission.md",
        genesis_path=tmp_path / "genesis.md",
    )


async def wait_for(predicate, timeout: float = 45.0, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def get_status(session, base: str, headers: dict) -> dict:
    async with session.get(base + "/v1/status", headers=headers) as response:
        return await response.json()


def mock_saw(mock: MockLLM, marker: str, start_at: int = 0) -> bool:
    for record in mock.records[start_at:]:
        if marker in json.dumps(record["body"], ensure_ascii=False):
            return True
    return False


@pytest.mark.skipif(
    os.environ.get("GENESIS_RUN_CLI_TESTS") != "1",
    reason="set GENESIS_RUN_CLI_TESTS=1 to run the real-CLI integration test",
)
async def test_real_cli_both_delivery_paths(tmp_path):
    if shutil.which("claude") is None:
        pytest.skip("claude CLI is not installed")

    settings = build_settings(tmp_path)
    run = RunContext(
        run_id=RUN_ID,
        run_dir=tmp_path / "run",
        token=TOKEN,
        started_at=time.time(),
    )
    store = RunStore(run.run_dir, run.run_id)
    prompts = PromptBundle(
        world=(
            "You are in a two-agent world. Communicate only with the agent_chat tool. "
            "Messages arrive between tool calls or at turn boundaries."
        ),
        mission="Exercise the message bus.",
        genesis_template=(
            "You are $agent_name. Your peer is $peer_name. Mission: $mission. "
            "Run $run_id started at $genesis_time. Begin."
        ),
    )
    broker = Broker(settings, run, prompts, store)
    server = TestServer(broker.make_app())
    await server.start_server()
    base = str(server.make_url("/")).rstrip("/")
    headers = {"Authorization": "Bearer " + TOKEN}
    session = aiohttp.ClientSession()

    mock_port = free_port()
    mock = MockLLM(
        port=mock_port,
        host="127.0.0.1",
        mode="tool_then_end",
        delay_s=0.05,
        tool_command="sleep 2; echo MOCK-TOOL-OK",
        log_path=tmp_path / "mock-requests.jsonl",
    )
    mock.start()

    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    log_path = tmp_path / "bridge.log"
    log_file = log_path.open("wb")

    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.update(
        {
            "AGENT_ID": "a",
            "AGENT_NAME": "agent-a",
            "PEER_ID": "b",
            "PEER_NAME": "agent-b",
            "RUN_ID": RUN_ID,
            "BROKER_URL": base,
            "BROKER_TOKEN": TOKEN,
            "AGENT_TOKEN": agent_token(TOKEN, "a"),
            "AGENT_WORKDIR": str(work),
            "AGENT_LOCAL_PORT": "0",
            "HOME": str(home),
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{mock_port}",
            "ANTHROPIC_AUTH_TOKEN": "mock-key",
            "ANTHROPIC_MODEL": "mock-model[1m]",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "mock-model[1m]",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "mock-model[1m]",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "mock-model",
            "CLAUDE_CODE_SUBAGENT_MODEL": "mock-model",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "LOG_LEVEL": "INFO",
        }
    )

    process = None
    stream_b = None
    try:
        # A fake peer must be connected for the genesis barrier to open.
        async with session.post(
            base + "/v1/agents/b/register",
            headers=headers,
            json={"run_id": RUN_ID, "agent_name": "agent-b", "model": "fake"},
        ) as response:
            assert response.status == 200
        stream_b = EventStream(session, base + "/v1/agents/b/stream?since=0", headers)
        await stream_b.start()

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "runtime.bridge",
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=log_file,
            stderr=asyncio.subprocess.STDOUT,
        )

        async def agent_working() -> bool:
            status = await get_status(session, base, headers)
            return status["agents"]["a"]["state"] == "working" and len(mock.records) >= 1

        assert await wait_for(agent_working, timeout=60), "agent never started working"
        genesis_b = await stream_b.next(timeout=10)
        assert genesis_b["event"] == "genesis"

        first = "MARKER-FROM-PEER-123"
        async with session.post(
            base + "/v1/agents/b/messages",
            headers=headers,
            json={"recipient": "agent-a", "text": first},
        ) as response:
            assert response.status == 200
            first_id = (await response.json())["message_id"]

        async def hook_delivered() -> bool:
            message = broker.messages.get(first_id)
            return message is not None and message.delivered_at is not None

        assert await wait_for(hook_delivered, timeout=30), "message was never delivered"
        assert broker.messages[first_id].delivered_channel == "hook"
        assert mock_saw(mock, first, start_at=1), "hook context never reached the model request"

        async def agent_idle() -> bool:
            status = await get_status(session, base, headers)
            return status["agents"]["a"]["state"] == "idle"

        assert await wait_for(agent_idle, timeout=30), "agent never became idle"

        second = "SECOND-MARKER-456"
        requests_before = len(mock.records)
        async with session.post(
            base + "/v1/agents/b/messages",
            headers=headers,
            json={"recipient": "agent-a", "text": second},
        ) as response:
            assert response.status == 200
            second_id = (await response.json())["message_id"]

        async def turn_delivered() -> bool:
            message = broker.messages.get(second_id)
            return message is not None and message.delivered_at is not None

        assert await wait_for(turn_delivered, timeout=30), "idle message was never delivered"
        assert broker.messages[second_id].delivered_channel == "turn"

        async def second_seen() -> bool:
            return mock_saw(mock, second, start_at=requests_before)

        assert await wait_for(second_seen, timeout=20), "idle message never reached a new model request"

        async def idle_again() -> bool:
            status = await get_status(session, base, headers)
            return status["agents"]["a"]["state"] == "idle"

        assert await wait_for(idle_again, timeout=30), "agent did not return to idle"

        # Third path: the model calls the MCP agent_chat tool. The mock switches
        # to demo mode and replies to the next delivered message through the
        # real MCP server -> local bridge -> host broker chain.
        mock.mode = "demo"
        third = "THIRD-MCP-TRIGGER"
        async with session.post(
            base + "/v1/agents/b/messages",
            headers=headers,
            json={"recipient": "agent-a", "text": third},
        ) as response:
            assert response.status == 200
            third_id = (await response.json())["message_id"]

        async def third_delivered() -> bool:
            message = broker.messages.get(third_id)
            return message is not None and message.delivered_at is not None

        assert await wait_for(third_delivered, timeout=30), "third message was never delivered"
        assert broker.messages[third_id].delivered_channel == "turn"

        async def outbound_from_agent() -> bool:
            return any(
                message.kind == "chat" and message.sender == "agent-a"
                for message in broker.messages.values()
            )

        assert await wait_for(outbound_from_agent, timeout=30), "agent_chat MCP tool never sent a message"
        outbound = next(
            message
            for message in broker.messages.values()
            if message.kind == "chat" and message.sender == "agent-a"
        )
        assert "received your message" in outbound.text

        # Graceful end: the supervisor sends a final notice, then shutdown.
        assert await broker.request_end("integration test complete") is True
        try:
            await asyncio.wait_for(process.wait(), timeout=45)
        except TimeoutError:
            pytest.fail("bridge did not exit after shutdown")
        assert process.returncode == 0, f"bridge exited with {process.returncode}"
        assert broker.shutdown_sent_at is not None

        await broker.finalize("integration test complete")
        assert (run.run_dir / "transcript.md").exists()
        transcript = (run.run_dir / "transcript.md").read_text(encoding="utf-8")
        assert first in transcript
        assert second in transcript
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await process.wait()
        if stream_b is not None:
            await stream_b.close()
        await session.close()
        await server.close()
        mock.stop()
        log_file.close()
        if process is not None and process.returncode not in (0, None):
            print("\n--- bridge log ---")
            print(log_path.read_text(encoding="utf-8", errors="replace")[-10000:])
        broker.store.close()
