"""End-to-end MCP stdio handshake against a live World API."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tsinghua_city.api import create_app
from tsinghua_city.config import Settings


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_health(base: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=1, trust_env=False) as client:
        while time.monotonic() < deadline:
            try:
                if client.get(base + "/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
    raise RuntimeError("world API did not start")


async def test_mcp_stdio_tools_against_live_api(tmp_path: Path) -> None:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "city.db"
    app = create_app(
        Settings(
            database_url=f"sqlite:///{db_path}",
            admin_token="admin",
            scheduler_enabled=False,
            runtime_log_dir=str(tmp_path / "runtime"),
        ),
        engine=None,
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_health(base)
        with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
            admin = {"Authorization": "Bearer admin"}
            world = client.post("/v1/worlds", headers=admin, json={"name": "mcp"}).json()
            alice = client.post(
                f"/v1/worlds/{world['id']}/agents",
                headers=admin,
                json={"name": "alice"},
            ).json()
            client.post(
                f"/v1/worlds/{world['id']}/agents",
                headers=admin,
                json={"name": "bob"},
            )

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "tsinghua_city", "mcp"],
            env={
                **os.environ,
                "CITY_API_URL": base,
                "CITY_AGENT_TOKEN": alice["token"],
            },
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {
                "check_state",
                "check_balance",
                "send_message",
                "start_life",
                "end_life",
                "list_lives",
            } <= names

            state = await session.call_tool("check_state", {})
            assert not state.isError
            payload = json.loads(state.content[0].text)
            assert payload["agent"]["name"] == "alice"
            assert payload["balance"] == 100

            sent = await session.call_tool(
                "send_message",
                {"to": "bob", "body": "via stdio"},
            )
            assert not sent.isError
            message = json.loads(sent.content[0].text)
            assert message["body"] == "via stdio"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
