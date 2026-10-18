"""Two real container runtimes + two real Claude Code processes + mock LLM.

Same as test_cli_integration, but both agents exist and talk to each other.
Skipped unless GENESIS_RUN_CLI_TESTS=1.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import sys
import time
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from supervisor.broker import Broker
from supervisor.mock_llm import MockLLM
from supervisor.prompts import PromptBundle
from supervisor.settings import AgentSpec, RunContext, Settings, agent_token
from supervisor.store import RunStore

pytestmark = [pytest.mark.integration]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "two-agent-token"
RUN_ID = "run-two-agent"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_for(predicate, timeout: float = 60.0, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


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
        end_grace_seconds=1.0,
        agent_image="test",
        mock_llm_port=0,
        world_path=tmp_path / "world.md",
        mission_path=tmp_path / "mission.md",
        genesis_path=tmp_path / "genesis.md",
    )


def agent_env(agent_id: str, name: str, peer: str, base: str, mock_port: int, tmp_path: Path) -> dict:
    home = tmp_path / f"home-{agent_id}"
    work = tmp_path / f"work-{agent_id}"
    home.mkdir(exist_ok=True)
    work.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.update(
        {
            "AGENT_ID": agent_id,
            "AGENT_NAME": name,
            "PEER_ID": "b" if agent_id == "a" else "a",
            "PEER_NAME": peer,
            "RUN_ID": RUN_ID,
            "BROKER_URL": base,
            "AGENT_TOKEN": agent_token(TOKEN, agent_id),
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
    return env


@pytest.mark.skipif(
    os.environ.get("GENESIS_RUN_CLI_TESTS") != "1",
    reason="set GENESIS_RUN_CLI_TESTS=1 to run the two-agent integration test",
)
async def test_two_agent_world(tmp_path):
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
        mission="Talk to your peer and confirm that both of you exist.",
        genesis_template=(
            "You are $agent_name. Your peer is $peer_name. Mission: $mission. "
            "Run $run_id started at $genesis_time. Begin."
        ),
    )
    broker = Broker(settings, run, prompts, store)
    server = TestServer(broker.make_app())
    await server.start_server()
    base = str(server.make_url("/")).rstrip("/")
    session = aiohttp.ClientSession()

    mock_port = free_port()
    mock = MockLLM(
        port=mock_port,
        host="127.0.0.1",
        mode="demo",
        delay_s=0.05,
        log_path=tmp_path / "mock-requests.jsonl",
    )
    mock.start()

    log_paths = {agent_id: tmp_path / f"bridge-{agent_id}.log" for agent_id in ("a", "b")}
    log_files = {agent_id: path.open("wb") for agent_id, path in log_paths.items()}
    processes = {}
    try:
        for agent_id, name, peer in (("a", "agent-a", "agent-b"), ("b", "agent-b", "agent-a")):
            env = agent_env(agent_id, name, peer, base, mock_port, tmp_path)
            processes[agent_id] = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "runtime.bridge",
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=log_files[agent_id],
                stderr=asyncio.subprocess.STDOUT,
            )

        async def both_started() -> bool:
            chat = [message for message in broker.messages.values() if message.kind == "chat"]
            senders = {message.sender for message in chat}
            return {"agent-a", "agent-b"} <= senders

        assert await wait_for(both_started), "the two agents never both sent a message"

        async def both_received() -> bool:
            delivered = [
                message
                for message in broker.messages.values()
                if message.kind == "chat" and message.delivered_at is not None
            ]
            recipients = {message.recipient for message in delivered}
            return {"agent-a", "agent-b"} <= recipients

        assert await wait_for(both_received), "messages did not reach both agents"

        async def has_reply() -> bool:
            return any(
                message.kind == "chat" and "received your message" in message.text
                for message in broker.messages.values()
            )

        assert await wait_for(has_reply, timeout=30), "neither agent replied to a delivered message"
        chat = [message for message in broker.messages.values() if message.kind == "chat"]
        greetings = [message for message in chat if "Hello from" in message.text]
        replies = [message for message in chat if "received your message" in message.text]
        assert len(greetings) >= 2, [message.text for message in chat]
        assert len(replies) >= 1, [message.text for message in chat]

        channels = {message.delivered_channel for message in chat if message.delivered_at}
        print("delivery channels:", channels)
        print("chat messages:", [(m.sender, m.recipient, m.text[:60]) for m in chat])

        assert await broker.request_end("two-agent test complete") is True
        for agent_id, process in processes.items():
            try:
                await asyncio.wait_for(process.wait(), timeout=90)
            except TimeoutError:
                pytest.fail(f"agent {agent_id} did not exit after shutdown")
            assert process.returncode == 0, f"agent {agent_id} exited with {process.returncode}"

        await broker.finalize("two-agent test complete")
        transcript = (run.run_dir / "transcript.md").read_text(encoding="utf-8")
        assert "Hello from agent-a" in transcript
        assert "Hello from agent-b" in transcript
    finally:
        for agent_id, process in processes.items():
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        await session.close()
        await server.close()
        mock.stop()
        for handle in log_files.values():
            handle.close()
        for agent_id, process in processes.items():
            if process.returncode not in (0, None):
                print(f"\n--- bridge {agent_id} log ---")
                print(log_paths[agent_id].read_text(encoding="utf-8", errors="replace")[-8000:])
        broker.store.close()
